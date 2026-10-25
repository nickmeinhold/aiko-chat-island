/// `island configure`: change a LIVE island's settings by editing its `.env`.
///
/// EDIT, NEVER REPLACE. `deploy/standup.sh`'s re-run rewrote the whole file from a
/// heredoc, so every key it did not write was destroyed: measured on the live boxes,
/// an unflagged re-run would have taken 15 keys off one and 11 off the other,
/// including ISLAND_SIGNING_SEED and APNS_PRIVATE_KEY, which exist nowhere else. Bash
/// answered with a guard that compares key lists and refuses, and its own refusal
/// names the real fix as "preserve-on-rewrite, UNBUILT" (claude-tasks#4437). This is
/// that fix. It replaces the lines of the keys it owns and leaves every other byte
/// alone, so there is no key list to guard and the guard dissolves.
///
/// WHAT IT OWNS: ISLAND_DISPLAY_NAME, ISLAND_SEED_PEERS, PASSKEY_ENABLED. A flag left
/// out means "leave it as it is", never "turn it off" (#3734). JWT_SECRET is not
/// touched, so it can't be rotated by accident. The domain is not here: it is the
/// passkey RP id, and changing it strands every passkey the island has issued.
///
/// COMPOSE PROVES THE EDIT before it lands. This file's parser chooses which lines
/// to replace; if it ever disagreed with compose about where a value ends (a quote,
/// a backslash, a PEM), it would corrupt a neighbour. So before publishing, the old
/// file and the new one are both read by `docker compose config` itself, the program
/// that reads `.env` on the box, and the edit is published only if compose sees
/// exactly the intended keys change and every other key unchanged. Measured: with
/// `env_file:`, `compose config` lists every key, multi-line values and `export`
/// included, and prints a literal `$` as `$$`.
///
/// THEN THE GATEWAY PROVES IT IS SERVING IT. After the restart, `configure` asks the
/// gateway on 127.0.0.1:8095 what it serves (`/v1/island`, `/v1/islands`,
/// `/v1/auth/providers`) and compares. A value written but never forwarded by
/// compose, or a peer the gateway dropped, is a green health check and a wrong
/// island; this catches both.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'box.dart';
import 'dotenv.dart';
import 'options.dart';

const keyName = 'ISLAND_DISPLAY_NAME';
const keyPeers = 'ISLAND_SEED_PEERS';
const keyPasskeys = 'PASSKEY_ENABLED';

/// The settings one run changes. A null field is left as it is.
final class ConfigureOptions {
  const ConfigureOptions({this.name, this.seedPeers, this.passkeys});
  final DisplayName? name;
  final SeedPeers? seedPeers;
  final bool? passkeys;

  bool get isEmpty => name == null && seedPeers == null && passkeys == null;

  /// What this run asks for, as `.env` key -> value, in a fixed order.
  Map<String, EnvValue> get wanted => {
    if (name != null) keyName: name!,
    if (seedPeers != null) keyPeers: seedPeers!,
    if (passkeys != null) keyPasskeys: PasskeysEnabled(passkeys!),
  };
}

const configureUsage = '''
usage: island configure [--name "<label>"] [--seed-peers <json>]
                        [--enable-passkeys | --no-passkeys]

Changes a live island's settings by editing its .env in place: only the keys named
below are touched, everything else stays byte for byte. Then the gateway is
restarted and checked to be serving the new values. With no options, prints the
current settings and changes nothing.

  --name "<label>"      human label shown in the app's island picker
  --seed-peers <json>   the WHOLE peer list, replacing the old one: a JSON array of
                        {"id","display_name","base_url"}. '[]' makes a solo island.
  --enable-passkeys     advertise passkey sign-in (only once /.well-known files serve)
  --no-passkeys         stop advertising passkey sign-in

An option left out leaves that setting as it is. The domain can't be changed here:
it is the passkey RP id, so changing it breaks every passkey already issued.''';

