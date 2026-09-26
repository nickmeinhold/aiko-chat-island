#!/usr/bin/env dart

/// check_env_drift — does an island's LIVE `.env` match the repo's encrypted copy?
///
/// THE ONE GAP THIS CLOSES, and it is the whole scope.
/// `preflight-compose-drift.sh` already refuses a deploy when the box's
/// `docker-compose.yml` or `deploy/` differ from the tag being pulled. It cannot
/// see `.env` BY CONSTRUCTION: `.env` holds per-box secrets and there is nothing
/// public to compare it against. So half the config surface — the half that held
/// `APNS_VOIP_TOPIC` on 2026-09-11 while compose failed to forward it — has
/// never been checkable at all. This makes it checkable. That is all it does.
///
/// WHAT IT DELIBERATELY DOES NOT DO. A design that SHIPPED `.env` to the box
/// went four rounds of `/design-temper` and never reached SOUND
/// (`docs/design/16-TEMPER.md`). Every finding across those rounds — the pin
/// that could not leave `.env`, the backup landing in a pruned generation, a
/// stopped tenant read as a missing one, an empty pin file silently meaning
/// `edge`, a half-applied discriminator comparing two names of the same live
/// symlink — was about WRITING, or about the generation directory writing
/// required. None of them survive a read-only tool. So: it never writes to the
/// box. No generation, no symlink, no restore, no baseline, no adoption, no
/// cutover, nothing new on the box to sync or prune.
///
/// NAMES ONLY, NEVER VALUES, matching `deploy/secrets/MANIFEST.txt`'s
/// convention. `--show-values` exists for an operator who has already decided to
/// look, and it is not the default.
///
/// WHY DART (#4824 Stage 1, first item; Nick 2026-09-26 "dart rewrite first").
/// This supersedes the unmerged `deploy/check-env-drift.py` of PR#190 — the
/// issue names it as the free one to redo, since redoing an unmerged file is not
/// a migration. The honest account of what the language buys is in
/// `lib/drift.dart`: the three outcomes are a sealed type, so the exit-code
/// mapping cannot drift from the set of things that can happen, and a switch
/// that forgets COULD-NOT-RUN does not compile. It buys nothing in
/// `lib/dotenv.dart`, which is a parser, and that file says so.
///
/// Usage:
///   `dart run deploy/dart/bin/check_env_drift.dart <island> [...] [--show-values]`
///   dart run deploy/dart/bin/check_env_drift.dart --all
///
/// Exit codes — three outcomes, and the third fails closed:
///   0  MATCH          every key present on both sides with equal values
///   1  DIFFERS        a real difference; the key names are on stdout
///   2  COULD NOT RUN  ssh failed, sops failed, no key, wrong box, parse failed
///
/// A check that could not run is not a check that passed.
library;

import 'dart:io';

import 'package:island_deploy/dotenv.dart';
import 'package:island_deploy/drift.dart';
import 'package:island_deploy/islands.dart';

Future<void> main(List<String> argv) async {
  final showValues = argv.contains('--show-values');
  final all = argv.contains('--all');
  final named = argv.where((a) => !a.startsWith('--')).toList();

  if (!all && named.isEmpty) {
    stderr.writeln(_usage);
    exit(2);
  }
  final unknown = named.where((n) => !islands.containsKey(n)).toList();
  if (unknown.isNotEmpty) {
    stderr.writeln('unknown island(s): ${unknown.join(", ")}\n$_usage');
    exit(2);
  }

  final targets = all ? islands.keys.toList() : named;
  final repoRoot = _repoRoot();

  var worst = 0;
  for (final name in targets) {
    final result = await check(repoRoot, islands[name]!);
    _report(result, showValues);
    final code = exitCodeFor(result.outcome);
    if (code > worst) worst = code;
  }
  exit(worst);
}

/// An outcome plus, only for a [Differs], the two sides' values.
///
/// The values ride HERE rather than on [Differs] so the outcome object an
/// operator might log or pass around can never carry a secret. An earlier draft
/// smuggled them through a module-level variable written by `check` and read by
/// the reporter — correct only because the loop happened to call them in that
/// order, which is a property of today's caller rather than of the code.
typedef CheckResult = ({
  CheckOutcome outcome,
  Map<String, (String, String)> values,
});

