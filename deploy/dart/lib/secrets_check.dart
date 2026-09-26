/// Verifying the committed encrypted island config WITHOUT any private key.
///
/// PORT OF `deploy/verify-secrets.sh` (252 lines). It is the ONE script under
/// `deploy/` that does not run on a sovereign box — `ci.yml`'s `secrets-integrity`
/// job runs it in GitHub Actions — so ISL-0003's no-toolchain-on-the-box argument
/// does not reach it. Every other script there is blocked, and the three that look
/// like the best candidates (`resolve-gateway-env`, `resolve-media-env`,
/// `preflight-apns`) are precisely the three design 15 proposed moving into the
/// attested image before it DISSOLVED 4/4.
///
/// WHY THE LANGUAGE EARNS ITS PLACE HERE, and it is not a general claim about
/// Dart. Read the shell script's own comments and one class appears in four
/// separate arms, each added after it fired:
///
///   * renaming a secret made the `*.env.sops` glob match nothing, so it printed
///     "nothing to verify" and exited 0 — "a checker that goes silent when its
///     targets disappear reports success for the state it exists to detect";
///   * a PR DELETING every secrets file kept CI green, because the zero-files case
///     had a lullaby instead of a check;
///   * a tool dying mid-pipe exits on the last command's status — success, with
///     empty output;
///   * checking only that required recipients are PRESENT was blind to an ADDED
///     one, which grants full plaintext and is invisible in a binary diff.
///
/// Every one is **silence read as success**. The shell answers it by remembering to
/// add another arm. This file answers it structurally: a check cannot produce
/// "nothing". [CheckResult] is sealed over {pass, fail, skipped}, a SKIP is a
/// first-class outcome that must be printed rather than an absence, and
/// [Verification.verdict] folds them exhaustively — so a new outcome is a compile
/// error rather than a value that falls out of every bucket.
///
/// And the runner asserts that checks actually RAN ([Verification.ranAtLeast]).
/// That is the affirming instrument the shell could not express: proving a
/// verification did work is different from proving it found no violation, and
/// every bug above lived in exactly that gap.
///
/// PURE. No I/O, no `Process`, no file reads — those live in `bin/verify_secrets.dart`.
/// The shell script could not separate them, which is why its own header says the
/// proofs "were run by hand and written into a PR body", and why Carnot's finding
/// on PR#166 was *"entropy wins when checks live in memory."*
library;

import 'dart:convert';

/// One check's outcome. Sealed so the verdict fold cannot forget an arm.
sealed class CheckResult {
  const CheckResult(this.label);

  /// What was checked, in the operator's words. Present on every arm INCLUDING
  /// skips, because an unlabelled skip is the silence this file exists to end.
  final String label;
}

final class Pass extends CheckResult {
  const Pass(super.label, [this.detail]);
  final String? detail;
}

final class Fail extends CheckResult {
  const Fail(super.label, this.reason);
  final String reason;
}

/// A check that COULD NOT run — never a pass, never a failure.
///
/// The shell has three of these (`origin/main` unavailable, `sops` not installed,
/// `--deep` not requested) and prints them as notes, which is correct but
/// unenforced: nothing stopped a fourth from being added as a silent `continue`.
/// Making it an arm of the sum type means a skip is carried to the verdict and
/// reported, and [Verification.verdict] decides what it means in ONE place.
final class Skipped extends CheckResult {
  const Skipped(super.label, this.why);
  final String why;
}

/// The overall answer. Separate from [CheckResult] because "did every check pass"
/// and "did enough checks run" are different questions, and the second is the one
/// the shell could not ask.
enum Verdict { ok, failed, didNotVerify }

/// A completed run.
class Verification {
  Verification(this.results);
  final List<CheckResult> results;

  Iterable<Fail> get failures => results.whereType<Fail>();
  Iterable<Skipped> get skips => results.whereType<Skipped>();

