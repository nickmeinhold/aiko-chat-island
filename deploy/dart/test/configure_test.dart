/// `island configure`: what each test pins is in its name.
///
/// `docker compose config` runs FOR REAL (the proof step is the point, and a fake
/// would only agree with this file's parser). The gateway is a fake that reads `.env`
/// at the moment it is restarted and serves from that, as the real one does.
/// `chmod` runs for real, in a temp directory.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:island_deploy/configure.dart';
import 'package:island_deploy/dotenv.dart';
import 'package:island_deploy/options.dart';
import 'package:island_deploy/box.dart';
import 'package:test/test.dart';

final bs = String.fromCharCode(92); // a backslash no tool on the way can decode

/// A .env shaped like the live ones (measured 2026-10-25, names and quoting only):
/// unquoted name and peers, a multi-line double-quoted PEM, single-quoted JSON with
/// backslash escapes, an `export`, comments, a `$`, and keys configure doesn't own.
String liveShaped() => [
  'JWT_SECRET=${'s' * 48}',
  'GATEWAY_BASE_URL=https://chat.example.org',
  'ISLAND_ID=me',
  'ISLAND_DISPLAY_NAME=Example',
  'ISLAND_SEED_PEERS=[{"id": "enspyr", "display_name": "Enspyr", "base_url": "https://chat.enspyr.co"}]',
  'PASSKEY_ENABLED=true',
  'ISLAND_SIGNING_SEED=Zseed',
  '',
  '# APNs: the PEM keeps real newlines inside double quotes',
  'APNS_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----',
  'MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg',
  '-----END PRIVATE KEY-----"',
  'export APNS_TOPIC=cc.example.app',
  "FCM_SERVICE_ACCOUNT_JSON='{\"private_key\":\"a${bs}nb\",\"x\":\"\$HOME\"}'",
  'TRAILING=last',
  '',
].join('\n');

final class FakeBox implements Runner {
  FakeBox(this.root);
  final Directory root;
  final calls = <String>[];
  String ownerUid = '1000';
  bool upFails = false;

  /// Like enspyr-melb: the operator is outside the docker group.
  bool dockerNeedsSudo = false;
  bool sudoWorks = true;

  /// Runs after the Nth real `compose config` call (0-based), to tamper with its
  /// answer or with the box.
  RunResult Function(int n, RunResult r)? onConfig;
  int _configs = 0;

  /// What the gateway read at its last restart; null = never restarted.
  Map<String, String>? gatewayEnv;

  @override
  Future<RunResult> run(
    String exe,
    List<String> args, {
    String? workingDirectory,
    bool stream = false,
  }) async {
    calls.add([exe, ...args].join(' '));
    switch (exe) {
      case 'chmod':
        final r = await Process.run(exe, args);
        return RunResult(r.exitCode, '${r.stdout}', '${r.stderr}');
      case 'id':
        return const RunResult(0, '1000\n', '');
      case 'stat':
        return RunResult(0, '$ownerUid\n', '');
      case 'docker' when dockerNeedsSudo:
        return const RunResult(1, '', 'permission denied on docker.sock');
      case 'sudo' when !sudoWorks:
        return const RunResult(1, '', 'sudo: a password is required');
      case 'docker' || 'sudo' when args.contains('config'):
        final p = await Process.run(
          'docker',
          exe == 'sudo' ? args.sublist(2) : args,
          workingDirectory: workingDirectory,
        );
        final r = RunResult(p.exitCode, '${p.stdout}', '${p.stderr}');
        return onConfig?.call(_configs++, r) ?? r;
    }
    if (exe.endsWith('deploy/compose.sh')) {
      if (upFails) return const RunResult(1, '', 'up failed');
      gatewayEnv = parseDotenv(
        File('${root.path}/.env').readAsStringSync(),
        '.env',
      );
      return const RunResult(0, '', '');
    }
    return const RunResult(0, '', '');
  }
}

/// The gateway, answering from what it read at restart.
final class FakeGateway {
  FakeGateway(this.box);
  final FakeBox box;
  bool healthy = true;
  Set<String> drops = {};
  bool forwardsName = true;

