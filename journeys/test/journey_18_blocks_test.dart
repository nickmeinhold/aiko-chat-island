// Journey 18 (DESIGN.md §2.2): blocks. A block is stored the way it was pressed
// (who blocked whom) and takes effect both ways: each side's messages and
// reactions leave the other's reads and live frames, a DM video token between
// them is refused, and unblocking restores all of it. The Python authority is
// `domain/moderation_service.py` (the doors, `not_blocked_predicate`,
// `blocked_pair_user_ids`, `is_blocked_between`), applied through
// `messages_service._visible`, `rest/reactions.py` and `rest/livekit.py`.
//
// Everything here happens in a DM, the one channel the harness can make without
// the bus (`messages.dart`). Inside a DM a NEW send across a block is refused
// (`create_outbound`, as the same `no_channel` a missing channel gets), so the
// messages a block hides are the ones sent before it.
//
// This journey also covers read positions' "your own stored value" bound arm,
// which journey 22 couldn't reach: a block is what hides a message a watermark
// already names.
//
// Contract gap, the same kind as journeys 19 and 22: the block doors' 400 and
// 404, and the video token's 404, are sent as `{detail: string}` and the frozen
// contract declares none of them. Those checks expect `Undeclared`.
//
// NOT covered: the live MESSAGE frame's block exclusion (`live_exclusions`).
// In a DM the send is refused before any fanout, so reaching it needs a public
// channel, which is born on the bus. The live REACTION frame's exclusion is
// covered.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_18.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0;

  // /dm, reactions and the video token are rate-limited: one client per test.
  late String forwardedFor;
  setUp(() => forwardedFor = gateway.newClient());

  setUpAll(() async {
    // LiveKit "configured" is a settings read and a token is minted locally, so
    // the video token's block gate is reachable without an SFU. The values only
    // have to pass production's boot guards (`config.py`).
    gateway = await PythonGateway.start(
      repoRoot,
      settings: {
        'LIVEKIT_URL': 'wss://livekit.journey.invalid',
        'LIVEKIT_API_KEY': 'journey18',
        'LIVEKIT_API_SECRET': 'journey18-not-a-secret-but-long-enough',
        'ISLAND_ID': 'journey18',
      },
    );
    mint = Minter(gateway.jwtSecret);
  });
  tearDownAll(() async {
    client.close();
    await gateway.stop();
    final unchecked = golden.finish();
    if (unchecked != null) fail(unchecked);
  });

  Account account() => Account.seed(gateway, 'journey18-${accounts++}');

  Map<String, String> auth(Account a) => {
    'authorization': 'Bearer ${mint.access(a.id)}',
    'x-forwarded-for': forwardedFor,
  };

  Future<http.Response> block(Account by, Account who) => client.post(
    gateway.base.resolve('/v1/users/${who.id}/block'),
    headers: auth(by),
  );

  Future<http.Response> unblock(Account by, Account who) => client.delete(
    gateway.base.resolve('/v1/users/${who.id}/block'),
    headers: auth(by),
  );

  Future<void> blocked(Account by, Account who) async {
    final res = await block(by, who);
    expect(res.statusCode, 204, reason: res.body);
  }

  Future<void> unblocked(Account by, Account who) async {
    final res = await unblock(by, who);
    expect(res.statusCode, 204, reason: res.body);
  }

  Future<List<Map<String, Object?>>> blocks(Account a) async {
    final res = await client.get(
      gateway.base.resolve('/v1/blocks'),
      headers: auth(a),
    );
    expect(res.statusCode, 200, reason: res.body);
    return ((jsonDecode(res.body) as Map<String, Object?>)['blocks']! as List)
        .cast<Map<String, Object?>>();
  }

  Future<http.Response> react(Account a, String messageId, String emoji) =>
      client.post(
        gateway.base.resolve('/v1/messages/$messageId/reactions'),
        headers: {...auth(a), 'content-type': 'application/json'},
        body: jsonEncode({'emoji': emoji}),
      );

  Future<void> reacted(Account a, String messageId, String emoji) async {
    final res = await react(a, messageId, emoji);
    expect(res.statusCode, 200, reason: res.body);
  }

  /// The messages [a] reads in [channelId], by id.
  Future<Map<String, Map<String, Object?>>> history(
    Account a,
    String channelId,
  ) async {
    final res = await client.get(
      gateway.base.resolve('/v1/channels/$channelId/messages'),
      headers: auth(a),
    );
    expect(res.statusCode, 200, reason: res.body);
    final items = (jsonDecode(res.body) as Map<String, Object?>)['messages']!;
    return {
      for (final m in (items as List).cast<Map<String, Object?>>())
        m['msg_id']! as String: m,
    };
  }

  Future<int> fetch(Account a, String messageId) async => (await client.get(
    gateway.base.resolve('/v1/messages/$messageId'),
    headers: auth(a),
  )).statusCode;

  Future<http.Response> videoToken(Account a, String channelId) => client.post(
    gateway.base.resolve('/v1/channels/$channelId/video-token'),
    headers: auth(a),
  );

  /// The id of the newest message [a]'s DM switcher previews for [channelId].
  Future<Object?> preview(Account a, String channelId) async {
    final res = await client.get(
      gateway.base.resolve('/v1/dm'),
      headers: auth(a),
    );
    expect(res.statusCode, 200, reason: res.body);
    final channels =
        ((jsonDecode(res.body) as Map<String, Object?>)['channels']! as List)
            .cast<Map<String, Object?>>();
    final dm = channels.singleWhere((c) => c['channel_id'] == channelId);
    return (dm['last_message'] as Map<String, Object?>?)?['msg_id'];
  }

  /// Who reacted to [messageId], as [a]'s history shows it.
  Future<Set<Object?>> reactors(
    Account a,
    String channelId,
    String messageId,
  ) async {
    final m = (await history(a, channelId))[messageId]!;
    return {
      for (final r in (m['reactions']! as List).cast<Map<String, Object?>>())
        for (final who in (r['reactors']! as List).cast<Map<String, Object?>>())
          who['user_id'],
    };
  }

  /// A DM between two fresh accounts: [a] sent `a1`, then [b] sent `b1`, and [b]
  /// reacted to `a1`. Nobody has blocked anybody yet.
  Future<(Account a, Account b, String cid, String a1, String b1)> dm() async {
    final a = account(), b = account();
    final cid = await openDm(
      client,
      gateway,
      token: mint.access(a.id),
      target: b.id,
      forwardedFor: forwardedFor,
    );
    final [a1] = await send(
      gateway,
      token: mint.access(a.id),
      channelId: cid,
      bodies: ['from a'],
    );
    final [b1] = await send(
      gateway,
      token: mint.access(b.id),
      channelId: cid,
      bodies: ['from b'],
    );
    await reacted(b, a1, '👍');
    return (a, b, cid, a1, b1);
  }

  group('the doors', () {
    test('serve the contract and their recorded shape', () async {
      final a = account(), b = account();
      final posted = await block(a, b);
      expect(posted.statusCode, 204, reason: posted.body);
      final p = contract.check(
        'POST /v1/users/{user_id}/block',
        status: 204,
        contentType: posted.headers['content-type'],
        body: posted.body,
      );
      expect(p, isA<Conforms>(), reason: '$p');

      final list = await client.get(
        gateway.base.resolve('/v1/blocks'),
        headers: auth(a),
      );
      final l = contract.check(
        'GET /v1/blocks',
        status: 200,
        contentType: list.headers['content-type'],
        body: list.body,
      );
      expect(l, isA<Conforms>(), reason: '$l');
      expect(golden.expect('GET /v1/blocks', list.body), isNull);

      final deleted = await unblock(a, b);
      expect(deleted.statusCode, 204, reason: deleted.body);
      final d = contract.check(
        'DELETE /v1/users/{user_id}/block',
        status: 204,
        contentType: deleted.headers['content-type'],
        body: deleted.body,
      );
      expect(d, isA<Conforms>(), reason: '$d');
    });

    test('a block is listed for the one who pressed it, with a name', () async {
      final a = account(), b = account();
      await blocked(a, b);
      final [only] = await blocks(a);
      expect(only['user_id'], b.id);
      expect(only['display_name'], b.handle);
      expect(await blocks(b), isEmpty);
    });

    test('blocks are listed newest first', () async {
      final a = account(), b = account(), c = account();
      await blocked(a, b);
      await blocked(a, c);
      expect([for (final x in await blocks(a)) x['user_id']], [c.id, b.id]);
    });

    test('a repeated block changes nothing', () async {
      final a = account(), b = account();
      await blocked(a, b);
      final before = await blocks(a);
      await blocked(a, b);
      expect(await blocks(a), before);
    });

    test('blocking yourself is refused', () async {
      final a = account();
      final res = await block(a, a);
      expect(res.statusCode, 400, reason: res.body);
      final check = contract.check(
        'POST /v1/users/{user_id}/block',
        status: 400,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(check, isA<Undeclared>(), reason: '$check');
      expect(await blocks(a), isEmpty);
    });

    test('blocking an account that does not exist is a 404', () async {
      final a = account();
      final res = await client.post(
        gateway.base.resolve('/v1/users/${Account.unseededId()}/block'),
        headers: auth(a),
      );
      expect(res.statusCode, 404, reason: res.body);
      final check = contract.check(
        'POST /v1/users/{user_id}/block',
        status: 404,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(check, isA<Undeclared>(), reason: '$check');
      expect(await blocks(a), isEmpty);
    });

    test('unblocking a pair never blocked is a no-op', () async {
      final a = account(), b = account();
      await unblocked(a, b);
      expect(await blocks(a), isEmpty);
    });

    test('only the one who pressed it can lift a block', () async {
      final (a, b, cid, a1, b1) = await dm();
      await blocked(a, b);
      await unblocked(b, a); // b's own (absent) block of a: nothing to lift
      expect([for (final x in await blocks(a)) x['user_id']], [b.id]);
      expect((await history(b, cid)).keys, [b1]);
      expect(await fetch(b, a1), 404);
    });
  });

  group('a block hides each side from the other', () {
    test('in history, both ways', () async {
      final (a, b, cid, a1, b1) = await dm();
      expect((await history(a, cid)).keys, [a1, b1]);
      expect((await history(b, cid)).keys, [a1, b1]);
      await blocked(a, b);
      expect((await history(a, cid)).keys, [a1]);
      expect((await history(b, cid)).keys, [b1]);
    });

    test('in the reactions on a message still seen', () async {
      final (a, b, cid, a1, _) = await dm();
      expect(await reactors(a, cid, a1), {b.id});
      await blocked(a, b);
      expect(await reactors(a, cid, a1), isEmpty);
    });

    test('in fetch by id: the other side\'s message is a 404', () async {
      final (a, b, _, a1, b1) = await dm();
      await blocked(a, b);
      expect(await fetch(a, b1), 404);
      expect(await fetch(b, a1), 404);
      expect(await fetch(a, a1), 200);
      expect(await fetch(b, b1), 200);
    });

    test('from reacting: the other side\'s message is a 404', () async {
      final (a, b, _, a1, b1) = await dm();
      await blocked(a, b);
      expect((await react(a, b1, '🎉')).statusCode, 404);
      expect((await react(b, a1, '🎉')).statusCode, 404);
    });

    test('in the DM preview', () async {
      final (a, b, cid, a1, b1) = await dm();
      expect(await preview(a, cid), b1);
      await blocked(a, b);
      expect(await preview(a, cid), a1);
      expect(await preview(b, cid), b1);
    });

    test('in a subscribe\'s fence', () async {
      final (a, b, cid, a1, b1) = await dm();
      await blocked(a, b);
      final la = await Live.connect(gateway, mint.access(a.id));
      final lb = await Live.connect(gateway, mint.access(b.id));
      try {
        expect(await la.subscribe([cid]), {cid: a1});
        expect(await lb.subscribe([cid]), {cid: b1});
      } finally {
        await la.close();
        await lb.close();
      }
    });

    test('from the live reaction frame, until an unblock lets the next one through', () async {
      final (a, b, cid, a1, _) = await dm();
      final lb = await Live.connect(gateway, mint.access(b.id));
      try {
        await lb.subscribe([cid]);
        await blocked(a, b);
        await reacted(a, a1, '❤️'); // b must not hear this one
        await unblocked(a, b);
        await reacted(a, a1, '🎉');
        // Frames arrive in order, so the first reaction frame b gets is the
        // one after the unblock, or the block leaked the one before it.
        final first = await lb.next((f) => f['type'] == 'reaction');
        expect(first['emoji'], '🎉');
        expect(first['user_id'], a.id);
      } finally {
        await lb.close();
      }
    });
  });

  group('a block refuses interaction in the DM', () {
    test('a new send, from either side, as "no channel"', () async {
      final (a, b, cid, _, _) = await dm();
      await blocked(a, b);
      for (final who in [a, b]) {
        final live = await Live.connect(gateway, mint.access(who.id));
        try {
          final reply = await live.send(cid, 'after-${who.id}', 'hello?');
          expect(reply['type'], 'error');
          expect(reply['code'], 'no_channel');
        } finally {
          await live.close();
        }
      }
      expect((await history(a, cid)).length, 1);
      expect((await history(b, cid)).length, 1);
    });

    test('but a retry of a send made before it is acked as before', () async {
      final (a, b, cid, _, _) = await dm();
      final live = await Live.connect(gateway, mint.access(a.id));
      try {
        final first = await live.send(cid, 'before-block', 'sent in time');
        expect(first['type'], 'ack');
        await blocked(a, b);
        final retry = await live.send(cid, 'before-block', 'sent in time');
        expect(retry['type'], 'ack', reason: '$retry');
        expect(retry['msg_id'], first['msg_id']);
      } finally {
        await live.close();
      }
    });

    test('a video token, either way, as a 404', () async {
      final (a, b, cid, _, _) = await dm();
      final open = await videoToken(a, cid);
      expect(open.statusCode, 200, reason: open.body);
      final check = contract.check(
        'POST /v1/channels/{channel_id}/video-token',
        status: 200,
        contentType: open.headers['content-type'],
        body: open.body,
      );
      expect(check, isA<Conforms>(), reason: '$check');

      await blocked(b, a);
      for (final who in [a, b]) {
        final res = await videoToken(who, cid);
        expect(res.statusCode, 404, reason: res.body);
        final refused = contract.check(
          'POST /v1/channels/{channel_id}/video-token',
          status: 404,
          contentType: res.headers['content-type'],
          body: res.body,
        );
        expect(refused, isA<Undeclared>(), reason: '$refused');
      }
    });
  });

  test('unblocking restores every read and door', () async {
    final (a, b, cid, a1, b1) = await dm();
    await blocked(a, b);
    await unblocked(a, b);
    expect(await blocks(a), isEmpty);
    expect((await history(a, cid)).keys, [a1, b1]);
    expect((await history(b, cid)).keys, [a1, b1]);
    expect(await reactors(a, cid, a1), {b.id});
    expect(await fetch(a, b1), 200);
    expect(await fetch(b, a1), 200);
    expect(await preview(a, cid), b1);
    await reacted(a, b1, '🎉');
    expect((await videoToken(b, cid)).statusCode, 200);
    final live = await Live.connect(gateway, mint.access(b.id));
    try {
      final reply = await live.send(cid, 'after-unblock', 'back again');
      expect(reply['type'], 'ack', reason: '$reply');
    } finally {
      await live.close();
    }
  });

  // Journey 22's missing arm (DESIGN.md row 22): the bound is the later of the
  // newest visible item and the caller's OWN stored value, so a watermark that
  // names a message a block has since hidden can still be re-sent.
  test(
    'a read position naming a since-hidden message can be re-sent',
    () async {
      final (a, b, cid, _, b1) = await dm();
      Future<http.Response> put(String lastRead, int baseSeq) => client.put(
        gateway.base.resolve('/v1/read-positions'),
        headers: {...auth(a), 'content-type': 'application/json'},
        body: jsonEncode({
          'positions': {
            cid: {'last_read': lastRead, 'base_seq': baseSeq},
          },
        }),
      );
      final first = await put(b1, 0);
      expect(first.statusCode, 200, reason: first.body);
      await blocked(a, b); // b1 is now past the newest item a can see
      final again = await put(b1, 1);
      expect(again.statusCode, 200, reason: again.body);
      final reply =
          ((jsonDecode(again.body) as Map<String, Object?>)['positions']!
                  as Map<String, Object?>)[cid]!
              as Map<String, Object?>;
      expect(reply['status'], 'applied', reason: '$reply');
    },
  );
}
