// Journey 21 (DESIGN.md §2.2): the caller's own profile, `/v1/me`. The handle
// cooldown (#2631), the free first claim (#437, #438) and the one-statement
// write (#439). The Python authority is `domain/users_service.py`
// `update_profile`.
//
// NOT covered, named so nobody reads this file as the whole row: "two devices
// racing their first claim → exactly one skips" is asserted by its end state
// (a third change is refused) from two concurrent requests, but the wire can't
// make them overlap inside the gateway, so a run where they serialise proves
// only the sequential rule.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

/// `handle_change_cooldown_seconds`' default: 30 days.
const _cooldown = 30 * 24 * 3600;

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_21.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0;

  // PATCH /me is rate-limited per client IP: one client per test.
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

  /// A fresh account per test. [assigned]: its handle is the one sign-up made
  /// up, not one it chose.
  Account account({bool assigned = false}) => Account.seed(
    gateway,
    'journey21-${accounts++}',
    handleAssigned: assigned,
  );

  Future<http.Response> get(Account a) => client.get(
    gateway.base.resolve('/v1/me'),
    headers: {'authorization': 'Bearer ${mint.access(a.id)}'},
  );

  Future<http.Response> patch(Account a, Map<String, Object?> body) =>
      client.patch(
        gateway.base.resolve('/v1/me'),
        headers: {
          'authorization': 'Bearer ${mint.access(a.id)}',
          'content-type': 'application/json',
          'x-forwarded-for': forwardedFor,
        },
        body: jsonEncode(body),
      );

  Future<Map<String, Object?>> me(Account a) async {
    final res = await get(a);
    expect(res.statusCode, 200, reason: res.body);
    return jsonDecode(res.body) as Map<String, Object?>;
  }

  /// PATCHes a handle change that must be applied.
  Future<void> changed(Account a, String handle) async {
    final res = await patch(a, {'handle': handle});
    expect(res.statusCode, 200, reason: res.body);
    expect((jsonDecode(res.body) as Map<String, Object?>)['username'], handle);
  }

  /// PATCHes [body] expecting the handle cooldown's 429, and checks it is that
  /// 429 and not the rate limiter's.
  Future<void> onCooldown(Account a, Map<String, Object?> body) async {
    final res = await patch(a, body);
    expect(res.statusCode, 429, reason: res.body);
    final check = contract.check(
      'PATCH /v1/me',
      status: 429,
      contentType: res.headers['content-type'],
      body: res.body,
    );
    expect(check, isA<Conforms>(), reason: '$check');
    final refusal = jsonDecode(res.body) as Map<String, Object?>;
    expect(refusal['code'], 'handle_change_cooldown');
    final after = refusal['retry_after']! as int;
    expect(after, inInclusiveRange(1, _cooldown));
    expect(res.headers['retry-after'], '$after');
  }

  group('GET', () {
    test('serves the contract and its recorded shape', () async {
      final res = await get(account(assigned: true));
      expect(res.statusCode, 200, reason: res.body);
      final check = contract.check(
        'GET /v1/me',
        status: 200,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(check, isA<Conforms>(), reason: '$check');
      expect(golden.expect('GET /v1/me', res.body), isNull);
    });

    test(
      'handle_auto is true until the first real change, then false',
      () async {
        final a = account(assigned: true);
        expect((await me(a))['handle_auto'], isTrue);
        await changed(a, '${a.handle}-chosen');
        expect((await me(a))['handle_auto'], isFalse);
      },
    );

    test('a chosen handle is not auto', () async {
      expect((await me(account()))['handle_auto'], isFalse);
    });
  });

  group('PATCH refusals', () {
    test('neither field → 400', () async {
      expect((await patch(account(), {})).statusCode, 400);
    });

    test('a taken handle → 409', () async {
      final taken = account();
      expect(
        (await patch(account(), {'handle': taken.handle})).statusCode,
        409,
      );
    });

    test(
      'a second change inside the window → 429 handle_change_cooldown',
      () async {
        final a = account();
        final res = await patch(a, {'handle': '${a.handle}-1'});
        expect(res.statusCode, 200, reason: res.body);
        expect(golden.expect('PATCH /v1/me', res.body), isNull);
        await onCooldown(a, {'handle': '${a.handle}-2'});
        expect((await me(a))['username'], '${a.handle}-1');
      },
    );
  });

  group('the window is started and consulted only by a real change', () {
    test('a display-name edit neither starts nor consults it', () async {
      final a = account();
      expect((await patch(a, {'display_name': 'Before'})).statusCode, 200);
      await changed(a, '${a.handle}-1'); // the edit didn't start it
      final res = await patch(a, {'display_name': 'During'});
      expect(res.statusCode, 200, reason: res.body); // nor is it consulted
      expect((await me(a))['display_name'], 'During');
    });

    test('a same-handle write changes nothing and starts nothing', () async {
      final a = account(assigned: true);
      final res = await patch(a, {'handle': a.handle, 'display_name': 'Same'});
      expect(res.statusCode, 200, reason: res.body);
      final after = await me(a);
      expect(
        after['display_name'],
        'Same',
      ); // a no-op handle still lands the name
      expect(after['handle_auto'], isTrue); // the free first claim is unspent
      await changed(a, '${a.handle}-1'); // free
      await changed(a, '${a.handle}-2'); // stamps
      await onCooldown(a, {'handle': '${a.handle}-3'});
    });

    test('a refused change does not apply the display name with it', () async {
      final a = account();
      await changed(a, '${a.handle}-1');
      await onCooldown(a, {'handle': '${a.handle}-2', 'display_name': 'Lost'});
      expect((await me(a))['display_name'], a.handle);
    });

    test('a taken handle does not apply the display name with it', () async {
      final taken = account();
      final a = account();
      final res = await patch(a, {
        'handle': taken.handle,
        'display_name': 'Lost',
      });
      expect(res.statusCode, 409);
      expect((await me(a))['display_name'], a.handle);
    });
  });

  group('the first claim away from an assigned handle is free', () {
    test(
      'it leaves no stamp, the next change is free too, and that one stamps',
      () async {
        final a = account(assigned: true);
        await changed(a, '${a.handle}-1');
        await changed(a, '${a.handle}-2');
        await onCooldown(a, {'handle': '${a.handle}-3'});
      },
    );

    test('two devices racing their first claim: exactly one skips', () async {
      final a = account(assigned: true);
      final both = await Future.wait([
        patch(a, {'handle': '${a.handle}-phone'}),
        patch(a, {'handle': '${a.handle}-laptop'}),
      ]);
      // Whichever order they landed in, the second is a real change after the
      // free one, so both are applied, and the second one stamps.
      expect(both.map((r) => r.statusCode), everyElement(200));
      await onCooldown(a, {'handle': '${a.handle}-third'});
    });
  });
}
