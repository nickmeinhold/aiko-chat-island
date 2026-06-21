"""Phase 1 auth-slice verification (run against a live local gateway)."""
import sys, time, urllib.request, urllib.error, json

BASE = "http://127.0.0.1:8095"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "null")


def ok(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    return cond


def main():
    uname = f"alice{int(time.time())}"
    results = []

    st, reg = call("POST", "/v1/auth/register",
                   {"username": uname, "display_name": "Alice", "password": "hunter2"})
    results.append(ok("register -> 200 + tokens", st == 200 and "access_token" in reg, f"status={st}"))
    access, refresh = reg.get("access_token"), reg.get("refresh_token")

    st, dup = call("POST", "/v1/auth/register",
                   {"username": uname, "display_name": "x", "password": "y"})
    results.append(ok("duplicate register -> 409", st == 409, f"status={st}"))

    st, me = call("GET", "/v1/me", token=access)
    results.append(ok("me with access token -> 200", st == 200 and me.get("username") == uname, f"status={st}"))

    st, _ = call("GET", "/v1/me", token="garbage.token.here")
    results.append(ok("me with bad token -> 401", st == 401, f"status={st}"))

    st, _ = call("GET", "/v1/me")
    results.append(ok("me with NO token -> 401/403", st in (401, 403), f"status={st}"))

    st, lg = call("POST", "/v1/auth/login", {"username": uname, "password": "hunter2"})
    results.append(ok("login -> 200 + tokens", st == 200 and "access_token" in lg, f"status={st}"))

    st, bad = call("POST", "/v1/auth/login", {"username": uname, "password": "wrong"})
    results.append(ok("login wrong password -> 401", st == 401, f"status={st}"))

    st, rf = call("POST", "/v1/auth/refresh", {"refresh_token": refresh})
    results.append(ok("refresh -> new access token", st == 200 and "access_token" in rf, f"status={st}"))

    st, rfbad = call("POST", "/v1/auth/refresh", {"refresh_token": access})  # access != refresh
    results.append(ok("refresh with ACCESS token -> 401 (type enforced)", st == 401, f"status={st}"))

    # Integration payoff: a message from this registered user (aiko_username ==
    # sanitized username) should now persist as sender_kind=human with a user id.
    aiko_username = me.get("aiko_username")
    nonce = f"auth-int-{int(time.time())}"
    call("POST", f"/v1/_debug/send?channel=general&username={aiko_username}&message={nonce}")
    chans = call("GET", "/v1/channels")[1]["channels"]
    cid = next(c["id"] for c in chans if c["aiko_channel"] == "general")
    found = None
    for _ in range(20):
        msgs = call("GET", f"/v1/channels/{cid}/messages?limit=200")[1]["messages"]
        found = next((m for m in msgs if nonce in m["body"]), None)
        if found:
            break
        time.sleep(0.5)
    results.append(ok("registered user's msg -> sender_kind=human",
                      bool(found) and found["sender"]["kind"] == "human",
                      f"sender={found['sender'] if found else None}"))
    results.append(ok("registered user's msg -> sender_user_id set",
                      bool(found) and found["sender"]["user_id"] is not None))

    print(f"\n{'ALL PASS' if all(results) else 'SOME FAILED'} "
          f"({sum(results)}/{len(results)})")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
