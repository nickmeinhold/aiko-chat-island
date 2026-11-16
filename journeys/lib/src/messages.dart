/// Messages without the bus: sent over `/v1/ws` into a DM.
///
/// No REST route posts a message, and a public channel is born on the bus, which
/// the harness points at a dead port. A DM is neither: `POST /v1/dm` creates it
/// (a self-DM is allowed, notes-to-self), and a send into it is persisted and
/// acked without federating (`messages_service.should_federate` is false for a
/// DM). So a journey that needs a channel with messages in it, and isn't about
/// the bus, gets one through public doors only.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

import 'gateway.dart';

/// Opens (finds or creates) the DM between the bearer of [token] and [target],
/// and returns its channel id. `/v1/dm` is rate-limited per client IP, so a
/// test passes its own [forwardedFor] (`PythonGateway.newClient`).
Future<String> openDm(
  http.Client client,
  PythonGateway gateway, {
  required String token,
  required String target,
  required String forwardedFor,
}) async {
  final res = await client.post(
    gateway.base.resolve('/v1/dm'),
    headers: {
      'authorization': 'Bearer $token',
      'content-type': 'application/json',
      'x-forwarded-for': forwardedFor,
    },
    body: jsonEncode({'target_user_id': target}),
  );
  if (res.statusCode != 200) {
    throw StateError('POST /v1/dm → ${res.statusCode}: ${res.body}');
  }
  return (jsonDecode(res.body) as Map<String, Object?>)['channel_id']!
      as String;
}

/// Sends [bodies] into [channelId], one at a time, and returns the server ids
/// the acks name, in order (so ascending: the server mints ULIDs).
Future<List<String>> send(
  PythonGateway gateway, {
  required String token,
  required String channelId,
  required List<String> bodies,
}) async {
  final ws = await WebSocket.connect(
    gateway.base
        .replace(
          scheme: 'ws',
          path: '/v1/ws',
          queryParameters: {'token': token},
        )
        .toString(),
  );
  final frames = StreamIterator(
    ws.map((f) => jsonDecode(f as String) as Map<String, Object?>),
  );
  try {
    final ids = <String>[];
    for (final (i, body) in bodies.indexed) {
      final cmid = 'journey-$i-${DateTime.now().microsecondsSinceEpoch}';
      ws.add(
        jsonEncode({
          'type': 'send',
          'client_msg_id': cmid,
          'channel_id': channelId,
          'body': body,
        }),
      );
      while (true) {
        if (!await frames.moveNext().timeout(const Duration(seconds: 10))) {
          throw StateError('the socket closed before acking "$body"');
        }
        final f = frames.current;
        if (f['client_msg_id'] != cmid) continue; // not ours (a fanout)
        if (f['type'] != 'ack') throw StateError('send "$body" → $f');
        ids.add(f['msg_id']! as String);
        break;
      }
    }
    return ids;
  } finally {
    await frames.cancel();
    await ws.close();
  }
}
