/// Tests aimed at the ONE class this script's history is made of.
///
/// `verify-secrets.sh` grew four separate arms, each added after a silence bug
/// fired: a rename made the glob match nothing (printed "nothing to verify",
/// exited 0); a PR deleting every secrets file kept CI green; a tool dying mid-pipe
/// exited on the last command's status with empty output; checking only that
/// required recipients were PRESENT was blind to an ADDED one.
///
/// Every one is **silence read as success**. So the tests that matter here are the
/// ones asserting that a check RAN, not only that it found nothing.
library;

import 'package:island_deploy/secrets_check.dart';
import 'package:test/test.dart';

const _r1 = 'age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqsecure';
const _r2 = 'age1wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwsecure';
const _r3 = 'age1eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeesecure';

String _envelope({
  List<String> recipients = const [_r1, _r2],
  String data = 'ENC[AES256_GCM,data:abc,type:str]',
  bool omitMac = false,
}) {
  final age = recipients.map((r) => '{"recipient":"$r","enc":"x"}').join(',');
  return '''
{"data":"$data","sops":{"age":[$age],
${omitMac ? '' : '"mac":"ENC[x]",'}"lastmodified":"2026-09-26T00:00:00Z","version":"3.11.0"}}''';
}

void main() {
  group('the verdict — the arm the shell could not express', () {
    test('DID NOT VERIFY when too few checks ran, even with zero failures', () {
      // THE CENTRAL TEST. Every silence bug in this script's history passed a
      // "no failures" test while having examined nothing. Zero failures over an
      // empty set is not a pass.
      final v = Verification([const Pass('only one thing')]);
      expect(v.verdictFor(expectedChecks: 6), Verdict.didNotVerify);
      expect(Verification.exitCodeFor(Verdict.didNotVerify), 2);
      expect(Verification.exitCodeFor(Verdict.didNotVerify), isNot(0));
    });

    test('a failure outranks a short run', () {
      final v = Verification([const Fail('x', 'boom')]);
      expect(v.verdictFor(expectedChecks: 99), Verdict.failed);
    });

    test('OK needs BOTH no failures and enough checks', () {
      final v = Verification([
        const Pass('a'),
        const Pass('b'),
        const Skipped('c', 'no key'),
      ]);
      expect(v.verdictFor(expectedChecks: 3), Verdict.ok);
      expect(v.verdictFor(expectedChecks: 4), Verdict.didNotVerify);
    });

    test('a SKIP is not a failure and not invisible', () {
      // The shell prints skips as notes, which is right but unenforced. Here a
      // skip is an arm of the sum type, so it reaches the verdict and the report.
      final v = Verification([const Skipped('deep', 'no key')]);
      expect(v.failures, isEmpty);
      expect(v.skips, hasLength(1));
      expect(v.verdictFor(expectedChecks: 1), Verdict.ok);
    });
  });

  group('recipients', () {
    test('parsed from a flat age1 sequence, not a YAML library', () {
      const yaml =
          '''
creation_rules:
  - path_regex: .*
    age: >-
      $_r1,
      $_r2
    key_groups:
      - age:
        - $_r1
        - $_r2
''';
      expect(parseRecipients(yaml), [_r1, _r2]);
    });

    test('fewer than two recipients fails (#3976)', () {
      expect(checkRecipientCount([_r1]), isA<Fail>());
      expect(checkRecipientCount([_r1, _r2]), isA<Pass>());
    });

    test('an ADDED recipient fails — not just a missing one', () {
      // Tesla's catch, PR#166: the recipient list is the one cleartext invariant
      // a reviewer can check without decrypting. Checking only for PRESENCE is
      // blind to a silent third wrap, which grants full plaintext and is
      // invisible in a binary diff.
      final out = checkRecipientSetExact(
        file: 'x.env.sops',
        want: [_r1, _r2],
        found: [_r1, _r2, _r3],
      );
      expect(out.whereType<Fail>(), hasLength(1));
      expect(out.whereType<Fail>().single.reason, contains('UNAUTHORISED'));
    });

    test('a MISSING required recipient fails', () {
      final out = checkRecipientSetExact(
        file: 'x.env.sops',
        want: [_r1, _r2],
        found: [_r1],
      );
      expect(out.whereType<Fail>().single.reason, contains('MISSING'));
    });

    test('exactly the policy set passes', () {
      final out = checkRecipientSetExact(
        file: 'x.env.sops',
        want: [_r1, _r2],
        found: [_r2, _r1],
      );
      expect(out.whereType<Fail>(), isEmpty);
    });
  });

  group('continuity against origin/main', () {
    test('unavailable main is SKIPPED, never passed', () {
      final r = checkRecipientContinuity(
        here: [_r1],
        onMain: null,
        rotationAllowed: false,
      );
      expect(r, isA<Skipped>());
    });

    test('a differing set fails unless rotation is declared', () {
      expect(
        checkRecipientContinuity(
          here: [_r1, _r3],
          onMain: [_r1, _r2],
          rotationAllowed: false,
        ),
        isA<Fail>(),
      );
      expect(
        checkRecipientContinuity(
          here: [_r1, _r3],
          onMain: [_r1, _r2],
          rotationAllowed: true,
        ),
        isA<Pass>(),
      );
    });

    test('order does not matter', () {
      expect(
        checkRecipientContinuity(
          here: [_r2, _r1],
          onMain: [_r1, _r2],
          rotationAllowed: false,
        ),
        isA<Pass>(),
      );
    });
  });

  group('the silence bugs, each pinned', () {
    test('a RENAMED secret is caught by the directory allowlist', () {
      // The original: renaming to `*.enc.env` made the glob match nothing, so it
      // printed "nothing to verify" and exited 0 — the checker going silent
      // exactly when its target went missing.
      final out = checkDirectoryContents([
        'README.md',
        'MANIFEST.txt',
        'enspyr.enc.env',
      ]);
      expect(out.whereType<Fail>().single.reason, contains('enspyr.enc.env'));
    });

    test(
      'DELETING every secrets file fails when the manifest names islands',
      () {
        // The sibling case: an earlier revision exited 0 here with a lullaby, so a
        // PR removing the only recovery copy kept CI green.
        final out = checkNoFilesIsLegitimate(['enspyr', 'imagineering']);
        expect(out.whereType<Fail>(), hasLength(2));
      },
    );

    test('a genuinely un-lifted repo may legitimately have nothing', () {
      expect(checkNoFilesIsLegitimate([]).whereType<Fail>(), isEmpty);
    });
  });

  group('envelope', () {
    test('a valid envelope yields its recipients from the SCHEMA', () {
      final e = parseEnvelope(_envelope()) as ValidEnvelope;
      expect(e.recipients, [_r1, _r2]);
      expect(e.dataIsEncrypted, isTrue);
      expect(e.version, '3.11.0');
    });

    test('non-JSON is InvalidEnvelope, not an exception', () {
      expect(parseEnvelope('not json at all'), isA<InvalidEnvelope>());
    });

    test('a missing required field names WHICH', () {
      final e = parseEnvelope(_envelope(omitMac: true)) as InvalidEnvelope;
      expect(e.reason, contains('mac'));
    });

    test('an empty age list is refused', () {
      expect(parseEnvelope(_envelope(recipients: [])), isA<InvalidEnvelope>());
    });

    test('CLEARTEXT data with a valid envelope is detected', () {
      // Presence of the envelope is not proof of encryption.
      final e = parseEnvelope(_envelope(data: 'PLAIN=secret')) as ValidEnvelope;
      expect(e.dataIsEncrypted, isFalse);
    });

    test('recipients come from sops.age[], NOT a regex over the file', () {
      // A regex could match an age1-shaped substring inside the base64 payload —
      // "a leaky calorimeter reading the container instead of the contents".
      final withDecoy = _envelope(data: 'ENC[AES256_GCM,data:$_r3,type:str]');
      final e = parseEnvelope(withDecoy) as ValidEnvelope;
      expect(e.recipients, [
        _r1,
        _r2,
      ], reason: 'the payload decoy must not leak in');
    });
  });

  group('plaintext markers', () {
    for (final marker in plaintextMarkers) {
      test('detects $marker', () {
        expect(checkNoPlaintextMarkers('f', 'xx $marker yy'), isA<Fail>());
      });
    }
    test('clean ciphertext passes', () {
      expect(checkNoPlaintextMarkers('f', _envelope()), isA<Pass>());
    });
  });

  group('manifest', () {
    test('sections parsed', () {
      expect(parseManifestIslands('[enspyr]\nA\nB\n\n[img]\nC\n'), [
        'enspyr',
        'img',
      ]);
    });

    test('a file with no manifest section fails', () {
      final out = checkManifestCoverage(
        islandsWithFiles: ['enspyr'],
        manifestIslands: [],
        manifestBody: '',
      );
      expect(out.whereType<Fail>().single.reason, contains('no [enspyr]'));
    });

    test('a manifest section with no file fails', () {
      final out = checkManifestCoverage(
        islandsWithFiles: [],
        manifestIslands: ['ghost'],
        manifestBody: '[ghost]\n',
      );
      expect(out.whereType<Fail>().single.reason, contains('does not exist'));
    });

    test("a manifest containing '=' fails — names only, never values", () {
      final out = checkManifestCoverage(
        islandsWithFiles: ['e'],
        manifestIslands: ['e'],
        manifestBody: '[e]\nJWT_SECRET=oops\n',
      );
      expect(out.whereType<Fail>(), isNotEmpty);
    });
  });
}