  Future<LocalResponse?> fetch(Uri u) async {
    final env = box.gatewayEnv;
    if (env == null || !healthy) return null;
    final self = {
      'id': 'me',
      'display_name': forwardsName
          ? env['ISLAND_DISPLAY_NAME'] ?? 'Aiko'
          : 'Aiko',
      'base_url': 'https://chat.example.org',
    };
    final Object body = switch (u.path) {
      '/health' => {'status': 'ok'},
      '/v1/island' => self,
      '/v1/islands' => {
        'islands': [
          self,
          for (final p in jsonDecode(env['ISLAND_SEED_PEERS'] ?? '[]') as List)
            if (!drops.contains((p as Map)['id'])) p,
        ],
      },
      '/v1/auth/providers' => {
        'providers': [
          if (env['PASSKEY_ENABLED'] == 'true') {'slug': 'passkey'},
        ],
      },
      _ => {},
    };
    return (status: 200, body: jsonEncode(body));
  }
}

late Directory root;
late FakeBox box;
late FakeGateway gateway;
late List<String> out;

File get envFile => File('${root.path}/.env');

Configure configure() => Configure(
  runner: box,
  root: root,
  say: Say(out.add, out.add),
  fetch: gateway.fetch,
  sleep: (_) async {},
  random: Random(7),
);

ConfigureOptions args(List<String> a) => parseConfigureArgs(a);

/// The lines of [after] that differ from [before], as (line number, text).
List<String> changedLines(String before, String after) {
  final a = before.split('\n'), b = after.split('\n');
  return [
    for (var i = 0; i < max(a.length, b.length); i++)
      if (i >= a.length || i >= b.length || a[i] != b[i])
        '${i + 1}: ${i < b.length ? b[i] : '(gone)'}',
  ];
}

/// Refuses `--name X` with [message], leaving .env byte for byte and no restart.
Future<void> refusesWith(Matcher message) async {
  final before = envFile.readAsStringSync();
  await expectLater(
    configure().run(parseConfigureArgs(['--name', 'X'])),
    throwsA(isA<Refusal>().having((r) => r.message, 'message', message)),
  );
  expect(envFile.readAsStringSync(), before);
  expect(box.gatewayEnv, isNull);
}

