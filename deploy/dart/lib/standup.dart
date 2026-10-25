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

import 'box.dart';
import 'options.dart';

export 'box.dart';
export 'options.dart';

/// Byte-identical to `BIRTH_SH` in `deploy/standup.sh` (a test pins the two).
const birthScript =
    'set -eC\n'
    'cat /app/seed/aiko.db > /data/.aiko.db.seed\n'
    'ln /data/.aiko.db.seed /data/aiko.db\n'
    'rm -f /data/.aiko.db.seed || :';

const islandRepo = 'ghcr.io/nickmeinhold/aiko-chat-island';
const dataVolume = 'aiko_data';
const _label = 'aiko.birth';

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

/// The operator's choices for one standup. Every field is a checked type
/// (options.dart), so holding a StandupOptions means holding valid values: there is
/// no separate `validate` to forget (Carnot, PR#406 r2 and r3).
final class StandupOptions {
  StandupOptions({
    required this.domain,
    required this.displayName,
    this.seedPeers = const SeedPeers.none(),
    this.passkeys = false,
    this.tls = true,
    this.tag,
  }) {
    // The one check that needs two fields: a peer that IS this island.
    seedPeers.refuseSelf(domain.baseUrl);
  }
  final Domain domain;
  final DisplayName displayName;
  final SeedPeers seedPeers;
  final bool passkeys;
  final bool tls;

  /// The release to pin; null pins `edge` (tracks main), said aloud.
  final ImageTag? tag;
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
    // A value that looks like a flag is a missing value: `--name --no-tls` must not
    // name the island "--no-tls" and silently leave TLS on (Noether, PR#406 r3).
    if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
      throw Refusal('$flag needs a value\n\n$usage');
    }
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
  return StandupOptions(
    domain: Domain(domain),
    displayName: DisplayName(name),
    seedPeers: SeedPeers.parse(peers),
    passkeys: passkeys,
    tls: tls,
    tag: tag == null ? null : ImageTag(tag),
  );
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
GATEWAY_BASE_URL=${o.domain.baseUrl}
ISLAND_DISPLAY_NAME=${o.displayName.dotenv}
PASSKEY_RP_ID=${o.domain}

# Federation: operator-curated peers. JSON array of {"id","display_name","base_url"}.
ISLAND_SEED_PEERS=${o.seedPeers.dotenv}

# Passkeys: advertise sign-in only once /.well-known assetlinks.json + AASA serve.
PASSKEY_ENABLED=${PasskeysEnabled(o.passkeys).dotenv}
''';

String renderImageEnv(String tag) => 'ISLAND_IMAGE=$islandRepo:$tag\n';

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

  /// Set by the first check; every Docker call goes through it (sudo where needed).
  late final Docker _dockerCli;
  Future<RunResult> _docker(List<String> args, {bool stream = false}) =>
      _dockerCli.run(args, stream: stream);

  /// Paths standup needs from the checkout, checked before anything changes.
  String get _composeSh => _f('deploy/compose.sh').path;
  String get _caddyCompose => _f('deploy/caddy/docker-compose.caddy.yml').path;

  Future<StandupResult> run(StandupOptions o) async {
    final tag = o.tag?.tag ?? 'edge';
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
    _dockerCli = await Docker.detect(runner);
    if (!(await _docker(['compose', 'version'])).ok) {
      throw Refusal(
        'docker compose v2 is not available (install the compose plugin)',
      );
    }
    await _refuseIfAnIslandIsHere();
    await _portCheck(tls: o.tls);
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
    final pulled = await _docker(['pull', image], stream: true);
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
      tlsConfirmed = await _tls(o.domain.host);
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
  • Settings:   change the name, peers or passkeys with `island configure`. Never
                re-run standup on this box: it refuses, by design.
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

  /// 8095 must be free on every run: another gateway already listening there would
  /// answer "ok" for a container of ours that cannot bind (Tesla, PR#406 r3). 80 and
  /// 443 only matter when the bundled Caddy will need them.
  Future<void> _portCheck({required bool tls}) async {
    final ss = await runner.run('ss', ['-ltnH']);
    if (!ss.ok) {
      say.warn(
        'could not check whether ports 8095${tls ? ', 80 and 443' : ''} are free (no `ss`).',
      );
      return;
    }
    final listening = const LineSplitter().convert(ss.stdout).map((l) {
      final cols = l.trim().split(RegExp(r'\s+'));
      return cols.length > 3 ? cols[3].split(':').last : '';
    }).toSet();
    if (listening.contains('8095')) {
      throw Refusal(
        'port 8095 is already in use: something here already serves where this '
        'island\'s gateway must. Nothing was changed.',
      );
    }
    if (tls && (listening.contains('80') || listening.contains('443'))) {
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
