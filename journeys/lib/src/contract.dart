/// The frozen wire contract (`contract/openapi.json` + `contract/websockets.json`),
/// read as an oracle: which operations exist, and whether a response conforms to
/// what the contract declares for it.
///
/// What it can and cannot prove, measured 2026-11-10: the contract types REQUEST
/// bodies well, but 48 of its 56 JSON success responses are an unconstrained
/// object (the Python routes return `dict`). Conforming to those proves the body is
/// an object and nothing more. The fields a client reads are held by the recorded
/// shapes (`shape.dart`), not by this file.
library;

import 'dart:convert';
import 'dart:io';

import 'package:json_schema/json_schema.dart';

/// What checking one response against the contract found.
sealed class ContractCheck {
  const ContractCheck();
}

final class Conforms extends ContractCheck {
  const Conforms();
}

final class Violates extends ContractCheck {
  const Violates(this.problems);
  final List<String> problems;
  @override
  String toString() => 'Violates(${problems.join('; ')})';
}

/// The contract says nothing about this status (FastAPI declares only the success
/// code and 422). Not a pass and not a failure: the journey's own obligation for
/// that status is the check.
final class Undeclared extends ContractCheck {
  const Undeclared(this.status);
  final int status;
}

final class Contract {
  Contract._(this._doc, this.websockets);

  /// Loads the contract from [repoRoot] and refuses one this validator would
  /// misread (see [_checkKeywords]).
  factory Contract.load(String repoRoot) {
    final dir = '$repoRoot/contract';
    final doc = jsonDecode(
      File('$dir/openapi.json').readAsStringSync(),
    ) as Map<String, Object?>;
    final ws = (jsonDecode(
      File('$dir/websockets.json').readAsStringSync(),
    ) as List<Object?>).cast<String>();
    _checkKeywords(doc);
    return Contract._(doc, List.unmodifiable(ws));
  }

  final Map<String, Object?> _doc;

  /// WebSocket paths. FastAPI leaves them out of openapi.json, so the freshness
  /// test freezes them beside it.
  final List<String> websockets;

  final _schemas = <String, JsonSchema>{};

  Map<String, Object?> get _paths => _doc['paths']! as Map<String, Object?>;

  /// Every HTTP operation, spelled `METHOD /path/{template}`.
  Set<String> get operations => {
    for (final MapEntry(key: path, value: ops) in _paths.entries)
      for (final method in (ops! as Map<String, Object?>).keys)
        '${method.toUpperCase()} $path',
  };

  /// Checks a response to [operation] (as spelled in [operations]).
  ContractCheck check(
    String operation, {
    required int status,
    required String? contentType,
    required String body,
  }) {
    final space = operation.indexOf(' ');
    final method = operation.substring(0, space).toLowerCase();
    final path = operation.substring(space + 1);
    final op =
        (_paths[path] as Map<String, Object?>?)?[method]
            as Map<String, Object?>?;
    if (op == null) return Violates(['$operation is not in the contract']);
    final declared =
        (op['responses']! as Map<String, Object?>)['$status']
            as Map<String, Object?>?;
    if (declared == null) return Undeclared(status);

    final content = declared['content'] as Map<String, Object?>?;
    if (content == null) {
      return body.isEmpty
          ? const Conforms()
          : Violates(['$status declares no body, got ${body.length} bytes']);
    }
    final media = contentType?.split(';').first.trim();
    final entry = content[media] as Map<String, Object?>?;
    if (entry == null) {
      return Violates([
        'media type $media is not declared for $status '
            '(declared: ${content.keys.join(', ')})',
      ]);
    }
    final schema = entry['schema'] as Map<String, Object?>?;
    if (schema == null) return const Conforms();

    final Object? instance;
    if (media == 'application/json') {
      try {
        instance = jsonDecode(body);
      } on FormatException catch (e) {
        return Violates(['body is not JSON: ${e.message}']);
      }
    } else {
      instance = body;
    }
    final results = _schemaFor(
      '$operation $status $media',
      schema,
    ).validate(instance);
    return results.errors.isEmpty
        ? const Conforms()
        : Violates([
            for (final e in results.errors) '${e.instancePath}: ${e.message}',
          ]);
  }