void main() {
  setUp(() {
    root = Directory.systemTemp.createTempSync('configure');
    File('${root.path}/docker-compose.yml').writeAsStringSync('services: {}\n');
    Directory('${root.path}/deploy').createSync();
    File('${root.path}/deploy/compose.sh').writeAsStringSync('#!/bin/sh\n');
    envFile.writeAsStringSync(liveShaped());
    Process.runSync('chmod', ['600', envFile.path]);
    box = FakeBox(root);
    gateway = FakeGateway(box);
    out = [];
  });
  tearDown(() => root.deleteSync(recursive: true));

  group('edits in place', () {
    test('a name change rewrites that one line and no other byte', () async {
      final before = envFile.readAsStringSync();
      final r = await configure().run(args(['--name', 'New Name #2']));
      expect(r, isA<Applied>());
      final after = envFile.readAsStringSync();
      expect(changedLines(before, after), [
        "4: ISLAND_DISPLAY_NAME='New Name #2'",
      ]);
    });

    test('peers and passkeys left out are left as they are (#3734)', () async {
      final before = parseDotenv(envFile.readAsStringSync(), 'before');
      await configure().run(args(['--name', 'X']));
      final after = parseDotenv(envFile.readAsStringSync(), 'after');
      expect(after['ISLAND_SEED_PEERS'], before['ISLAND_SEED_PEERS']);
      expect(after['PASSKEY_ENABLED'], 'true');
      expect(after['JWT_SECRET'], before['JWT_SECRET']);
    });

    test('--no-passkeys turns them off, explicitly', () async {
      final r = await configure().run(args(['--no-passkeys']));
      expect(r, isA<Applied>());
      expect(
        parseDotenv(envFile.readAsStringSync(), '.env')['PASSKEY_ENABLED'],
        'false',
      );
    });

    test('a key the file lacks is appended, and the file still ends in one newline', () async {
      envFile.writeAsStringSync(
        liveShaped().replaceFirst(RegExp(r'PASSKEY_ENABLED=true\n'), ''),
      );
      await configure().run(args(['--enable-passkeys']));
      final after = envFile.readAsStringSync();
      expect(after, endsWith('TRAILING=last\nPASSKEY_ENABLED=true\n'));
    });

    test('the multi-line PEM, the export and the escaped JSON survive (compose agrees)', () async {
      final before = parseDotenv(envFile.readAsStringSync(), 'before');
      final peers =
          '[{"id":"b","display_name":"Bee\'s #1 \$X","base_url":"https://b.example"}]';
      final r = await configure().run(args(['--seed-peers', peers]));
      expect(r, isA<Applied>());
      final after = parseDotenv(envFile.readAsStringSync(), 'after');
      for (final k in before.keys.where((k) => k != 'ISLAND_SEED_PEERS')) {
        expect(after[k], before[k], reason: k);
      }
      // A quote in a peer name is carried, as the JSON escape, not refused.
      expect(jsonDecode(after['ISLAND_SEED_PEERS']!), jsonDecode(peers));
      expect(envFile.readAsStringSync(), contains('${bs}u0027'));
    });

    test(
      'the temporary is ignored by git (a stranded one holds every secret)',
      () async {
        final r = await Process.run('git', [
          'check-ignore',
          '-q',
          '.env.configure-abcd1234',
        ], workingDirectory: '../..');
        expect(r.exitCode, 0, reason: '.gitignore must cover *.configure-*');
        expect(
          File('lib/configure.dart').readAsStringSync(),
          contains("_f('.env.configure-"),
        );
      },
    );

    test('.env stays mode 600 and no temporary is left beside it', () async {
      await configure().run(args(['--name', 'X']));
      expect(envFile.statSync().modeString(), 'rw-------');
      expect(
        root.listSync().map((e) => e.path.split('/').last),
        isNot(contains(startsWith('.env.configure-'))),
      );
    });

    test(
      'compose is asked about both files, then only the gateway restarts',
      () async {
        await configure().run(args(['--name', 'X']));
        expect(box.calls.where((c) => c.contains(' config ')), hasLength(2));
        expect(
          box.calls.last,
          endsWith('deploy/compose.sh up -d --no-deps chat-island'),
        );
      },
    );

    test('says what changed, and how to put it back', () async {
      await configure().run(args(['--name', 'X']));
      expect(out.join('\n'), contains('ISLAND_DISPLAY_NAME: Example  ->  X'));
    });
  });

  group('changes nothing when', () {
    test(
      'every setting already has that value (and does not restart)',
      () async {
        final before = envFile.readAsStringSync();
        final r = await configure().run(
          args(['--name', 'Example', '--enable-passkeys']),
        );
        expect(r, isA<Unchanged>());
        expect(envFile.readAsStringSync(), before);
        expect(box.gatewayEnv, isNull);
      },
    );

    Future<void> refuses(
      ConfigureOptions o,
      Matcher message, {
      bool restarted = false,
    }) async {
      final before = envFile.existsSync() ? envFile.readAsStringSync() : null;
      await expectLater(
        configure().run(o),
        throwsA(isA<Refusal>().having((r) => r.message, 'message', message)),
      );
      if (before != null) expect(envFile.readAsStringSync(), before);
      expect(box.gatewayEnv == null, !restarted);
    }

    test(
      'compose would read the edit differently (its proof is load-bearing)',
      () async {
        box.onConfig = (n, r) => n == 1
            ? RunResult(r.exitCode, r.stdout.replaceFirst('Zseed', 'Zxxxx'), '')
            : r;
        await refuses(args(['--name', 'X']), contains('ISLAND_SIGNING_SEED'));
      },
    );

    test('the refusal names keys, never values', () async {
      box.onConfig = (n, r) => n == 1
          ? RunResult(r.exitCode, r.stdout.replaceFirst('Zseed', 'Zxxxx'), '')
          : r;
      await refuses(args(['--name', 'X']), isNot(contains('Zseed')));
    });

    test('.env changed while configure ran', () async {
      box.onConfig = (n, r) {
        if (n == 1) envFile.writeAsStringSync('${liveShaped()}NEW=1\n');
        return r;
      };
      final o = args(['--name', 'X']);
      await expectLater(configure().run(o), throwsA(isA<Refusal>()));
      // The other writer's file, not ours, is what is there.
      expect(envFile.readAsStringSync(), endsWith('NEW=1\n'));
      expect(box.gatewayEnv, isNull);
    });

    test('compose cannot read the file at all', () async {
      box.onConfig = (n, r) => const RunResult(1, '', 'secret-bearing stderr');
      await refuses(args(['--name', 'X']), isNot(contains('secret-bearing')));
    });

    test('.env is owned by another user (sudo would steal it)', () async {
      box.ownerUid = '0';
      await refuses(args(['--name', 'X']), contains('owned by uid 0'));
    });

    test('an owned key is set twice', () async {
      envFile.writeAsStringSync('${liveShaped()}ISLAND_DISPLAY_NAME=Again\n');
      await refuses(args(['--name', 'X']), contains('set 2 times'));
    });

    test('the file has CRLF line endings', () async {
      envFile.writeAsStringSync(liveShaped().replaceAll('\n', '\r\n'));
      await refuses(args(['--name', 'X']), contains('CRLF'));
    });

    test('a peer list names this island itself', () async {
      await refuses(
        args([
          '--seed-peers',
          '[{"id":"me","display_name":"Me","base_url":"https://chat.example.org"}]',
        ]),
        contains('itself'),
      );
    });

    test('there is no .env (no island here)', () async {
      envFile.deleteSync();
      await refuses(args(['--name', 'X']), contains('no island here'));
    });

    test('.env is a symlink', () async {
      final real = File('${root.path}/real.env')
        ..writeAsStringSync(liveShaped());
      envFile.deleteSync();
      Link(envFile.path).createSync(real.path);
      await expectLater(
        configure().run(args(['--name', 'X'])),
        throwsA(isA<Refusal>()),
      );
      expect(real.readAsStringSync(), liveShaped());
    });
  });

  group('Docker outside the docker group (enspyr-melb)', () {
    test(
      'compose is asked through `sudo -n docker`, and configure still applies',
      () async {
        box.dockerNeedsSudo = true;
        final r = await configure().run(args(['--name', 'X']));
        expect(r, isA<Applied>());
        final asked = box.calls.where((c) => c.contains(' config ')).toList();
        expect(asked, hasLength(2));
        expect(asked, everyElement(startsWith('sudo -n docker compose')));
      },
    );

    test('no docker and no passwordless sudo refuses before writing', () async {
      box
        ..dockerNeedsSudo = true
        ..sudoWorks = false;
      await refusesWith(contains('sudo -n docker'));
    });
  });

  group('after the restart, the gateway must SERVE it', () {
    test('a peer the gateway dropped is not called applied (exit 5)', () async {
      gateway.drops = {'b'};
      final r = await configure().run(
        args([
          '--seed-peers',
          '[{"id":"b","display_name":"B","base_url":"https://b.example"}]',
        ]),
      );
      expect(r, isA<AnsweringNotServing>());
      expect(configureExitCode(r), 5);
      expect(out.join('\n'), contains('island configure --seed-peers'));
    });

    test('a removed peer still served is caught (gossip off)', () async {
      // The gateway kept the old list: as if the restart never re-read .env.
      final stale = parseDotenv(liveShaped(), '.env');
      final r = await Configure(
        runner: box,
        root: root,
        say: Say(out.add, out.add),
        fetch: (u) async {
          box.gatewayEnv = stale;
          return gateway.fetch(u);
        },
        sleep: (_) async {},
      ).run(args(['--seed-peers', '[]']));
      expect(r, isA<AnsweringNotServing>());
    });

    test('a name compose never forwarded is caught', () async {
      gateway.forwardsName = false;
      final r = await configure().run(args(['--name', 'X']));
      expect(r, isA<AnsweringNotServing>());
    });

    test('a gateway that never answers is exit 3, not success', () async {
      gateway.healthy = false;
      final r = await configure().run(args(['--name', 'X']));
      expect(r, isA<WrittenNotAnswering>());
      expect(configureExitCode(r), 3);
    });

    test(
      'a failed restart says the file IS written, and how to revert',
      () async {
        box.upFails = true;
        await expectLater(
          configure().run(args(['--name', 'X'])),
          throwsA(
            isA<Refusal>().having(
              (r) => r.message,
              'message',
              allOf(contains('IS written'), contains("--name 'Example'")),
            ),
          ),
        );
      },
    );
  });

  group('editDotenv on its own', () {
    test('keeps an export and the indentation of the line it replaces', () {
      final t = 'A=1\n  export ISLAND_DISPLAY_NAME=Old\nB=2\n';
      expect(
        editDotenv(t, {keyName: DisplayName('New')}),
        "A=1\n  export ISLAND_DISPLAY_NAME='New'\nB=2\n",
      );
    });

    test('replaces every line of a multi-line value', () {
      final t = 'A=1\nISLAND_DISPLAY_NAME="two\nlines"\nB=2\n';
      expect(
        editDotenv(t, {keyName: DisplayName('One')}),
        "A=1\nISLAND_DISPLAY_NAME='One'\nB=2\n",
      );
    });

    test('appends to a file without a final newline', () {
      expect(
        editDotenv('A=1', {keyPasskeys: const PasskeysEnabled(false)}),
        'A=1\nPASSKEY_ENABLED=false\n',
      );
    });
  });

  group('arguments', () {
    for (final (why, a, says) in [
      (
        'both passkey flags',
        ['--enable-passkeys', '--no-passkeys'],
        'contradict',
      ),
      ('a domain change', ['--domain', 'x.example.org'], 'RP id'),
      ('a flag as a value', ['--name', '--no-passkeys'], 'needs a value'),
      ('media', ['--with-media'], 'media'),
      ('an unknown flag', ['--nmae', 'X'], 'unknown argument'),
    ]) {
      test('refuses $why', () {
        expect(
          () => parseConfigureArgs(a),
          throwsA(isA<Refusal>().having((r) => r.message, 'm', contains(says))),
        );
      });
    }

    test('no options means show, not change', () {
      expect(parseConfigureArgs([]).isEmpty, isTrue);
      configure().show();
      expect(out, contains('ISLAND_DISPLAY_NAME=Example'));
    });
  });

  group('seed-peer rules match the gateway (shared vectors)', () {
    final file = File('test/fixtures/seed_peers.json');
    final vectors =
        (jsonDecode(file.readAsStringSync()) as Map)['vectors'] as List;
    for (final v in vectors.cast<Map<String, Object?>>()) {
      final keep = v['python'] == 'keep';
      test(
        '${keep ? 'accepts' : 'refuses'} ${v['why']} (gateway: ${v['python']})',
        () {
          if (keep) {
            final p = SeedPeer.fromJson(v['entry']);
            expect(p.toJson(), v['entry']);
          } else {
            expect(
              () => SeedPeer.fromJson(v['entry']),
              throwsA(isA<Refusal>()),
            );
          }
        },
      );
    }

    test('a duplicate id or base_url is refused (the gateway keeps only the first)', () {
      const a = '{"id":"a","display_name":"A","base_url":"https://a.example"}';
      const a2 =
          '{"id":"a","display_name":"A2","base_url":"https://a2.example"}';
      const b = '{"id":"b","display_name":"B","base_url":"https://a.example"}';
      expect(() => SeedPeers.parse('[$a,$a2]'), throwsA(isA<Refusal>()));
      expect(() => SeedPeers.parse('[$a,$b]'), throwsA(isA<Refusal>()));
    });
  });
}