/// Parse `island configure` arguments. Throws [Refusal] with the reason.
ConfigureOptions parseConfigureArgs(List<String> args) {
  String? name, peers;
  bool? passkeys;
  String value(int i, String flag) {
    // As in standup: a value that looks like a flag is a missing value.
    if (i + 1 >= args.length || args[i + 1].startsWith('--')) {
      throw Refusal('$flag needs a value\n\n$configureUsage');
    }
    return args[i + 1];
  }

  void setPasskeys(bool v, String flag) {
    if (passkeys != null && passkeys != v) {
      throw Refusal(
        '--enable-passkeys and --no-passkeys contradict each other',
      );
    }
    passkeys = v;
  }

  for (var i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--name':
        name = value(i++, '--name');
      case '--seed-peers':
        peers = value(i++, '--seed-peers');
      case '--enable-passkeys':
        setPasskeys(true, args[i]);
      case '--no-passkeys':
        setPasskeys(false, args[i]);
      case '--domain':
        throw Refusal(
          "--domain: an island's domain is its passkey RP id, so changing it "
          'breaks every passkey already issued. configure does not change it.',
        );
      case '--with-media' || '--turn-domain' || '--livekit-domain':
        throw Refusal(
          '${args[i]}: media is not in `island configure` yet. Use '
          'deploy/standup.sh for media.',
        );
      default:
        throw Refusal('unknown argument: ${args[i]}\n\n$configureUsage');
    }
  }
  return ConfigureOptions(
    name: name == null ? null : DisplayName(name),
    seedPeers: peers == null ? null : SeedPeers.parse(peers),
    passkeys: passkeys,
  );
}

/// [text] with each key in [changes] set to its value: the lines of an existing
/// assignment are replaced (keeping its indentation and any `export `), a missing
/// key is appended, and every other byte is left as it was. Pure, so it can be
/// tested on its own; [Configure.run] still has compose check the result.
String editDotenv(String text, Map<String, EnvValue> changes) {
  if (text.contains('\r')) {
    // A CR left on a replaced line's neighbours would be ours to explain.
    throw Refusal(
      '.env has Windows (CRLF) line endings; convert it (dos2unix .env) first',
    );
  }
  final entries = scanDotenv(text, '.env');
  final lines = text.split('\n');
  final replace = <int, (DotenvEntry, String)>{};
  final append = <String>[];
  for (final MapEntry(key: key, value: v) in changes.entries) {
    final at = entries.where((e) => e.key == key).toList();
    if (at.length > 1) {
      // Compose would use the last; the operator reading the file sees the first.
      // Which one they meant is theirs to say, not ours to guess.
      throw Refusal(
        '$key is set ${at.length} times in .env (lines '
        '${at.map((e) => e.firstLine + 1).join(', ')}). Delete all but one, then '
        'run configure again. Nothing was changed.',
      );
    }
    final line = '$key=${v.dotenv}';
    if (at.isEmpty) {
      append.add(line);
    } else {
      replace[at.single.firstLine] = (at.single, '${at.single.lead}$line');
    }
  }
  final out = <String>[];
  for (var i = 0; i < lines.length;) {
    final r = replace[i];
    if (r == null) {
      out.add(lines[i]);
      i++;
    } else {
      out.add(r.$2);
      i = r.$1.endLine;
    }
  }
  if (append.isNotEmpty) {
    // `lines` ends with '' when the file ends in a newline; keep exactly one.
    if (out.isNotEmpty && out.last.isEmpty) out.removeLast();
    out
      ..addAll(append)
      ..add('');
  }
  return out.join('\n');
}

/// How a configure that did not refuse ended. Sealed, so a caller cannot forget the
/// cases that are not success.
sealed class ConfigureResult {
  const ConfigureResult();
}

/// Every setting asked for already had that value. Nothing was written or restarted.
final class Unchanged extends ConfigureResult {
  const Unchanged();
}

/// Written, restarted, and the gateway serves the new values.
final class Applied extends ConfigureResult {
  const Applied();
}

/// Written and restarted, but the gateway never answered /health.
final class WrittenNotAnswering extends ConfigureResult {
  const WrittenNotAnswering();
}

/// Written, restarted and answering, but not serving what was written.
final class AnsweringNotServing extends ConfigureResult {
  const AnsweringNotServing(this.mismatches);
  final List<String> mismatches;
}

/// The process exit code for an outcome; 0 means the island serves what was asked.
int configureExitCode(ConfigureResult r) => switch (r) {
  Unchanged() || Applied() => 0,
  WrittenNotAnswering() => 3,
  AnsweringNotServing() => 5,
};

