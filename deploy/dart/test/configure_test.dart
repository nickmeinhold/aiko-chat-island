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
  'PASSKEY_RP_ID=chat.example.org',
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

  /// What the running gateway read when it last started. setUp starts it on the
  /// fixture, as a live island is; null = the gateway is down.
  Map<String, String>? gatewayEnv;

  /// How many times configure restarted the gateway.
  int restarts = 0;

  /// What `docker ps` reports the gateway was created from; null = not running.
  String? runningImage = pinnedImage;

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
      case 'docker' || 'sudo'
          when args.contains('ps') && args.contains('--filter'):
        return RunResult(0, runningImage == null ? '' : '$runningImage\n', '');
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
      restarts++;
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

const pinnedImage = 'ghcr.io/nickmeinhold/aiko-chat-island:0.19.0';

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

/// Rewrites the fixture's unquoted name line as configure itself writes it.
void canonicalName() => envFile.writeAsStringSync(
  envFile.readAsStringSync().replaceFirst(
    'ISLAND_DISPLAY_NAME=Example',
    "ISLAND_DISPLAY_NAME='Example'",
  ),
);

/// Refuses `--name <name>` with [message], leaving .env byte for byte and no restart.
Future<void> refusesWith(Matcher message, {String name = 'X'}) async {
  final before = envFile.readAsStringSync();
  await expectLater(
    configure().run(parseConfigureArgs(['--name', name])),
    throwsA(isA<Refusal>().having((r) => r.message, 'message', message)),
  );
  expect(envFile.readAsStringSync(), before);
  expect(box.restarts, 0);
}

