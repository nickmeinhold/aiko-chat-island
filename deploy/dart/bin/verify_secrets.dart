#!/usr/bin/env dart

/// verify_secrets — check the committed encrypted island config, WITHOUT any key.
///
/// Port of `deploy/verify-secrets.sh`. The decision logic is pure and lives in
/// `lib/secrets_check.dart`, which also carries the argument for why this one
/// script under `deploy/` may be Dart at all (it runs in GitHub Actions, not on a
/// sovereign box — ISL-0003).
///
/// Everything here reads SOPS's CLEARTEXT METADATA, so it needs no key and runs in
/// CI on every push. It cannot prove the plaintext is correct — only a decrypt can,
/// and that is `--deep`, opt-in, for someone holding a key.
///
/// Exit codes — three outcomes, matching check_env_drift's posture:
///   0  OK             every check ran and passed
///   1  FAILED         at least one check failed; reasons on stdout
///   2  DID NOT VERIFY fewer checks ran than the repo's contents require
///
/// The third is the one the shell could not express. Every silence bug in its
/// history — a rename making the glob match nothing, a PR deleting every secrets
/// file, a tool dying mid-pipe — produced "no failures found" over an empty set and
/// exited 0. "Did not verify" is not "verified nothing wrong".
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:island_deploy/secrets_check.dart';

const _secretsDir = 'deploy/secrets';
const _policy = '.sops.yaml';

Future<void> main(List<String> argv) async {
  final deep = argv.contains('--deep');
  final repoRoot = _repoRootFrom(argv);
  // GitHub Actions sets CI=true. In CI the checks that can only SKIP locally
  // (continuity needs origin/main; the rotation path needs sops) are REQUIRED:
  // a skip there is the gate not running, not an inconvenience.
  final inCi = Platform.environment['CI'] == 'true';
  final results = <CheckResult>[];

  // --- policy ------------------------------------------------------------
  final policyFile = File('$repoRoot/$_policy');
  if (!policyFile.existsSync()) {
    stdout.writeln(
      'FAILED — $_policy is missing; nothing declares the recipients.',
    );
    exit(1);
  }
  final want = parseRecipients(policyFile.readAsStringSync());
  results.add(checkRecipientCount(want));

  stdout.writeln('Policy requires ${want.length} recipients:');
  for (final r in want) {
    stdout.writeln('  ${r.length <= 16 ? r : "${r.substring(0, 16)}…"}');
  }

  // --- continuity against origin/main -------------------------------------
  results.add(
    checkRecipientContinuity(
      here: want,
      onMain: await _policyOnMain(repoRoot),
      rotationAllowed: Platform.environment['ALLOW_RECIPIENT_CHANGE'] == '1',
      inCi: inCi,
    ),
  );

  // --- directory allowlist BEFORE globbing --------------------------------
  final dir = Directory('$repoRoot/$_secretsDir');
  if (!dir.existsSync()) {
    stdout.writeln('FAILED — $_secretsDir does not exist.');
    exit(1);
  }
  final entries = dir.listSync().map((e) => e.uri.pathSegments.last).toList()
    ..sort();
  results.addAll(checkDirectoryContents(entries));

  final manifestFile = File('$repoRoot/$_secretsDir/MANIFEST.txt');
  final manifestBody = manifestFile.existsSync()
      ? manifestFile.readAsStringSync()
      : '';
  if (!manifestFile.existsSync()) {
    results.add(
      const Fail(
        'manifest present',
        'MANIFEST.txt missing — the only reviewable record of which keys exist',
      ),
    );
  }
  final manifestIslands = parseManifestIslands(manifestBody);

  final files = entries.where((e) => e.endsWith('.env.sops')).toList()..sort();

  if (files.isEmpty) {
    results.addAll(checkNoFilesIsLegitimate(manifestIslands));
    // Recipient count + directory contents + the no-files verdict, plus
    // continuity in CI. NOT `results.length`: an expectation read off the
    // results it judges can never be unmet (PR #320 cage-match).
    _report(results, expectedChecks: 3 + (inCi ? 1 : 0));
    return;
  }

  // --- per file ------------------------------------------------------------
  for (final name in files) {
    stdout.writeln('\n== $_secretsDir/$name');
    final content = File('$repoRoot/$_secretsDir/$name').readAsStringSync();

    final env = parseEnvelope(content);
    switch (env) {
      case InvalidEnvelope(:final reason):
        results.add(Fail('$name envelope', reason));
        // No recipients to check against a file that is not an envelope. This is
        // a SKIP, recorded, not an absence — the whole point of the sum type.
        results.add(
          Skipped(
            '$name recipients',
            'envelope did not parse; nothing to compare',
          ),
        );
      case ValidEnvelope(
        :final version,
        :final recipients,
        :final dataIsEncrypted,
      ):
        results.add(
          Pass(
            '$name envelope',
            'valid SOPS envelope (v$version, ${recipients.length} age stanzas)',
          ),
        );
        results.addAll(
          checkRecipientSetExact(file: name, want: want, found: recipients),
        );
        results.add(
          dataIsEncrypted
              ? Pass('$name payload', 'ENC[...] encrypted')
              : Fail(
                  '$name payload',
                  'data is not an ENC[...] payload — values may be cleartext',
                ),
        );
    }

    results.add(checkNoPlaintextMarkers(name, content));
  }

  // --- rotation path --------------------------------------------------------
  results.add(await _checkRotationPath(repoRoot, files));

  // --- manifest coverage ----------------------------------------------------
  results.addAll(
    checkManifestCoverage(
      islandsWithFiles: [
        for (final f in files) f.substring(0, f.length - '.env.sops'.length),
      ],
      manifestIslands: manifestIslands,
      manifestBody: manifestBody,
    ),
  );

  // --- --deep ---------------------------------------------------------------
  results.add(
    deep
        ? await _checkDeep(repoRoot, files, manifestBody)
        : const Skipped(
            'deep manifest binding',
            '--deep not requested (needs a decryption key; never runs in CI)',
          ),
  );

  // EXPECTED EXECUTED CHECKS, from a witness INDEPENDENT of the listing the
  // checks walked. An earlier revision used `files.length`, the same listing
  // that produced the results, so the expectation could never be unmet and exit
  // 2 was unreachable from this binary (PR #320 cage-match). The islands are
  // now the UNION of secrets files and MANIFEST sections: an island the
  // manifest names is owed four checks whether or not its file is there.
  //
  // Counted over EXECUTED checks only (`ranAtLeast` excludes Skipped): recipient
  // count + directory contents + manifest coverage, four per island, and in CI
  // the two checks that may only skip locally — continuity and the rotation
  // path. So a CI runner without sops now ends DID NOT VERIFY rather than OK.
  final islands = {
    for (final f in files) f.substring(0, f.length - '.env.sops'.length),
    ...manifestIslands,
  };
  _report(results, expectedChecks: 3 + islands.length * 4 + (inCi ? 2 : 0));
}