/// One configure run. Effects go through [runner]; [fetch] asks the local gateway.
final class Configure {
  Configure({
    required this.runner,
    required this.root,
    required this.say,
    required this.fetch,
    this.sleep = _sleep,
    Random? random,
  }) : random = random ?? Random.secure();

  final Runner runner;
  final Directory root;
  final Say say;
  final Future<LocalResponse?> Function(Uri) fetch;
  final Future<void> Function(Duration) sleep;
  final Random random;

  static Future<void> _sleep(Duration d) => Future<void>.delayed(d);

  File _f(String name) => File('${root.path}/$name');
  File get _env => _f('.env');

  /// Set by the checks; compose is asked through it (sudo where needed).
  late final Docker _dockerCli;

  /// Print the current settings. Changes nothing.
  void show() {
    final cur = parseDotenv(_readEnv(), '.env');
    String v(String k) => cur[k] ?? '(not set: the compose default applies)';
    say.out('$keyName=${v(keyName)}');
    say.out('$keyPeers=${v(keyPeers)}');
    say.out('$keyPasskeys=${v(keyPasskeys)}');
  }

  String _readEnv() {
    final type = FileSystemEntity.typeSync(_env.path, followLinks: false);
    if (type == FileSystemEntityType.notFound) {
      throw Refusal(
        'no .env in ${root.path}: there is no island here to configure. Run from '
        'the island checkout, or stand one up with `island standup`.',
      );
    }
    if (type != FileSystemEntityType.file) {
      // A rename would replace the link with a file, silently moving the island's
      // config out from under whatever the link pointed at.
      throw Refusal(
        '.env is not a regular file ($type); configure edits only a file',
      );
    }
    return _env.readAsStringSync();
  }

  /// One configure at a time on a box, held from the first read of `.env` to the
  /// end of the restart. Without it two runs can both read the same file, both pass
  /// the re-read before the rename, and the second rename silently discards the
  /// first's edit: an atomic rename prevents a torn file, not a lost update
  /// (Carnot, PR#407 r2). A hand edit doesn't take this lock; the re-read before the
  /// rename is what catches one, in all but a tiny window.
  Future<ConfigureResult> run(ConfigureOptions o) async {
    // Before the lock file is created: a run from the wrong directory must leave
    // nothing behind (Maxwell, PR#407 r3).
    _readEnv();
    final lock = _f('.env.configure-lock').openSync(mode: FileMode.append);
    try {
      try {
        lock.lockSync(FileLock.exclusive); // non-blocking: refuse, don't queue
      } on FileSystemException {
        throw Refusal(
          'another `island configure` is running on this box. Let it finish, '
          'then run this again. Nothing was changed.',
        );
      }
      return await _run(o);
    } finally {
      lock.closeSync(); // closing releases the lock
    }
  }

  /// The text of `.env` this run read, for the revert hint's exact old lines.
  String _before = '';

