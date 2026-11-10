import 'dart:convert';
import 'dart:io';

import 'package:island_journeys/island_journeys.dart';
import 'package:test/test.dart';

void main() {
  final contract = Contract.load(repoRoot);

  group('the orphan check', () {
    test('every operation in the contract has a journey', () {
      final http = journeyOf.keys.where((op) => !op.startsWith('WS '));
      expect(
        contract.operations.difference(http.toSet()),
        isEmpty,
        reason: 'map each new route to its journey in lib/src/journey_map.dart',
      );
    });

    test('every mapped operation is still in the contract', () {
      final http = journeyOf.keys.where((op) => !op.startsWith('WS ')).toSet();
      expect(http.difference(contract.operations), isEmpty);
    });

    test('the socket routes are exactly the frozen ones', () {
      final ws = [
        for (final op in journeyOf.keys)
          if (op.startsWith('WS ')) op.substring(3),
      ]..sort();
      expect(ws, contract.websockets);
    });

    test('every mapped journey exists', () {
      expect(
        journeyOf.values.toSet().difference(journeys.keys.toSet()),
        isEmpty,
      );
    });

    test('journeys still to build (step 2 drives this to none)', () {
      final built = {
        for (final f in Directory('$repoRoot/journeys/test').listSync())
          if (RegExp(r'journey_(\d+)_\w+_test\.dart$').firstMatch(f.path)
              case final m?)
            int.parse(m[1]!),
      };
      expect(
        built.difference(journeys.keys.toSet()),
        isEmpty,
        reason: 'a journey test file names a journey DESIGN.md does not have',
      );
      final pending = journeys.keys.toSet().difference(built).toList()..sort();
      print('built: ${built.toList()..sort()}  pending: $pending');
    });
  });

  group('the contract oracle is not hollow', () {
    test('a typed response is checked through its \$ref', () {
      // POST /v1/auth/login declares 422 as the HTTPValidationError component,
      // whose `detail` is a list. A string there must not conform.
      final bad = contract.check(
        'POST /v1/auth/login',
        status: 422,
        contentType: 'application/json',
        body: '{"detail": "x"}',
      );
      expect(bad, isA<Violates>());
      final good = contract.check(
        'POST /v1/auth/login',
        status: 422,
        contentType: 'application/json',
        body: '{"detail": [{"loc": ["body"], "msg": "m", "type": "t"}]}',
      );
      expect(good, isA<Conforms>());
    });

    test('an undeclared status is Undeclared, not a pass', () {
      expect(
        contract.check(
          'GET /health',
          status: 503,
          contentType: 'application/json',
          body: '{}',
        ),
        isA<Undeclared>(),
      );
    });

    test(
      'a contract using a keyword the validator would misread is refused',
      () {
        final dir = Directory.systemTemp.createTempSync('contract-');
        addTearDown(() => dir.deleteSync(recursive: true));
        Directory('${dir.path}/contract').createSync();
        File('${dir.path}/contract/websockets.json').writeAsStringSync('[]');
        File('${dir.path}/contract/openapi.json').writeAsStringSync(
          jsonEncode({
            'paths': <String, Object?>{},
            'components': {
              'schemas': {
                'Pair': {
                  'type': 'array',
                  'prefixItems': [
                    {'type': 'string'},
                  ],
                },
              },
            },
          }),
        );
        expect(() => Contract.load(dir.path), throwsStateError);
      },
    );
  });

  group('shapes', () {
    test('drop values, keep keys and types', () {
      expect(shapeOf(jsonDecode('{"b": 1, "a": [true, false], "c": null}')), {
        'a': {
          'list of': ['bool'],
        },
        'b': 'number',
        'c': 'null',
      });
    });

    test('a long list and a short one have the same shape', () {
      expect(
        shapeOf(jsonDecode('[{"id": "x"}, {"id": "y"}, {"id": "z"}]')),
        shapeOf(jsonDecode('[{"id": "q"}]')),
      );
    });

    test('a renamed field changes the shape', () {
      expect(
        shapeOf(jsonDecode('{"user_id": "x"}')),
        isNot(shapeOf(jsonDecode('{"userId": "x"}'))),
      );
    });
  });
}
