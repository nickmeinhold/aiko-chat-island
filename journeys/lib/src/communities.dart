/// Communities without a create door: rows written straight into the gateway's
/// database, as an operator would.
///
/// No route creates a community, and no code path does either: the `Community`
/// model says user-created communities are deferred, so the only community a
/// fresh island holds is the default "Aiko" the migrations seed. A journey that
/// reads, lists and joins communities (17) needs others, so the harness writes
/// them, with their channels and memberships, to the tables alembic built.
///
/// This is the harness's second reach past the wire, after `accounts.dart`, and
/// for the same reason. It goes further: a community's channels are normally
/// born on the bus, so this also depends on the channel tables' layout, which
/// the Dart gateway must read anyway (DESIGN.md, "Stored bytes").
library;

import 'package:sqlite3/sqlite3.dart';

import 'accounts.dart';
import 'gateway.dart';

/// A channel to seed inside a community. A private one is readable only by
/// its [members].
final class ChannelSeed {
  const ChannelSeed(this.name, {this.private = false, this.members = const []});

  final String name;
  final bool private;
  final List<Account> members;
}

final class Community {
  Community._(this.id, this.name, this.channelIds);

  final String id;
  final String name;

  /// The seeded channels' ids, in the order their seeds were given.
  final List<String> channelIds;

  /// Writes a community named [name] into [gateway]'s database, with
  /// [members] in it (`member_count` is their number unless [memberCount]
  /// says otherwise) and one `standard` channel per [channels]. The first
  /// channel is the default one, as a community's landing channel.
  factory Community.seed(
    PythonGateway gateway,
    String name, {
    String visibility = 'public',
    String category = 'general',
    List<Account> members = const [],
    int? memberCount,
    bool takenDown = false,
    List<ChannelSeed> channels = const [],
  }) {
    final now = storedTime(DateTime.now().toUtc());
    final community = Community._(ulidLike(), name, [
      for (final _ in channels) ulidLike(),
    ]);
    final db = sqlite3.open(gateway.database);
    try {
      db.execute('BEGIN');
      db.execute(
        'INSERT INTO communities (id, name, visibility, category, '
        'member_count, default_channel_id, taken_down_at, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [
          community.id,
          name,
          visibility,
          category,
          memberCount ?? members.length,
          community.channelIds.firstOrNull,
          takenDown ? now : null,
          now,
        ],
      );
      for (final m in members) {
        db.execute(
          'INSERT INTO community_memberships (community_id, user_id, role, '
          'joined_at) VALUES (?, ?, ?, ?)',
          [community.id, m.id, 'member', now],
        );
      }
      for (final (i, ch) in channels.indexed) {
        final id = community.channelIds[i];
        db.execute(
          'INSERT INTO channels (id, name, kind, aiko_channel, is_private, '
          'join_policy, created_at, community_id) '
          'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
          [
            id,
            ch.name,
            'standard',
            'journey-$id',
            ch.private ? 1 : 0,
            ch.private ? 'invite_only' : 'open',
            now,
            community.id,
          ],
        );
        for (final m in ch.members) {
          db.execute(
            'INSERT INTO memberships (channel_id, user_id, role, can_post, '
            'joined_at) VALUES (?, ?, ?, ?, ?)',
            [id, m.id, 'member', 1, now],
          );
        }
      }
      db.execute('COMMIT');
    } finally {
      db.close();
    }
    return community;
  }
}
