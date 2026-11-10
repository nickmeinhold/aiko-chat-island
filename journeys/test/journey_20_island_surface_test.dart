// Journey 20 (DESIGN.md §2.2): the island's read-only surface, and the doors
// production must not serve.
@Tags(['gateway'])
library;

import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

const _reads = [
  '/.well-known/apple-app-site-association',
  '/.well-known/assetlinks.json',
  '/capabilities',
  '/health',
  '/privacy',
  '/terms',
  '/v1/island',
  '/v1/islands',
  '/v1/gateways',
  // `creates_accounts` here must equal what the passkey door does: journey 1's
  // obligation, because seeing it needs a registration attempt.
  '/v1/auth/providers',
];

// Configured the way the live islands are, so the recorded shapes are
// production's (checked against chat.enspyr.co 2026-11-10: 8 of 8 GETs match
// except `/health`, below). Without a fingerprint, assetlinks is an empty list and
// its element shape is never seen.
const _settings = {
  'PASSKEY_ANDROID_CERT_SHA256': '["AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"]',
};

// NOT covered, named so nobody reads the golden as production's whole shape:
// `/health` gains `push.android_ready` only once FCM is configured (credentials),
// and its `build.*` fields are strings only inside the image, which bakes
// /app/build-info.json (deliberately not settable from the environment). Both
// gateways run without either here, so the comparison is fair, not complete.

void main() {
  final contract = Contract.load(repoRoot);
  final golden = Golden('$repoRoot/journeys/golden/journey_20.json');
  final client = http.Client();
  late PythonGateway gateway;

  setUpAll(
    () async =>
        gateway = await PythonGateway.start(repoRoot, settings: _settings),
  );
  tearDownAll(() async {
    client.close();
    await gateway.stop();
    final unchecked = golden.finish();
    if (unchecked != null) fail(unchecked);
  });

  for (final path in _reads) {
    test('GET $path serves the contract and its recorded shape', () async {
      final res = await client.get(gateway.base.resolve(path));
      expect(res.statusCode, 200, reason: res.body);
      final type = res.headers['content-type'];
      final check = contract.check(
        'GET $path',
        status: 200,
        contentType: type,
        body: res.body,
      );
      expect(check, isA<Conforms>(), reason: '$check');
      if (type != null && type.startsWith('application/json')) {
        expect(golden.expect('GET $path', res.body), isNull);
      }
    });
  }

  test('assetlinks carries BOTH relations, or Android passkeys fail', () async {
    // `get_login_creds` alone passes Google's assetlinks:check, but GMS's
    // on-device RP-ID validation also needs `handle_all_urls`: without it every
    // Android passkey create fails with [50152] (2026-07-28). A shape can't see
    // this; it's a value.
    final res = await client.get(
      gateway.base.resolve('/.well-known/assetlinks.json'),
    );
    final targets = jsonDecode(res.body) as List<Object?>;
    expect(targets, hasLength(1));
    final target = targets.single! as Map<String, Object?>;
    expect((target['relation']! as List<Object?>).toSet(), {
      'delegate_permission/common.get_login_creds',
      'delegate_permission/common.handle_all_urls',
    });
  });

  test('password /register is not served in production', () async {
    // The dev router is never mounted there (Nick, 2026-10-28), whatever
    // OPEN_REGISTRATION says; a Dart gateway must not serve it either.
    final res = await client.post(
      gateway.base.resolve('/v1/auth/register'),
      headers: {'content-type': 'application/json'},
      body: '{"username": "journey", "password": "journey"}',
    );
    expect(res.statusCode, 404);
  });

  test(
    '/login is served: a bad body is a validation error, not a 404',
    () async {
      final res = await client.post(
        gateway.base.resolve('/v1/auth/login'),
        headers: {'content-type': 'application/json'},
        body: '{}',
      );
      expect(res.statusCode, 422);
      final check = contract.check(
        'POST /v1/auth/login',
        status: 422,
        contentType: res.headers['content-type'],
        body: res.body,
      );
      expect(check, isA<Conforms>(), reason: '$check');
    },
  );

  test(
    'the dev bus poke 404s in production even with every parameter',
    () async {
      final res = await client.post(
        gateway.base.resolve('/v1/_debug/send?channel=c&username=u&message=m'),
      );
      expect(res.statusCode, 404);
    },
  );
}
