/// `island standup`: stand up a NEW island on a box that has none.
///
/// NEW ISLAND ONLY (Nick, 2026-10-25). `deploy/standup.sh` was written for new
/// islands and also told operators to re-run it to change settings, and most of its
/// 900 lines exist to make that re-run safe: keep the JWT secret, read the recorded
/// peers and passkey switch back, refuse to drop keys it did not write, carry a
/// legacy pin and media credentials forward. This command refuses whenever the box
/// already has an island, so it writes into a box with no `.env` and none of that
/// machinery has anything to do. Changing a live island's settings is `configure`'s
/// job, which edits the existing file instead of replacing it.
///
/// THE ORDER IS THE SAFETY ARGUMENT. Everything that can be checked is checked
/// before anything is changed; then the image is pulled before the volume exists;
/// then the volume is created and the seed copied in; only then are `.env` and
/// `image.env` written. Each failure stops with nothing written after it.
///
/// THE BIRTH is the same as `deploy/standup.sh`'s (bash stays for people who prefer
/// it): create `aiko_data` with a fresh random label, read the label back, and copy
/// the image's seed in only if it is this run's. `docker volume create` succeeds on
/// an existing volume and keeps its old label (measured on Docker 29.5.2 and 29.1.3),
/// so the read-back proves this run made the volume, whatever it holds. The gateway
/// itself cannot create a database (PR #405).
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

/// Byte-identical to `BIRTH_SH` in `deploy/standup.sh` (a test pins the two).
const birthScript =
    'set -eC\n'
    'cat /app/seed/aiko.db > /data/.aiko.db.seed\n'
    'ln /data/.aiko.db.seed /data/aiko.db\n'
    'rm -f /data/.aiko.db.seed || :';

const islandRepo = 'ghcr.io/nickmeinhold/aiko-chat-island';
const dataVolume = 'aiko_data';
const _label = 'aiko.birth';

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

/// How a standup that did not refuse ended. Sealed, so a caller cannot forget
/// the case that is NOT success.
sealed class StandupResult {
  const StandupResult();
}

/// Created, started, and the gateway answered /health.
final class Healthy extends StandupResult {
  const Healthy();
}

/// Created and started, but the gateway never answered: not confirmed working.
final class StartedNotAnswering extends StandupResult {
  const StartedNotAnswering();
}

/// Created, started and healthy, but TLS was asked for and is not confirmed: Caddy
/// would not start, or `https://<domain>/health` never answered (Noether, PR#406 r1).
final class HealthyTlsUnconfirmed extends StandupResult {
  const HealthyTlsUnconfirmed();
}

/// The process exit code for an outcome. In the library, not `main`, so a test can
/// pin it: scripts read this number, and 0 must mean only "up and confirmed".
int exitCodeFor(StandupResult r) => switch (r) {
  Healthy() => 0,
  StartedNotAnswering() => 3,
  HealthyTlsUnconfirmed() => 4,
};

/// Standup stopped on purpose. The message is what the operator reads.
final class Refusal implements Exception {
  Refusal(this.message);
  final String message;
  @override
  String toString() => message;
}

/// The operator's choices for one standup.
final class StandupOptions {
  const StandupOptions({
    required this.domain,
    required this.displayName,
    this.seedPeers = '[]',
    this.passkeys = false,
    this.tls = true,
    this.tag,
  });
  final String domain;
  final String displayName;

  /// JSON array of `{"id","display_name","base_url"}`. `[]` = a solo island.
  final String seedPeers;
  final bool passkeys;
  final bool tls;

  /// The release to pin; null pins `edge` (tracks main), said aloud.
  final String? tag;
}

