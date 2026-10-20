# Verifying downloads

## Android release files

An Android tag run signs the AAB and APK it attaches to the app's GitHub Release with
Sigstore (keyless, `*.sigstore.json` next to each file) and records build provenance
(`actions/attest-build-provenance`). The signing job is store-release's
`android-publish.yml`, so the certificate identity is
`https://github.com/apoorvdarshan/store-release/.github/workflows/android-publish.yml@<sha>`
for **every** app that uses store-release. Always pin the app's repository as well;
otherwise a file signed by another app's run would verify too.

```bash
# <asset> is the app's asset name, for example fudai-v7.2.apk
cosign verify-blob <asset> --bundle <asset>.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/apoorvdarshan/store-release/\.github/workflows/android-publish\.yml@[0-9a-f]{40}$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-github-workflow-repository <owner>/<app repository>

gh attestation verify <asset> --repo <owner>/<app repository> \
  --signer-workflow apoorvdarshan/store-release/.github/workflows/android-publish.yml
```

The `<sha>` in the identity is the store-release commit the app pinned; the app's
caller workflow shows it (`# vX.Y.Z` names the version). Releases an app made before
it moved to store-release were signed by the app's own workflow; that app's
`store/README.md` keeps their identity.

Both files are also checked before they are signed: the package, versionName and
versionCode must match the tag, and the pinned upload certificate
(`android.upload_cert_sha256`) must be the only signer, with every AAB entry covered
by the signature.

## iOS

The iOS GitHub Release carries the release notes only; the binary goes to App Store
Connect and is distributed by Apple. Before upload, every app and extension bundle in
the archive is checked for the bundle id, version and build number, and the archived
Info.plist for its export compliance answer.

## store-release itself

- Apps pin store-release by full commit SHA with a `# vX.Y.Z` comment. In Store
  checks, `pins check` and `caller check --pr` fail unless every pin is the same
  commit, its comment names the version that runs, and the `vX.Y.Z` tag points at that
  commit, so a pin to an untagged `main` commit with a plausible comment fails. In
  release runs, `caller check --runtime` only warns about the tag.
- Those checks run inside the code the app pinned, so they cannot catch a hostile pin:
  a commit pushed to a fork of store-release also resolves through `uses:` by SHA, and
  its code could skip them. Catch that outside the pinned code: merge only pin bumps
  that Dependabot opened (it resolves `vX.Y.Z` in apoorvdarshan/store-release itself),
  or check the pin in the app's own CI, for example that
  `git ls-remote https://github.com/apoorvdarshan/store-release refs/tags/vX.Y.Z`
  prints the pinned SHA, or run zizmor's `impostor-commit` audit.
- Release tags `vX.Y.Z` are created only by `release.yml`, after every required
  check passed on that commit, and never move: releases are immutable, and the
  "Release tags" ruleset (created right after the first tag,
  [release-process.md](release-process.md)) blocks tag updates and deletion.
- Every job checks out store-release at `job.workflow_sha` and `tool check` confirms
  the checkout is that commit, so the workflow YAML and the Python it runs are the
  pinned commit's.
