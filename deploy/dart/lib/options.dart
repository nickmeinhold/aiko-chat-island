/// The operator's values, checked ONCE, when they are made.
///
/// Each type below can only be constructed from a value it has already validated, so
/// a command holding one never needs to ask "was this checked?". That is the door
/// Carnot asked for in PR#406 rounds 2 and 3: `standup` used to validate a bag of
/// strings at the parser and again at `run`, and any third caller could have skipped
/// both. Now there is nothing to skip.
///
/// Values are rendered for `.env` ONLY by [EnvValue.dotenv]. Peers are typed records
/// until that moment, never a string passed around and re-parsed.
///
/// THE SEED-PEER RULES MIRROR THE GATEWAY'S, ON PURPOSE. The gateway's
/// `coerce_island` (src/aiko_gateway/domain/peers_service.py) DROPS a peer it does
/// not like, silently: no error, no log line, a green health check, and an island
/// that is simply not in the picker. So this tool refuses everything that function
/// would drop, plus what it would quietly rewrite (an uppercase id, a trailing
/// slash), so what the operator wrote is what the gateway serves. The rules are
/// stricter where Dart and Python regexes differ (`\w` is ASCII here). A shared
/// vector file (test/fixtures/seed_peers.json) is run against BOTH, so the two can't
/// drift without a test going red.
library;

import 'dart:convert';

import 'box.dart';

/// A value that knows how to appear on the right of `KEY=` in `.env`.
abstract interface class EnvValue {
  /// Exactly the text after `=`, quoting included.
  String get dotenv;

  /// The value the gateway receives (after compose reads `.env`).
  String get value;
}

final _host = RegExp(
  r'^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$',
);

/// A bare public hostname: `chat.example.org`.
final class Domain {
  Domain(this.host) {
    if (!_host.hasMatch(host)) {
      throw Refusal(
        '--domain must be a bare hostname like chat.example.org (no scheme, '
        'port or path): $host',
      );
    }
  }
  final String host;
  String get baseUrl => 'https://$host';
  @override
  String toString() => host;
}

/// The longest name another island will accept for a peer (`MAX_NAME_LEN`). An
/// island's own name travels in OTHER islands' seed lists, so it obeys the same cap.
const maxNameLength = 64;

/// Checks shared by an island's own name and a peer's name.
void _checkName(String what, String v) {
  if (v.trim().isEmpty) throw Refusal('$what is empty');
  if (v.trim() != v) {
    throw Refusal('$what has leading or trailing spaces: "$v"');
  }
  if (v.length > maxNameLength) {
    throw Refusal(
      '$what is ${v.length} characters; islands drop names over '
      '$maxNameLength',
    );
  }
  if (v.codeUnits.any((c) => c < 0x20 || c == 0x7f)) {
    throw Refusal('$what must be one line with no control characters');
  }
}

/// The island's human label, written single-quoted.
///
/// Single quotes make dotenv and compose take the value literally: a ` #` can't
/// truncate it and a `$` can't interpolate. The one character single quotes can't
/// carry is the quote itself, so it is refused, not mangled (deploy/standup.sh,
/// #3734). Compose's parser also treats a backslash before the closing quote as an
/// escape (measured: `F='ends with \'` is "unterminated"), so a name ending in `\`
/// would swallow the lines after it (Carnot, PR#406 r1). Rather than reason about
/// which positions are safe, a name has no backslash at all.
final class DisplayName implements EnvValue {
  DisplayName(this.value) {
    _checkName('--name', value);
    if (value.contains("'")) {
      throw Refusal(
        "--name: a single quote (') can't be represented in the island's .env",
      );
    }
    if (value.contains(r'\')) {
      throw Refusal(r"--name can't contain a backslash (\)");
    }
  }
  @override
  final String value;
  @override
  String get dotenv => "'$value'";
  @override
  String toString() => value;
}

final _peerId = RegExp(r'^[a-z0-9][a-z0-9-]{0,63}$');
// peers_service.py `_HTTPS_RE`, with `\w` spelled out as the ASCII it means here.
final _peerUrl = RegExp(
  r'^https://[a-zA-Z0-9.-]+(:\d+)?(/[A-Za-z0-9_./~-]*)?$',
);
const _maxUrlLength = 255;

/// One island this island lists as a peer.
final class SeedPeer {
  SeedPeer({
    required this.id,
    required this.displayName,
    required this.baseUrl,
  }) {
    if (!_peerId.hasMatch(id)) {
      throw Refusal(
        '--seed-peers: id "$id" must be a lowercase slug (a-z, 0-9, -; at most '
        '64, not starting with -). The gateway drops any other id without a word.',
      );
    }
    _checkName('--seed-peers: display_name of "$id"', displayName);
    if (baseUrl.length > _maxUrlLength || !_peerUrl.hasMatch(baseUrl)) {
      throw Refusal(
        '--seed-peers: base_url of "$id" must be https://host[:port][/path], '
        'at most $_maxUrlLength characters: $baseUrl',
      );
    }
    if (baseUrl.endsWith('/')) {
      throw Refusal(
        '--seed-peers: base_url of "$id" ends in "/"; write it without one: '
        '${baseUrl.replaceFirst(RegExp(r'/+$'), '')}',
      );
    }
  }

