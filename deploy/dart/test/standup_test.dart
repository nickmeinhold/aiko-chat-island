/// `island standup`: what each test pins is in its name. Docker is a fake that
/// behaves the way the birth depends on (measured on Docker 29.5.2 and 29.1.3):
/// `volume create` on an EXISTING volume succeeds and keeps its old label. `ln` and
/// `chmod` run for real, in a temp directory, so the file publish is exercised.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:island_deploy/dotenv.dart';
import 'package:island_deploy/standup.dart';
import 'package:test/test.dart';

/// Records every call; answers like Docker. `volumes` maps name -> aiko.birth label.
final class FakeDocker implements Runner {
  FakeDocker({Map<String, String>? volumes, this.failOn})
    : volumes = volumes ?? {};
  final Map<String, String> volumes;
  final String? failOn;
  final calls = <String>[];
  bool lsFails = false;

  @override
  Future<RunResult> run(
    String exe,
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  }) async {
    final line = [exe, ...args].join(' ');
    calls.add(line);
    if (failOn != null && line.contains(failOn!)) {
      return const RunResult(1, '', 'boom');
    }
    if (exe == 'ln' || exe == 'chmod') {
      final r = await Process.run(exe, args);
      return RunResult(r.exitCode, '${r.stdout}', '${r.stderr}');
    }
    if (exe == 'ss') return const RunResult(0, '', '');
    if (exe != 'docker') return const RunResult(0, '', '');
    final name = args.last;
    switch (args) {
      case ['volume', 'ls', ...]:
        if (lsFails) {
          return const RunResult(1, '', 'Cannot connect to the Docker daemon');
        }
        return RunResult(
          0,
          volumes.containsKey('aiko_data') ? 'aiko_data\n' : '',
          '',
        );
      case ['volume', 'create', '--label', final kv, _]:
        volumes.putIfAbsent(
          name,
          () => kv.split('=').last,
        ); // keeps an existing label
        return RunResult(0, '$name\n', '');
      case ['volume', 'inspect', '-f', _, _]:
        final l = volumes[name];
        return l == null
            ? const RunResult(1, '', 'no such volume')
            : RunResult(0, '$l\n', '');
      default:
        return const RunResult(0, '', '');
    }
  }
}

const opts = StandupOptions(
  domain: 'chat.example.org',
  displayName: 'Island #1',
);

late Directory root;
late List<String> out;

Standup standup(
  FakeDocker docker, {
  Map<String, String> env = const {},
  bool healthy = true,
}) => Standup(
  runner: docker,
  root: root,
  say: Say(out.add, out.add),
  environment: env,
  probe: (_) async => healthy,
  sleep: (_) async {},
  random: Random(7),
);

int at(FakeDocker d, String prefix) =>
    d.calls.indexWhere((c) => c.startsWith(prefix));

/// For a call made by ABSOLUTE path (deploy/compose.sh), whose prefix is the temp root.
int atPath(FakeDocker d, String tail) =>
    d.calls.indexWhere((c) => c.startsWith(root.path) && c.contains(tail));