  Future<ConfigureResult> _run(ConfigureOptions o) async {
    // ---- 1. checks, and the edit, all before anything is written ----
    say.step('Checks (nothing is changed until they all pass)');
    for (final n in ['docker-compose.yml', 'deploy/compose.sh']) {
      if (!_f(n).existsSync()) {
        throw Refusal(
          '$n not found in ${root.path}. Run from the island checkout. Nothing '
          'was changed.',
        );
      }
    }
    final before = _before = _readEnv();
    await _refuseIfNotOwner();
    _dockerCli = await Docker.detect(runner);
    await _refuseIfRestartWouldChangeImage();
    final current = parseDotenv(before, '.env');
    // syd's .env has no GATEWAY_BASE_URL (compose's default supplies it), so ask
    // the running gateway who it is rather than check against nothing (Noether, r2).
    final self = await _json('/v1/island');
    for (final url in {
      current['GATEWAY_BASE_URL'],
      if (self is Map) self['base_url'],
    }.whereType<String>()) {
      o.seedPeers?.refuseSelf(url);
    }
    if (o.passkeys == true) _refuseMismatchedRpId(current);

    // Unchanged means the LINE is already exactly what configure writes, not that
    // this parser reads an equal value: an unquoted legacy value with a `$` reads
    // equal here while compose expands it, and a key set twice reads as its last
    // copy (Tesla, PR#407 r1). Anything else is rewritten, through the compose proof.
    final changes = {
      for (final MapEntry(key: k, value: v) in o.wanted.entries)
        if (!_isWrittenAs(before, k, v)) k: v,
    };
    if (changes.isEmpty) {
      // "The file already says so" is not "the island does": a run whose restart
      // failed left the new .env behind, and a retry (or the printed revert) must
      // still restart, not report success over a gateway serving the old values
      // (Carnot, PR#407 r1). So nothing is done only when the gateway agrees too.
      if (await _healthyNow() &&
          (await _servedMismatches(o, current)).isEmpty) {
        say.ok('every setting asked for already has that value, and is served');
        return const Unchanged();
      }
      say.step(
        '.env already has these values, but the gateway is not serving them: '
        'restarting it',
      );
      return _restartAndVerify(o, current, const {});
    }
    final after = editDotenv(before, changes);

    // ---- 2. the candidate, written beside .env, proved by compose ----
    final tmp = _f('.env.configure-${newSecret(random).substring(0, 8)}');
    try {
      tmp.createSync(exclusive: true);
      final m = await runner.run('chmod', ['600', tmp.path]);
      if (!m.ok) {
        throw Refusal(
          'could not restrict ${tmp.path} to mode 600: ${m.stderr.trim()}',
        );
      }
      tmp.writeAsStringSync(after, flush: true);
      await _composeAgrees(_env.path, tmp.path, changes);

      // ---- 3. publish, unless .env changed while this ran ----
      if (_env.readAsStringSync() != before) {
        throw Refusal(
          '.env changed while configure was running (another edit?). Nothing was '
          'written; run configure again.',
        );
      }
      tmp.renameSync(_env.path);
    } finally {
      if (tmp.existsSync()) tmp.deleteSync();
    }
    for (final MapEntry(key: k, value: v) in changes.entries) {
      say.ok('$k: ${current[k] ?? '(not set)'}  ->  ${v.value}');
    }
    return _restartAndVerify(o, current, changes);
  }

  /// Whether [text] sets [key] exactly once, on a line configure itself would write.
  static bool _isWrittenAs(String text, String key, EnvValue v) {
    final at = scanDotenv(text, '.env').where((e) => e.key == key).toList();
    if (at.length != 1) return false;
    final e = at.single;
    final span = text.split('\n').sublist(e.firstLine, e.endLine).join('\n');
    return span.substring(e.lead.length) == '$key=${v.dotenv}';
  }

  /// Restart the gateway and confirm it serves what [o] asks for. [changes] is what
  /// this run wrote (empty when it only reconciles), for the revert hint.
  Future<ConfigureResult> _restartAndVerify(
    ConfigureOptions o,
    Map<String, String> current,
    Map<String, EnvValue> changes,
  ) async {
    // ---- 4. restart the gateway, then ask it what it serves ----
    say.step('Restarting the gateway so it reads the new settings');
    final up = await runner.run(
      _f('deploy/compose.sh').path,
      ['up', '-d', '--no-deps', 'chat-island'],
      workingDirectory: root.path,
      stream: true,
    );
    if (!up.ok) {
      throw Refusal(
        '${changes.isEmpty ? 'This run wrote nothing; .env was already as asked' : 'The new .env IS written'}, '
        'but restarting the gateway failed:\n'
        '${up.stderr.trim()}\n${_revertHint(current, changes)}',
      );
    }
    if (!await _waitHealthy()) {
      say.warn(
        'gateway did not answer /health within ~60s. Inspect: deploy/compose.sh '
        'logs chat-island\n${_revertHint(current, changes)}',
      );
      // The write landed and the outcome is open: an operator who waits out a slow
      // boot and keeps the edit needs the reminder too (Tesla, r2).
      if (changes.isNotEmpty) _sopsReminder();
      return const WrittenNotAnswering();
    }
    final wrong = await _servedMismatches(o, current);
    if (wrong.isNotEmpty) {
      for (final w in wrong) {
        say.warn(w);
      }
      say.warn(_revertHint(current, changes));
      return AnsweringNotServing(wrong);
    }
    say.step('The gateway is serving the new settings.');
    // Only now: on a mismatch the operator is about to revert, and a copy updated
    // first would carry the rejected value back on its next delivery (Tesla, r1).
    if (changes.isNotEmpty) _sopsReminder();
    return const Applied();
  }