  /// Did at least [n] checks actually execute?
  ///
  /// THE AFFIRMING INSTRUMENT. Every silence bug in the shell script passed a
  /// "no failures found" test while having examined nothing. A run that produced
  /// three results when the repo holds two islands has not verified them — it has
  /// failed to find a violation in a set it never enumerated.
  bool ranAtLeast(int n) => results.length >= n;

  /// `didNotVerify` outranks `ok`: too few checks is not a pass.
  Verdict verdictFor({required int expectedChecks}) {
    if (failures.isNotEmpty) return Verdict.failed;
    if (!ranAtLeast(expectedChecks)) return Verdict.didNotVerify;
    return Verdict.ok;
  }

  /// Exit code. Exhaustive over [Verdict], so adding one is a compile error.
  static int exitCodeFor(Verdict v) => switch (v) {
    Verdict.ok => 0,
    Verdict.failed => 1,
    // 2, not 1: "the check could not run" is the same third outcome
    // check_env_drift uses, and for the same reason — a verification that did
    // not happen is not a verification that failed, and an operator needs to
    // tell them apart.
    Verdict.didNotVerify => 2,
  };
}

// ---------------------------------------------------------------------------
// the checks — pure functions over already-read inputs
// ---------------------------------------------------------------------------

/// Age recipients declared by `.sops.yaml`, in file order.
///
/// Deliberately NOT a YAML parse. The shell script's round-2 finding stands: an
/// earlier draft used `python3 -c "import yaml"`, and CI installs only sops, so the
/// gate depended on PyYAML happening to be on the runner image — "undeclared
/// ambient state is not a dependency, it is a coincidence with a good track
/// record." The recipient list is a flat `- age1…` sequence; read it as one.
List<String> parseRecipients(String sopsYaml) {
  final re = RegExp(r'^\s*-\s*(age1[a-z0-9]{20,})\s*$', multiLine: true);
  return [for (final m in re.allMatches(sopsYaml)) m.group(1)!];
}

/// At least two recipients with uncorrelated failure modes (#3976).
CheckResult checkRecipientCount(List<String> want) {
  const label = 'policy declares enough recipients';
  if (want.length >= 2) return Pass(label, '${want.length} recipients');
  return Fail(
    label,
    '.sops.yaml declares ${want.length} recipient(s); at least 2 with '
    'uncorrelated failure modes are required (#3976)',
  );
}

/// The recipient set must match `origin/main`'s unless rotation is declared.
///
/// SCOPE, stated exactly as the shell states it: this is a CONTINUITY check, not
/// authentication. The recipient field is a plain string, so without a key nothing
/// proves the DEK is really wrapped to those keys. It catches the careless case.
CheckResult checkRecipientContinuity({
  required List<String> here,
  required List<String>? onMain,
  required bool rotationAllowed,
}) {
  const label = 'recipient set matches origin/main';
  if (onMain == null) {
    return const Skipped(label, 'origin/main not available (local run)');
  }
  final a = [...onMain]..sort();
  final b = [...here]..sort();
  if (a.join(',') == b.join(',')) return const Pass(label);
  if (rotationAllowed) {
    return const Pass(label, 'DIFFERS — permitted by ALLOW_RECIPIENT_CHANGE=1');
  }
  return Fail(
    label,
    'recipient set differs from origin/main. Rotation is rare and must be '
    'deliberate: re-run with ALLOW_RECIPIENT_CHANGE=1 and say why in the PR '
    'body.\n  on main: ${a.join(", ")}\n  here:    ${b.join(", ")}',
  );
}

