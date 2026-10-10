/// Comparison and outcome tests — the half the language was chosen for.
library;

import 'package:island_deploy/drift.dart';
import 'package:test/test.dart';

void main() {
  group('diff', () {
    test('identical maps are a MATCH (null diff)', () {
      expect(diff('x', {'A': '1'}, {'A': '1'}), isNull);
    });

    test('classifies the three kinds of difference separately', () {
      final d = diff(
        'x',
        {'A': '1', 'B': '2', 'SAME': 's'},
        {'B': 'CHANGED', 'C': '3', 'SAME': 's'},
      )!;
      expect(d.onlyInRepo, ['A']);
      expect(d.onlyOnBox, ['C']);
      expect(d.valuesDiffer, ['B']);
    });

    test('an empty value differs from an absent key', () {
      // Two different facts about the box. Collapsing them reports drift that
      // is not there, or hides drift that is.
      expect(diff('x', {'A': ''}, {})!.onlyInRepo, ['A']);
      expect(diff('x', {'A': ''}, {'A': ''}), isNull);
    });

    test('output is sorted, so two runs are diffable', () {
      final d = diff('x', {'Z': '1', 'A': '1'}, {})!;
      expect(d.onlyInRepo, ['A', 'Z']);
    });
  });

  group('exit codes', () {
    test('map to 0 / 1 / 2', () {
      expect(exitCodeFor(const Match('x', 5)), 0);
      expect(
        exitCodeFor(
          const Differs(
            'x',
            onlyInRepo: ['A'],
            onlyOnBox: [],
            valuesDiffer: [],
          ),
        ),
        1,
      );
      expect(exitCodeFor(const CannotRun('x', 'nope')), 2);
    });

    test('COULD-NOT-RUN is never 0 — the tool\'s stated fear', () {
      // "A check that could not run is not a check that passed." This is the
      // assertion the sealed type makes hard to break, not one it makes
      // unnecessary: the type stops a FORGOTTEN arm, this stops a wrong one.
      expect(exitCodeFor(const CannotRun('x', 'ssh died')), isNot(0));
    });
  });

  group('wrongBox', () {
    test('a present-and-different identity key refuses the comparison', () {
      final r = wrongBox(
        'enspyr',
        'nick-mel',
        {'ISLAND_ID': 'enspyr'},
        {'ISLAND_ID': 'imagineering'},
      );
      expect(r, contains('WRONG BOX'));
      expect(r, contains('ISLAND_ID'));
    });

    test('an ABSENT identity key is not evidence', () {
      // An absence has two causes (not set, or not forwarded). Treating it as a
      // wrong-box signal would refuse to check a correctly-configured island
      // whose display name simply is not pinned — the same two-causes error as
      // `install_id IS NULL`.
      expect(
        wrongBox('enspyr', 'nick-mel', {'ISLAND_ID': 'enspyr'}, {}),
        isNull,
      );
      expect(
        wrongBox('enspyr', 'nick-mel', {}, {'ISLAND_ID': 'other'}),
        isNull,
      );
    });

    test('an EMPTY identity key is not evidence either', () {
      expect(
        wrongBox(
          'enspyr',
          'nick-mel',
          {'ISLAND_ID': ''},
          {'ISLAND_ID': 'imagineering'},
        ),
        isNull,
      );
    });

    test('matching identity keys pass', () {
      expect(
        wrongBox(
          'enspyr',
          'nick-mel',
          {'ISLAND_ID': 'enspyr'},
          {'ISLAND_ID': 'enspyr'},
        ),
        isNull,
      );
    });
  });
}