  /// From one decoded JSON entry. Only the three canonical keys: a typo such as
  /// `"baseurl"` or the legacy camelCase spelling is refused, not ignored.
  factory SeedPeer.fromJson(Object? j) {
    const keys = {'id', 'display_name', 'base_url'};
    if (j is! Map ||
        !j.keys.toSet().containsAll(keys) ||
        j.keys.any((k) => !keys.contains(k)) ||
        j.values.any((v) => v is! String)) {
      throw Refusal(
        '--seed-peers: every entry needs exactly the string fields "id", '
        '"display_name" and "base_url": ${jsonEncode(j)}',
      );
    }
    return SeedPeer(
      id: j['id'] as String,
      displayName: j['display_name'] as String,
      baseUrl: j['base_url'] as String,
    );
  }

  final String id;
  final String displayName;
  final String baseUrl;

  Map<String, String> toJson() => {
    'id': id,
    'display_name': displayName,
    'base_url': baseUrl,
  };
}

/// The islands this island lists, in order. `[]` is a solo island.
final class SeedPeers implements EnvValue {
  SeedPeers(Iterable<SeedPeer> peers) : peers = List.unmodifiable(peers) {
    // The gateway keeps the FIRST of two entries with one id or one base_url and
    // drops the other without a word. Refused here so the list means what it says.
    final ids = <String>{}, urls = <String>{};
    for (final p in peers) {
      if (!ids.add(p.id)) {
        throw Refusal('--seed-peers lists id "${p.id}" twice');
      }
      if (!urls.add(p.baseUrl)) {
        throw Refusal('--seed-peers lists ${p.baseUrl} twice');
      }
    }
  }

  const SeedPeers.none() : peers = const [];

  /// From the operator's `--seed-peers` JSON.
  factory SeedPeers.parse(String json) {
    final Object? decoded;
    try {
      decoded = jsonDecode(json);
    } on FormatException catch (e) {
      throw Refusal('--seed-peers is not valid JSON: ${e.message}');
    }
    if (decoded is! List) throw Refusal('--seed-peers must be a JSON array');
    return SeedPeers(decoded.map(SeedPeer.fromJson));
  }

  final List<SeedPeer> peers;

  /// Refuse a list naming the island itself: the gateway drops it ("self is
  /// immutable"), so the operator would see one peer fewer than they wrote.
  void refuseSelf(String ownBaseUrl) {
    for (final p in peers) {
      if (p.baseUrl == ownBaseUrl) {
        throw Refusal(
          '--seed-peers lists this island itself (${p.baseUrl}); an island '
          'never lists itself as a peer',
        );
      }
    }
  }

  /// Compact JSON, the same on every run, so an unchanged list renders unchanged.
  /// A `'` inside a name is written as the JSON escape `\u0027`: the same list once
  /// decoded, and the one character single quotes can't carry. This escaped string IS
  /// what the gateway receives, so it is the value (an earlier version escaped only
  /// in [dotenv], and configure's own read-back refused: value and file disagreed).
  @override
  String get value {
    // Spelled as two pieces so no tool on the way can decode the escape into the
    // very quote it replaces (one did, while this line was being written).
    return jsonEncode(peers).replaceAll(
      "'",
      r'\'
          'u0027',
    );
  }

  /// Single-quoted. JSON can't end in a backslash (it ends in `]`), so compose can't
  /// read the closing quote as escaped.
  @override
  String get dotenv => "'$value'";

  @override
  String toString() => value;
}

/// The passkey switch, written as a bare `true` or `false`.
final class PasskeysEnabled implements EnvValue {
  const PasskeysEnabled(this.enabled);
  final bool enabled;
  @override
  String get value => '$enabled';
  @override
  String get dotenv => '$enabled';
}

final _tag = RegExp(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$');

/// A release's image tag, without the leading `v`.
final class ImageTag {
  ImageTag(this.tag) {
    if (RegExp(r'^v\d').hasMatch(tag)) {
      throw Refusal(
        '--version $tag: image tags have no leading v (use ${tag.substring(1)})',
      );
    }
    if (!_tag.hasMatch(tag)) {
      throw Refusal('--version $tag is not a valid image tag');
    }
  }
  final String tag;
  @override
  String toString() => tag;
}