/// Only expected artifacts may live in `deploy/secrets/`.
///
/// ALLOWLIST BEFORE GLOB, and the shell's comment says why better than a summary
/// could: mutation-proving caught it "eating its own tail" — renaming a secret back
/// to the broken `*.enc.env` made the glob match nothing, so it printed "nothing to
/// verify" and exited 0.
List<CheckResult> checkDirectoryContents(List<String> basenames) {
  const allowed = {'README.md', '.gitignore', 'MANIFEST.txt'};
  final out = <CheckResult>[];
  for (final name in basenames) {
    if (allowed.contains(name) || name.endsWith('.env.sops')) continue;
    out.add(
      Fail(
        'deploy/secrets contents',
        'unexpected artifact: $name — only README.md, MANIFEST.txt, .gitignore '
            'and *.env.sops belong here. A misnamed secret would otherwise fall '
            'outside the glob and be silently skipped.',
      ),
    );
  }
  if (out.isEmpty) {
    out.add(const Pass('deploy/secrets contents', 'no unexpected artifacts'));
  }
  return out;
}

/// Zero secrets files: a failure IF the manifest names any island.
///
/// The shell's sibling case, added after a PR deleting every secrets file kept CI
/// green: "confessing one instance is not naming the class." The manifest is the
/// witness — only a genuinely un-lifted repo may pass with nothing to verify.
List<CheckResult> checkNoFilesIsLegitimate(List<String> manifestIslands) {
  const label = 'no *.env.sops present';
  if (manifestIslands.isEmpty) {
    return [
      const Pass(label, 'no island sections in MANIFEST — un-lifted repo'),
    ];
  }
  return [
    for (final i in manifestIslands)
      Fail(
        label,
        'MANIFEST names island [$i] but $i.env.sops IS GONE — every encrypted '
        'island artifact has been removed',
      ),
  ];
}

/// A parsed SOPS envelope, or the reason it is not one.
sealed class Envelope {
  const Envelope();
}

final class ValidEnvelope extends Envelope {
  const ValidEnvelope({
    required this.version,
    required this.recipients,
    required this.dataIsEncrypted,
  });
  final String version;
  final List<String> recipients;
  final bool dataIsEncrypted;
}

final class InvalidEnvelope extends Envelope {
  const InvalidEnvelope(this.reason);
  final String reason;
}

/// Structural check, ONE parse.
///
/// The shell's round-2 finding: an earlier draft accepted a file merely CONTAINING
/// the string `lastmodified` — a loose grep standing in for a shape assertion.
///
/// Recipients come from the SCHEMA (`sops.age[].recipient`), never a regex over the
/// whole file. Measured zero false positives today, but a regex could match an
/// age1-shaped substring inside the base64 payload — "a leaky calorimeter reading
/// the container instead of the contents, when the structured field is right there."
Envelope parseEnvelope(String content) {
  final Map<String, dynamic> doc;
  try {
    final decoded = jsonDecode(content);
    if (decoded is! Map<String, dynamic>) {
      return const InvalidEnvelope('not a JSON object — not a SOPS envelope');
    }
    doc = decoded;
  } on FormatException catch (e) {
    return InvalidEnvelope(
      'not parseable JSON — not a SOPS envelope (${e.message})',
    );
  }

  final sops = doc['sops'];
  if (sops is! Map<String, dynamic>) {
    return const InvalidEnvelope(
      'SOPS envelope missing required field(s): sops',
    );
  }
  final missing = [
    for (final k in ['age', 'mac', 'lastmodified', 'version'])
      if (!sops.containsKey(k)) k,
    if (!doc.containsKey('data')) 'data',
  ];
  if (missing.isNotEmpty) {
    return InvalidEnvelope(
      'SOPS envelope missing required field(s): ${missing.join(", ")}',
    );
  }

  final age = sops['age'];
  if (age is! List || age.isEmpty) {
    return const InvalidEnvelope(
      'sops.age is empty — no recipient can decrypt this file',
    );
  }
  final recipients = <String>[
    for (final a in age)
      if (a is Map && a['recipient'] is String) a['recipient'] as String,
  ];

  final data = doc['data'];
  return ValidEnvelope(
    version: '${sops["version"]}',
    recipients: recipients,
    // Presence of a valid envelope is NOT proof of encryption: a SOPS file whose
    // values are cleartext still carries valid metadata.
    dataIsEncrypted: data is String && data.startsWith('ENC['),
  );
}