  /// The restart recreates the gateway from the image `image.env` pins. If that is
  /// not the image the gateway is running, the restart is an UPGRADE, and its boot
  /// migrates the database with no backup: deploy/update.sh is the door that backs
  /// up first, and configure must not be a second one (Maxwell, PR#407 r1). Measured
  /// on both boxes: this `ps` prints the pinned reference for a gateway created from
  /// it. Measured locally (Docker 29.5.2): once that tag is moved to another image,
  /// it prints the short image id instead, which also refuses.
  Future<void> _refuseIfRestartWouldChangeImage() async {
    final pinFile = _f('image.env');
    if (!pinFile.existsSync()) {
      throw Refusal(
        'no image.env in ${root.path}, so the restart could not be pinned to the '
        'running release. Nothing was changed.',
      );
    }
    final pin = parseDotenv(
      pinFile.readAsStringSync(),
      'image.env',
    )['ISLAND_IMAGE'];
    final ps = await _dockerCli.run([
      'ps',
      '--filter',
      'label=com.docker.compose.service=chat-island',
      '--format',
      '{{.Image}}',
    ]);
    if (!ps.ok) {
      throw Refusal(
        'could not list running containers, so this run can\'t tell whether a '
        'restart would change the release. Nothing was changed.',
      );
    }
    final running = const LineSplitter()
        .convert(ps.stdout)
        .map((l) => l.trim())
        .where((l) => l.isNotEmpty)
        .toSet();
    if (running.isEmpty) {
      throw Refusal(
        'the gateway (chat-island) is not running. configure changes a running '
        'island; start it with deploy/update.sh first. Nothing was changed.',
      );
    }
    if (running.length != 1 || running.single != pin) {
      throw Refusal(
        'image.env pins ${pin ?? '(nothing)'}, but the gateway is running '
        '${running.join(', ')}. Restarting it to apply settings would also change '
        'the release, with no backup. Run deploy/update.sh first (it backs up), '
        'then configure. Nothing was changed.',
      );
    }
  }

  /// Passkeys bind to the RP id. A `.env` that sets only one of PASSKEY_RP_ID and
  /// GATEWAY_BASE_URL gets compose's default for the other, which names
  /// chat.imagineering.cc (docker-compose.yml), so every other island would
  /// advertise passkeys under the wrong id: a green check and no working sign-in
  /// (Maxwell, PR#407 r1). Both unset is consistent (both defaults name one island).
  void _refuseMismatchedRpId(Map<String, String> current) {
    final rp = current['PASSKEY_RP_ID'], base = current['GATEWAY_BASE_URL'];
    if (rp == null && base == null) {
      // Consistent, but only for the island compose's defaults name. On any other
      // box the whole identity, base_url included, is wrong, which configure can't
      // tell from syd's legitimate shape; so it is said, not refused (Tesla, r2).
      say.warn(
        'neither PASSKEY_RP_ID nor GATEWAY_BASE_URL is set in .env, so both come '
        "from docker-compose.yml's defaults (chat.imagineering.cc). If this island "
        'is not chat.imagineering.cc, passkeys will not work: set both first.',
      );
      return;
    }
    final host = base == null ? null : Uri.tryParse(base)?.host;
    if (rp == null || host == null || host != rp) {
      throw Refusal(
        'passkeys need PASSKEY_RP_ID to be this island\'s domain, and .env has '
        'PASSKEY_RP_ID=${rp ?? '(not set)'} with '
        'GATEWAY_BASE_URL=${base ?? '(not set)'}. Set PASSKEY_RP_ID to the domain '
        'in GATEWAY_BASE_URL, then run again. Nothing was changed.',
      );
    }
  }

  Future<bool> _healthyNow() async {
    final r = await fetch(Uri.parse('http://127.0.0.1:8095/health'));
    return r != null && isGatewayHealthy(r.status, r.body);
  }

