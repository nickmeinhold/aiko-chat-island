# `deploy/secrets/`

SOPS-encrypted, per-island secrets. **Encrypted files belong in git**; plaintext never does.

Recipients and the reasoning behind them live in [`.sops.yaml`](../../.sops.yaml).

```sh
sops deploy/secrets/<island>.enc.env          # edit in place, re-encrypts on save
sops -d deploy/secrets/<island>.enc.env       # decrypt to stdout
sops updatekeys deploy/secrets/*.enc.env      # re-encrypt after changing recipients
```

Nothing is here yet: extracting the live boxes' keys into these files is #2301's rollout
step 1, and #3976 required a second, uncorrelated recipient to exist first. It now does.
