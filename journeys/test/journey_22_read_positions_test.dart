// Journey 22 (DESIGN.md §2.2): the caller's read positions,
// `/v1/read-positions`. Compare-and-swap per channel (#429's v2 contract), the
// visible-item bound, and skip-not-refuse for a channel the caller can't see.
// The Python authority is `domain/read_positions_service.py`, with the entry
// parsing in `rest/read_positions.py` `_entries`.
//
// The channels are DMs, opened and filled through public doors (`messages.dart`):
// no route makes a public channel without the bus.
//
// NOT covered, named so nobody reads this file as the whole row: the bound's
// "your own stored value" arm, which lets a watermark naming a since-HIDDEN
// message (soft-deleted, or its author blocked) be re-sent. Reaching it needs a
// delete or a block, which are journey 18's doors; until then the stored value
// is never above the newest visible item, so a mutant dropping the arm survives.
//
// A CONTRACT GAP this journey pins rather than hides: openapi.json declares
// FastAPI's validation shape for the PUT's 422 (`detail` an array), but the
// refusal the app heals from is `{error, channel_id, detail: string}`. The wire
// is the reference, so the Dart port must send the structured body, and the
// check below expects `Violates` until the Python route declares its 422.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

/// Above every server ULID minted this millennium, and canonical (first
/// character 0-7), so it fails the bound, not the shape.
const _future = '7ZZZZZZZZZZZZZZZZZZZZZZZZZ';