  /// A file written by another user (say, root via sudo) would leave .env owned by
  /// that user, and compose run as the operator could no longer read it.
  Future<void> _refuseIfNotOwner() async {
    final me = await runner.run('id', ['-u']);
    final owner = await runner.run('stat', ['-c', '%u', _env.path]);
    if (!me.ok || !owner.ok) {
      say.warn('could not compare .env\'s owner with this user; continuing');
      return;
    }
    if (me.stdout.trim() != owner.stdout.trim()) {
      throw Refusal(
        '.env is owned by uid ${owner.stdout.trim()} but this is uid '
        '${me.stdout.trim()}. Run configure as the user who owns .env (not with '
        'sudo), so the file stays theirs. Nothing was changed.',
      );
    }
  }

  /// Every key and value compose sees in [path]. Values never leave this process.
  Future<Map<String, String>> _composeEnv(
    String path,
    Directory scratch,
  ) async {
    final yml = File('${scratch.path}/compose.yml')
      ..writeAsStringSync(
        'services:\n  probe:\n    image: scratch\n    env_file: [${jsonEncode(path)}]\n',
      );
    final r = await _dockerCli.run([
      'compose',
      '-f',
      yml.path,
      'config',
      '--format',
      'json',
    ], workingDirectory: scratch.path);
    if (!r.ok) {
      // stderr can quote a line of the file; say which file, not what compose said.
      throw Refusal(
        'docker compose could not read ${path == _env.path ? '.env' : 'the edited .env'}, '
        'so the edit can\'t be proved safe. Nothing was changed.',
      );
    }
    final Object? env;
    try {
      env =
          ((jsonDecode(r.stdout) as Map)['services']
              as Map)['probe']['environment'];
    } on Object {
      throw Refusal(
        'could not read `docker compose config` output. Nothing was changed.',
      );
    }
    return {
      for (final MapEntry(:key, :value) in ((env as Map?) ?? {}).entries)
        '$key': '${value ?? ''}',
    };
  }

  /// Refuse unless compose reads [newPath] as [oldPath] with exactly [changes].
  Future<void> _composeAgrees(
    String oldPath,
    String newPath,
    Map<String, EnvValue> changes,
  ) async {
    final scratch = Directory.systemTemp.createTempSync('island-configure');
    try {
      final a = await _composeEnv(oldPath, scratch);
      final b = await _composeEnv(newPath, scratch);
      // A comparison over no keys passes by having compared nothing (Noether, r2).
      // A live .env always has keys, so compose answering with none is a failure.
      if (a.isEmpty || b.isEmpty) {
        throw Refusal(
          'docker compose reported no keys for ${a.isEmpty ? '.env' : 'the edited .env'}, '
          'so the edit can\'t be proved safe. Nothing was changed.',
        );
      }
      // `compose config` prints a literal `$` as `$$` (measured), on both sides.
      String rendered(EnvValue v) => v.value.replaceAll(r'$', r'$$');
      final off = [
        for (final k in {...a.keys, ...b.keys, ...changes.keys})
          if (changes.containsKey(k)
              ? b[k] != rendered(changes[k]!)
              : a[k] != b[k])
            k,
      ];
      if (off.isNotEmpty) {
        throw Refusal(
          'docker compose would read the edited .env differently from what '
          'configure meant, at: ${off.join(', ')}. Nothing was changed. This is a '
          'disagreement between configure and compose about the file\'s syntax; '
          'please report it with those key names (never the values).',
        );
      }
    } finally {
      scratch.deleteSync(recursive: true);
    }
  }

  Future<bool> _waitHealthy() async {
    for (var i = 0; i < 30; i++) {
      final r = await fetch(Uri.parse('http://127.0.0.1:8095/health'));
      if (r != null && isGatewayHealthy(r.status, r.body)) return true;
      await sleep(const Duration(seconds: 2));
    }
    return false;
  }

  Future<Object?> _json(String path) async {
    final r = await fetch(Uri.parse('http://127.0.0.1:8095$path'));
    if (r == null || r.status != 200) return null;
    try {
      return jsonDecode(r.body);
    } on FormatException {
      return null;
    }
  }

