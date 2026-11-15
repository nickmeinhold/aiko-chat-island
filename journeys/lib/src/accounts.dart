/// Signed-in callers without a sign-in: an account written straight into the
/// gateway's database, and tokens minted with the secret the harness gave it.
///
/// The only production door to a token is a passkey ceremony, and a journey that
/// isn't ABOUT signing in (profile, read positions, the refresh rotation) only
/// needs a caller who has done one. A token is an HS256 JWT over `sub`, `type`,
/// `gen`, `iat`, `exp` (plus `sid` and `jti` on a refresh token since #420), and
/// the gateway accepts one when the signature holds and the user's row exists at
/// that generation (`domain/security.py`, `auth_session.resolve_session_user`).
/// The harness chose the secret, so it can mint what a sign-in would have.
///
/// This is the one place a journey reaches past the wire: the row is written to
/// the table alembic built, so the harness depends on that table's layout. The
/// Dart gateway must read that same layout anyway (DESIGN.md, "Stored bytes"),
/// and a journey that mints for an UNSEEDED id is refused, which proves the
/// gateway really reads the row rather than trusting any well-signed token.
library;

import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:sqlite3/sqlite3.dart';

import 'gateway.dart';

final _random = Random.secure();

final class Account {
  Account._(this.id, this.handle);

  /// `users.id`, the token's `sub`.
  final String id;
  final String handle;

  /// Writes a human account with [handle] into [gateway]'s database, with the
  /// columns a sign-up fills and the rest at their schema defaults
  /// (generation 0, not banned, no handle change yet).
  ///
  /// [handleAssigned] is what `register/finish` stores for the handle it made
  /// up (alembic 0030): `handle_auto` reads it, and the first change away from
  /// it doesn't start the cooldown. False is an account whose handle was chosen.
  factory Account.seed(
    PythonGateway gateway,
    String handle, {
    bool handleAssigned = false,
  }) {
    final account = Account._(_ulidLike(), handle);
    final db = sqlite3.open(gateway.database);
    try {
      db.execute(
        'INSERT INTO users (id, username, display_name, aiko_username, '
        'created_at, handle_assigned) VALUES (?, ?, ?, ?, ?, ?)',
        [
          account.id,
          handle,
          handle,
          handle,
          _stored(DateTime.now().toUtc()),
          handleAssigned ? 1 : 0,
        ],
      );
    } finally {
      db.close();
    }
    return account;
  }

  /// An id of the right length that no seeded row holds.
  static String unseededId() => _ulidLike();
}

/// Mints tokens the way `domain/security.py` does, under [secret].
final class Minter {
  Minter(this.secret);

  final String secret;

  /// An access token for [sub], valid for [ttl] from [now].
  String access(
    String sub, {
    int gen = 0,
    Duration ttl = const Duration(minutes: 15),
    DateTime? now,
  }) => _jwt({'sub': sub, 'type': 'access', 'gen': gen, ..._window(now, ttl)});

  /// A refresh token for [sub] naming session [sid] and itself [jti], as a
  /// sign-in mints one (both fresh unless given). [legacy] leaves both claims
  /// out, as every refresh token minted before #420 did.
  String refresh(
    String sub, {
    int gen = 0,
    String? sid,
    String? jti,
    bool legacy = false,
    Duration ttl = const Duration(days: 30),
    DateTime? now,
  }) => _jwt({
    if (!legacy) 'sid': sid ?? tokenId(),
    if (!legacy) 'jti': jti ?? tokenId(),
    'sub': sub,
    'type': 'refresh',
    'gen': gen,
    ..._window(now, ttl),
  });

  String _jwt(Map<String, Object> claims) {
    final signed =
        '${_b64(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))}'
        '.${_b64(utf8.encode(jsonEncode(claims)))}';
    final mac = Hmac(sha256, utf8.encode(secret)).convert(utf8.encode(signed));
    return '$signed.${_b64(mac.bytes)}';
  }
}

/// 128 random bits, URL-safe, as `security.new_token_id` makes them.
String tokenId() => _b64([for (var i = 0; i < 16; i++) _random.nextInt(256)]);

/// The claims of a JWT, read without checking its signature: a journey asks
/// which session or token id the gateway put in one, not whether it's valid.
Map<String, Object?> claimsOf(String jwt) => jsonDecode(
  utf8.decode(base64Url.decode(base64Url.normalize(jwt.split('.')[1]))),
) as Map<String, Object?>;

Map<String, int> _window(DateTime? now, Duration ttl) {
  final at = now ?? DateTime.now();
  return {'iat': _seconds(at), 'exp': _seconds(at.add(ttl))};
}

int _seconds(DateTime t) => t.millisecondsSinceEpoch ~/ 1000;

String _b64(List<int> bytes) => base64Url.encode(bytes).replaceAll('=', '');

/// SQLAlchemy's SQLite DATETIME text, UTC with the zone dropped
/// (`domain/types.py` `UtcDateTime`).
String _stored(DateTime utc) {
  String two(int n) => '$n'.padLeft(2, '0');
  return '${utc.year}-${two(utc.month)}-${two(utc.day)} '
      '${two(utc.hour)}:${two(utc.minute)}:${two(utc.second)}'
      '.${'${utc.microsecond + utc.millisecond * 1000}'.padLeft(6, '0')}';
}

const _crockford = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

/// A ULID's spelling (its first character is at most `7`: 128 bits in 26
/// base-32 digits), random throughout rather than time-ordered.
String _ulidLike() => [
  _crockford[_random.nextInt(8)],
  for (var i = 1; i < 26; i++) _crockford[_random.nextInt(32)],
].join();
