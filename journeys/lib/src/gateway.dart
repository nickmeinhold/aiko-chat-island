/// A real gateway process to drive over the wire: born and booted the way the
/// image does it, under PRODUCTION settings, on a database of its own.
///
/// The image builds an empty island at head (`scripts/build_seed.py`), a new
/// island starts as a copy of it, and every boot runs the migrator before uvicorn
/// (`entrypoint.sh`). This does the same three steps against a temp directory, so
/// a journey sees what an island user sees: production's route table (no dev
/// doors), production's guards, and a schema alembic built.
///
/// Hermetic on purpose: the child gets a clean environment and a temp working
/// directory (Settings reads `.env` from the cwd, and a developer's dev `.env`
/// must not reach "production"), the bus points at a dead port, and the update
/// nudge is off. Measured 2026-11-10: the gateway serves HTTP with no broker, so
/// journeys that don't need the bus don't need the rig.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

final class PythonGateway {
  PythonGateway._(this.base, this._process, this._dir, this._log);

  /// Where the gateway listens, e.g. `http://127.0.0.1:53211`.
  final Uri base;
  final Process _process;
  final Directory _dir;
  final StringBuffer _log;

  /// The child's output so far, for a failure message.
  String get log => _log.toString();

  /// Births and boots a gateway from the checkout at [repoRoot].
  ///
  /// The interpreter is `ISLAND_PYTHON`, else the checkout's `.venv`, else
  /// `python3` on PATH (CI installs the package there).
  static Future<PythonGateway> start(
    String repoRoot, {
    Map<String, String> settings = const {},
  }) async {
    final python =
        Platform.environment['ISLAND_PYTHON'] ??
        (File('$repoRoot/.venv/bin/python').existsSync()
            ? '$repoRoot/.venv/bin/python'
            : 'python3');
    final dir = await Directory.systemTemp.createTemp('island-journeys-');
    final path = Platform.environment['PATH'] ?? '';

    await _run(
      python,
      ['$repoRoot/scripts/build_seed.py'],
      dir,
      {
        'PATH': path,
        'ENVIRONMENT': 'dev',
        'DB_URL': 'sqlite+aiosqlite:///${dir.path}/seed.db',
      },
    );
    await File('${dir.path}/seed.db').copy('${dir.path}/aiko.db');

    final env = {
      'PATH': path,
      ..._production(),
      'DB_URL': 'sqlite+aiosqlite:///${dir.path}/aiko.db',
      ...settings,
    };
    await _run(python, ['-m', 'aiko_gateway.migrate'], dir, env);

    final port = await _freePort();
    final process = await Process.start(
      python,
      [
        '-m',
        'uvicorn',
        'aiko_gateway.main:app',
        '--host',
        '127.0.0.1',
        '--port',
        '$port',
      ],
      workingDirectory: dir.path,
      environment: env,
      includeParentEnvironment: false,
    );
    final log = StringBuffer();
    const decode = Utf8Decoder(allowMalformed: true);
    process.stdout.transform(decode).listen(log.write);
    process.stderr.transform(decode).listen(log.write);

    final gateway = PythonGateway._(
      Uri.parse('http://127.0.0.1:$port'),
      process,
      dir,
      log,
    );
    await gateway._awaitHealthy();
    return gateway;
  }

  Future<void> _awaitHealthy() async {
    int? exited;
    unawaited(_process.exitCode.then((c) => exited = c));
    final client = HttpClient();
    try {
      final deadline = DateTime.now().add(const Duration(seconds: 60));
      while (DateTime.now().isBefore(deadline)) {
        if (exited != null) {
          throw StateError(
            'the gateway exited ($exited) before serving:\n$log',
          );
        }
        try {
          final req = await client.getUrl(base.resolve('/health'));
          final res = await req.close();
          await res.drain<void>();
          if (res.statusCode == 200) return;
        } on SocketException {
          // not listening yet
        }
        await Future<void>.delayed(const Duration(milliseconds: 200));
      }
      throw StateError('the gateway never answered /health in 60s:\n$log');
    } finally {
      client.close(force: true);
    }
  }

  /// SIGTERM, as a restart sends it; SIGKILL if it doesn't go in 10s.
  Future<void> stop() async {
    _process.kill();
    final code = await _process.exitCode.timeout(
      const Duration(seconds: 10),
      onTimeout: () {
        _process.kill(ProcessSignal.sigkill);
        return _process.exitCode;
      },
    );
    await _dir.delete(recursive: true);
    if (code != 0 && code != -15) {
      stderr.writeln('the gateway exited $code on stop:\n$log');
    }
  }
}

/// Throwaway values that satisfy production's boot guards. None is a setting a
/// journey is testing; a journey that tests one passes it in `settings`.
Map<String, String> _production() {
  final random = Random.secure();
  List<int> bytes(int n) => [for (var i = 0; i < n; i++) random.nextInt(256)];
  return {
    'ENVIRONMENT': 'production',
    'JWT_SECRET': bytes(32)
        .map((b) => b.toRadixString(16).padLeft(2, '0'))
        .join(),
    'ISLAND_SIGNING_SEED': base64Url.encode(bytes(32)).replaceAll('=', ''),
    'PASSKEY_ENABLED': 'true',
    'MODERATOR_USER_IDS': '["journey-placeholder"]',
    'CSAM_RUNBOOK_ACKNOWLEDGED': 'true',
    'ISLAND_UPDATE_NUDGE': 'off',
    'AIKO_MQTT_HOST': '127.0.0.1',
    'AIKO_MQTT_PORT': '9',
  };
}

Future<void> _run(
  String exe,
  List<String> args,
  Directory dir,
  Map<String, String> env,
) async {
  final r = await Process.run(
    exe,
    args,
    workingDirectory: dir.path,
    environment: env,
    includeParentEnvironment: false,
  );
  if (r.exitCode != 0) {
    throw StateError(
      '`$exe ${args.join(' ')}` failed (${r.exitCode}):\n${r.stdout}${r.stderr}',
    );
  }
}

Future<int> _freePort() async {
  final socket = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
  final port = socket.port;
  await socket.close();
  return port;
}
