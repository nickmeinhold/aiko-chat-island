/// The Dart port's oracle (docs/crucible/dart-port/DESIGN.md §2.2, step 1): fixed
/// journeys driven over the real wire against a real gateway binary, so the same
/// journeys can later run unchanged against the Dart gateway.
///
/// Three instruments, each for one question:
/// - the frozen contract (`contract/`): does this operation exist, and does a
///   response conform to what it declares;
/// - recorded shapes (`golden/`): are the fields a client reads still there, with
///   the same types (the contract mostly can't say);
/// - each journey's obligations: the behaviours a shape can't see (a refusal, a
///   status, an ordering).
library;

import 'dart:io';

export 'src/contract.dart';
export 'src/gateway.dart';
export 'src/journey_map.dart';
export 'src/shape.dart';

/// The checkout under test: the nearest directory, from here upward, holding
/// the frozen contract.
final String repoRoot = () {
  var dir = Directory.current.absolute;
  while (!File('${dir.path}/contract/openapi.json').existsSync()) {
    final parent = dir.parent;
    if (parent.path == dir.path) {
      throw StateError('run inside the aiko-chat-island checkout');
    }
    dir = parent;
  }
  return dir.path;
}();
