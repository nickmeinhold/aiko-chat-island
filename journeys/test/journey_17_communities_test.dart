// Journey 17 (DESIGN.md §2.2): communities. The directory lists what is public
// and live; detail and join answer a community the caller may not see with the
// same 404 as one that doesn't exist; a new join is for public communities
// only, counts once, and is idempotent; "my communities" is what the caller
// belongs to and is still live. The Python authority is
// `domain/communities_service.py` (`discoverable_predicate`,
// `accessible_predicate`, the conditional join insert), with the channel list
// from `acl.visible_channels_in_community`.
//
// No door creates a community (the row said "create" until this journey was
// built; there is no such route or code path), so the communities here are
// seeded straight into the database (`communities.dart`). Every test names its
// communities with its own tag and searches by it, because the directory is
// one list shared by every test in the run.
//
// Contract gap, the same kind as journeys 18, 19 and 22: the 404s and the bad
// cursor's 400 are sent as `{detail: string}` and the frozen contract declares
// none of them. Those checks expect `Undeclared`.
//
// Pinned as it stands, not as the model's docstring hopes: an UNLISTED
// community is not joinable by link yet (that is phase B3). To a non-member it
// is a 404 like a private one.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

/// `communities_service.PAGE_SIZE`.
const _page = 20;

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_17.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0, tags = 0;

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

  Account account() => Account.seed(gateway, 'journey17-${accounts++}');

  /// A word no other test's community names hold.
  String tag() => 'j17tag${tags++}x';

  Map<String, String> auth(Account a) => {
    'authorization': 'Bearer ${mint.access(a.id)}',
  };

  Future<http.Response> get(Account a, String path, [Map<String, String>? q]) =>
      client.get(
        gateway.base.resolve(path).replace(queryParameters: q),
        headers: auth(a),
      );

  Future<http.Response> join(Account a, Community c) => client.post(
    gateway.base.resolve('/v1/communities/${c.id}/join'),
    headers: auth(a),
  );

  Map<String, Object?> body(http.Response res) =>
      jsonDecode(res.body) as Map<String, Object?>;

  List<Map<String, Object?>> communities(http.Response res) =>
      (body(res)['communities']! as List).cast<Map<String, Object?>>();

  /// The names on one directory page for [q], in the order served.
  Future<List<Object?>> discover(
    Account a,
    String q, {
    String? sort,
    String? category,
    String? cursor,
  }) async {
    final res = await get(a, '/v1/communities/discover', {
      'q': q,
      'sort': ?sort,
      'category': ?category,
      'cursor': ?cursor,
    });
    expect(res.statusCode, 200, reason: res.body);
    return [for (final c in communities(res)) c['name']];
  }

  Future<List<Object?>> mine(Account a) async {
    final res = await get(a, '/v1/communities');
    expect(res.statusCode, 200, reason: res.body);
    return [for (final c in communities(res)) c['id']];
  }

  Future<int> detail(Account a, Community c) async =>
      (await get(a, '/v1/communities/${c.id}')).statusCode;

  List<Object?> channelIds(Map<String, Object?> json) => [
    for (final ch in (json['channels']! as List).cast<Map<String, Object?>>())
      ch['id'],
  ];

  void conforms(String op, http.Response res, int status) {
    final check = contract.check(
      op,
      status: status,
      contentType: res.headers['content-type'],
      body: res.body,
    );
    expect(check, isA<Conforms>(), reason: '$check');
  }

  void undeclared(String op, http.Response res, int status) {
    final check = contract.check(
      op,
      status: status,
      contentType: res.headers['content-type'],
      body: res.body,
    );
    expect(check, isA<Undeclared>(), reason: '$check');
  }

  test('every route serves the contract and its recorded shape', () async {
    final a = account();
    final t = tag();
    final c = Community.seed(
      gateway,
      '$t one',
      channels: [const ChannelSeed('lobby')],
    );

    final found = await get(a, '/v1/communities/discover', {'q': t});
    conforms('GET /v1/communities/discover', found, 200);
    expect(golden.expect('GET /v1/communities/discover', found.body), isNull);

    final shown = await get(a, '/v1/communities/${c.id}');
    conforms('GET /v1/communities/{community_id}', shown, 200);
    expect(
      golden.expect('GET /v1/communities/{community_id}', shown.body),
      isNull,
    );

    final joined = await join(a, c);
    expect(joined.statusCode, 201, reason: joined.body);
    conforms('POST /v1/communities/{community_id}/join', joined, 201);
    expect(
      golden.expect('POST /v1/communities/{community_id}/join', joined.body),
      isNull,
    );

    final listed = await get(a, '/v1/communities');
    conforms('GET /v1/communities', listed, 200);
    expect(golden.expect('GET /v1/communities', listed.body), isNull);
  });

  group('the directory', () {
    test('lists public, live communities only', () async {
      final a = account();
      final t = tag();
      for (final v in ['public', 'unlisted', 'private']) {
        Community.seed(gateway, '$t $v', visibility: v);
      }
      Community.seed(gateway, '$t gone', takenDown: true);
      expect(await discover(a, t), ['$t public']);
    });

    test('lists a private community to no one, members included', () async {
      final a = account();
      final t = tag();
      Community.seed(
        gateway,
        '$t private',
        visibility: 'private',
        members: [a],
      );
      expect(await discover(a, t), isEmpty);
    });

    test('sorts by member count, most first, by default', () async {
      final a = account();
      final t = tag();
      // Alphabetical order is the reverse, so a name sort can't pass for this.
      Community.seed(gateway, '$t apple', memberCount: 1);
      Community.seed(gateway, '$t cherry', memberCount: 9);
      Community.seed(gateway, '$t banana', memberCount: 4);
      final byMembers = ['$t cherry', '$t banana', '$t apple'];
      expect(await discover(a, t), byMembers);
      expect(await discover(a, t, sort: 'members'), byMembers);
      // An unknown sort falls back to members, never an error.
      expect(await discover(a, t, sort: 'loudest'), byMembers);
    });

    test('sorts by name on request', () async {
      final a = account();
      final t = tag();
      for (final n in ['cherry', 'apple', 'banana']) {
        Community.seed(gateway, '$t $n', memberCount: n.length);
      }
      expect(await discover(a, t, sort: 'name'), [
        '$t apple',
        '$t banana',
        '$t cherry',
      ]);
    });

    test('matches q as a substring, ignoring case', () async {
      final a = account();
      final t = tag();
      Community.seed(gateway, '$t Garden Club');
      Community.seed(gateway, '$t Book Club');
      expect(await discover(a, '${t.toUpperCase()} GARDEN'), [
        '$t Garden Club',
      ]);
      // Mid-name, so a prefix match can't pass (no other test names a garden).
      expect(await discover(a, 'arden CLUB'), ['$t Garden Club']);
    });

    test('takes % and _ in q literally', () async {
      final a = account();
      final t = tag();
      Community.seed(gateway, '$t 100% real');
      Community.seed(gateway, '$t 1000 real');
      Community.seed(gateway, '$t a_b');
      Community.seed(gateway, '$t axb');
      expect(await discover(a, '$t 100%'), ['$t 100% real']);
      expect(await discover(a, '$t a_b'), ['$t a_b']);
    });

    test('filters by category exactly', () async {
      final a = account();
      final t = tag();
      Community.seed(gateway, '$t tunes', category: 'music');
      Community.seed(gateway, '$t chat', category: 'social');
      expect(await discover(a, t, category: 'music'), ['$t tunes']);
    });

    test('pages through a cursor until it runs out', () async {
      final a = account();
      final t = tag();
      // Distinct counts make the order total, so the pages can be checked
      // against it.
      final names = [
        for (var i = 0; i <= _page; i++) '$t ${'$i'.padLeft(2, '0')}',
      ];
      for (final (i, n) in names.indexed) {
        Community.seed(gateway, n, memberCount: 100 - i);
      }
      final first = await get(a, '/v1/communities/discover', {'q': t});
      expect(first.statusCode, 200, reason: first.body);
      expect([
        for (final c in communities(first)) c['name'],
      ], names.take(_page));
      final cursor = body(first)['next_cursor']! as String;

      final second = await get(a, '/v1/communities/discover', {
        'q': t,
        'cursor': cursor,
      });
      expect([for (final c in communities(second)) c['name']], [names.last]);
      expect(body(second)['next_cursor'], isNull);
    });

    test('refuses a tampered cursor', () async {
      final a = account();
      final res = await get(a, '/v1/communities/discover', {
        'cursor': 'not-a-cursor',
      });
      expect(res.statusCode, 400, reason: res.body);
      undeclared('GET /v1/communities/discover', res, 400);
    });
  });

  group('detail', () {
    test(
      'shows a public community to anyone, with its public channels',
      () async {
        final a = account(), insider = account();
        final c = Community.seed(
          gateway,
          '${tag()} open',
          channels: [
            const ChannelSeed('lobby'),
            ChannelSeed('back room', private: true, members: [insider]),
          ],
        );
        final res = await get(a, '/v1/communities/${c.id}');
        expect(res.statusCode, 200, reason: res.body);
        final json = body(res);
        expect(json['id'], c.id);
        expect(json['default_channel_id'], c.channelIds.first);
        expect(channelIds(json), [c.channelIds.first]);
      },
    );

    test('adds the private channels the caller is in', () async {
      final insider = account();
      final c = Community.seed(
        gateway,
        '${tag()} open',
        channels: [
          const ChannelSeed('lobby'),
          ChannelSeed('back room', private: true, members: [insider]),
          const ChannelSeed('vault', private: true),
        ],
      );
      final res = await get(insider, '/v1/communities/${c.id}');
      expect(res.statusCode, 200, reason: res.body);
      expect(
        channelIds(body(res))..sort(),
        [c.channelIds[0], c.channelIds[1]]..sort(),
      );
    });

    test('is a 404 to a non-member of a private or unlisted one', () async {
      final a = account();
      for (final v in ['private', 'unlisted']) {
        final c = Community.seed(gateway, '${tag()} $v', visibility: v);
        final res = await get(a, '/v1/communities/${c.id}');
        expect(res.statusCode, 404, reason: '$v: ${res.body}');
        undeclared('GET /v1/communities/{community_id}', res, 404);
        // The same answer as a community that doesn't exist.
        final none = await get(a, '/v1/communities/${Account.unseededId()}');
        expect(none.statusCode, 404);
        expect(none.body, res.body);
      }
    });

    test('shows a private one to its members', () async {
      final member = account();
      final c = Community.seed(
        gateway,
        '${tag()} private',
        visibility: 'private',
        members: [member],
      );
      expect(await detail(member, c), 200);
    });

    test('is a 404 for a taken-down one, members included', () async {
      final member = account();
      final c = Community.seed(
        gateway,
        '${tag()} gone',
        members: [member],
        takenDown: true,
      );
      expect(await detail(member, c), 404);
      expect(await detail(account(), c), 404);
    });
  });

  group('join', () {
    test('joins a public community once, counting the member', () async {
      final a = account();
      final c = Community.seed(
        gateway,
        '${tag()} open',
        memberCount: 5,
        channels: [const ChannelSeed('lobby')],
      );
      final res = await join(a, c);
      expect(res.statusCode, 201, reason: res.body);
      final json = body(res);
      expect(json['joined'], isTrue);
      expect((json['community']! as Map)['member_count'], 6);
      expect(channelIds(json), c.channelIds);
      expect(await mine(a), [c.id]);
    });

    test('a second join changes nothing and says so', () async {
      final a = account();
      final c = Community.seed(gateway, '${tag()} open', memberCount: 5);
      await join(a, c);
      final again = await join(a, c);
      expect(again.statusCode, 201, reason: again.body);
      expect(body(again)['joined'], isFalse);
      expect((body(again)['community']! as Map)['member_count'], 6);
      expect(await mine(a), [c.id]);
    });

    test('refuses a private or unlisted community as a 404', () async {
      final a = account();
      for (final v in ['private', 'unlisted']) {
        final c = Community.seed(gateway, '${tag()} $v', visibility: v);
        final res = await join(a, c);
        expect(res.statusCode, 404, reason: '$v: ${res.body}');
        undeclared('POST /v1/communities/{community_id}/join', res, 404);
        expect(await mine(a), isEmpty);
      }
    });

    test('lets a member of a private community re-join, unchanged', () async {
      final member = account();
      final c = Community.seed(
        gateway,
        '${tag()} private',
        visibility: 'private',
        members: [member],
      );
      final res = await join(member, c);
      expect(res.statusCode, 201, reason: res.body);
      expect(body(res)['joined'], isFalse);
      expect((body(res)['community']! as Map)['member_count'], 1);
    });

    test('refuses a taken-down community, members included', () async {
      final member = account(), stranger = account();
      final c = Community.seed(
        gateway,
        '${tag()} gone',
        members: [member],
        takenDown: true,
      );
      expect((await join(member, c)).statusCode, 404);
      expect((await join(stranger, c)).statusCode, 404);
    });
  });

  group('my communities', () {
    test('lists what the caller belongs to, by id, live ones only', () async {
      final a = account();
      final t = tag();
      final ids = [
        Community.seed(gateway, '$t one', members: [a]).id,
        Community.seed(
          gateway,
          '$t two',
          visibility: 'private',
          members: [a],
        ).id,
      ];
      Community.seed(gateway, '$t gone', members: [a], takenDown: true);
      Community.seed(gateway, '$t elsewhere', members: [account()]);
      expect(await mine(a), ids..sort());
    });

    test('is empty for a new account', () async {
      expect(await mine(account()), isEmpty);
    });
  });
}
