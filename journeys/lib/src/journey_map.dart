/// Which journey (DESIGN.md §2.2) drives each operation the gateway serves.
///
/// The orphan check (`test/journey_map_test.dart`) holds this equal to the frozen
/// contract in both directions, so a new route can't ship without a journey and a
/// removed one can't linger here. It finds a missing route, never a missing
/// OBLIGATION inside a journey that exists: that is what each journey's own
/// obligations and its recorded shapes are for.
library;

/// The journeys, by the number DESIGN.md §2.2 gives them.
const journeys = <int, String>{
  1: 'register a passkey',
  2: 'sign in',
  3: 'join a channel',
  4: 'send and fan out',
  5: 'echo',
  6: 'DM and reactions',
  7: 'delete an account',
  8: 'push',
  9: 'topology',
  10: 'kill after commit',
  11: 'moderation',
  12: 'calls',
  13: 'social sign-in',
  14: 'OAuth broker',
  15: 'recovery re-key',
  16: 'recovery enrol and policy',
  17: 'communities',
  18: 'blocks',
  19: 'signing keys',
  20: 'island surface, read-only',
  21: 'profile, /v1/me',
  22: 'read positions',
};

/// `METHOD /path/{template}` → journey. `WS` entries are the socket routes, which
/// FastAPI leaves out of openapi.json (the contract freezes them separately).
/// Journeys 5, 9 and 10 are bus and process behaviours with no route of their own.
const journeyOf = <String, int>{
  'GET /.well-known/apple-app-site-association': 20,
  'GET /.well-known/assetlinks.json': 20,
  'GET /capabilities': 20,
  'GET /health': 20,
  'GET /privacy': 20,
  'GET /terms': 20,
  'GET /v1/island': 20,
  'GET /v1/islands': 20,
  'GET /v1/gateways': 20,
  'GET /v1/auth/providers': 20,
  // Dev-only bus poke: in the production contract, 404 in production.
  'POST /v1/_debug/send': 20,

  'POST /v1/auth/passkey/register/start': 1,
  'POST /v1/auth/passkey/register/finish': 1,

  'POST /v1/auth/login': 2,
  'POST /v1/auth/passkey/authenticate/start': 2,
  'POST /v1/auth/passkey/authenticate/finish': 2,
  'POST /v1/auth/refresh': 2,

  'GET /v1/channels': 3,
  'POST /v1/channels': 3,
  'POST /v1/channels/{channel_id}/join': 3,
  'DELETE /v1/channels/{channel_id}/leave': 3,
  'GET /v1/channels/{channel_id}/members': 3,
  'POST /v1/channels/{channel_id}/members': 3,
  'DELETE /v1/channels/{channel_id}/members/{user_id}': 3,

  'WS /v1/ws': 4,
  'GET /v1/channels/{channel_id}/messages': 4,

  'GET /v1/dm': 6,
  'POST /v1/dm': 6,
  'GET /v1/messages/{message_id}': 6,
  'POST /v1/messages/{message_id}/reactions': 6,
  'DELETE /v1/messages/{message_id}/reactions': 6,

  'DELETE /v1/account': 7,

  'POST /v1/devices': 8,
  'DELETE /v1/devices': 8,

  'POST /v1/users/{user_id}/ban': 11,
  'DELETE /v1/users/{user_id}/ban': 11,
  'POST /v1/messages/{message_id}/report': 11,
  'GET /v1/reports': 11,
  'POST /v1/reports/{report_id}/resolve': 11,
  'POST /v1/reports/{report_id}/dismiss': 11,

  'POST /v1/channels/{channel_id}/video-token': 12,
  'GET /v1/channels/{channel_id}/call': 12,

  'POST /v1/auth/nonce': 13,
  'POST /v1/auth/social': 13,
  'POST /v1/auth/social/claim': 13,

  'GET /v1/auth/oauth/{provider}/start': 14,
  'GET /v1/auth/oauth/{provider}/callback': 14,
  'POST /v1/auth/oauth/exchange': 14,

  'POST /v1/auth/passkey/recover/start': 15,
  'POST /v1/auth/passkey/recover/finish': 15,
  'POST /v1/auth/passkey/recover/finalize': 15,
  'POST /v1/auth/passkey/recover/cancel': 15,

  'POST /v1/auth/recovery/enroll': 16,
  'GET /v1/auth/recovery/policy': 16,
  'GET /v1/auth/recovery/status': 16,
  'POST /v1/auth/passkey/add/finish': 16,

  'GET /v1/communities': 17,
  'GET /v1/communities/discover': 17,
  'GET /v1/communities/{community_id}': 17,
  'POST /v1/communities/{community_id}/join': 17,

  'GET /v1/blocks': 18,
  'POST /v1/users/{user_id}/block': 18,
  'DELETE /v1/users/{user_id}/block': 18,

  'GET /v1/keys': 19,
  'POST /v1/keys': 19,
  'DELETE /v1/keys/{pubkey}': 19,

  'GET /v1/me': 21,
  'PATCH /v1/me': 21,

  'GET /v1/read-positions': 22,
  'PUT /v1/read-positions': 22,
};