void _report(List<CheckResult> results, {required int expectedChecks}) {
  stdout.writeln();
  for (final r in results) {
    switch (r) {
      case Pass(:final label, :final detail):
        stdout.writeln('  ok      $label${detail == null ? "" : " — $detail"}');
      case Skipped(:final label, :final why):
        stdout.writeln('  SKIP    $label — $why');
      case Fail(:final label, :final reason):
        stdout.writeln('  FAIL    $label\n            $reason');
    }
  }

  final v = Verification(results);
  final verdict = v.verdictFor(expectedChecks: expectedChecks);
  stdout.writeln();
  switch (verdict) {
    case Verdict.ok:
      stdout.writeln(
        'OK — ${v.executed} checks ran (${v.skips.length} skipped); every committed secret carries '
        'every required recipient, with no plaintext.',
      );
    case Verdict.failed:
      stdout.writeln(
        'FAILED — ${v.failures.length} of ${results.length} '
        'checks failed. See above.',
      );
    case Verdict.didNotVerify:
      stdout.writeln(
        'DID NOT VERIFY — only ${v.executed} checks ran (${v.skips.length} skipped), '
        'expected at least $expectedChecks. Nothing failed, and that is not '
        'the same as passing.',
      );
  }
  exit(Verification.exitCodeFor(verdict));
}

/// `.sops.yaml` as it exists on `origin/main`, or null if unavailable.
Future<List<String>?> _policyOnMain(String repoRoot) async {
  try {
    final r = await Process.run('git', [
      'show',
      'origin/main:$_policy',
    ], workingDirectory: repoRoot);
    if (r.exitCode != 0) return null;
    return parseRecipients(r.stdout as String);
  } on ProcessException {
    return null;
  }
}

