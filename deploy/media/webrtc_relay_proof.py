#!/usr/bin/env python3
"""webrtc_relay_443_proof.py — a REAL WebRTC client completing a relay-only connection
over turns:<domain>:443, from a vantage where UDP to the box is blocked.

Why Chromium and not the python livekit SDK: that SDK bundles a root store containing NO
ISRG root (verified 2026-08-13), so it cannot validate any Let's Encrypt cert and can never
complete a TURNS handshake against these islands — an instrument defect, not a server one.
Chromium ships a current root store and a production WebRTC stack, so it can answer the
question the python SDK structurally cannot.

What it asserts:
  1. the room connects at all (ICE completes) with iceTransportPolicy:'relay'
  2. EVERY local candidate is type 'relay' (relay-only really is in force)
  3. the SELECTED candidate pair's local candidate is a relay whose relayProtocol is TLS
     -- i.e. the media path is the :443 TURNS relay, not UDP/3478

Env: LK_URL, LK_TOKEN (pre-minted), TURN_DOMAIN.
Exit 0 only if the selected pair is a TLS relay.
"""
import asyncio, json, os, sys, http.server, threading, functools

LK_URL = os.environ["LK_URL"]
TOKEN = os.environ["LK_TOKEN"]
PORT = int(os.environ.get("PAGE_PORT", "8099"))

PAGE = """<!doctype html><meta charset=utf-8><title>relay443</title>
<script src="https://unpkg.com/livekit-client@2.7.2/dist/livekit-client.umd.js"></script>
<script>
window.__result = null;
async function go() {
  const { Room } = LivekitClient;
  const room = new Room();
  try {
    await room.connect(URL_, TOKEN_, { rtcConfig: { iceTransportPolicy: 'relay' } });
  } catch (e) {
    window.__result = { ok:false, stage:'connect', error: String(e) }; return;
  }
  // give ICE a moment to settle on a pair
  await new Promise(r => setTimeout(r, 4000));
  const pc = room.engine?.pcManager?.subscriber?.pc || room.engine?.pcManager?.publisher?.pc;
  if (!pc) { window.__result = { ok:false, stage:'pc', error:'no peer connection' }; return; }
  const stats = await pc.getStats();
  let locals = [], pairs = [], selectedId = null, transport = null;
  stats.forEach(r => {
    if (r.type === 'local-candidate') locals.push(r);
    if (r.type === 'candidate-pair') pairs.push(r);
    if (r.type === 'transport') transport = r;
  });
  if (transport && transport.selectedCandidatePairId) selectedId = transport.selectedCandidatePairId;
  let sel = pairs.find(p => p.id === selectedId) || pairs.find(p => p.selected) ||
            pairs.find(p => p.state === 'succeeded' && p.nominated) ||
            pairs.find(p => p.state === 'succeeded');
  const selLocal = sel ? locals.find(l => l.id === sel.localCandidateId) : null;
  window.__result = {
    ok: true,
    connectionState: pc.connectionState,
    iceConnectionState: pc.iceConnectionState,
    local_candidate_types: [...new Set(locals.map(l => l.candidateType))],
    all_relay: locals.length > 0 && locals.every(l => l.candidateType === 'relay'),
    relay_protocols: [...new Set(locals.map(l => l.relayProtocol).filter(Boolean))],
    selected: sel ? { state: sel.state, bytesSent: sel.bytesSent, bytesReceived: sel.bytesReceived } : null,
    selected_local: selLocal ? { type: selLocal.candidateType, relayProtocol: selLocal.relayProtocol,
                                 url: selLocal.url, address: selLocal.address } : null,
  };
}
go().catch(e => { window.__result = { ok:false, stage:'throw', error:String(e) }; });
</script>"""


def serve(page_bytes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page_bytes))); self.end_headers()
            self.wfile.write(page_bytes)
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def main() -> int:
    from playwright.async_api import async_playwright
    page_html = PAGE.replace("URL_", json.dumps(LK_URL)).replace("TOKEN_", json.dumps(TOKEN))
    serve(page_html.encode())
    # CHROMIUM_EXTRA_ARGS exists so this same artifact — not a forked copy of it — can run
    # against the rehearsal rig, whose certs come from a local Pebble CA that no browser
    # trusts. Pin that CA by SPKI (--ignore-certificate-errors-spki-list=<b64 sha256>), never
    # by blanket --ignore-certificate-errors: the cert chain is part of what a TURNS client
    # must do, so disabling verification wholesale would make the proof unable to fail.
    extra = [a for a in os.environ.get("CHROMIUM_EXTRA_ARGS", "").split() if a]
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"] + extra)
        page = await browser.new_page()
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        await page.goto(f"http://127.0.0.1:{PORT}/")
        result = None
        for _ in range(60):
            result = await page.evaluate("window.__result")
            if result is not None:
                break
            await asyncio.sleep(1)
        await browser.close()

    if result is None:
        print(json.dumps({"result": "FAIL", "reason": "browser never reported (timeout)",
                          "console_errors": errs[:5]})); return 3
    if not result.get("ok"):
        print(json.dumps({"result": "FAIL", "reason": "client could not connect over relay-only",
                          "detail": result, "console_errors": errs[:5]})); return 3

    print("  " + json.dumps(result, indent=None))
    sel = result.get("selected_local") or {}
    proto = (sel.get("relayProtocol") or "").lower()
    if not result.get("all_relay"):
        print(json.dumps({"result": "FAIL", "reason": "not every local candidate was a relay",
                          "detail": result})); return 3
    if proto != "tls":
        print(json.dumps({"result": "FAIL", "reason": f"media path used relayProtocol={proto!r}, "
                          "not the TLS :443 relay", "detail": result})); return 3
    print("WEBRTC_443=" + json.dumps({
        "result": "OK",
        "reason": "a real WebRTC client (Chromium) completed a relay-only connection whose "
                  "SELECTED candidate pair is a TURN relay over TLS, from a vantage with UDP to "
                  "the box blocked — TURN-over-TLS on :443 carries a real call",
        "selected_local": sel, "ice": result.get("iceConnectionState"),
        "connection": result.get("connectionState")}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
