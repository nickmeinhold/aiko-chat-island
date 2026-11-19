/// Ed25519 Multikeys for journeys that publish signing keys: `z` +
/// base58btc(0xed01 ‖ 32 raw bytes), the shape `signing.decode_multikey`
/// admits. The gateway checks the shape only, never that the bytes are a curve
/// point, so random bytes make a key it accepts.
library;

import 'dart:math';

const _alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const _ed25519 = [0xed, 0x01];
final _random = Random.secure();

/// A fresh, never-seen key.
String newMultikey() =>
    multikey(List.generate(32, (_) => _random.nextInt(256)));

/// The Multikey spelling of [raw], with the ed25519 multicodec prefix (or
/// [prefix], to spell a key the gateway must refuse).
String multikey(List<int> raw, {List<int> prefix = _ed25519}) =>
    'z${base58(<int>[...prefix, ...raw])}';

/// base58btc, keeping leading zero bytes as leading `1`s.
String base58(List<int> bytes) {
  var n = BigInt.zero;
  for (final b in bytes) {
    n = (n << 8) | BigInt.from(b);
  }
  final out = <String>[];
  final base = BigInt.from(58);
  while (n > BigInt.zero) {
    out.add(_alphabet[(n % base).toInt()]);
    n ~/= base;
  }
  final zeros = bytes.takeWhile((b) => b == 0).length;
  return '1' * zeros + out.reversed.join();
}
