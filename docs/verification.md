# Verifying downloads

Three kinds of files are signed: store-release's own release assets (and its PyPI files,
which are the same sdist and wheel), the Android files an app's GitHub Release carries, and,
when an app turns it on, the source archive of an iOS release. All of them are signed keylessly with [Sigstore](https://www.sigstore.dev/)
(`cosign`) and carry a SLSA build provenance attestation that `gh attestation verify`
checks.

## store-release's own releases

Each `vX.Y.Z` release attaches eleven files, made by store-release's
[`release.yml`](../.github/workflows/release.yml) from the tagged commit (v0.3.0 and
earlier: the first four only):

| File | What it is |
|---|---|
| `store-release-X.Y.Z-source.tar.gz` | `git archive` of the tagged commit (prefix `store-release-X.Y.Z/`) |
| `store-release-X.Y.Z-source.tar.gz.sha256` | its SHA-256 |
| `store-release-X.Y.Z-source.tar.gz.sigstore.json` | the cosign keyless signature bundle |
| `store-release-X.Y.Z-source.tar.gz.intoto.jsonl` | the SLSA build provenance attestation (also stored by GitHub) |
| `store_release-X.Y.Z.tar.gz` | the sdist, the file PyPI also has |
| `store_release-X.Y.Z.tar.gz.sha256` | its SHA-256 |
| `store_release-X.Y.Z.tar.gz.sigstore.json` | its cosign keyless signature bundle |
| `store_release-X.Y.Z-py3-none-any.whl` | the wheel, the file PyPI also has |
| `store_release-X.Y.Z-py3-none-any.whl.sha256` | its SHA-256 |
| `store_release-X.Y.Z-py3-none-any.whl.sigstore.json` | its cosign keyless signature bundle |
| `store_release-X.Y.Z.intoto.jsonl` | the SLSA build provenance attestation of the sdist and the wheel |

```bash
v=0.3.0   # the release to check
gh release download "v$v" --repo apoorvdarshan/store-release
sha256sum -c "store-release-$v-source.tar.gz.sha256"
cosign verify-blob "store-release-$v-source.tar.gz" \
  --bundle "store-release-$v-source.tar.gz.sigstore.json" \
  --certificate-identity 'https://github.com/apoorvdarshan/store-release/.github/workflows/release.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify "store-release-$v-source.tar.gz" --repo apoorvdarshan/store-release \
  --signer-workflow apoorvdarshan/store-release/.github/workflows/release.yml
# Offline, with the attached bundle instead of GitHub's attestation store:
gh attestation verify "store-release-$v-source.tar.gz" --repo apoorvdarshan/store-release \
  --bundle "store-release-$v-source.tar.gz.intoto.jsonl"
```

The sdist and the wheel check the same way: replace `store-release-$v-source.tar.gz` with
`store_release-$v.tar.gz` or `store_release-$v-py3-none-any.whl`, and use
`store_release-$v.intoto.jsonl` as the offline bundle. `--source-digest <tagged commit>`
also pins the commit they were built from.

### From PyPI

PyPI's `store-release` files are the release's sdist and wheel: the Release workflow
uploads exactly the attached files, through Trusted Publishing, with PEP 740 attestations
signed by the same workflow identity. Check either way:

```bash
v=X.Y.Z   # a release after v0.3.0
# PyPI serves the release's own files (run where the release's files were downloaded):
curl -s "https://pypi.org/pypi/store-release/$v/json" \
  | python3 -c 'import json, sys; [print(f["digests"]["sha256"] + "  " + f["filename"]) for f in json.load(sys.stdin)["urls"]]' \
  | sha256sum -c -
# PyPI's attestations name this repository's release.yml:
python3 -m pip install pypi-attestations
pypi-attestations verify pypi --repository https://github.com/apoorvdarshan/store-release \
  "pypi:store_release-$v-py3-none-any.whl"
```

To install exactly the release's wheel, install the file you checked
(`pipx install ./store_release-$v-py3-none-any.whl`), or pin its SHA-256 in a requirements
file (`store-release==X.Y.Z --hash=sha256:<digest>`) and install it with
`pip install --require-hashes -r <file>`.

On macOS use `shasum -a 256 -c` for the checksum. Without cosign, the Python
[sigstore](https://pypi.org/project/sigstore/) client checks the same bundle:
`python -m sigstore verify identity --bundle <bundle> --cert-identity <identity above>
--cert-oidc-issuer https://token.actions.githubusercontent.com <archive>`.

The archive is exactly the tagged commit's tree, so a clone rebuilds it: the tar inside is
identical whatever git version makes it (compare `gzip -dc store-release-$v-source.tar.gz |
sha256sum` with `git archive --format=tar --prefix=store-release-$v/ v$v | sha256sum`). The
releases are immutable, and the tag is the commit apps pin
([release-process.md](release-process.md)).

## An app's Android release files

An Android tag run signs the AAB and APK it attaches to the app's GitHub Release with
Sigstore (`*.sigstore.json` next to each file) and records build provenance. The signing
job is store-release's `android-publish.yml`, so the certificate identity is
`https://github.com/apoorvdarshan/store-release/.github/workflows/android-publish.yml@<sha>`
for **every** app that uses store-release. Always pin the app's repository as well;
otherwise a file signed by another app's run would verify too.

```bash
# <asset> is the app's asset name (android.github_release.asset_name), e.g. my-app-v1.2.apk
cosign verify-blob <asset> --bundle <asset>.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/apoorvdarshan/store-release/\.github/workflows/android-publish\.yml@[0-9a-f]{40}$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-github-workflow-repository <owner>/<app repository>

gh attestation verify <asset> --repo <owner>/<app repository> \
  --signer-workflow apoorvdarshan/store-release/.github/workflows/android-publish.yml
```

The `<sha>` in the identity is the store-release commit the app pinned; the app's caller
workflow shows it (`# vX.Y.Z` names the version). The rendered `android-release.yml` carries
these commands with the app's own names in its header.

Both files are also checked before they are signed: the package, versionName and
versionCode must match the tag, and the pinned upload certificate
(`android.upload_cert_sha256`) must be the only signer, with every AAB entry covered by the
signature.

## An app's iOS release

The iOS GitHub Release carries the release notes only; the binary goes to App Store
Connect and Apple distributes it. Before upload, every app and extension bundle in the
archive is checked for the bundle id, version and build number, and the archived
Info.plist for its export compliance answer.

### Source archives (`ios.github_release.source_archive`)

With `ios.github_release.source_archive = true` the iOS GitHub Release also carries
`<slug>-<version>-source.tar.gz`, its `.sha256` and a cosign bundle (`.sigstore.json`),
made by store-release's `ios-source-archive.yml` from the tagged commit. The signing
identity is that reusable workflow, called from the app's repository:

```bash
sha256sum -c <slug>-X.Y.Z-source.tar.gz.sha256
cosign verify-blob <slug>-X.Y.Z-source.tar.gz \
  --bundle <slug>-X.Y.Z-source.tar.gz.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/apoorvdarshan/store-release/\.github/workflows/ios-source-archive\.yml@[0-9a-f]{40}$' \
  --certificate-github-workflow-repository <owner>/<app repository> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify <slug>-X.Y.Z-source.tar.gz --repo <owner>/<app repository>
```

## The pin an app runs

- Apps pin store-release by full commit SHA with a `# vX.Y.Z` comment. In Store checks,
  `pins check` and `caller check --pr` fail unless every pin is the same commit, its
  comment names the version that runs, and the `vX.Y.Z` tag points at that commit, so a pin
  to an untagged `main` commit with a plausible comment fails. In release runs,
  `caller check --runtime` only warns about the tag.
- Those checks run inside the code the app pinned, so they cannot catch a hostile pin: a
  commit pushed to a fork of store-release also resolves through `uses:` by SHA, and its
  code could skip them. Catch that outside the pinned code: merge only pin bumps that
  Dependabot opened (it resolves `vX.Y.Z` in apoorvdarshan/store-release itself), or check
  the pin in the app's own CI, for example that
  `git ls-remote https://github.com/apoorvdarshan/store-release refs/tags/vX.Y.Z` prints
  the pinned SHA, or run zizmor's `impostor-commit` audit.
- Release tags `vX.Y.Z` come from `release.yml`, after every required check passed on that
  commit, and never move: releases are immutable (the tag is locked as the release is
  published), and the "Release tags" ruleset blocks tag updates and deletion
  ([release-process.md](release-process.md)). That only `release.yml` creates them is a
  convention: the ruleset does not block creating a tag, so anyone with write access could
  push one.
- Every job checks out store-release at `job.workflow_sha` and `tool check` confirms the
  checkout is that commit, so the workflow YAML and the Python it runs are the pinned
  commit's.