/// Recipients must be present AND ONLY those — the set, pinned exactly.
///
/// Tesla's catch on PR#166: the recipient list is the ONE cleartext invariant left
/// in a binary-encrypted file, the single line a reviewer can check without
/// decrypting. Checking only that required keys are PRESENT is blind to an ADDED
/// one — a silent third wrap grants full plaintext and is invisible in a diff.
List<CheckResult> checkRecipientSetExact({
  required String file,
  required List<String> want,
  required List<String> found,
}) {
  final out = <CheckResult>[];
  final foundSet = found.toSet();
  for (final r in want) {
    if (!foundSet.contains(r)) {
      out.add(
        Fail(
          '$file recipients',
          'recipient ${_elide(r)} MISSING — this file cannot be decrypted by a '
              'required key',
        ),
      );
    }
  }
  for (final got in foundSet) {
    if (!want.contains(got)) {
      out.add(
        Fail(
          '$file recipients',
          'UNAUTHORISED RECIPIENT ${_elide(got)} — this key can decrypt the file '
              'and is not in .sops.yaml. A silent third wrap grants full plaintext '
              'and is invisible in a binary diff.',
        ),
      );
    }
  }
  if (out.isEmpty) {
    out.add(
      Pass(
        '$file recipients',
        'exactly ${want.length} required recipients, none beyond policy',
      ),
    );
  }
  return out;
}

/// Plaintext key markers in what should be ciphertext.
///
/// Cheap, and it is the failure that cannot be undone once pushed to a public repo.
const plaintextMarkers = [
  'BEGIN PRIVATE KEY',
  'BEGIN EC PRIVATE KEY',
  'BEGIN RSA PRIVATE KEY',
  'AGE-SECRET-KEY',
];

CheckResult checkNoPlaintextMarkers(String file, String content) {
  final hits = [
    for (final m in plaintextMarkers)
      if (content.contains(m)) m,
  ];
  if (hits.isEmpty) return Pass('$file plaintext markers', 'none');
  return Fail(
    '$file plaintext markers',
    "PLAINTEXT MARKER(S) ${hits.join(', ')} present in ciphertext",
  );
}

/// Every secrets file has a manifest section, and every manifest section a file.
///
/// The binary encoding means a diff cannot show WHICH key changed, so MANIFEST.txt
/// carries key NAMES in cleartext to buy that review surface back.
List<CheckResult> checkManifestCoverage({
  required List<String> islandsWithFiles,
  required List<String> manifestIslands,
  required String manifestBody,
}) {
  final out = <CheckResult>[];
  for (final i in islandsWithFiles) {
    if (!manifestIslands.contains(i)) {
      out.add(
        Fail(
          'manifest coverage',
          'MANIFEST has no [$i] section — a secrets file with no manifest entry '
              'is unreviewable',
        ),
      );
    }
  }
  for (final i in manifestIslands) {
    if (!islandsWithFiles.contains(i)) {
      out.add(
        Fail(
          'manifest coverage',
          'MANIFEST names [$i] but $i.env.sops does not exist',
        ),
      );
    }
  }
  if (manifestBody.contains('=')) {
    out.add(
      const Fail(
        'manifest coverage',
        "MANIFEST contains '=' — it must hold key NAMES only, never values",
      ),
    );
  }
  if (out.isEmpty) {
    out.add(
      const Pass(
        'manifest coverage',
        'every island covered (names only; binding check is --deep)',
      ),
    );
  }
  return out;
}

/// `[island]` section headers in MANIFEST.txt.
List<String> parseManifestIslands(String manifest) {
  final re = RegExp(r'^\[([a-z0-9_-]+)\]$', multiLine: true);
  return [for (final m in re.allMatches(manifest)) m.group(1)!];
}

String _elide(String s) => s.length <= 16 ? s : '${s.substring(0, 16)}…';
