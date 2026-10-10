/// Parser tests, aimed at the ONE failure mode that can make this tool lie.
///
/// Not general dotenv conformance. Both sides of the comparison go through this
/// same parser, so a systematic divergence from python-dotenv shifts both sides
/// equally and cancels. What does NOT cancel is a parse that drops or merges a
/// key on one side but not the other — which needs the two files to differ in
/// SYNTAX. Every test here is a syntax shape that could do that.
library;

import 'package:island_deploy/dotenv.dart';
import 'package:test/test.dart';

/// The real shape of `APNS_PRIVATE_KEY`: a PEM with ACTUAL newlines inside
/// double quotes. This is why SOPS's own dotenv parser rejects these files and
/// why `deploy/secrets/*.env.sops` are stored as binary blobs.
const _pemEnv = '''
ISLAND_ID=enspyr
APNS_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----
MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg
-----END PRIVATE KEY-----"
APNS_KEY_ID=ABC123
''';

void main() {
  group('multi-line quoted values', () {
    test('a PEM with real newlines stays ONE key', () {
      final env = parseDotenv(_pemEnv, 'test');
      // THE ASSERTION THAT MATTERS: three keys, not three-plus-garbage. A parser
      // that ended the value at the first newline would leave the PEM's
      // remaining lines looking like malformed pairs — and the key count on this
      // side would no longer match the other side's.
      expect(env.keys, ['ISLAND_ID', 'APNS_PRIVATE_KEY', 'APNS_KEY_ID']);
      expect(env['APNS_PRIVATE_KEY'], contains('\n'));
      expect(env['APNS_PRIVATE_KEY'], startsWith('-----BEGIN'));
      expect(env['APNS_PRIVATE_KEY'], endsWith('-----END PRIVATE KEY-----'));
      // And the keys AFTER it are still reachable — the failure mode is not
      // just a broken value, it is everything downstream of it going missing.
      expect(env['APNS_KEY_ID'], 'ABC123');
    });

    test('an unterminated quote is COULD-NOT-RUN, never a short parse', () {
      expect(
        () => parseDotenv('A=1\nB="oops\n', 'test'),
        throwsA(isA<DotenvParseException>()),
      );
    });

    test('single quotes do not unescape', () {
      final env = parseDotenv(r"A='a\nb'", 'test');
      expect(env['A'], r'a\nb');
    });

    test('double quotes do unescape', () {
      final env = parseDotenv(r'A="a\nb"', 'test');
      expect(env['A'], 'a\nb');
    });
  });

  group('lines that could silently vanish', () {
    test('a line with no "=" raises rather than being skipped', () {
      // Skipping it is how a malformed file reads as a shorter one, and the
      // zero-key guard cannot catch a SINGLE dropped line.
      expect(
        () => parseDotenv('A=1\ngarbage line\n', 'test'),
        throwsA(isA<DotenvParseException>()),
      );
    });

    test('comments and blank lines are skipped, indented ones too', () {
      final env = parseDotenv('# c\n\n   # indented\nA=1\n', 'test');
      expect(env, {'A': '1'});
    });

    test('export prefix is stripped', () {
      expect(parseDotenv('export A=1', 'test'), {'A': '1'});
    });

    test('an invalid key raises', () {
      expect(
        () => parseDotenv('9BAD=1', 'test'),
        throwsA(isA<DotenvParseException>()),
      );
    });

    test('zero keys is refused, not returned empty', () {
      expect(
        () => parseDotenv('# only a comment\n', 'test'),
        throwsA(isA<DotenvParseException>()),
      );
    });
  });

  group('inline comments', () {
    test('a # after whitespace starts a comment', () {
      expect(parseDotenv('A=1 # note', 'test')['A'], '1');
    });

    test('a # with no preceding whitespace is part of the value', () {
      // `URL=http://x#frag` is one value. Treating it as a comment would drop
      // the fragment on one side if only one side wrote it that way.
      expect(parseDotenv('A=http://x#frag', 'test')['A'], 'http://x#frag');
    });

    test('a # inside quotes is never a comment', () {
      expect(parseDotenv('A="a # b"', 'test')['A'], 'a # b');
    });
  });

  group('duplicate keys', () {
    test('last wins, matching dotenv and Compose', () {
      // Keeping the FIRST would make this parser disagree with the box about
      // which value is actually live.
      expect(parseDotenv('A=1\nA=2\n', 'test')['A'], '2');
    });
  });

  group('whitespace', () {
    test('unquoted values are trimmed', () {
      expect(parseDotenv('A=  1  ', 'test')['A'], '1');
    });

    test('quoted values are NOT trimmed', () {
      expect(parseDotenv('A="  1  "', 'test')['A'], '  1  ');
    });

    test('an empty value is a value, not an absence', () {
      // An absent key and a key set to empty are different facts about the box,
      // and collapsing them would report drift that is not there (or hide drift
      // that is).
      final env = parseDotenv('A=\nB=1\n', 'test');
      expect(env.containsKey('A'), isTrue);
      expect(env['A'], '');
    });
  });
}
