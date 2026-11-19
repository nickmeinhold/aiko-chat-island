/// A `/v1/ws` connection a journey steers frame by frame, for what `send()` in
/// `messages.dart` hides: a chosen `client_msg_id` (a retry), a refusal, a
/// subscribe's fences, and the live frames a channel fans out.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'gateway.dart';

typedef Frame = Map<String, Object?>;

final class Live {
  Live._(this._ws, this._frames);

  final WebSocket _ws;
  final StreamIterator<Frame> _frames;

  static Future<Live> connect(PythonGateway gateway, String token) async {
    final ws = await WebSocket.connect(
      gateway.base
          .replace(
            scheme: 'ws',
            path: '/v1/ws',
            queryParameters: {'token': token},
          )
          .toString(),
    );
    return Live._(
      ws,
      StreamIterator(ws.map((f) => jsonDecode(f as String) as Frame)),
    );
  }

  /// The next frame [where] accepts, skipping the rest.
  Future<Frame> next(bool Function(Frame) where) async {
    while (true) {
      if (!await _frames.moveNext().timeout(const Duration(seconds: 10))) {
        throw StateError('the socket closed before the frame arrived');
      }
      if (where(_frames.current)) return _frames.current;
    }
  }

  /// Subscribes to [channelIds] and returns the suback's fences.
  Future<Map<String, Object?>> subscribe(List<String> channelIds) async {
    _ws.add(jsonEncode({'type': 'subscribe', 'channel_ids': channelIds}));
    final suback = await next((f) => f['type'] == 'suback');
    return suback['channel_fences']! as Map<String, Object?>;
  }

  /// Sends [body] into [channelId] as [clientMsgId] and returns the reply that
  /// names it: an `ack`, or an `error`.
  Future<Frame> send(String channelId, String clientMsgId, String body) {
    _ws.add(
      jsonEncode({
        'type': 'send',
        'client_msg_id': clientMsgId,
        'channel_id': channelId,
        'body': body,
      }),
    );
    return next(
      (f) =>
          f['client_msg_id'] == clientMsgId ||
          f['ref_client_msg_id'] == clientMsgId,
    );
  }

  Future<void> close() async {
    await _frames.cancel();
    await _ws.close();
  }
}
