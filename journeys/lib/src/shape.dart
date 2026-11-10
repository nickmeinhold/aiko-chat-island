/// The recorded shape of a JSON response: every key, and the JSON type under it,
/// all the way down. Values are dropped, so a shape survives a fresh database, a
/// new random id and a different clock, and still changes when a field is renamed,
/// removed, added, or changes type.
///
/// This is the oracle for the fields a client reads, because the contract mostly
/// can't be (see `contract.dart`). Python is the reference: shapes are recorded
/// from it, and the Dart gateway must produce the same ones (DESIGN.md §2.2,
/// "Python's behaviour is the reference only for 'unchanged'").
library;

import 'dart:convert';
import 'dart:io';

/// The shape of a decoded JSON value, itself JSON so a golden file can hold it.
///
/// - a scalar is its type name (`string`, `number`, `bool`, `null`);
/// - an object is a map from each key to that key's shape;
/// - a list is `{"list of": [shapes]}`, the distinct element shapes in a stable
///   order, so a page of ten messages and a page of two have the same shape.
///   An empty list is `{"list of": []}`: a journey that needs to see the element
///   shape has to make the list non-empty, and its golden shows whether it did.
Object shapeOf(Object? json) => switch (json) {
  null => 'null',
  bool() => 'bool',
  num() => 'number',
  String() => 'string',
  List<Object?>() => {
    'list of': (json.map(shapeOf).map(_canonical).toSet().toList()..sort())
        .map(jsonDecode)
        .toList(),
  },
  Map<String, Object?>() => {
    for (final key in json.keys.toList()..sort()) key: shapeOf(json[key]),
  },
  _ => throw ArgumentError('not a decoded JSON value: ${json.runtimeType}'),
};

String _canonical(Object? shape) => jsonEncode(shape);

/// A journey's recorded shapes, keyed by what the journey calls each response.
///
/// `RECORD_SHAPES=1` rewrites the file from what the gateway under test returned;
/// otherwise every [expect] compares and every key must be visited, so a golden
/// entry no journey checks any more is a failure, not dead weight.
final class Golden {
  Golden(this.path)
    : _recording = Platform.environment['RECORD_SHAPES'] == '1',
      _stored = File(path).existsSync()
          ? (jsonDecode(File(path).readAsStringSync()) as Map<String, Object?>)
          : const {};

  final String path;
  final bool _recording;
  final Map<String, Object?> _stored;
  final _seen = <String, Object>{};

  /// The difference between [body]'s shape and the recorded one, or null when
  /// they match (or when recording).
  String? expect(String key, String body) {
    final shape = shapeOf(jsonDecode(body));
    _seen[key] = shape;
    if (_recording) return null;
    if (!_stored.containsKey(key)) {
      return 'no recorded shape for "$key" (record with RECORD_SHAPES=1 '
          'against the PYTHON gateway)';
    }
    final want = _canonical(_stored[key]), got = _canonical(shape);
    return want == got
        ? null
        : 'shape of "$key" changed\n  want $want\n  got  $got';
  }

  /// Writes the file when recording; otherwise names recorded keys never checked.
  String? finish() {
    if (_recording) {
      final keys = _seen.keys.toList()..sort();
      File(path)
        ..createSync(recursive: true)
        ..writeAsStringSync(
          '${const JsonEncoder.withIndent('  ').convert({for (final k in keys) k: _seen[k]})}\n',
        );
      return null;
    }
    final unchecked = _stored.keys.toSet().difference(_seen.keys.toSet());
    return unchecked.isEmpty
        ? null
        : 'recorded but never checked: ${unchecked.toList()..sort()}';
  }
}
