# Secrets

store-release has no secrets of its own. Each app keeps its secrets in its own
repository (Settings → Secrets and variables → Actions → Secrets), and each caller
job passes exactly the secrets its reusable workflow declares, by name:

```yaml
    secrets:
      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}
```

`secrets: inherit` is never used. A secret the workflow does not declare, or a
`required: true` secret the caller does not pass, is a startup failure
([workflows.md](workflows.md#startup-failures)); `callers render` and the PR caller
check keep the mappings exact.

Every job that uses a secret checks, before anything else uses it, that the secret
is set (as a boolean, `secrets.X != ''`; the value is never read for this), and
names any that is missing.

## Per workflow

| Workflow | Secret | Required | Used for |
|---|---|---|---|
| `ios-prepare.yml` | `APP_STORE_CONNECT_API_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`, `APP_STORE_CONNECT_API_KEY_P8` | no; checked when the run builds or an App Store Connect switch is on | version creation, the read-only version preflight, the next build number |
| `ios-build.yml` | the three `APP_STORE_CONNECT_*` | yes | cloud-managed distribution signing at export, altool validation, upload |
| | `IOS_DEV_SIGNING_P12_BASE64`, `IOS_DEV_SIGNING_P12_PASSWORD` | yes | the Apple Development identity that signs the archive (below) |
| `ios-wait-for-build.yml` | the three `APP_STORE_CONNECT_*` | yes | waiting until this run's build is VALID (read-only) |
| `ios-app-store-connect.yml` | the three `APP_STORE_CONNECT_*` | yes | listing, What's New, screenshots, creative images, build selection, review |
| `ios-github-release.yml` | none | | the caller's `GITHUB_TOKEN` (`contents: write`) |
| `ios-store-plan.yml` | the three `APP_STORE_CONNECT_*` | yes | the read-only plan |
| `android-gates.yml` | `PLAY_SERVICE_ACCOUNT_JSON` | no; checked when a Play track is on | the read-only check that the versionCode is new to Play |
| `android-publish.yml` | `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` | yes (validation runs too) | signing the AAB and APK with the upload key |
| | `PLAY_SERVICE_ACCOUNT_JSON` | no; checked when a listing, screenshot or track switch is on | Play listing, uploads, track read-back |
| `android-store-plan.yml` | `PLAY_SERVICE_ACCOUNT_JSON` | no; without it the Play part is skipped | the read-only plan |
| `checks.yml` | none | | |

The App Store Connect key must have the Admin role (build numbers, cloud-managed
signing, upload, listing, review). The key and issuer IDs are set only on the steps
that need them; altool receives them on its command line, the one place an ID is in
an argv. Failure logs are redacted (`ios redact-logs`) before they are uploaded as
artifacts, and nothing is uploaded when redaction fails.

## Handling

- Signing material is written only under `$RUNNER_TEMP`, with umask 077, and removed
  in `always()` steps. The Android keystore properties use Java properties escaping,
  so any password character works; the app checkout gets only a link for Gradle.
- The Android upload certificate is pinned per app (`android.upload_cert_sha256`).
  `android verify` fails when `ANDROID_KEYSTORE_BASE64` holds any other key, before
  anything is signed or published. After a Play upload-key reset, change the config
  value in the same change as the new keystore secret.
- App hooks run with every credential-like variable removed from their environment
  (names containing `TOKEN`, `SECRET`, `PASSWORD`, `PASSPHRASE`, `CREDENTIAL`, `_KEY`,
  `KEY_`, `P12` or `P8`, or starting with `ASC_`, `APP_STORE_CONNECT_`, `ANDROID_KEY`,
  `PLAY_SERVICE_ACCOUNT`, `GH_`, `ACTIONS_ID_TOKEN_REQUEST_` or `ACTIONS_RUNTIME_`).
- Runs started by Dependabot get no Actions secrets, so the release workflows cannot
  validate a Dependabot branch until someone dispatches them by hand.

## The shared CI signing identity

`IOS_DEV_SIGNING_P12_BASE64` holds the CI Apple Development identity: certificate
"Apple Development: Created via API", ID `NVNGDQGCKK`, **expires 2027-10-14**. It
signs every app's archive; App Store signing happens at export with Apple's
cloud-managed distribution certificate. Apple allows one API-created development
certificate per API key, so the same identity is shared by every app that builds on
GitHub Actions, and its private key exists only in those repositories'
`IOS_DEV_SIGNING_*` secrets.

When it expires or is revoked: revoke it first (App Store Connect API,
`DELETE /v1/certificates/<id>`), create a new one (`certificateType: DEVELOPMENT`),
and replace both `IOS_DEV_SIGNING_*` secrets in every app repository that has them.

### Giving another app the identity

Do not rotate it. Copy both `IOS_DEV_SIGNING_*` secrets on GitHub's side, so the
private key never leaves GitHub:

1. Read the target repository's Actions public key:
   `gh api repos/<owner>/<repo>/actions/secrets/public-key` (`key` and `key_id`).
2. On a temporary branch of a repository that already has the secrets and whose
   workflows do not run on other branches, push the workflow below with `<key>` filled
   in. It prints only the sealed ciphertext, which only GitHub can decrypt for the
   target repository. A public repository's run log is public until step 4 deletes it,
   but it never contains a usable secret.
3. Store each sealed value in the target (`<run id>` is the finished temporary run).
   This stops on the first error and refuses anything but exactly two sealed values:

   ```sh
   (
     set -euo pipefail
     sealed=$(gh run view <run id> -R <owner>/<source repo> --log \
       | grep -oE 'SEALED [A-Z0-9_]+ [A-Za-z0-9+/=]+')
     [ "$(printf '%s\n' "$sealed" | grep -c '^SEALED ')" -eq 2 ] \
       || { echo "expected 2 sealed values" >&2; exit 1; }
     while read -r _ name value; do
       gh api -X PUT "repos/<owner>/<repo>/actions/secrets/$name" \
         -f encrypted_value="$value" -f key_id="<key_id>" --silent
     done <<< "$sealed"
   )
   ```

4. Once `gh secret list -R <owner>/<repo>` shows both `IOS_DEV_SIGNING_*` secrets with
   today's date, delete the temporary branch and its run
   (`gh api -X DELETE repos/<owner>/<source repo>/git/refs/heads/<branch>`,
   `gh run delete <run id> -R <owner>/<source repo>`).

Then add the three `APP_STORE_CONNECT_*` secrets and run a validation dispatch.

The temporary workflow (`.github/workflows/tmp-seal-ci-signing.yml`):

```yaml
name: Seal CI signing secrets (temporary)
on:
  push:
    branches: [tmp/seal-ci-signing]
permissions: {}
jobs:
  seal:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: Seal with the target repository's Actions public key
        env:
          IOS_DEV_SIGNING_P12_BASE64: ${{ secrets.IOS_DEV_SIGNING_P12_BASE64 }}
          IOS_DEV_SIGNING_P12_PASSWORD: ${{ secrets.IOS_DEV_SIGNING_P12_PASSWORD }}
          TARGET_PUBLIC_KEY: "<key>"
        run: |
          python3 -m venv "$RUNNER_TEMP/venv"
          "$RUNNER_TEMP/venv/bin/pip" install --quiet pynacl==1.5.0
          "$RUNNER_TEMP/venv/bin/python" - <<'PY'
          import base64, os
          from nacl import encoding, public
          key = public.PublicKey(os.environ["TARGET_PUBLIC_KEY"].encode(), encoding.Base64Encoder())
          box = public.SealedBox(key)
          for name in ("IOS_DEV_SIGNING_P12_BASE64", "IOS_DEV_SIGNING_P12_PASSWORD"):
              value = os.environ[name]
              if not value:
                  raise SystemExit(f"missing {name}")
              sealed = base64.b64encode(box.encrypt(value.encode())).decode()
              print(f"SEALED {name} {sealed}")
          PY
```