void main() {
  setUp(() {
    root = Directory.systemTemp.createTempSync('configure');
    File('${root.path}/docker-compose.yml').writeAsStringSync('services: {}\n');
    Directory('${root.path}/deploy').createSync();
    File('${root.path}/deploy/compose.sh').writeAsStringSync('#!/bin/sh\n');
    envFile.writeAsStringSync(liveShaped());
    Process.runSync('chmod', ['600', envFile.path]);
    File('${root.path}/image.env')
        .writeAsStringSync('ISLAND_IMAGE=$pinnedImage\n');
    box = FakeBox(root)..gatewayEnv = parseDotenv(liveShaped(), '.env');
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
        "5: ISLAND_DISPLAY_NAME='New Name #2'",
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
      // The lock (.env.configure-lock) stays, empty, by design: deleting a lock
      // file after use reopens the race it closes. Only the secret-bearing
      // candidate must be gone.
      final left = root.listSync().map((e) => e.path.split('/').last);
      expect(File('${root.path}/.env.configure-lock').lengthSync(), 0);
      expect(
        left.where((n) => n != '.env.configure-lock'),
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
        canonicalName();
        final before = envFile.readAsStringSync();
        final r = await configure().run(
          args(['--name', 'Example', '--enable-passkeys']),
        );
        expect(r, isA<Unchanged>());
        expect(envFile.readAsStringSync(), before);
        expect(box.restarts, 0);
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
      expect(box.restarts > 0, restarted);
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
      expect(box.restarts, 0);
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
      'compose is asked through `sudo -n docker` (compose.sh elevates itself)',
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

  group('PR#407 r1', () {
    test('a restart that would change the release refuses (no un-backed-up upgrade)', () async {
      box.runningImage = 'ghcr.io/nickmeinhold/aiko-chat-island:0.18.0';
      await refusesWith(
        allOf(contains('deploy/update.sh'), contains('0.18.0')),
      );
    });

    test(
      'a gateway that is not running refuses (configure changes a live island)',
      () async {
        box.runningImage = null;
        await refusesWith(contains('not running'));
      },
    );

    test('no image.env refuses: the restart could not be pinned', () async {
      File('${root.path}/image.env').deleteSync();
      await refusesWith(contains('image.env'));
    });

    test(
      'a retry after a failed restart restarts and verifies, never Unchanged',
      () async {
        box.upFails = true;
        await expectLater(
          configure().run(args(['--name', 'X'])),
          throwsA(isA<Refusal>()),
        );
        // .env now says X; the gateway still serves Example.
        box.upFails = false;
        final r = await configure().run(args(['--name', 'X']));
        expect(r, isA<Applied>());
        expect(box.restarts, 2);
      },
    );

    test(
      'Unchanged only when the gateway serves it too, and then no restart',
      () async {
        canonicalName();
        final r = await configure().run(args(['--name', 'Example']));
        expect(r, isA<Unchanged>());
        expect(box.restarts, 0);
      },
    );

    test('an unquoted legacy line is rewritten once, then Unchanged', () async {
      expect(
        await configure().run(args(['--name', 'Example'])),
        isA<Applied>(),
      );
      expect(
        envFile.readAsStringSync(),
        contains("ISLAND_DISPLAY_NAME='Example'"),
      );
      expect(
        await configure().run(args(['--name', 'Example'])),
        isA<Unchanged>(),
      );
      expect(box.restarts, 1);
    });

    test('an unquoted value with a \$ is not "already set": compose would expand it', () async {
      envFile.writeAsStringSync(
        liveShaped().replaceFirst(
          'ISLAND_DISPLAY_NAME=Example',
          r'ISLAND_DISPLAY_NAME=Cost$X',
        ),
      );
      final r = await configure().run(args(['--name', r'Cost$X']));
      expect(r, isA<Applied>());
      expect(
        envFile.readAsStringSync(),
        contains(r"ISLAND_DISPLAY_NAME='Cost$X'"),
      );
    });

    test(
      'a key set twice whose last copy matches still refuses, not Unchanged',
      () async {
        canonicalName();
        envFile.writeAsStringSync(
          "${envFile.readAsStringSync()}ISLAND_DISPLAY_NAME='Example'\n",
        );
        await refusesWith(contains('set 2 times'), name: 'Example');
      },
    );

    test(
      'an old value this version refuses is given back as its exact line',
      () async {
        envFile.writeAsStringSync(
          liveShaped().replaceFirst(
            'ISLAND_DISPLAY_NAME=Example',
            'ISLAND_DISPLAY_NAME="Bob\'s Island"',
          ),
        );
        box.upFails = true;
        Refusal? got;
        try {
          await configure().run(args(['--name', 'X']));
        } on Refusal catch (e) {
          got = e;
        }
        expect(
          got!.message,
          contains('    ISLAND_DISPLAY_NAME="Bob\'s Island"'),
        );
        // And no recovery command this tool would refuse.
        expect(got.message, isNot(contains('island configure --name')));
      },
    );

    test('the revert gives back the exact old bytes, even a value compose interpolated', () async {
      // Unquoted, so compose expanded $X: no flag value could reproduce that
      // (Carnot, PR#407 r3). The old line can.
      envFile.writeAsStringSync(
        liveShaped().replaceFirst(
          'ISLAND_DISPLAY_NAME=Example',
          r'ISLAND_DISPLAY_NAME=Cost$X',
        ),
      );
      box.upFails = true;
      Refusal? got;
      try {
        await configure().run(args(['--name', 'X']));
      } on Refusal catch (e) {
        got = e;
      }
      expect(got!.message, contains(r'    ISLAND_DISPLAY_NAME=Cost$X'));
      expect(got.message, isNot(contains('island configure --')));
    });

    test('a name of 64 emoji is 64 characters, as the gateway counts', () {
      final name = String.fromCharCodes(List.filled(64, 0x1F30A));
      expect(DisplayName(name).value.runes.length, 64);
      expect(
        () => DisplayName(String.fromCharCodes(List.filled(65, 0x1F30A))),
        throwsA(isA<Refusal>()),
      );
    });

    test(
      'a refused run from the wrong directory leaves no lock file behind',
      () async {
        envFile.deleteSync();
        await expectLater(
          configure().run(args(['--name', 'X'])),
          throwsA(isA<Refusal>()),
        );
        expect(File('${root.path}/.env.configure-lock').existsSync(), isFalse);
      },
    );

    test(
      '`--name -h` is a name, not a request for help that exits 0',
      () async {
        // The real executable, in a directory with no island: it must refuse
        // (exit 1, "no .env"), not print usage and exit 0. No .env, so it stops at
        // the first check and never reaches this machine's Docker.
        envFile.deleteSync();
        final r = await Process.run('dart', [
          'run',
          '${Directory.current.path}/bin/island.dart',
          'configure',
          '--name',
          '-h',
        ], workingDirectory: root.path);
        expect(r.exitCode, 1, reason: '${r.stdout}${r.stderr}');
        expect('${r.stderr}', contains('no .env'));
        expect('${r.stdout}', isNot(contains('usage:')));
      },
    );

    test('a second configure while one runs refuses (the lock is cross-process)', () async {
      final lockPath = '${root.path}/.env.configure-lock';
      File(lockPath).createSync();
      // fcntl locks never conflict inside one process, so hold it from another.
      final holder = await Process.start('python3', [
        '-c',
        'import fcntl,sys,time; f=open(sys.argv[1],"a"); '
            'fcntl.lockf(f, fcntl.LOCK_EX); print("held", flush=True); time.sleep(30)',
        lockPath,
      ]);
      await holder.stdout
          .transform(utf8.decoder)
          .firstWhere((l) => l.contains('held'));
      try {
        await refusesWith(contains('another `island configure` is running'));
      } finally {
        holder.kill();
      }
    });

    test('without GATEWAY_BASE_URL, a peer naming the running gateway is still refused', () async {
      envFile.writeAsStringSync(
        liveShaped()
            .replaceFirst('GATEWAY_BASE_URL=https://chat.example.org\n', '')
            .replaceFirst('PASSKEY_RP_ID=chat.example.org\n', ''),
      );
      final before = envFile.readAsStringSync();
      await expectLater(
        configure().run(
          args([
            '--seed-peers',
            '[{"id":"me","display_name":"Me","base_url":"https://chat.example.org"}]',
          ]),
        ),
        throwsA(
          isA<Refusal>().having((r) => r.message, 'm', contains('itself')),
        ),
      );
      expect(envFile.readAsStringSync(), before);
      expect(box.restarts, 0);
    });

    test(
      'compose reporting no keys at all refuses (no comparison over nothing)',
      () async {
        box.onConfig = (n, r) => const RunResult(
          0,
          '{"services":{"probe":{"image":"scratch"}}}',
          '',
        );
        await refusesWith(contains('no keys'));
      },
    );

    test(
      'a failed restart on the reconcile path does not claim it wrote .env',
      () async {
        canonicalName();
        gateway.forwardsName = false; // served != file, so configure reconciles
        box.upFails = true;
        await expectLater(
          configure().run(args(['--name', 'Example'])),
          throwsA(
            isA<Refusal>().having(
              (r) => r.message,
              'm',
              allOf(
                contains('This run wrote nothing'),
                isNot(contains('IS written')),
              ),
            ),
          ),
        );
      },
    );

    test(
      'a key that was not set before is named as a line to delete, not dropped',
      () async {
        envFile.writeAsStringSync(
          liveShaped().replaceFirst(
            RegExp(r'ISLAND_DISPLAY_NAME=Example\n'),
            '',
          ),
        );
        box.upFails = true;
        await expectLater(
          configure().run(args(['--name', 'X'])),
          throwsA(
            isA<Refusal>().having(
              (r) => r.message,
              'message',
              contains('delete the ISLAND_DISPLAY_NAME line'),
            ),
          ),
        );
      },
    );

    test(
      'passkeys refuse when PASSKEY_RP_ID does not match GATEWAY_BASE_URL',
      () async {
        envFile.writeAsStringSync(
          liveShaped().replaceFirst(
            'PASSKEY_RP_ID=chat.example.org',
            'PASSKEY_RP_ID=chat.imagineering.cc',
          ),
        );
        final before = envFile.readAsStringSync();
        await expectLater(
          configure().run(args(['--enable-passkeys'])),
          throwsA(isA<Refusal>()),
        );
        expect(envFile.readAsStringSync(), before);
      },
    );

    test(
      'passkeys with neither key set (compose defaults agree) are allowed',
      () async {
        envFile.writeAsStringSync(
          liveShaped()
              .replaceFirst('GATEWAY_BASE_URL=https://chat.example.org\n', '')
              .replaceFirst('PASSKEY_RP_ID=chat.example.org\n', '')
              .replaceFirst('PASSKEY_ENABLED=true', 'PASSKEY_ENABLED=false'),
        );
        expect(
          await configure().run(args(['--enable-passkeys'])),
          isA<Applied>(),
        );
      },
    );
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
      // The old peer line, byte for byte, to put back.
      expect(
        out.join('\n'),
        contains('    ISLAND_SEED_PEERS=[{"id": "enspyr"'),
      );
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

    test('a mismatch does not tell the operator to update their copy (they will revert)', () async {
      gateway.forwardsName = false;
      await configure().run(args(['--name', 'X']));
      expect(out.join('\n'), isNot(contains('older than the box')));
    });

    test('success does remind them their copy is now older', () async {
      await configure().run(args(['--name', 'X']));
      expect(out.join('\n'), contains('older than the box'));
    });

    test(
      'exit 3 (written, not answering) still reminds about the copy',
      () async {
        gateway.healthy = false;
        await configure().run(args(['--name', 'X']));
        expect(out.join('\n'), contains('older than the box'));
      },
    );

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
              allOf(
                contains('IS written'),
                contains('    ISLAND_DISPLAY_NAME=Example'),
              ),
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