const _noValues = <String, (String, String)>{};

/// One island, end to end. Returns an outcome rather than throwing, so the
/// caller's exit-code mapping stays exhaustive over the sealed type.
Future<CheckResult> check(String repoRoot, Island island) async {
  CheckResult cannot(String reason) =>
      (outcome: CannotRun(island.name, reason), values: _noValues);

  final repoRead = await decryptRepoEnv(repoRoot, island.name);
  if (repoRead case ReadFailed(:final reason)) return cannot(reason);
  final boxRead = await readLiveEnv(island);
  if (boxRead case ReadFailed(:final reason)) return cannot(reason);

  final Map<String, String> repo;
  final Map<String, String> box;
  try {
    repo = parseDotenv(
      (repoRead as ReadOk).text,
      'repo copy of ${island.name}',
    );
    box = parseDotenv(
      (boxRead as ReadOk).text,
      'live .env on ${island.sshHost}',
    );
  } on DotenvParseException catch (e) {
    return cannot('$e');
  }

  final wrong = wrongBox(island.name, island.sshHost, repo, box);
  if (wrong != null) return cannot(wrong);

  final d = diff(island.name, repo, box);
  if (d == null) {
    return (outcome: Match(island.name, repo.length), values: _noValues);
  }
  return (
    outcome: d,
    values: {for (final k in d.valuesDiffer) k: (repo[k]!, box[k]!)},
  );
}

void _report(CheckResult result, bool showValues) {
  switch (result.outcome) {
    case Match(:final island, :final keyCount):
      stdout.writeln('\n=== $island (${islands[island]!.sshHost}) ===');
      stdout.writeln(
        "  MATCH — $keyCount keys, the box's .env agrees with the repo's "
        'encrypted copy.',
      );

    case Differs(
      :final island,
      :final onlyInRepo,
      :final onlyOnBox,
      :final valuesDiffer,
    ):
      stdout.writeln('\n=== $island (${islands[island]!.sshHost}) ===');
      stdout.writeln('  DIFFERS');
      if (onlyInRepo.isNotEmpty) {
        stdout.writeln(
          '\n  in the REPO but NOT on the box (${onlyInRepo.length}):',
        );
        for (final k in onlyInRepo) {
          stdout.writeln('    - $k');
        }
      }
      if (onlyOnBox.isNotEmpty) {
        stdout.writeln(
          '\n  on the BOX but NOT in the repo (${onlyOnBox.length}):',
        );
        for (final k in onlyOnBox) {
          stdout.writeln('    + $k');
        }
      }
      if (valuesDiffer.isNotEmpty) {
        stdout.writeln(
          '\n  present in both, VALUES DIFFER (${valuesDiffer.length}):',
        );
        for (final k in valuesDiffer) {
          if (showValues) {
            final (r, b) = result.values[k]!;
            stdout.writeln('    ~ $k\n        repo: $r\n        box:  $b');
          } else {
            stdout.writeln('    ~ $k');
          }
        }
        if (!showValues) {
          stdout.writeln(
            '\n  (key names only — pass --show-values to print the values)',
          );
        }
      }

    case CannotRun(:final island, :final reason):
      stderr.writeln('\n=== $island ===\n  COULD NOT RUN — $reason');
  }
}

/// The repo root, derived from this script's own location rather than from the
/// caller's cwd — so the tool works from anywhere, which is how an operator will
/// actually invoke it.
String _repoRoot() {
  var dir = File.fromUri(Platform.script).absolute.parent; // bin/
  return dir.parent.parent.parent.path; // dart/ -> deploy/ -> repo root
}

const _usage = '''
usage:
  dart run deploy/dart/bin/check_env_drift.dart <island> [<island> ...] [--show-values]
  dart run deploy/dart/bin/check_env_drift.dart --all

islands: enspyr, imagineering
exit:    0 match, 1 differs, 2 could not run (fails closed)''';
