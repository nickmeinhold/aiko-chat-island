/// A `.env` parser, and an honest account of what parser fidelity can mean here.
///
/// THE CLAIM THE PYTHON VERSION MADE, AND WHY IT IS NOT INHERITED. The script
/// this replaces used `python-dotenv` deliberately, arguing that parsing with
/// "the SAME library the gateway parses with" made an agreement here mean what
/// it means at boot. That argument is weaker than it reads, and the same
/// docstring half-conceded it ("Compose's interpolation parser is not this
/// parser").
///
/// MEASURED: the file being compared is the HOST `.env`, and the host `.env` is
/// read by **docker compose**, not by the gateway's Python. The container gets
/// its values through explicit `VAR: ${VAR}` forwarding in `docker-compose.yml`
/// — which is the whole reason `concept_inert_var_config_not_forwarded` exists
/// as a defect class. `pydantic-settings` then parses the CONTAINER's
/// environment. So `python-dotenv` was never the authoritative reader of this
/// file, and swapping it out does not surrender a guarantee that existed.
///
/// WHAT ACTUALLY KEEPS THE COMPARISON HONEST is that BOTH sides go through this
/// one parser. A systematic divergence from python-dotenv shifts both sides
/// equally and cancels. The residual risk is narrower and worth naming plainly:
/// a parser bug that DROPS OR MERGES a key on one side but not the other, which
/// requires the two files to differ in SYNTAX rather than in values. That is
/// what this file's tests aim at, rather than at general dotenv conformance.
///
/// THE HARD CASE IS REAL AND IS TESTED. `APNS_PRIVATE_KEY` is a PEM holding
/// actual newlines inside double quotes. It is why SOPS's own dotenv parser
/// rejects these files and why they are stored as binary blobs
/// (`deploy/secrets/*.env.sops`). A parser that ends a value at the first
/// newline would silently split one key into a key plus several garbage lines —
/// the exact drop-a-key-on-one-side failure above.
///
/// SCOPE, stated rather than discovered: no interpolation, no `${VAR}`
/// expansion, no `export` semantics beyond stripping the word. This tool
/// compares two files for equality; it does not evaluate them. Adding
/// interpolation would make it a second implementation of Compose's evaluator,
/// which is a larger claim than "these two files agree".
library;

/// Everything that can stop a parse producing a comparable map.
class DotenvParseException implements Exception {
  DotenvParseException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Parse `text` into key -> value.
///
/// [where] names the source in any error, because "parsed zero keys" is useless
/// without knowing which side it happened on.
Map<String, String> parseDotenv(String text, String where) {
  final out = <String, String>{};
  final runes = text.split('\n');
  var i = 0;

  while (i < runes.length) {
    var line = runes[i];
    i++;

    final trimmed = line.trimLeft();
    if (trimmed.isEmpty || trimmed.startsWith('#')) continue;

    var body = trimmed;
    if (body.startsWith('export ')) body = body.substring(7).trimLeft();

    final eq = body.indexOf('=');
    // A line with no '=' is not a comment and not a pair. Skipping it silently
    // is how a malformed file reads as a shorter one; the caller's zero-key
    // guard would not catch a single dropped line.
    if (eq < 0) {
      throw DotenvParseException(
        '$where: line $i: no "=" and not a comment: ${_elide(body)}',
      );
    }

    final key = body.substring(0, eq).trimRight();
    if (key.isEmpty || !_validKey(key)) {
      throw DotenvParseException(
        '$where: line $i: not a valid key: ${_elide(key)}',
      );
    }

    var rest = body.substring(eq + 1);
    final String value;

    if (rest.startsWith('"') || rest.startsWith("'")) {
      final quote = rest[0];
      // MULTI-LINE QUOTED VALUE — the PEM case. Keep consuming lines until the
      // closing quote. Without this the value ends at the first newline and the
      // PEM's remaining lines become bogus "no =" lines, which is the
      // drop-a-key-on-one-side failure this parser is written to avoid.
      final buf = StringBuffer();
      var scan = rest.substring(1);
      var closed = false;
      while (true) {
        final end = _unescapedQuote(scan, quote);
        if (end >= 0) {
          buf.write(scan.substring(0, end));
          closed = true;
          break;
        }
        buf.write(scan);
        if (i >= runes.length) break;
        buf.write('\n');
        scan = runes[i];
        i++;
      }
      if (!closed) {
        throw DotenvParseException(
          '$where: unterminated $quote-quoted value for $key',
        );
      }
      value = quote == '"' ? _unescapeDouble(buf.toString()) : buf.toString();
    } else {
      // Unquoted: strip a trailing inline comment only when whitespace precedes
      // the '#'. `URL=http://x#frag` is one value, not a value plus a comment.
      var v = rest;
      final hash = _inlineCommentStart(v);
      if (hash >= 0) v = v.substring(0, hash);
      value = v.trim();
    }

    // LAST WINS, matching both dotenv and Compose. Recording it as a collision
    // instead would be a different tool; silently keeping the FIRST would make
    // this parser disagree with the box about which value is live.
    out[key] = value;
  }

  if (out.isEmpty) {
    throw DotenvParseException(
      'parsed zero keys from $where — refusing to call that a match',
    );
  }
  return out;
}

bool _validKey(String k) => RegExp(r'^[A-Za-z_][A-Za-z0-9_.]*$').hasMatch(k);

/// Index of the first `#` that starts an inline comment, or -1.
int _inlineCommentStart(String v) {
  for (var i = 0; i < v.length; i++) {
    if (v[i] != '#') continue;
    if (i == 0) return 0;
    final prev = v[i - 1];
    if (prev == ' ' || prev == '\t') return i;
  }
  return -1;
}

/// Index of the next [quote] not preceded by a backslash, or -1.
int _unescapedQuote(String s, String quote) {
  for (var i = 0; i < s.length; i++) {
    if (s[i] != quote) continue;
    if (i > 0 && s[i - 1] == r'\') continue;
    return i;
  }
  return -1;
}

String _unescapeDouble(String s) => s
    .replaceAll(r'\n', '\n')
    .replaceAll(r'\t', '\t')
    .replaceAll(r'\"', '"')
    .replaceAll(r'\\', r'\');

String _elide(String s) => s.length <= 60 ? s : '${s.substring(0, 60)}…';
