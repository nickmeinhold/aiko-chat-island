# `deploy/secrets/`

The **complete** `.env` for each island, SOPS-encrypted. Encrypted files belong in git;
plaintext never does.

Recipients and the reasoning behind them live in [`.sops.yaml`](../../.sops.yaml).

```sh
sops deploy/secrets/<island>.enc.env       # edit in place, re-encrypts on save
sops -d deploy/secrets/<island>.enc.env    # decrypt to stdout
sops updatekeys deploy/secrets/*.enc.env   # re-encrypt after changing recipients
```

## Why these are `binary`, not `dotenv`

SOPS's dotenv parser **rejects these files**. `APNS_PRIVATE_KEY` is a PEM with real
newlines inside quotes — deliberate, documented in `docker-compose.yml` and enforced by
`config.py`, which rejects an escaped-newline form. Compose and `python-dotenv` both
parse it; SOPS's dotenv parser does not (`invalid dotenv input line: MIGTAgEA…`). A
third parser for one `.env`, on top of the two #3592 already names.

That is a representation limit, not a formatting one — no encoding tweak makes a
real-newline PEM legal dotenv. So the file is encrypted as an opaque **binary** blob,
which round-trips byte for byte.

**The cost, named:** key *names* are not visible in the ciphertext, so a git diff cannot
show which keys changed — only that the file did. That is the price of fidelity, and
fidelity is what "the artifact in git IS the artifact on the box" rests on.

## What is in them

Whatever was on the box on 2026-09-06, **verbatim**. Nothing was curated during the lift,
deliberately: curating here would make the first deploy silently *change* the islands,
and a migration that also changes behaviour cannot be reviewed as either one. Cleanup is
a later, visible diff.

Known divergences captured as-is, not fixed:

- imagineering carries the social-signin set (`APPLE_CLIENT_IDS`, `GOOGLE_CLIENT_IDS`,
  `GITHUB_CLIENT_*`, `SOCIAL_SIGNIN_ENABLED`); enspyr does not.
- enspyr carries `ENVIRONMENT` and `GATEWAY_BASE_URL`/`PASSKEY_RP_ID`; imagineering does
  not. `GATEWAY_BASE_URL` is **live and correct** — checked, not assumed: it survived the
  `GATEWAY_*` deletion in PR#158 because a base_url genuinely is a gateway concern
  (#157). `ENVIRONMENT` is worth a look against #3365.

22 keys on imagineering, 20 on enspyr.