void main() {
  setUp(() {
    root = Directory.systemTemp.createTempSync('standup');
    File('${root.path}/docker-compose.yml').writeAsStringSync('services: {}\n');
    Directory('${root.path}/deploy/caddy').createSync(recursive: true);
    File('${root.path}/deploy/compose.sh').writeAsStringSync('#!/bin/sh\n');
    File('${root.path}/deploy/caddy/docker-compose.caddy.yml')
        .writeAsStringSync('');
    out = [];
  });
  tearDown(() => root.deleteSync(recursive: true));

  test('the birth script is byte-identical to deploy/standup.sh BIRTH_SH', () {
    // Two front ends, one birth. A copy drifts; the bash one is the reference.
    final sh = File('../standup.sh').readAsStringSync();
    final start = sh.indexOf("BIRTH_SH='") + "BIRTH_SH='".length;
    expect(sh.substring(start, sh.indexOf("'\n", start)), birthScript);
  });

  group('a new island', () {
    test('pull, then the volume and seed, then the files, then up', () async {
      final d = FakeDocker();
      await standup(d).run(opts);
      final pull = at(d, 'docker pull'),
          create = at(d, 'docker volume create --label aiko.birth='),
          inspect = at(d, 'docker volume inspect'),
          copy = at(d, 'docker run --rm -v aiko_data:/data'),
          ln = at(d, 'ln '),
          up = atPath(d, '/deploy/compose.sh up -d');
      expect(
        [pull, create, inspect, copy, ln, up].every((i) => i >= 0),
        isTrue,
        reason: '${d.calls}',
      );
      expect(
        pull < create &&
            create < inspect &&
            inspect < copy &&
            copy < ln &&
            ln < up,
        isTrue,
        reason: 'a failed pull or birth must leave no .env behind: ${d.calls}',
      );
    });

    test('.env is mode 600, production (no ENVIRONMENT), and reads back what was given', () async {
      final o = StandupOptions(
        domain: 'chat.example.org',
        displayName:
            'Island #1', // a " #" an unquoted dotenv value would truncate
        seedPeers:
            '[{"id":"a","display_name":"A #2","base_url":"https://a.example"}]',
        passkeys: true,
      );
      await standup(FakeDocker()).run(o);
      final f = File('${root.path}/.env');
      expect(f.statSync().modeString(), 'rw-------');
      final env = parseDotenv(f.readAsStringSync(), '.env');
      expect(env.containsKey('ENVIRONMENT'), isFalse);
      expect(env['JWT_SECRET'], hasLength(64));
      expect(env['ISLAND_DISPLAY_NAME'], 'Island #1');
      expect(env['ISLAND_SEED_PEERS'], o.seedPeers);
      expect(env['GATEWAY_BASE_URL'], 'https://chat.example.org');
      expect(env['PASSKEY_RP_ID'], 'chat.example.org');
      expect(env['PASSKEY_ENABLED'], 'true');
    });

    test('a gateway that never answers is NOT reported as up', () async {
      final r = await standup(FakeDocker(), healthy: false).run(opts);
      expect(r, isA<StartedNotAnswering>());
      final said = out.join('\n');
      expect(said, contains('NOT confirmed working'));
      expect(said, isNot(contains('is up.')));
    });

    test('a gateway that answers is reported healthy', () async {
      expect(await standup(FakeDocker()).run(opts), isA<Healthy>());
    });

    test('image.env pins the given release, else edge said aloud', () async {
      await standup(FakeDocker()).run(opts);
      expect(
        File('${root.path}/image.env').readAsStringSync(),
        'ISLAND_IMAGE=ghcr.io/nickmeinhold/aiko-chat-island:edge\n',
      );
      expect(out.join('\n'), contains("pinning 'edge'"));
    });

    test('no temporary files are left beside the published ones', () async {
      await standup(FakeDocker()).run(opts);
      final names = root.listSync().map(
        (e) => e.uri.pathSegments.where((s) => s.isNotEmpty).last,
      );
      expect(names.where((n) => n.contains('standup-')), isEmpty);
    });
  });

  group('refuses, and changes nothing, when', () {
    Future<void> refuses(
      FakeDocker d, {
      Map<String, String> env = const {},
    }) async {
      await expectLater(
        standup(d, env: env).run(opts),
        throwsA(isA<Refusal>()),
      );
      expect(
        d.calls.where(
          (c) =>
              c.startsWith('docker volume create') ||
              c.startsWith('docker run'),
        ),
        isEmpty,
        reason: 'no volume and no copy: ${d.calls}',
      );
      expect(
        File('${root.path}/.env').existsSync() &&
            !_preexisting.contains('.env'),
        isFalse,
      );
    }

    test('a .env is already here', () async {
      _preexisting = {'.env'};
      File('${root.path}/.env').writeAsStringSync('JWT_SECRET=keep\n');
      await refuses(FakeDocker());
      expect(File('${root.path}/.env').readAsStringSync(), 'JWT_SECRET=keep\n');
    });

    test('an image.env is already here', () async {
      _preexisting = {};
      File('${root.path}/image.env').writeAsStringSync('ISLAND_IMAGE=x\n');
      await refuses(FakeDocker());
    });

    test('the aiko_data volume already exists', () async {
      _preexisting = {};
      await refuses(FakeDocker(volumes: {'aiko_data': ''}));
    });

    test('Docker cannot list volumes (a failed look is not "none")', () async {
      _preexisting = {};
      await refuses(FakeDocker()..lsFails = true);
    });

    test('an image pin is exported in the environment', () async {
      _preexisting = {};
      await refuses(FakeDocker(), env: {'ISLAND_IMAGE': 'x'});
    });

    test('the pull fails', () async {
      _preexisting = {};
      await refuses(FakeDocker(failOn: 'docker pull'));
    });
  });

  group('the birth', () {
    test('a volume that appears between the check and the create is never taken over', () async {
      // The r3 finding-1 shape: the listing said "none", but the volume exists by the
      // time of the create (a flake, or a concurrent run). Its label isn't ours.
      final d = _AppearsLate();
      await expectLater(standup(d).run(opts), throwsA(isA<Refusal>()));
      expect(d.calls.where((c) => c.startsWith('docker run')), isEmpty);
      expect(File('${root.path}/.env').existsSync(), isFalse);
      expect(out.join('\n') + d.message, isNot(contains('restore')));
    });

    test(
      'a failed copy stops before any file is written and claims no history',
      () async {
        final d = FakeDocker(failOn: 'docker run --rm -v aiko_data:/data');
        Refusal? r;
        try {
          await standup(d).run(opts);
        } on Refusal catch (e) {
          r = e;
        }
        expect(r, isNotNull);
        expect(r!.message, contains('A failed birth'));
        expect(r.message, isNot(contains('safe to remove')));
        expect(File('${root.path}/.env').existsSync(), isFalse);
        expect(d.calls.where((c) => c.contains('compose.sh up')), isEmpty);
      },
    );
  });

  group('validation, before anything touches the box', () {
    for (final (why, args) in [
      (
        'scheme in the domain',
        ['--domain', 'https://chat.example.org', '--name', 'X'],
      ),
      (
        'a quote in the name',
        ['--domain', 'chat.example.org', '--name', "Nick's"],
      ),
      (
        'peers not JSON',
        ['--domain', 'a.example.org', '--name', 'X', '--seed-peers', '[{'],
      ),
      (
        'a peer missing base_url',
        [
          '--domain',
          'a.example.org',
          '--name',
          'X',
          '--seed-peers',
          '[{"id":"a","display_name":"A"}]',
        ],
      ),
      (
        'a v-prefixed tag',
        ['--domain', 'a.example.org', '--name', 'X', '--version', 'v0.18.0'],
      ),
      (
        'media asked for',
        ['--domain', 'a.example.org', '--name', 'X', '--with-media'],
      ),
      ('no name', ['--domain', 'a.example.org']),
    ]) {
      test(
        'refuses $why',
        () => expect(() => parseStandupArgs(args), throwsA(isA<Refusal>())),
      );
    }

    test('accepts a full, valid set', () {
      final o = parseStandupArgs([
        '--domain',
        'chat.example.org',
        '--name',
        'Example',
        '--no-tls',
        '--enable-passkeys',
        '--version',
        '0.19.0',
        '--seed-peers',
        '[]',
      ]);
      expect((o.tls, o.passkeys, o.tag), (false, true, '0.19.0'));
    });
  });
  group('PR#406 r1', () {
    test('the exit code is 0 only for up AND confirmed', () {
      // Scripts read this number (Noether r1: the mapping was untested).
      expect(exitCodeFor(const Healthy()), 0);
      expect(exitCodeFor(const StartedNotAnswering()), 3);
      expect(exitCodeFor(const HealthyTlsUnconfirmed()), 4);
    });

    for (final missing in [
      'deploy/compose.sh',
      'deploy/caddy/docker-compose.caddy.yml',
    ]) {
      test('a missing $missing refuses before anything changes', () async {
        File('${root.path}/$missing').deleteSync();
        final d = FakeDocker();
        await expectLater(standup(d).run(opts), throwsA(isA<Refusal>()));
        expect(
          d.calls.where(
            (c) =>
                c.startsWith('docker pull') ||
                c.startsWith('docker volume create'),
          ),
          isEmpty,
        );
      });
    }

    test('without TLS the Caddy compose file is not required', () async {
      File('${root.path}/deploy/caddy/docker-compose.caddy.yml').deleteSync();
      const noTls = StandupOptions(
        domain: 'chat.example.org',
        displayName: 'X',
        tls: false,
      );
      expect(await standup(FakeDocker()).run(noTls), isA<Healthy>());
    });

    test('TLS that never confirms is its own outcome, not Healthy', () async {
      final d = FakeDocker(failOn: 'docker-compose.caddy.yml');
      expect(await standup(d).run(opts), isA<HealthyTlsUnconfirmed>());
      expect(out.join('\n'), contains('HTTPS is NOT confirmed'));
    });

    test('a failure after the birth says what this run created, and publishes nothing half-way', () async {
      // ln fails: the temps were all written, so neither file may appear.
      final d = FakeDocker(failOn: 'ln ${root.path}');
      Refusal? r;
      try {
        await standup(d).run(opts);
      } on Refusal catch (e) {
        r = e;
      }
      expect(r, isNotNull);
      expect(
        r!.message,
        contains('This run created: volume aiko_data (label aiko.birth='),
      );
      expect(r.message, contains('A failed birth'));
      expect(File('${root.path}/.env').existsSync(), isFalse);
      expect(File('${root.path}/image.env').existsSync(), isFalse);
      expect(
        root.listSync().where((e) => e.path.contains('.standup-')),
        isEmpty,
        reason: 'temps removed',
      );
      expect(d.calls.where((c) => c.contains('compose.sh up')), isEmpty);
    });

    test('a failed compose up says what this run created', () async {
      final d = FakeDocker(failOn: 'compose.sh up');
      await expectLater(
        standup(d).run(opts),
        throwsA(
          isA<Refusal>().having(
            (r) => r.message,
            'message',
            contains(
              RegExp(
                r'This run created: volume aiko_data \(label aiko\.birth=[0-9a-f]+\), image\.env, \.env\. ',
              ),
            ),
          ),
        ),
      );
    });

    test('a volume without a .env is not called an island, and no command is prescribed', () async {
      Refusal? r;
      try {
        await standup(FakeDocker(volumes: {'aiko_data': ''})).run(opts);
      } on Refusal catch (e) {
        r = e;
      }
      expect(r!.message, contains('stopped partway'));
      expect(r.message, isNot(contains('island configure')));
    });

    for (final (why, name) in [
      ('a backslash', r'Island \'),
      ('a carriage return', 'Island\r1'),
      ('a tab', 'a\tb'),
    ]) {
      test('refuses a name with $why', () {
        expect(
          () => validate(
            StandupOptions(domain: 'a.example.org', displayName: name),
          ),
          throwsA(isA<Refusal>()),
        );
      });
    }

    test('the temporary names are ignored by git (a stranded one may hold the JWT secret)', () async {
      final r = await Process.run('git', [
        'check-ignore', // no -q: git allows --quiet with ONE path only (exit 128)
        '.env.standup-abcd1234',
        'image.env.standup-abcd1234',
      ], workingDirectory: '../..');
      expect(r.exitCode, 0, reason: '.gitignore must cover *.standup-*');
      expect(
        '${r.stdout}'.trim().split('\n'),
        hasLength(2),
        reason: 'both names ignored',
      );
      expect(
        File('lib/standup.dart').readAsStringSync(),
        contains("'\$name.standup-"),
      );
    });
  });
  group('PR#406 r2', () {
    test('--version pins that release', () async {
      const o = StandupOptions(
        domain: 'chat.example.org',
        displayName: 'X',
        tag: '0.19.0',
      );
      await standup(FakeDocker()).run(o);
      expect(
        File('${root.path}/image.env').readAsStringSync(),
        'ISLAND_IMAGE=ghcr.io/nickmeinhold/aiko-chat-island:0.19.0\n',
      );
      expect(out.join('\n'), isNot(contains("pinning 'edge'")));
    });

    test(
      'run() validates too: options built directly are not trusted',
      () async {
        final d = FakeDocker();
        const bad = StandupOptions(
          domain: 'a.example.org',
          displayName: 'X',
          seedPeers: '[{',
        );
        await expectLater(standup(d).run(bad), throwsA(isA<Refusal>()));
        expect(d.calls, isEmpty);
      },
    );

    test('TLS is not attempted when the gateway never answered', () async {
      final d = FakeDocker();
      expect(
        await standup(d, healthy: false).run(opts),
        isA<StartedNotAnswering>(),
      );
      expect(
        d.calls.where((c) => c.contains('docker-compose.caddy.yml')),
        isEmpty,
      );
      expect(File('${root.path}/deploy/caddy/.env').existsSync(), isFalse);
    });

    test('health means THIS gateway said ok, not any 2xx', () {
      expect(isGatewayHealthy(200, '{"status":"ok","version":"x"}'), isTrue);
      expect(isGatewayHealthy(200, '<html>proxy</html>'), isFalse);
      expect(isGatewayHealthy(204, ''), isFalse);
      expect(isGatewayHealthy(200, '{"status":"degraded"}'), isFalse);
      expect(isGatewayHealthy(502, '{"status":"ok"}'), isFalse);
    });

    test('docker compose itself reads the .env back exactly (the real consumer)', () async {
      // Not our own parser: compose is what reads .env on the box (Noether r2). A " #"
      // must not truncate, a "$" must not interpolate. Needs docker compose (CI has it).
      const o = StandupOptions(
        domain: 'chat.example.org',
        displayName: r'Island #1 $HOME',
        seedPeers: r'[{"id":"a","display_name":"A #2 $X","base_url":"https://a.example"}]',
      );
      File('${root.path}/.env').writeAsStringSync(renderEnv(o, 'x' * 64));
      File('${root.path}/c.yml').writeAsStringSync(
        'services:\n  x:\n    image: alpine\n    environment:\n'
        '      N: \${ISLAND_DISPLAY_NAME}\n      P: \${ISLAND_SEED_PEERS}\n',
      );
      final r = await Process.run('docker', [
        'compose',
        '-f',
        'c.yml',
        '--env-file',
        '.env',
        'config',
        '--format',
        'json',
      ], workingDirectory: root.path);
      expect(r.exitCode, 0, reason: '${r.stderr}');
      final env =
          (jsonDecode('${r.stdout}') as Map)['services']['x']['environment']
              as Map;
      // Measured: `compose config` re-escapes a literal $ as $$ in its OUTPUT, so
      // the rendered file stays valid compose. Interpolation would have printed
      // $HOME's value instead; turning $$ back into $ undoes only the rendering.
      String lit(Object? v) => '$v'.replaceAll(r'$$', r'$');
      expect(lit(env['N']), o.displayName);
      expect(lit(env['P']), o.seedPeers);
    });
  });
}

Set<String> _preexisting = {};

/// `volume ls` says no volume, but one with someone else's label is there by the create.
final class _AppearsLate extends FakeDocker {
  _AppearsLate() : super();
  String message = '';
  @override
  Future<RunResult> run(
    String exe,
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  }) {
    if (args.length > 1 && args[0] == 'volume' && args[1] == 'create') {
      volumes.putIfAbsent('aiko_data', () => 'someone-else');
    }
    return super.run(exe, args, workingDirectory: workingDirectory);
  }
}