const usage = '''
usage: island standup --domain <host> --name "<label>" [options]

Stands up a NEW island on a box that has none. Refuses if this box already has one
(a .env, an image.env, or the aiko_data volume). To change a live island's
settings, use `island configure` instead.

  --domain <host>       public hostname for this island (DNS A record -> this host)
  --name "<label>"      human label shown in the app's island picker
  --seed-peers <json>   JSON array of {"id","display_name","base_url"} to federate with
  --enable-passkeys     advertise passkey sign-in (only once /.well-known files serve)
  --no-tls              skip the bundled Caddy (you run your own reverse proxy)
  --version <tag>       the release to run, without the leading v (default: edge)

Media (--with-media) is not here yet: use deploy/standup.sh for a media island.''';

/// Parse `island standup` arguments. Throws [Refusal] with the reason.
StandupOptions parseStandupArgs(List<String> args) {
  String? domain, name, tag;
  var peers = '[]';
  var passkeys = false, tls = true;
  String value(int i, String flag) {
    if (i + 1 >= args.length) throw Refusal('$flag needs a value\n\n$usage');
    return args[i + 1];
  }

  for (var i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--domain':
        domain = value(i++, '--domain');
      case '--name':
        name = value(i++, '--name');
      case '--seed-peers':
        peers = value(i++, '--seed-peers');
      case '--version':
        tag = value(i++, '--version');
      case '--enable-passkeys':
        passkeys = true;
      case '--no-tls':
        tls = false;
      case '--with-media' || '--turn-domain' || '--livekit-domain':
        throw Refusal(
          '${args[i]}: media is not in `island standup` yet. Use '
          'deploy/standup.sh --with-media for a media island.',
        );
      default:
        throw Refusal('unknown argument: ${args[i]}\n\n$usage');
    }
  }
  if (domain == null || domain.isEmpty) {
    throw Refusal('--domain is required\n\n$usage');
  }
  if (name == null || name.isEmpty) {
    throw Refusal('--name is required\n\n$usage');
  }
  final opts = StandupOptions(
    domain: domain,
    displayName: name,
    seedPeers: peers,
    passkeys: passkeys,
    tls: tls,
    tag: tag,
  );
  validate(opts);
  return opts;
}