/// `sops updatekeys` must be able to PARSE every file.
///
/// The shell's reason, worth keeping: `sops updatekeys` reads the extension to pick
/// a parser, so a file named `*.enc.env` was parsed as dotenv and this command
/// FAILED — silently breaking the documented way to add or remove a recipient.
/// "Nobody would have discovered that until the day they needed it most."
Future<CheckResult> _checkRotationPath(
  String repoRoot,
  List<String> files,
) async {
  const label = 'rotation path (sops updatekeys) parses every file';
  for (final name in files) {
    final int code;
    final String err;
    try {
      // `n` on stdin declines any change; a PARSE error still fails, which is the
      // point of running it at all.
      final p = await Process.start('sops', [
        'updatekeys',
        '$_secretsDir/$name',
      ], workingDirectory: repoRoot);
      // DRAIN BOTH STREAMS, concurrently with the wait (Carnot + Maxwell, PR
      // #320). The earlier revision never read them: the reason sops gave was
      // thrown away, and output past a pipe buffer would have blocked the child
      // forever with nothing bounding the wait.
      final outDone = p.stdout.drain<void>();
      final errText = p.stderr.transform(utf8.decoder).join();
      p.stdin.writeln('n');
      await p.stdin.close();
      try {
        code = await p.exitCode.timeout(const Duration(seconds: 60));
      } on TimeoutException {
        p.kill();
        return Fail(label, 'sops updatekeys on $name did not finish in 60s');
      }
      await outDone;
      err = await errText;
    } on ProcessException {
      return const Skipped(
        label,
        'sops not installed — metadata checks above still ran',
      );
    }
    if (code != 0) {
      final excerpt = err.trim().split('\n').take(5).join('\n              ');
      return Fail(
        label,
        'sops updatekeys cannot read $name — the documented rotation path is '
        'broken (exit $code)${excerpt.isEmpty ? "" : ":\n              $excerpt"}',
      );
    }
  }
  return const Pass(label);
}

/// `--deep`: a real decrypt, and proof the manifest tells the truth.
Future<CheckResult> _checkDeep(
  String repoRoot,
  List<String> files,
  String manifestBody,
) async {
  const label = 'deep manifest binding';
  final problems = <String>[];
  for (final name in files) {
    final island = name.substring(0, name.length - '.env.sops'.length);
    final ProcessResult r;
    try {
      r = await Process.run('sops', [
        '-d',
        '$_secretsDir/$name',
      ], workingDirectory: repoRoot);
    } on ProcessException {
      return const Skipped(label, 'sops not installed');
    }
    if (r.exitCode != 0) {
      problems.add('$island: decrypt failed');
      continue;
    }
    final actual =
        RegExp(r'^\s*(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=', multiLine: true)
            .allMatches(r.stdout as String)
            .map((m) => m.group(2)!)
            .toSet()
            .toList()
          ..sort();
    if (actual.isEmpty) {
      problems.add('$island: decrypted to ZERO assignments');
      continue;
    }
    final claimed = _manifestSection(manifestBody, island)..sort();
    if (actual.join(',') != claimed.join(',')) {
      problems.add(
        '$island: MANIFEST DOES NOT MATCH the decrypted file — the '
        'reviewable record is lying (claimed ${claimed.length}, '
        'actual ${actual.length})',
      );
    }
  }
  if (problems.isEmpty) {
    return const Pass(label, 'manifest matches every island');
  }
  return Fail(label, problems.join('\n            '));
}

List<String> _manifestSection(String manifest, String island) {
  final lines = manifest.split('\n');
  final out = <String>[];
  var inSection = false;
  for (final raw in lines) {
    final line = raw.trim();
    if (line == '[$island]') {
      inSection = true;
      continue;
    }
    if (inSection) {
      if (line.isEmpty) break;
      if (RegExp(r'^[A-Za-z_][A-Za-z0-9_]*$').hasMatch(line)) out.add(line);
    }
  }
  return out;
}

/// The checkout to verify.
///
/// `--root <dir>` OVERRIDES the script-relative default, and it is not a
/// convenience — it is what makes this tool testable at all. Deriving the root
/// solely from `Platform.script` (as `check_env_drift` does, correctly, because it
/// verifies BOXES rather than a checkout) means every invocation reads the repo the
/// script lives in, whatever directory it is run from.
///
/// MEASURED 2026-09-26, and it is why this flag exists: a five-case mutation
/// harness copied `deploy/` + `.sops.yaml` to a scratch tree, mutated the copy, and
/// invoked this script by its absolute path. All five mutations reported OK with
/// IDENTICAL output — including "14 checks ran" for a tree whose secrets files had
/// all been deleted. The tool was reading the real repo every time; the harness's
/// control passed because the control was the only thing ever executed, so a
/// control that the mutation cannot reach proves nothing about the mutation.
///
/// A checker that cannot be pointed at a fixture can only be tested by damaging the
/// thing it protects.
String _repoRootFrom(List<String> argv) {
  final i = argv.indexOf('--root');
  if (i >= 0) {
    // A `--root` with no value used to fall through to the script-relative
    // default: the REAL repo, silently — the exact false green this flag was
    // created to end (Carnot, PR #320).
    if (i + 1 >= argv.length || argv[i + 1].startsWith('--')) {
      stderr.writeln('verify_secrets: --root needs a directory');
      exit(2);
    }
    return Directory(argv[i + 1]).absolute.path;
  }
  // .../deploy/dart/bin/verify_secrets.dart -> up three
  final bin = File.fromUri(Platform.script).absolute.parent;
  return bin.parent.parent.parent.path;
}
