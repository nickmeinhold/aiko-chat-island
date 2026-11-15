// Journey 2 (DESIGN.md §2.2): sign in, and what a session does after it. The
// refresh rotation shipped in #420 (v0.24.0); the row states its rule, and the
// Python authority is `tests/test_session_revocation.py`.
//
// Starts from minted tokens (`accounts.dart`), not a passkey ceremony, so the
// journey is about the session and not the authenticator.
//
// NOT covered, named so nobody reads this file as the whole row:
// - wrong origin, sign-count regression: those ARE the ceremony, and need an
//   authenticator the harness doesn't have yet;
// - a refresh that loses every compare-and-swap race → 503 + `Retry-After: 1`:
//   needs barriers inside the gateway, which the wire can't place;
// - lowering `jwt_refresh_ttl_seconds` never moves a session's horizon earlier:
//   seeing a prune needs the injected clock (§2.2), not built yet.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_2.json');
  final client = http.Client();
  late PythonGateway gateway;
  late Minter mint;
  var accounts = 0;

  // The refresh door is rate-limited per client IP (20 a minute), keyed on the
  // rightmost X-Forwarded-For, the entry production's one Caddy appends
  // (`domain/rate_limit.py` `client_ip`). The harness plays that Caddy and gives
  // each test a client of its own, so the limiter stays on, as in production,
  // and no test spends another's budget.
  var client_ = 0;
  late String forwardedFor;
  setUp(() => forwardedFor = '198.51.100.${++client_}');

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

  /// A fresh account per test, so no test's session is another's.
  Account account() => Account.seed(gateway, 'journey2-${accounts++}');

  Future<http.Response> refresh(String token) => client.post(
    gateway.base.resolve('/v1/auth/refresh'),
    headers: {
      'content-type': 'application/json',
      'x-forwarded-for': forwardedFor,
    },
    body: jsonEncode({'refresh_token': token}),
  );

  Future<http.Response> me(String access) => client.get(
    gateway.base.resolve('/v1/me'),
    headers: {'authorization': 'Bearer $access'},
  );

  /// Refreshes [token], expecting success; returns the successor.
  Future<String> rotated(String token) async {
    final res = await refresh(token);
    expect(res.statusCode, 200, reason: res.body);
    final body = jsonDecode(res.body) as Map<String, Object?>;
    return body['refresh_token']! as String;
  }

  Future<void> refused(String token) async {
    final res = await refresh(token);
    expect(res.statusCode, 401, reason: res.body);
  }

  group('the minted caller is real', () {
    test('a seeded account\'s access token is served', () async {
      final a = account();
      final res = await me(mint.access(a.id));
      expect(res.statusCode, 200, reason: res.body);
      expect(
        (jsonDecode(res.body) as Map<String, Object?>)['username'],
        a.handle,
      );
    });

    test('a well-signed token for an unseeded id is refused', () async {
      // Without this, every pass above could be a gateway that trusts any
      // signature, and the seeding would be proving nothing.
      expect((await me(mint.access(Account.unseededId()))).statusCode, 401);
      await refused(mint.refresh(Account.unseededId()));
    });

    test('a token signed with another secret is refused', () async {
      final a = account();
      final forged = Minter('not-the-gateway-secret');
      expect((await me(forged.access(a.id))).statusCode, 401);
      await refused(forged.refresh(a.id));
    });
  });

  group('JWT expiry crossed', () {
    // Minted already expired: crossing `exp` needs no clock control.
    final anHourAgo = DateTime.now().subtract(const Duration(hours: 1));

    test('an expired access token is refused', () async {
      final a = account();
      expect((await me(mint.access(a.id, now: anHourAgo))).statusCode, 401);
    });

    test('an expired refresh token is refused', () async {
      final a = account();
      await refused(
        mint.refresh(a.id, now: anHourAgo, ttl: const Duration(minutes: 1)),
      );
    });
  });

  group('the refresh rotates', () {
    test(
      'CURRENT → a new current, a fresh 30 days, and working access',
      () async {
        final a = account();
        final t0 = mint.refresh(a.id);
        final res = await refresh(t0);
        expect(res.statusCode, 200, reason: res.body);
        final check = contract.check(
          'POST /v1/auth/refresh',
          status: 200,
          contentType: res.headers['content-type'],
          body: res.body,
        );
        expect(check, isA<Conforms>(), reason: '$check');
        expect(golden.expect('POST /v1/auth/refresh', res.body), isNull);

        final body = jsonDecode(res.body) as Map<String, Object?>;
        final t1 = claimsOf(body['refresh_token']! as String);
        final before = claimsOf(t0);
        expect(t1['sid'], before['sid'], reason: 'the same session');
        expect(t1['jti'], isNot(before['jti']), reason: 'a new token');
        final lifetime = (t1['exp']! as int) - (t1['iat']! as int);
        expect(lifetime, const Duration(days: 30).inSeconds);
        expect((await me(body['access_token']! as String)).statusCode, 200);
      },
    );

    test('a chain of successors each refreshes', () async {
      var t = mint.refresh(account().id);
      for (var i = 0; i < 4; i++) {
        t = await rotated(t);
      }
    });

    test('an app that ignores the new field keeps working', () async {
      // Every installed app before aiko_chat_app#427 presents the token it
      // signed in with, forever. It stays the session's PREVIOUS token.
      final t0 = mint.refresh(account().id);
      for (var i = 0; i < 3; i++) {
        await rotated(t0);
      }
    });

    test(
      'PREVIOUS is a retry: a fresh current, never the one handed back',
      () async {
        final t0 = mint.refresh(account().id);
        final lost = await rotated(t0); // the response the device never kept
        final kept = await rotated(t0);
        expect(claimsOf(kept)['jti'], isNot(claimsOf(lost)['jti']));
        await rotated(kept);
      },
    );
  });

  group('an older token ends the session', () {
    test('a copy used after its successor ends it, current included', () async {
      final t0 = mint.refresh(account().id);
      final t1 = await rotated(t0);
      final t2 = await rotated(t1);
      await refused(t0); // older than previous (t1)
      await refused(t2); // the session is over
    });

    test('two holders refreshing in step fork, and both are signed out', () async {
      // Tesla's phase lock (cage-match round 1, PR #450): with a re-issue window
      // the two were handed the same successor forever and a copy never showed.
      final t0 = mint.refresh(account().id);
      final owner = await rotated(t0);
      final thief = await rotated(t0);
      expect(claimsOf(owner)['jti'], isNot(claimsOf(thief)['jti']));
      await refused(owner);
      await refused(thief);
      await refused(t0);
    });

    test('an ended session stays ended for every token it named', () async {
      final t0 = mint.refresh(account().id);
      final t1 = await rotated(t0);
      await rotated(t1);
      await refused(t0);
      await refused(t1); // was previous, a valid retry until the session ended
    });
  });

  group('a token minted before #420', () {
    test('upgrades into a rotating session, then reuse ends it', () async {
      final legacy = mint.refresh(account().id, legacy: true);
      final t1 = await rotated(legacy);
      expect(claimsOf(t1)['sid'], startsWith('legacy:'));
      await rotated(t1);
      await refused(legacy);
    });

    test('a padded respelling is the same session, not a second one', () async {
      // Carnot, cage-match round 2: the session id hashes the decoded signature,
      // which every spelling shares.
      final legacy = mint.refresh(account().id, legacy: true);
      final t1 = await rotated(legacy);
      await rotated(t1);
      await refused('$legacy=');
    });
  });
}
