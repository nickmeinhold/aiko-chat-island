# `deploy/secrets/`

The **complete** `.env` for each island, SOPS-encrypted. Encrypted files belong in git;
plaintext never does.

Recipients and the reasoning behind them live in [`.sops.yaml`](../../.sops.yaml).

```sh
sops deploy/secrets/<island>.env.sops       # edit in place, re-encrypts on save
sops -d deploy/secrets/<island>.env.sops    # decrypt to stdout
sops updatekeys deploy/secrets/*.env.sops   # re-encrypt after changing recipients
./deploy/verify-secrets.sh                  # verify WITHOUT a key (CI runs this)
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


## Why `.env.sops` and not `.enc.env`

SOPS picks its parser from the **file extension**. A name ending in `.env` is parsed
as dotenv — and these are binary blobs, so `sops updatekeys` failed outright with
`invalid dotenv input line: {`. That is the documented rotation path, and the way a
hardware key would later be added: **broken, and it would only have surfaced on the
day someone needed it.** Found by running the documented procedure rather than
reading it. The name now matches the content, so no command needs a remembered
`--input-type binary`.

## Verification

`./deploy/verify-secrets.sh` needs **no key** and runs in CI on every push. It fails if:

- any recipient required by `.sops.yaml` is missing from any file — the arm that fires
  when a botched `updatekeys` silently drops the offline recovery key;
- `.sops.yaml` declares fewer than two recipients (the #3976 invariant);
- a plaintext key marker appears in ciphertext;
- the payload is not an `ENC[...]` blob (a valid envelope around cleartext still parses);
- `sops updatekeys` cannot read a file — the rotation path itself;
- **anything unexpected sits in this directory.** Mutation-testing caught the script
  eating its own tail: renaming a secret out of the `*.env.sops` glob made it report
  "nothing to verify" and exit 0. A checker that goes quiet when its targets vanish
  reports success for the state it exists to detect.

`--deep` additionally proves a real decrypt **and that `MANIFEST.txt` tells the truth**.
It needs a key, so it never runs in CI — **run it before merging any change to a
`*.env.sops`.** A shallow pass confirms the manifest exists and covers every island; only
the deep pass confirms it matches reality.

### `MANIFEST.txt` — the review surface binary encoding took away

Key **names** in cleartext, never values. A binary blob makes a diff show *that* a file
changed, not *which* key — a permanent loss on a public, immortal artifact. The manifest
buys it back: adding, removing or renaming a key changes this file visibly.

It leaks nothing new. Every name in it is already public in `docker-compose.yml`, which
has always been committed. (That resolves a tension the reviewers raised across two
rounds — "key counts are reconnaissance" versus "commit a manifest" — on the first
objection's own terms.)

Every arm is mutation-proven — each was made to fail on purpose before being trusted.

## Disaster recovery

**Read this before you need it.** The ciphertext is in a PUBLIC repo and is permanent:
it is forkable, archivable and cannot be unpublished. That changes what "compromise"
means here — see below.

### The Mac dies, or the Secure Enclave key is lost

Not an emergency; the islands keep running (the boxes hold plaintext `.env` and never
needed a key). You have lost the ability to *edit* config, not to *operate*.

1. Retrieve the recovery key (password manager, or the paper copy).
2. `SOPS_AGE_KEY_FILE=<file> sops -d deploy/secrets/<island>.env.sops` — confirm it reads.
3. Generate a new primary (`age-plugin-se keygen` on the new Mac, or a YubiKey).
4. Replace the old primary in `.sops.yaml`, then `sops updatekeys deploy/secrets/*.env.sops`.
5. `./deploy/verify-secrets.sh` — it fails if any file lost a recipient.

No secret rotation is required: nothing leaked.

### A private key LEAKS — the one that matters

Removing the recipient is **not sufficient and not the first step.** Anyone who copied
the ciphertext already holds it forever, and a leaked key decrypts every historical
version encrypted to it. `sops updatekeys` protects only future commits.

**Assume every secret in the affected file is public. Rotate all of them.**

1. **`ISLAND_SIGNING_SEED`** — the island's identity. Rotating changes what the signed
   manifest means to clients; bump `ISLAND_KEY_VERSION` and coordinate with the app tab.
2. **`JWT_SECRET`** — rotate; every session is invalidated and every user is logged out.
3. **`APNS_PRIVATE_KEY`** — revoke the key in the Apple Developer console and issue a new
   `.p8`. Revocation is the only thing that stops the old one; re-encrypting does nothing.
4. **`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`** — rotate on the SFU and in the island config.
5. **OAuth client secrets** (`GITHUB_CLIENT_SECRET`) — rotate at the provider.
6. Only now: remove the compromised age recipient, `sops updatekeys`, re-verify, deploy.

The ordering is the point. Steps 1-5 are what actually reduce harm; step 6 only stops
the bleeding for future commits and feels like the fix while changing nothing about what
is already public.

### Both keys lost

The committed config is unrecoverable ciphertext. The islands keep running, and the boxes
still hold plaintext `.env` — so **re-lift from the boxes** exactly as #2301 step 1 did,
against fresh recipients. This is why the recovery key has two storage locations with
uncorrelated failure modes, and why the boxes holding plaintext is a backstop rather than
purely a weakness.

## Handling plaintext safely — the happy path IS the exfil path

Raised in the PR#166 cage-match (Tesla), and it is the most under-appreciated risk here:
the ciphertext is safe, but **the decrypted form lands on the one machine that can also
decrypt it**, which is the machine running editors, indexers and cloud sync.

- `sops -d file` writes the signing seed to **stdout** — into shell scrollback, tmux
  history, `script(1)` captures, CI logs. Redirect to a `umask 077` file, or pipe it;
  do not let it land in a terminal you will scroll back through.
- `sops file` (edit in place) decrypts into a **temp file for `$EDITOR`**. Set
  `SOPS_EDITOR` to something without plugins, LSP indexers or crash-recovery
  swapfiles. An editor that indexes its temp dir has just indexed your signing seed.
- **APFS makes deletion a wish, not a fact.** Copy-on-write, local snapshots and Time
  Machine can all retain a "shredded" temp file. `shred -u` on APFS is a gesture. Prefer
  never writing plaintext to disk to trying to erase it afterwards.
- Keep the decrypt off any path that syncs. This Mac's `~/Desktop` and `~/Documents` are
  iCloud-synced (measured) — a plaintext `.env` there uploads itself.

## Known hazard until `deploy-to.sh` exists: split brain

These files are the boxes as of 2026-09-06. Git now *claims* desired state, but nothing
delivers it yet, so the boxes remain the live truth and the two can diverge silently —
the encrypted blob shows no readable diff when it does.

**The sharp edge:** an emergency change made directly on a box (revoking a compromised
APNS key, say) will be **clobbered** by the first delivery of this snapshot unless it is
lifted back into the repo first. Until step 2 lands: after any hand-edit on a box,
re-lift that island. `./deploy/verify-secrets.sh` cannot detect this — it verifies the
artifact's integrity, not its agreement with production.