final _host = RegExp(
  r'^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$',
);
final _tag = RegExp(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$');

/// Everything about the options that can be checked without touching the box.
void validate(StandupOptions o) {
  if (!_host.hasMatch(o.domain)) {
    throw Refusal(
      '--domain must be a bare hostname like chat.example.org (no scheme, '
      'port or path): ${o.domain}',
    );
  }
  final tag = o.tag;
  if (tag != null) {
    if (tag.startsWith('v') && RegExp(r'^v\d').hasMatch(tag)) {
      throw Refusal(
        '--version $tag: image tags have no leading v (use ${tag.substring(1)})',
      );
    }
    if (!_tag.hasMatch(tag)) {
      throw Refusal('--version $tag is not a valid image tag');
    }
  }
  // Values are written single-quoted, which dotenv and compose both take literally
  // (a ` #` inside a name can't truncate it). The one character that can't be carried
  // is the quote itself, so it is refused, not mangled (deploy/standup.sh, #3734).
  for (final (what, v) in [
    ('--name', o.displayName),
    ('--seed-peers', o.seedPeers),
  ]) {
    if (v.contains("'")) {
      throw Refusal(
        "$what: a single quote (') can't be represented in the island's .env. "
        'Rename, or use the JSON escape \\u0027 inside --seed-peers.',
      );
    }
    if (v.codeUnits.any((c) => c < 0x20 || c == 0x7f)) {
      throw Refusal('$what must be one line with no control characters');
    }
  }
  // Compose's dotenv parser treats a backslash before the closing quote as an escape,
  // so a name ending in `\` would swallow the following lines (Carnot, PR#406 r1). A
  // JSON array always ends in `]`, so only the name can end in one; refuse them all
  // in the name rather than reason about which positions are safe.
  if (o.displayName.contains(r'\')) {
    throw Refusal('--name can\'t contain a backslash (\\)');
  }
  _validatePeers(o.seedPeers);
}

void _validatePeers(String json) {
  final Object? decoded;
  try {
    decoded = jsonDecode(json);
  } on FormatException catch (e) {
    throw Refusal('--seed-peers is not valid JSON: ${e.message}');
  }
  if (decoded is! List) throw Refusal('--seed-peers must be a JSON array');
  for (final p in decoded) {
    if (p is! Map ||
        p['id'] is! String ||
        p['display_name'] is! String ||
        p['base_url'] is! String) {
      throw Refusal(
        '--seed-peers: every entry needs string "id", "display_name" and '
        '"base_url": ${jsonEncode(p)}',
      );
    }
  }
}

/// The `.env` for a new island. No ENVIRONMENT line: absence means production,
/// which arms the gateway's fail-closed JWT guard.
String renderEnv(StandupOptions o, String jwtSecret) =>
    '''
# Generated by `island standup` for island: ${o.domain}
# Production config. ENVIRONMENT is intentionally UNSET (absence => production =>
# fail-closed JWT guard armed). Do NOT add ENVIRONMENT=dev here.
# Change settings with `island configure`, never by re-running standup.

JWT_SECRET=$jwtSecret

# --- island identity ---
GATEWAY_BASE_URL=https://${o.domain}
ISLAND_DISPLAY_NAME='${o.displayName}'
PASSKEY_RP_ID=${o.domain}

# Federation: operator-curated peers. JSON array of {"id","display_name","base_url"}.
ISLAND_SEED_PEERS='${o.seedPeers}'

# Passkeys: advertise sign-in only once /.well-known assetlinks.json + AASA serve.
PASSKEY_ENABLED=${o.passkeys}
''';

String renderImageEnv(String tag) => 'ISLAND_IMAGE=$islandRepo:$tag\n';

/// 32 random bytes as 64 hex characters (the gateway requires at least 32).
String newSecret(Random r) => List.generate(
  32,
  (_) => r.nextInt(256).toRadixString(16).padLeft(2, '0'),
).join();

/// Where standup reports. Plain sinks, so tests can read what an operator would.
final class Say {
  Say(this.out, this.err);
  final void Function(String) out;
  final void Function(String) err;
  void step(String s) => out('==> $s');
  void ok(String s) => out('  ok $s');
  void warn(String s) => err(' warn $s');
}

/// One standup run. Effects go through [runner]; [probe] answers a health URL;
/// [sleep] is the wait between probes.
final class Standup {
  Standup({
    required this.runner,
    required this.root,
    required this.say,
    required this.environment,
    required this.probe,
    this.sleep = _sleep,
    Random? random,
  }) : random = random ?? Random.secure();

  final Runner runner;
  final Directory root;
  final Say say;
  final Map<String, String> environment;
  final Future<bool> Function(Uri) probe;
  final Future<void> Function(Duration) sleep;
  final Random random;

  static Future<void> _sleep(Duration d) => Future<void>.delayed(d);

  File _f(String name) => File('${root.path}/$name');

  Future<RunResult> _docker(List<String> args) => runner.run('docker', args);

  /// Paths standup needs from the checkout, checked before anything changes.
  String get _composeSh => _f('deploy/compose.sh').path;
  String get _caddyCompose => _f('deploy/caddy/docker-compose.caddy.yml').path;

  Future<StandupResult> run(StandupOptions o) async {
    // ONE door: the options are checked here too, not only by the argument parser,
    // so a caller constructing StandupOptions directly can't publish an unchecked
    // value (Carnot, PR#406 r2).
    validate(o);
    final tag = o.tag ?? 'edge';
    final image = '$islandRepo:$tag';

    // ---- 1. checks: nothing below this block runs until all of them pass ----
    say.step('Checks (nothing is changed until they all pass)');
    final needed = [
      'docker-compose.yml',
      'deploy/compose.sh',
      if (o.tls) 'deploy/caddy/docker-compose.caddy.yml',
    ];
    for (final n in needed) {
      if (!_f(n).existsSync()) {
        throw Refusal(
          '$n not found in ${root.path}. Run from the island checkout (the '
          'directory holding docker-compose.yml and deploy/), with deploy/ synced. '
          'Nothing was changed.',
        );
      }
    }
    for (final v in ['ISLAND_IMAGE', 'ISLAND_VERSION']) {
      if (environment.containsKey(v)) {
        throw Refusal(
          '$v is set in this shell\'s environment. The image comes only from '
          'image.env; unset it and run again.',
        );
      }
    }
    if (!(await _docker(['info'])).ok) {
      throw Refusal(
        'cannot talk to the Docker daemon (is it running? are you in the '
        'docker group?)',
      );
    }
    if (!(await _docker(['compose', 'version'])).ok) {
      throw Refusal(
        'docker compose v2 is not available (install the compose plugin)',
      );
    }
    await _refuseIfAnIslandIsHere();
    if (o.tls) await _tlsPortAdvisory();
    say.ok('this box has no island; Docker and compose answer');
    final env = renderEnv(o, newSecret(random));
    final nonce = newSecret(random).substring(0, 16);

    // ---- 2. the image, before anything exists that a failed pull would strand ----
    say.step('Pulling $image');
    if (o.tag == null) {
      say.warn(
        "no --version given: pinning 'edge', which tracks main. Pass --version "
        '<tag> for a release.',
      );
    }
    final pulled = await runner.run('docker', ['pull', image], stream: true);
    if (!pulled.ok) {
      throw Refusal(
        'could not pull $image (nothing was changed):\n${pulled.stderr.trim()}',
      );
    }

    // ---- 3. the birth: a volume this run can prove it created, then the seed ----
    say.step(
      'Creating volume $dataVolume and the island\'s database (from the seed)',
    );
    await _birth(image, nonce);
    say.ok('created volume $dataVolume and the new island\'s database');

    // ---- 4 and 5: from here on, this run has created things. Any failure reports
    // exactly what (Maxwell, Noether, Tesla, PR#406 r1), never a stack trace, never
    // advice to run a command that can't help.
    final made = ['volume $dataVolume (label $_label=$nonce)'];
    try {
      say.step('Writing image.env and .env');
      await _publishBoth({
        'image.env': renderImageEnv(tag),
        '.env': env,
      }, made: made);
      say.ok('wrote image.env ($image) and .env (mode 600, fresh JWT secret)');

      say.step(
        'Starting the island (gateway + broker + registrar + ChatServer)',
      );
      final up = await runner.run(
        _composeSh,
        ['up', '-d'],
        workingDirectory: root.path,
        stream: true,
      );
      if (!up.ok) {
        throw Refusal('deploy/compose.sh up -d failed:\n${up.stderr.trim()}');
      }
    } on Object catch (e) {
      throw Refusal(
        'standup stopped partway: $e\n'
        'This run created: ${made.join(', ')}. A failed compose may still have '
        'started some services, so this run makes no claim about whether anything '
        'was used: see docs/runbooks/backup-and-rollback.md, "A failed birth", '
        'before removing anything (Carnot, PR#406 r2).',
      );
    }

    final healthy = await _wait(Uri.parse('http://127.0.0.1:8095/health'), 30);
    if (healthy) {
      say.ok('gateway healthy on 127.0.0.1:8095');
    } else {
      say.warn(
        'gateway did not answer /health within ~60s. Inspect: '
        'deploy/compose.sh logs chat-island',
      );
    }

    // ---- 6. TLS ----
    var tlsConfirmed = true;
    if (o.tls && !healthy) {
      // No proxy in front of a gateway that never answered (Noether, PR#406 r2).
      say.step('TLS not attempted: the gateway is not answering');
    } else if (o.tls) {
      tlsConfirmed = await _tls(o.domain);
    } else {
      say.step(
        'TLS skipped (--no-tls): point your own proxy at 127.0.0.1:8095',
      );
    }
    say.out('');
    if (!healthy) {
      say.step(
        "Island '${o.displayName}' (${o.domain}) was created and started, but its "
        'gateway never answered. It is NOT confirmed working: check the logs above.',
      );
      return const StartedNotAnswering();
    }
    if (!tlsConfirmed) {
      say.step(
        "Island '${o.displayName}' (${o.domain}) is up on 127.0.0.1:8095, but "
        'HTTPS is NOT confirmed (see the TLS warning above).',
      );
      return const HealthyTlsUnconfirmed();
    }
    say.step("Island '${o.displayName}' (${o.domain}) is up.");
    say.out('''

Next steps:
  • Verify:     curl -s https://${o.domain}/health
  • Settings:   never re-run standup on this box: it refuses, by design. Until
                `island configure` exists, change peers, passkeys or the name by
                re-running deploy/standup.sh with the flag to change.
  • Backups:    the SQLite store lives in volume '$dataVolume'. Set up backups before
                real users arrive: docs/runbooks/backup-and-rollback.md.
  • Updates:    nobody can push this island an update. Edit ISLAND_IMAGE in image.env
                and run deploy/update.sh. Releases:
                https://github.com/nickmeinhold/aiko-chat-island/releases''');
    return const Healthy();
  }

  Future<void> _refuseIfAnIslandIsHere() async {
    bool present(String n) =>
        FileSystemEntity.typeSync(_f(n).path, followLinks: false) !=
        FileSystemEntityType.notFound;
    if (present('.env')) {
      throw Refusal(
        'this box already has an island (.env exists in ${root.path}). Nothing '
        'was changed. standup is for a box that has none.',
      );
    }
    // Positive evidence only: a listing that FAILED is not "no volume".
    final ls = await _docker([
      'volume',
      'ls',
      '-q',
      '--filter',
      'name=^$dataVolume\$',
    ]);
    if (!ls.ok) {
      throw Refusal(
        'could not list Docker volumes, so this run can\'t tell whether the '
        'box already has an island. Nothing was changed.\n${ls.stderr.trim()}',
      );
    }
    final volume = const LineSplitter()
        .convert(ls.stdout)
        .any((l) => l.trim() == dataVolume);
    if (volume || present('image.env')) {
      // No .env, but a volume or an image.env: an island, or an earlier standup that
      // stopped partway. This run can't tell which, so it names what it sees and
      // hands the decision to the runbook, never to a command (PR#406 r1).
      final seen = [
        if (volume) 'volume $dataVolume',
        if (present('image.env')) 'image.env',
      ];
      throw Refusal(
        'this box already has ${seen.join(' and ')} (but no .env). It is either an '
        'island or an earlier standup that stopped partway. Nothing was changed. '
        'See docs/runbooks/backup-and-rollback.md, "A failed birth".',
      );
    }
  }

  Future<void> _tlsPortAdvisory() async {
    final ss = await runner.run('ss', ['-ltnH']);
    if (!ss.ok) {
      say.warn(
        'could not check ports 80/443 (no `ss`); if another proxy runs here, use --no-tls.',
      );
      return;
    }
    final busy = const LineSplitter()
        .convert(ss.stdout)
        .map((l) {
          final cols = l.trim().split(RegExp(r'\s+'));
          return cols.length > 3 ? cols[3].split(':').last : '';
        })
        .where((p) => p == '80' || p == '443');
    if (busy.isNotEmpty) {
      throw Refusal(
        'port 80 and/or 443 is already in use (an existing proxy?). The bundled '
        'Caddy needs them for Let\'s Encrypt. Stop the other proxy, or use --no-tls and '
        'point it at 127.0.0.1:8095. Nothing was changed.',
      );
    }
  }

  Future<void> _birth(String image, String nonce) async {
    const pointer =
        'See docs/runbooks/backup-and-rollback.md, "A failed birth".';
    final created = await _docker([
      'volume',
      'create',
      '--label',
      '$_label=$nonce',
      dataVolume,
    ]);
    if (!created.ok) {
      throw Refusal(
        'could not create volume $dataVolume:\n${created.stderr.trim()}',
      );
    }
    final owner = await _docker([
      'volume',
      'inspect',
      '-f',
      '{{index .Labels "$_label"}}',
      dataVolume,
    ]);
    if (!owner.ok) {
      throw Refusal(
        'could not read volume $dataVolume\'s label back, so this run can\'t '
        'prove it created that volume. Nothing was copied. $pointer',
      );
    }
    if (owner.stdout.trim() != nonce) {
      throw Refusal(
        'volume $dataVolume already existed before this run (it was not created '
        'now), so it may hold an island. It was left untouched and no island was created.',
      );
    }
    final copied = await _docker([
      'run',
      '--rm',
      '-v',
      '$dataVolume:/data',
      '--entrypoint',
      'sh',
      image,
      '-c',
      birthScript,
    ]);
    if (!copied.ok) {
      throw Refusal(
        'could not copy the seed into volume $dataVolume (an image released '
        'before the seed has no /app/seed/aiko.db, so check --version):\n'
        '${copied.stderr.trim()}\nThis run created volume $dataVolume '
        '($_label=$nonce); nothing else was written. $pointer',
      );
    }
  }

  /// Publish every file in [files] so none is visible half-written, none holds a
  /// secret at a looser mode, and none lands over an existing file: ALL the
  /// temporaries are written first (exclusive, `chmod 600` while still EMPTY), and
  /// only then each is published with `ln`, which refuses an existing target. So a
  /// failed write publishes nothing (Tesla, PR#406 r1). Temporaries are named
  /// `<name>.standup-*`, which .gitignore covers, so one stranded by a SIGKILL holding
  /// the JWT secret can't be committed. Each published name is added to [made].
  Future<void> _publishBoth(
    Map<String, String> files, {
    required List<String> made,
  }) async {
    final tmps = <String, File>{};
    try {
      for (final MapEntry(key: name, value: content) in files.entries) {
        final tmp = _f('$name.standup-${newSecret(random).substring(0, 8)}');
        tmp.createSync(exclusive: true);
        tmps[name] = tmp;
        final m = await runner.run('chmod', ['600', tmp.path]);
        if (!m.ok) {
          throw Refusal(
            'could not restrict ${tmp.path} to mode 600: ${m.stderr.trim()}',
          );
        }
        tmp.writeAsStringSync(content, flush: true);
      }
      for (final MapEntry(key: name, value: tmp) in tmps.entries) {
        final l = await runner.run('ln', [tmp.path, _f(name).path]);
        if (!l.ok) throw Refusal('could not publish $name: ${l.stderr.trim()}');
        made.add(name);
      }
    } finally {
      for (final t in tmps.values) {
        if (t.existsSync()) t.deleteSync();
      }
    }
  }

  Future<bool> _wait(Uri uri, int tries) async {
    for (var i = 0; i < tries; i++) {
      if (await probe(uri)) return true;
      await sleep(const Duration(seconds: 2));
    }
    return false;
  }

  /// Whether HTTPS was confirmed. A failure here never throws: the island is already
  /// up on 127.0.0.1, so it is reported, and the outcome says TLS is unconfirmed.
  Future<bool> _tls(String domain) async {
    say.step('TLS reverse proxy (Caddy, host network)');
    try {
      _f('deploy/caddy/.env')
          .writeAsStringSync('ISLAND_DOMAIN=$domain\n', flush: true);
    } on FileSystemException catch (e) {
      say.warn(
        'could not write deploy/caddy/.env (${e.message}); Caddy not started.',
      );
      return false;
    }
    final c = await _docker(['compose', '-f', _caddyCompose, 'up', '-d']);
    if (!c.ok) {
      say.warn(
        'Caddy did not start:\n${c.stderr.trim()}\nThe island is up on '
        '127.0.0.1:8095; fix the proxy and run: docker compose -f '
        'deploy/caddy/docker-compose.caddy.yml up -d',
      );
      return false;
    }
    if (await _wait(Uri.parse('https://$domain/health'), 15)) {
      say.ok('https://$domain/health is live');
      return true;
    }
    say.warn(
      'https://$domain/health not answering yet. Check DNS points here and ports '
      '80+443 are open, then: docker compose -f deploy/caddy/docker-compose.caddy.yml logs',
    );
    return false;
  }
}

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
