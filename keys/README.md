Signing keys and trust policy for `scripts/update-deps.py`.

* `trust.json`: which primary-key fingerprints may sign each project, where
  each key is published, how the fingerprints were cross-checked, and the
  Sigstore identities of CPython's release managers. Changing it is a trust
  decision; see "How signers are pinned" in the top-level README.
* `<project>.asc`: each maintainer's public key as they publish it, exported
  with only the pinned primary keys and their subkeys (`gpg
  --export-options export-minimal`). Refresh with
  `scripts/update-deps.py --refresh-keys`; don't edit by hand.

The keys belong to their owners and are redistributed only so that
signatures can be verified.