/// Both replies key `positions` by channel id: data, not fields.
const _byChannel = {'positions'};

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_22.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0;

  // /dm and PUT are rate-limited per client IP: one client per test.
  late String forwardedFor;
  setUp(() => forwardedFor = gateway.newClient());

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

  Account account() => Account.seed(gateway, 'journey22-${accounts++}');

  /// A channel [a] can read, holding one message per body; returns its id and
  /// the messages' ids, oldest first.
  Future<(String, List<String>)> channel(
    Account a, [
    List<String> bodies = const ['one', 'two'],
  ]) async {
    final token = mint.access(a.id);
    final cid = await openDm(
      client,
      gateway,
      token: token,
      target: a.id,
      forwardedFor: forwardedFor,
    );
    final ids = bodies.isEmpty
        ? <String>[]
        : await send(gateway, token: token, channelId: cid, bodies: bodies);
    return (cid, ids);
  }

  Future<http.Response> put(Account a, Map<String, Object?> positions) =>
      client.put(
        gateway.base.resolve('/v1/read-positions'),
        headers: {
          'authorization': 'Bearer ${mint.access(a.id)}',
          'content-type': 'application/json',
          'x-forwarded-for': forwardedFor,
        },
        body: jsonEncode({'positions': positions}),
      );

  /// PUTs [positions] expecting a 200, and returns the per-channel replies.
  Future<Map<String, Object?>> write(
    Account a,
    Map<String, Object?> positions,
  ) async {
    final res = await put(a, positions);
    expect(res.statusCode, 200, reason: res.body);
    return (jsonDecode(res.body) as Map<String, Object?>)['positions']!
        as Map<String, Object?>;
  }

  Future<Map<String, Object?>> stored(Account a) async {
    final res = await client.get(
      gateway.base.resolve('/v1/read-positions'),
      headers: {'authorization': 'Bearer ${mint.access(a.id)}'},
    );
    expect(res.statusCode, 200, reason: res.body);
    return (jsonDecode(res.body) as Map<String, Object?>)['positions']!
        as Map<String, Object?>;
  }

  Map<String, Object?> at(String lastRead, int baseSeq) => {
    'last_read': lastRead,
    'base_seq': baseSeq,
  };

  /// PUTs [positions] expecting the structured 422 naming [channelId], then
  /// checks the refusal stored nothing.
  Future<void> refused(
    Account a,
    Map<String, Object?> positions,
    String channelId,
  ) async {
    final before = await stored(a);
    final res = await put(a, positions);
    expect(res.statusCode, 422, reason: res.body);
    final body = jsonDecode(res.body) as Map<String, Object?>;
    expect(body['error'], 'invalid_read_position');
    expect(body['channel_id'], channelId);
    expect(await stored(a), before);
  }

  group('compare-and-swap', () {
    test('a first write at base 0 is applied at version 1', () async {
      final a = account();
      final (cid, [m1, _]) = await channel(a);
      final res = await put(a, {cid: at(m1, 0)});
      expect(res.statusCode, 200, reason: res.body);
      final check = contract.check(
        'PUT /v1/read-positions',
        status: 200,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(check, isA<Conforms>(), reason: '$check');
      expect(
        golden.expect(
          'PUT /v1/read-positions',
          res.body,
          dictionaries: _byChannel,
        ),
        isNull,
      );
      expect((jsonDecode(res.body) as Map<String, Object?>)['positions'], {
        cid: {'status': 'applied', 'last_read': m1, 'seq': 1},
      });

      final get = await client.get(
        gateway.base.resolve('/v1/read-positions'),
        headers: {'authorization': 'Bearer ${mint.access(a.id)}'},
      );
      final getCheck = contract.check(
        'GET /v1/read-positions',
        status: 200,
        contentType: get.headers['content-type'],
        body: get.body,
      );
      expect(getCheck, isA<Conforms>(), reason: '$getCheck');
      expect(
        golden.expect(
          'GET /v1/read-positions',
          get.body,
          dictionaries: _byChannel,
        ),
        isNull,
      );
      expect(await stored(a), {
        cid: {'last_read': m1, 'seq': 1},
      });
    });

    test('the next write names the version it edits', () async {
      final a = account();
      final (cid, [m1, m2]) = await channel(a);
      await write(a, {cid: at(m1, 0)});
      expect(await write(a, {cid: at(m2, 1)}), {
        cid: {'status': 'applied', 'last_read': m2, 'seq': 2},
      });
    });

    test(
      'a stale base is a conflict carrying the stored value and version',
      () async {
        final a = account();
        final (cid, [m1, m2]) = await channel(a);
        await write(a, {cid: at(m1, 0)});
        await write(a, {cid: at(m2, 1)}); // another device moved it on
        // This device still thinks it holds version 1.
        expect(await write(a, {cid: at(m1, 1)}), {
          cid: {'status': 'conflict', 'last_read': m2, 'seq': 2},
        });
        expect(await stored(a), {
          cid: {'last_read': m2, 'seq': 2},
        });
      },
    );

    test('a base above a missing row is a conflict at version 0', () async {
      final a = account();
      final (cid, [m1, _]) = await channel(a);
      expect(await write(a, {cid: at(m1, 5)}), {
        cid: {'status': 'conflict', 'last_read': null, 'seq': 0},
      });
      expect(await stored(a), isEmpty);
    });

    test('a second first write is a conflict, not an overwrite', () async {
      final a = account();
      final (cid, [m1, m2]) = await channel(a);
      await write(a, {cid: at(m2, 0)});
      expect(await write(a, {cid: at(m1, 0)}), {
        cid: {'status': 'conflict', 'last_read': m2, 'seq': 1},
      });
    });

    test('an earlier ULID (mark-as-unread) applies like any edit', () async {
      final a = account();
      final (cid, [m1, m2]) = await channel(a);
      await write(a, {cid: at(m2, 0)});
      expect(await write(a, {cid: at(m1, 1)}), {
        cid: {'status': 'applied', 'last_read': m1, 'seq': 2},
      });
    });
  });

  group('the bound: the newest item you can see, or your own value', () {
    test('past both → 422 naming the channel, nothing stored', () async {
      final a = account();
      final (cid, [m1, _]) = await channel(a);
      await write(a, {cid: at(m1, 0)});
      final res = await put(a, {cid: at(_future, 1)});
      expect(res.statusCode, 422, reason: res.body);
      final check = contract.check(
        'PUT /v1/read-positions',
        status: 422,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      // The contract gap in the header: the wire wins, and the day the route
      // declares its 422 this flips to Conforms and fails, on purpose.
      expect(check, isA<Violates>(), reason: '$check');
      expect(golden.expect('PUT /v1/read-positions 422', res.body), isNull);
      await refused(a, {cid: at(_future, 1)}, cid);
    });

    test('the newest message itself is in bounds', () async {
      final a = account();
      final (cid, [_, m2]) = await channel(a);
      expect(
        (await write(a, {cid: at(m2, 0)}))[cid],
        containsPair('status', 'applied'),
      );
    });

    test('an empty channel bounds every ULID, but not the floor', () async {
      final a = account();
      final (cid, _) = await channel(a, const []);
      await refused(a, {cid: at(_future, 0)}, cid);
      expect(
        (await write(a, {cid: at('', 0)}))[cid],
        containsPair('status', 'applied'),
      );
    });

    test('one entry out of bounds refuses the whole batch', () async {
      final a = account();
      final (good, [g1, _]) = await channel(a);
      // A second channel `a` can read: a DM with another account, with
      // messages in it, so the bound is a real one and not the empty floor.
      final shared = await openDm(
        client,
        gateway,
        token: mint.access(a.id),
        target: account().id,
        forwardedFor: forwardedFor,
      );
      await send(
        gateway,
        token: mint.access(a.id),
        channelId: shared,
        bodies: ['hi'],
      );
      await refused(a, {good: at(g1, 0), shared: at(_future, 0)}, shared);
    });
  });

  group('a channel you cannot see is skipped, the rest stored', () {
    test('an unknown channel', () async {
      final a = account();
      final (cid, [m1, _]) = await channel(a);
      final unknown = Account.unseededId();
      expect(await write(a, {cid: at(m1, 0), unknown: at(m1, 0)}), {
        cid: {'status': 'applied', 'last_read': m1, 'seq': 1},
        unknown: {'status': 'skipped'},
      });
      expect((await stored(a)).keys, [cid]);
    });

    test(
      'a channel that exists but is not yours: skipped, and never bounded',
      () async {
        final a = account();
        final (cid, [m1, _]) = await channel(a);
        final (theirs, _) = await channel(account()); // someone else's DM
        // Past the end of a channel `a` can't read: skipped, not a 422 — a
        // refusal would answer a question about rows `a` can't see.
        expect(await write(a, {cid: at(m1, 0), theirs: at(_future, 0)}), {
          cid: {'status': 'applied', 'last_read': m1, 'seq': 1},
          theirs: {'status': 'skipped'},
        });
        expect((await stored(a)).keys, [cid]);
      },
    );
  });

  group('a malformed entry refuses the whole batch, naming its channel', () {
    for (final (name, entry) in [
      ('a null last_read', {'last_read': null, 'base_seq': 0}),
      ('a string base_seq', {'last_read': '', 'base_seq': '0'}),
      ('a boolean base_seq', {'last_read': '', 'base_seq': true}),
      ('a negative base_seq', {'last_read': '', 'base_seq': -1}),
      (
        'a lowercase ULID',
        {'last_read': '01aaaaaaaaaaaaaaaaaaaaaaaa', 'base_seq': 0},
      ),
      (
        'a ULID past 128 bits',
        {'last_read': '8ZZZZZZZZZZZZZZZZZZZZZZZZZ', 'base_seq': 0},
      ),
      ('an entry that is not an object', 'oops'),
    ]) {
      test(name, () async {
        final a = account();
        final (good, [g1, _]) = await channel(a);
        const bad = 'channel-with-the-bad-entry';
        await refused(a, {good: at(g1, 0), bad: entry}, bad);
      });
    }
  });
}
