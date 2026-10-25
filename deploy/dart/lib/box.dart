/// What `island standup` and `island configure` share: how they run commands, how
/// they stop, how they speak, and how they ask THIS box's gateway a question.
///
/// Moved out of `standup.dart` when `configure` became the second caller, so the two
/// can't drift apart on what a refusal, a fake runner or a local probe means.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

/// What one external command did.
final class RunResult {
  const RunResult(this.exitCode, this.stdout, this.stderr);
  final int exitCode;
  final String stdout;
  final String stderr;
  bool get ok => exitCode == 0;
}

/// Every effect outside this process goes through here, so tests can stand in a
/// fake that records calls and answers like Docker does.
abstract interface class Runner {
  /// [stream]: also show the child's output live (a pull or a `compose up` takes
  /// minutes, and buffered silence reads as a hang; Noether, PR#406 r2). The
  /// output is still captured, so a failure can quote it.
  Future<RunResult> run(
    String exe,
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  });
}

final class ProcessRunner implements Runner {
  const ProcessRunner();
  @override
  Future<RunResult> run(
    String exe,
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  }) async {
    try {
      if (!stream) {
        final r = await Process.run(
          exe,
          args,
          workingDirectory: workingDirectory,
        );
        return RunResult(r.exitCode, '${r.stdout}', '${r.stderr}');
      }
      final p = await Process.start(
        exe,
        args,
        workingDirectory: workingDirectory,
      );
      final out = StringBuffer(), err = StringBuffer();
      final done = [
        p.stdout.transform(utf8.decoder).forEach((s) {
          stdout.write(s);
          out.write(s);
        }),
        p.stderr.transform(utf8.decoder).forEach((s) {
          stderr.write(s);
          err.write(s);
        }),
      ];
      final code = await p.exitCode;
      await Future.wait(done);
      // A failure's reason may be on either stream: quote both.
      return RunResult(code, '$out', '$err$out');
    } on ProcessException catch (e) {
      // A missing executable is an answer ("not installed"), not a crash.
      return RunResult(127, '', '$exe: ${e.message}');
    }
  }
}

/// How this box runs Docker: as the operator, or, where the operator is outside the
/// docker group, elevated for Docker ALONE with `sudo -n` (enspyr-melb: user `ubuntu`).
/// The port of `island_docker_detect` in deploy/lib/island-compose.sh. Never run the
/// whole tool under sudo instead: it would write the operator's files as root.
/// `sudo -n` never prompts, so a box without passwordless sudo for Docker refuses
/// with a reason instead of hanging.
final class Docker {
  Docker._(this.runner, this.prefix);
  final Runner runner;

  /// `['docker']` or `['sudo', '-n', 'docker']`.
  final List<String> prefix;

  static Future<Docker> detect(Runner runner) async {
    if ((await runner.run('docker', ['ps'])).ok) {
      return Docker._(runner, const ['docker']);
    }
    if ((await runner.run('sudo', ['-n', 'docker', 'ps'])).ok) {
      return Docker._(runner, const ['sudo', '-n', 'docker']);
    }
    throw Refusal(
      'cannot talk to the Docker daemon, as this user or with `sudo -n docker`. '
      'Is it running? Add this user to the docker group, or allow passwordless '
      'sudo for docker. Nothing was changed.',
    );
  }

  Future<RunResult> run(
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  }) => runner.run(
    prefix.first,
    [...prefix.skip(1), ...args],
    workingDirectory: workingDirectory,
    stream: stream,
  );
}

/// The command stopped on purpose. The message is what the operator reads.
final class Refusal implements Exception {
  Refusal(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Where a command reports. Plain sinks, so tests can read what an operator would.
final class Say {
  Say(this.out, this.err);
  final void Function(String) out;
  final void Function(String) err;
  void step(String s) => out('==> $s');
  void ok(String s) => out('  ok $s');
  void warn(String s) => err(' warn $s');
}

/// 32 random bytes as 64 hex characters (the gateway requires at least 32).
String newSecret(Random r) => List.generate(
  32,
  (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0'),
).join();

/// Whether a `/health` response is THIS gateway saying it is up: HTTP 200 and the
/// gateway's own body, `{"status": "ok", ...}` (main.py `health`). Any 2xx was not
/// enough: a proxy, a captive portal or another service on the port answers 200 too
/// (Tesla, PR#406 r2).
bool isGatewayHealthy(int statusCode, String body) {
  if (statusCode != 200) return false;
  try {
    final j = jsonDecode(body);
    return j is Map && j['status'] == 'ok';
  } on FormatException {
    return false;
  }
}

/// One answer from [getLocal]: the status and the body as text.
typedef LocalResponse = ({int status, String body});

/// GET [uri], asking THIS box, or null if nothing answered in time.
///
/// The connection always goes to [address] (127.0.0.1), whatever DNS says, and never
/// through a proxy or a redirect. For https the TLS handshake still sends `uri.host`
/// as SNI and verifies the certificate against it, so a pass means "this box's Caddy
/// holds a valid certificate for the domain and proxies to a healthy gateway", not
/// "something at the domain's current DNS answer said ok": a box whose A record still
/// names another island no longer passes (Carnot, Tesla, PR#406 r3). Measured from the
/// SDK source: with a connectionFactory and no proxy, HttpClient uses the returned
/// socket as is, so the factory does the TLS itself.
///
/// One deadline covers the whole exchange, and the body is capped in BYTES: anything
/// longer than [maxBytes] is not an answer this tool asked for.
Future<LocalResponse?> getLocal(
  Uri uri, {
  String address = '127.0.0.1',
  int maxBytes = 4096,
}) async {
  final client = HttpClient()
    ..connectionTimeout = const Duration(seconds: 3)
    ..findProxy = ((_) => 'DIRECT')
    ..connectionFactory = (u, _, _) async {
      final plain = Socket.connect(address, u.port);
      final Future<Socket> socket = u.scheme == 'https'
          ? plain.then((s) => SecureSocket.secure(s, host: u.host))
          : plain;
      return ConnectionTask.fromSocket(socket, () {});
    };
  try {
    return await () async {
      final req = await client.getUrl(uri);
      req.followRedirects = false;
      final res = await req.close();
      final bytes = <int>[];
      await for (final chunk in res) {
        bytes.addAll(chunk);
        if (bytes.length > maxBytes) return null;
      }
      return (
        status: res.statusCode,
        body: utf8.decode(bytes, allowMalformed: true),
      );
    }().timeout(const Duration(seconds: 6));
  } on Object {
    return null;
  } finally {
    client.close(force: true);
  }
}

/// Whether [uri] answers as this gateway (see [isGatewayHealthy]), asking THIS box.
Future<bool> probeHealth(Uri uri, {String address = '127.0.0.1'}) async {
  final r = await getLocal(uri, address: address);
  return r != null && isGatewayHealthy(r.status, r.body);
}
