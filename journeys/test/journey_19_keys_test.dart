// Journey 19 (DESIGN.md §2.2): signing keys, `/v1/keys`. A key is bound to the
// account that publishes it, never to one the body names; the roster a caller
// reads is their own; retiring is "forget MY key", idempotent and leak-free;
// the explicit route caps a roster at 32 new keys. The Python authority is
// `rest/keys.py` over `domain/signing_keys_service.py`.
//
// Contract gap, the same kind as journey 22's: the route sends 400 (a malformed
// key) and 429 (the cap) as `{detail: string}`, and the frozen contract
// declares neither status. The wire is the reference, so those checks expect
// `Undeclared` until the Python route declares them.
//
// NOT covered: the implicit door (a signed WS message records its sender's key
// through the same service, uncapped). It needs a signed send, which the
// harness can't make yet.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

/// `rest/keys.py` `_MAX_KEYS_PER_USER`.
const _cap = 32;

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_19.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0;

  setUpAll(() async {
    gateway = await PythonGateway.start(repoRoot);
    mint = Minter(gateway.jwtSecret);
  });
  tearDownAll(() async {
    client.close();
    await gateway.stop();
    final unchecked = golden.finish();
    if (unchecked != null) fail(unchecked);
  });

  Account account() => Account.seed(gateway, 'journey19-${accounts++}');

  Map<String, String> auth(Account a) => {
    'authorization': 'Bearer ${mint.access(a.id)}',
  };

  Future<http.Response> publish(Account a, Map<String, Object?> body) =>
      client.post(
        gateway.base.resolve('/v1/keys'),
        headers: {...auth(a), 'content-type': 'application/json'},
        body: jsonEncode(body),
      );

  Future<http.Response> retire(Account a, String pubkey) =>
      client.delete(gateway.base.resolve('/v1/keys/$pubkey'), headers: auth(a));

  /// Publishes [pubkey] for [a], which must be accepted.
  Future<void> published(Account a, String pubkey, {int? version}) async {
    final res = await publish(a, {'pubkey': pubkey, 'key_version': ?version});
    expect(res.statusCode, 201, reason: res.body);
  }

  /// [a]'s roster, in the order the gateway lists it.
  Future<List<Map<String, Object?>>> roster(Account a) async {
    final res = await client.get(
      gateway.base.resolve('/v1/keys'),
      headers: auth(a),
    );
    expect(res.statusCode, 200, reason: res.body);
    return (jsonDecode(res.body) as List).cast<Map<String, Object?>>();
  }

  Future<List<Object?>> pubkeys(Account a) async => [
    for (final k in await roster(a)) k['pubkey'],
  ];

  DateTime seen(Map<String, Object?> key, String which) =>
      DateTime.parse(key['${which}_seen_at']! as String);

  group('publish and list', () {
    test('serves the contract and its recorded shapes', () async {
      final a = account();
      final key = newMultikey();
      final res = await publish(a, {'pubkey': key});
      expect(res.statusCode, 201, reason: res.body);
      final posted = contract.check(
        'POST /v1/keys',
        status: 201,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(posted, isA<Conforms>(), reason: '$posted');
      expect(golden.expect('POST /v1/keys', res.body), isNull);
      expect(jsonDecode(res.body), {'pubkey': key, 'key_version': 1});

      final list = await client.get(
        gateway.base.resolve('/v1/keys'),
        headers: auth(a),
      );
      final listed = contract.check(
        'GET /v1/keys',
        status: 200,
        contentType: list.headers['content-type'],
        body: list.body,
      );
      expect(listed, isA<Conforms>(), reason: '$listed');
      expect(golden.expect('GET /v1/keys', list.body), isNull);
    });

    test(
      'a new key is listed once, born with first seen = last seen',
      () async {
        final a = account();
        final key = newMultikey();
        await published(a, key);
        final [only] = await roster(a);
        expect(only['pubkey'], key);
        expect(only['key_version'], 1);
        expect(only['first_seen_at'], only['last_seen_at']);
      },
    );

    test('a stated key_version is stored', () async {
      final a = account();
      await published(a, newMultikey(), version: 3);
      expect((await roster(a)).single['key_version'], 3);
    });

    test('keys are listed in the order they were first seen', () async {
      final a = account();
      final keys = [newMultikey(), newMultikey(), newMultikey()];
      for (final k in keys) {
        await published(a, k);
      }
      expect(await pubkeys(a), keys);
    });

    test('publishing again is idempotent: one row, its first version kept, '
        'last seen moved', () async {
      final a = account();
      final key = newMultikey();
      await published(a, key);
      final before = (await roster(a)).single;
      final res = await publish(a, {'pubkey': key, 'key_version': 2});
      expect(res.statusCode, 201, reason: res.body);
      expect(jsonDecode(res.body), {'pubkey': key, 'key_version': 1});
      final after = (await roster(a)).single;
      expect(after['key_version'], 1);
      expect(after['first_seen_at'], before['first_seen_at']);
      expect(seen(after, 'last').isAfter(seen(before, 'last')), isTrue);
    });
  });

  group('whose key it is', () {
    test("a roster is the caller's own, never another account's", () async {
      final a = account();
      final b = account();
      await published(a, newMultikey());
      expect(await roster(b), isEmpty);
    });

    test(
      'the key binds to the caller, whatever account the body names',
      () async {
        final a = account();
        final b = account();
        final key = newMultikey();
        await published(a, key);
        final res = await publish(a, {
          'pubkey': newMultikey(),
          'user_id': b.id,
        });
        expect(res.statusCode, 201, reason: res.body);
        expect(await roster(b), isEmpty);
        expect(await pubkeys(a), hasLength(2));
        expect((await pubkeys(a)).first, key);
      },
    );

    test(
      'two accounts may hold the same key, each in their own roster',
      () async {
        final a = account();
        final b = account();
        final key = newMultikey();
        await published(a, key);
        await published(b, key);
        expect(await pubkeys(a), [key]);
        expect(await pubkeys(b), [key]);
      },
    );
  });

  group('retire', () {
    test('retiring one of my keys removes it from my roster', () async {
      final a = account();
      final keep = newMultikey();
      final drop = newMultikey();
      await published(a, keep);
      await published(a, drop);
      final res = await retire(a, drop);
      expect(res.statusCode, 204, reason: res.body);
      expect(res.body, isEmpty);
      expect(await pubkeys(a), [keep]);
    });

    test('retiring a key I never had is 204, not a 404', () async {
      final res = await retire(account(), newMultikey());
      expect(res.statusCode, 204, reason: res.body);
    });

    test(
      "retiring another account's key is 204 and leaves it in place",
      () async {
        final a = account();
        final b = account();
        final key = newMultikey();
        await published(a, key);
        expect((await retire(b, key)).statusCode, 204);
        expect(await pubkeys(a), [key]);
      },
    );

    test('a malformed path matches nothing and is 204', () async {
      final a = account();
      await published(a, newMultikey());
      expect((await retire(a, 'not-a-key')).statusCode, 204);
      expect(await roster(a), hasLength(1));
    });

    test('publishing a retired key again is a new row', () async {
      final a = account();
      final key = newMultikey();
      await published(a, key);
      final first = (await roster(a)).single;
      await retire(a, key);
      await published(a, key);
      final again = (await roster(a)).single;
      expect(seen(again, 'first').isAfter(seen(first, 'first')), isTrue);
    });
  });

  group('refusals', () {
    /// 32 random bytes, for spelling keys the gateway must refuse.
    final raw = List.generate(32, (i) => i + 1);

    final malformed = {
      'no multibase prefix': multikey(raw).substring(1),
      'a character outside base58': 'z0${multikey(raw).substring(2)}',
      'a codec other than ed25519': multikey(raw, prefix: [0xe7, 0x01]),
      '31 raw bytes': multikey(raw.sublist(1)),
      '33 raw bytes': multikey([...raw, 0]),
    };
    for (final MapEntry(key: why, value: pubkey) in malformed.entries) {
      test('$why: 400, and nothing is written', () async {
        final a = account();
        final res = await publish(a, {'pubkey': pubkey});
        expect(res.statusCode, 400, reason: res.body);
        expect(await roster(a), isEmpty);
      });
    }

    test('the 400 is undeclared, and its recorded shape', () async {
      final res = await publish(account(), {'pubkey': 'zz'});
      expect(res.statusCode, 400, reason: res.body);
      final check = contract.check(
        'POST /v1/keys',
        status: 400,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      // The contract gap in the header: flips to Conforms, and fails, the day
      // the route declares its 400.
      expect(check, isA<Undeclared>(), reason: '$check');
      expect(golden.expect('POST /v1/keys 400', res.body), isNull);
    });

    final invalid = <String, Map<String, Object?>>{
      'no pubkey': {},
      'an empty pubkey': {'pubkey': ''},
      'a pubkey over 128 characters': {'pubkey': 'z${'1' * 128}'},
      'key_version 0': {'pubkey': newMultikey(), 'key_version': 0},
    };
    for (final MapEntry(key: why, value: body) in invalid.entries) {
      test('$why: the declared 422', () async {
        final a = account();
        final res = await publish(a, body);
        expect(res.statusCode, 422, reason: res.body);
        final check = contract.check(
          'POST /v1/keys',
          status: 422,
          contentType: res.headers['content-type'],
          body: res.body,
        );
        expect(check, isA<Conforms>(), reason: '$check');
        expect(await roster(a), isEmpty);
      });
    }

    test('every route refuses a caller with no token', () async {
      final key = newMultikey();
      final list = await client.get(gateway.base.resolve('/v1/keys'));
      final post = await client.post(
        gateway.base.resolve('/v1/keys'),
        headers: {'content-type': 'application/json'},
        body: jsonEncode({'pubkey': key}),
      );
      final delete = await client.delete(gateway.base.resolve('/v1/keys/$key'));
      expect(
        [list.statusCode, post.statusCode, delete.statusCode],
        [401, 401, 401],
      );
    });
  });

  group('the cap', () {
    test('a $_cap-key roster refuses a new key with 429, and nothing is '
        'written', () async {
      final a = account();
      final keys = [for (var i = 0; i < _cap; i++) newMultikey()];
      for (final k in keys) {
        await published(a, k);
      }
      final res = await publish(a, {'pubkey': newMultikey()});
      expect(res.statusCode, 429, reason: res.body);
      final check = contract.check(
        'POST /v1/keys',
        status: 429,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      // The contract gap in the header.
      expect(check, isA<Undeclared>(), reason: '$check');
      expect(golden.expect('POST /v1/keys 429', res.body), isNull);
      expect(await pubkeys(a), keys);

      // At the cap, publishing a key already held is still accepted.
      await published(a, keys.last);
      expect(await pubkeys(a), keys);

      // Retiring one frees its slot.
      await retire(a, keys.first);
      final next = newMultikey();
      await published(a, next);
      expect(await pubkeys(a), [...keys.skip(1), next]);
    });
  });
}
