/// Comparing an island's live `.env` against the repo's encrypted copy.
///
/// WHY THIS IS THE PART THAT WANTED DART. Design 15's temper (DISSOLVED 4/4,
/// 2026-09-24) left one unanimous language finding: *types protect values you
/// CONSTRUCT and do nothing for values you PARSE.* The parser next door gets no
/// benefit from being here. This file does.
///
/// The Python version returned an `int` exit code from `check()` and raised
/// `CannotRun` for the third outcome. Those live in two different channels, and
/// the tool's own docstring names the failure that lets through: *"A check that
/// could not run is not a check that passed."* Nothing in the type system said
/// so — a caller that forgot the `except` got a clean `0`.
///
/// Here the three outcomes are ONE sealed type. A switch that forgets
/// [CannotRun] does not compile (`non_exhaustive_switch_expression: error` in
/// analysis_options.yaml), so the exit-code mapping cannot drift from the set of
/// things that can happen. That is the whole argument for the rewrite, and it is
/// worth being precise that it is an argument about THIS file and not about the
/// package.
library;

/// The outcome of checking one island. Exhaustive by construction.
sealed class CheckOutcome {
  const CheckOutcome(this.island);
  final String island;
}

/// The box's `.env` agrees with the repo's encrypted copy.
final class Match extends CheckOutcome {
  const Match(super.island, this.keyCount);

  /// How many keys agreed. Carried so a MATCH over a suspiciously small file is
  /// visible rather than reassuring — a zero-key parse is already refused, but
  /// "12 keys matched" when the box should hold 60 is a finding the operator can
  /// only make if the number is printed.
  final int keyCount;
}

/// A real difference. Key names only; values stay out of this object entirely
/// unless the caller asked, mirroring `deploy/secrets/MANIFEST.txt`'s convention.
final class Differs extends CheckOutcome {
  const Differs(
    super.island, {
    required this.onlyInRepo,
    required this.onlyOnBox,
    required this.valuesDiffer,
  });

  final List<String> onlyInRepo;
  final List<String> onlyOnBox;
  final List<String> valuesDiffer;
}

/// The comparison did not happen. NOT a difference, and never a match.
final class CannotRun extends CheckOutcome {
  const CannotRun(super.island, this.reason);
  final String reason;
}

/// Exit codes. The switch is exhaustive, so adding a fourth outcome is a compile
/// error here rather than an unmapped code discovered by an operator.
int exitCodeFor(CheckOutcome o) => switch (o) {
  Match() => 0,
  Differs() => 1,
  CannotRun() => 2,
};

/// Compare two parsed environments.
///
/// Pure: no I/O, no printing. The Python version interleaved comparison and
/// reporting through a `report(...)` that took eight positional arguments and
/// read both maps; splitting them is what lets the comparison be tested without
/// capturing stdout.
Differs? diff(
  String island,
  Map<String, String> repo,
  Map<String, String> box,
) {
  final onlyInRepo = repo.keys.where((k) => !box.containsKey(k)).toList()
    ..sort();
  final onlyOnBox = box.keys.where((k) => !repo.containsKey(k)).toList()
    ..sort();
  final valuesDiffer =
      repo.keys.where((k) => box.containsKey(k) && box[k] != repo[k]).toList()
        ..sort();

  if (onlyInRepo.isEmpty && onlyOnBox.isEmpty && valuesDiffer.isEmpty) {
    return null;
  }
  return Differs(
    island,
    onlyInRepo: onlyInRepo,
    onlyOnBox: onlyOnBox,
    valuesDiffer: valuesDiffer,
  );
}

/// Keys that identify WHICH island a box is. Checked before anything is
/// reported, because both boxes are shared multi-tenant hosts and a wrong-box
/// comparison produces a wall of spurious differences that reads exactly like
/// catastrophic drift.
const identityKeys = ['ISLAND_ID', 'ISLAND_DISPLAY_NAME'];

/// Returns a reason if the box is demonstrably not the island we decrypted for.
///
/// Only a PRESENT-AND-DIFFERENT pair is evidence. A key absent from either side
/// is an absence with two causes (not set, or not forwarded), and treating it as
/// a wrong-box signal would refuse to check a correctly-configured island whose
/// display name simply is not pinned.
String? wrongBox(
  String island,
  String sshHost,
  Map<String, String> repo,
  Map<String, String> box,
) {
  for (final key in identityKeys) {
    final a = repo[key];
    final b = box[key];
    if (a != null && b != null && a.isNotEmpty && b.isNotEmpty && a != b) {
      return 'WRONG BOX: $key differs between the repo\'s $island config and '
          '$sshHost ($a vs $b). Refusing to compare.';
    }
  }
  return null;
}