  JsonSchema _schemaFor(
    String key,
    Map<String, Object?> schema,
  ) => _schemas[key] ??= JsonSchema.create({
    // The package implements draft 2019-09, not the 2020-12 dialect OpenAPI
    // 3.1 names. _checkKeywords is what makes that safe.
    r'$schema': 'https://json-schema.org/draft/2019-09/schema',
    // Not a JSON Schema keyword: carried so `#/components/schemas/...` refs
    // resolve. Only `schemas`: the package walks unknown keys as schemas, and
    // `securitySchemes` holds `"type": "http"`, which is no JSON type.
    'components': {
      'schemas': (_doc['components']! as Map<String, Object?>)['schemas'],
    },
    ...schema,
  }, schemaVersion: SchemaVersion.draft2019_09);
}

/// The keywords this validator reads exactly as draft 2020-12 does. A schema
/// using anything else (`prefixItems`, `$dynamicRef`, `unevaluated*`, array-form
/// `items`) would be validated under different rules, or silently ignored, so the
/// contract is refused instead of half-checked. Widen this only together with a
/// test that the new keyword is enforced.
const _sameInBothDrafts = {
  r'$ref',
  'additionalProperties',
  'allOf',
  'anyOf',
  'const',
  'default',
  'deprecated',
  'description',
  'enum',
  'examples',
  'exclusiveMaximum',
  'exclusiveMinimum',
  'format',
  'items',
  'maxItems',
  'maxLength',
  'maximum',
  'minItems',
  'minLength',
  'minimum',
  'multipleOf',
  'not',
  'oneOf',
  'pattern',
  'properties',
  'readOnly',
  'required',
  'title',
  'type',
  'uniqueItems',
  'writeOnly',
};

void _checkKeywords(Map<String, Object?> doc) {
  final found = <String>{};
  void schema(Object? node) {
    if (node is! Map<String, Object?>) return;
    found.addAll(node.keys);
    for (final key in ['items', 'additionalProperties', 'not']) {
      schema(node[key]);
    }
    if (node['items'] is List) found.add('items (array form)');
    for (final key in ['allOf', 'anyOf', 'oneOf']) {
      for (final sub in (node[key] as List<Object?>?) ?? const []) {
        schema(sub);
      }
    }
    for (final sub
        in ((node['properties'] as Map<String, Object?>?) ?? const {}).values) {
      schema(sub);
    }
  }

  final components = doc['components'] as Map<String, Object?>?;
  for (final s
      in ((components?['schemas'] as Map<String, Object?>?) ?? const {})
          .values) {
    schema(s);
  }
  for (final ops in (doc['paths']! as Map<String, Object?>).values) {
    for (final op in (ops! as Map<String, Object?>).values) {
      final o = op! as Map<String, Object?>;
      for (final p in (o['parameters'] as List<Object?>?) ?? const []) {
        schema((p! as Map<String, Object?>)['schema']);
      }
      final bodies = [
        (o['requestBody'] as Map<String, Object?>?)?['content'],
        for (final r
            in ((o['responses'] as Map<String, Object?>?) ?? const {}).values)
          (r! as Map<String, Object?>)['content'],
      ];
      for (final content in bodies.whereType<Map<String, Object?>>()) {
        for (final media in content.values) {
          schema((media! as Map<String, Object?>)['schema']);
        }
      }
    }
  }
  final unsupported = found.difference(_sameInBothDrafts);
  if (unsupported.isNotEmpty) {
    throw StateError(
      'contract/openapi.json uses schema keywords this oracle cannot check the '
      'way OpenAPI 3.1 means them: ${unsupported.toList()..sort()}',
    );
  }
}