  /// What the gateway serves that differs from what this run asked for. Checks
  /// every setting asked for, changed or not: "already had that value" was a
  /// reading of .env, and this is the reading of the running gateway.
  Future<List<String>> _servedMismatches(
    ConfigureOptions o,
    Map<String, String> before,
  ) async {
    final wrong = <String>[];
    final self = await _json('/v1/island');
    final selfId = self is Map ? self['id'] : null;
    if (o.name != null) {
      final served = self is Map ? self['display_name'] : null;
      if (served != o.name!.value) {
        wrong.add(
          'the gateway serves its name as ${jsonEncode(served)}, not '
          '${jsonEncode(o.name!.value)}',
        );
      }
    }
    if (o.seedPeers != null) {
      final dir = await _json('/v1/islands');
      final list = dir is Map ? dir['islands'] : null;
      if (list is! List || selfId == null) {
        wrong.add(
          'could not read the island directory (/v1/island, /v1/islands)',
        );
      } else {
        // By the three fields, so the comparison can't hinge on key order.
        String peer(Map<Object?, Object?> p) =>
            jsonEncode([p['id'], p['display_name'], p['base_url']]);
        final served = {
          for (final p in list)
            if (p is Map && p['id'] != selfId) peer(p),
        };
        final asked = {for (final p in o.seedPeers!.peers) peer(p.toJson())};
        for (final p in asked.difference(served)) {
          wrong.add('peer not served (the gateway dropped it?): $p');
        }
        // With gossip on, the gateway learns peers it was never told about, so an
        // extra entry is expected there and is not this run's to judge.
        if (before['GATEWAY_GOSSIP_ENABLED'] != 'true') {
          for (final p in served.difference(asked)) {
            wrong.add('peer still served though not in the new list: $p');
          }
        }
      }
    }
    if (o.passkeys != null) {
      final prov = await _json('/v1/auth/providers');
      final list = prov is Map ? prov['providers'] : null;
      if (list is! List) {
        wrong.add('could not read /v1/auth/providers');
      } else {
        final offered = list.any((p) => p is Map && p['slug'] == 'passkey');
        if (offered != o.passkeys) {
          wrong.add(
            'passkey sign-in is ${offered ? '' : 'NOT '}offered, though '
            '$keyPasskeys=${o.passkeys}',
          );
        }
      }
    }
    return wrong;
  }

  /// How to put back what this run changed: the exact lines each key had, to put
  /// back by hand. Safe to print: the keys configure owns are not secrets.
  ///
  /// Exact lines, never a synthesized `island configure` command. Three rounds of
  /// cage-match each found a new way a synthesized command failed to restore the
  /// old state: quoting (r1), old values this version's validators refuse (r2), and
  /// values compose had interpolated, which a parsed value can't reproduce (r3,
  /// Carnot: "parser acceptance proves representability, not reversibility"). The
  /// old bytes are reversible by construction, so no rule has to be got right.
  String _revertHint(
    Map<String, String> before,
    Map<String, EnvValue> changes,
  ) {
    if (changes.isEmpty) return 'This run wrote nothing to .env.';
    const restart = 'deploy/compose.sh up -d --no-deps chat-island';
    final set = [for (final k in changes.keys) ..._oldLines(k)];
    final unset = [
      for (final k in changes.keys)
        if (before[k] == null) k,
    ];
    return [
      'To put the previous values back, edit .env by hand, then run: $restart',
      if (set.isNotEmpty) ...[
        '  replace those lines with exactly:',
        for (final l in set) '    $l',
      ],
      if (unset.isNotEmpty)
        '  delete the ${unset.join(', ')} line(s) (not set before this run)',
    ].join('\n');
  }

  /// The exact lines [key] occupied in the `.env` this run read.
  List<String> _oldLines(String key) {
    final lines = _before.split('\n');
    return [
      for (final e in scanDotenv(_before, '.env'))
        if (e.key == key) ...lines.sublist(e.firstLine, e.endLine),
    ];
  }

  /// A copy kept elsewhere (this repo keeps its two islands' in deploy/secrets/ as
  /// SOPS files) is now older than the box, and the next delivery of it would undo
  /// this run: the split-brain hazard in deploy/secrets/README.md.
  void _sopsReminder() => say.out(
    'If a copy of this .env is kept anywhere else (an encrypted copy in git, a '
    'backup), it is now older than the box: once you keep this change, update it, '
    'or its next delivery will undo the change.',
  );
}
