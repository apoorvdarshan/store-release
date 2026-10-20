# Secrets

store-release has no secrets of its own. Each app keeps its credentials in its own
repository (Settings → Secrets and variables → Actions → Secrets), and each caller job
passes exactly the secrets its reusable workflow declares, by name:

```yaml
    secrets:
      APP_STORE_CONNECT_API_KEY_ID: ${{ secrets.APP_STORE_CONNECT_API_KEY_ID }}
```

`secrets: inherit` is never used. A secret the workflow does not declare, or a
`required: true` secret the caller does not pass, is a startup failure
([workflows.md](workflows.md#startup-failures)); `callers render` and the pull request
caller check keep the mappings exact.

Every job that uses a secret first checks that it is set (as a boolean, `secrets.X != ''`;
the value is never read for this) and names any that is missing.

## What an app needs

| Secret | Platform | What it is | How to get it |
|---|---|---|---|
| `APP_STORE_CONNECT_API_KEY_ID` | iOS | the App Store Connect API key's ID | App Store Connect → Users and Access → Integrations → App Store Connect API → Team Keys: generate a key with the **Admin** role (build numbers, cloud-managed signing, upload, listing, review), copy its Key ID |
| `APP_STORE_CONNECT_ISSUER_ID` | iOS | the team's API issuer ID | the same page, above the keys |
| `APP_STORE_CONNECT_API_KEY_P8` | iOS | the key's private key, the whole `AuthKey_<ID>.p8` file | downloaded once when the key is created |
| `IOS_DEV_SIGNING_P12_BASE64` | iOS | an Apple Development identity (certificate and private key) as a base64 `.p12` | [below](#the-ios-development-identity) |
| `IOS_DEV_SIGNING_P12_PASSWORD` | iOS | the `.p12`'s password | chosen when exporting it |
| `ANDROID_KEYSTORE_BASE64` | Android | the Play **upload** keystore (`.jks` or `.keystore`) as base64 | the keystore the app's releases are uploaded with (with Play App Signing, not Google's app signing key) |
| `ANDROID_KEYSTORE_PASSWORD` | Android | the keystore's password | |
| `ANDROID_KEY_ALIAS` | Android | the upload key's alias | `keytool -list -keystore upload.jks` |
| `ANDROID_KEY_PASSWORD` | Android | the upload key's password | often the same as the keystore's |
| `PLAY_SERVICE_ACCOUNT_JSON` | Android | a Google Cloud service account's JSON key | [below](#the-google-play-service-account); optional until a Play listing, screenshot or track switch is on |

Set them with the GitHub CLI from the app repository (values are read from stdin or a file,
so they never appear in your shell history):

```bash
gh secret set APP_STORE_CONNECT_API_KEY_ID          # paste the Key ID
gh secret set APP_STORE_CONNECT_ISSUER_ID           # paste the Issuer ID
gh secret set APP_STORE_CONNECT_API_KEY_P8 < AuthKey_ABC123DEFG.p8
base64 -i dev-signing.p12 | gh secret set IOS_DEV_SIGNING_P12_BASE64
gh secret set IOS_DEV_SIGNING_P12_PASSWORD
base64 -i upload.jks | gh secret set ANDROID_KEYSTORE_BASE64
gh secret set ANDROID_KEYSTORE_PASSWORD
gh secret set ANDROID_KEY_ALIAS
gh secret set ANDROID_KEY_PASSWORD
gh secret set PLAY_SERVICE_ACCOUNT_JSON < play-service-account.json
```

Then delete the local copies of the key files you no longer need.

### The iOS development identity

The build job archives with an Apple Development identity and lets Xcode manage the
development profiles (`-allowProvisioningUpdates` with the API key). The App Store
signature is applied at export with Apple's cloud-managed distribution certificate, so no
distribution certificate or profile is ever stored. The app target needs automatic signing
for its Release configuration.

To make the identity on a Mac: Xcode → Settings → Accounts → your team → Manage
Certificates → **+** → Apple Development. Then, in Keychain Access, export the new
"Apple Development: ..." identity (the certificate with its private key) as a `.p12` with a
password, and store it as the two `IOS_DEV_SIGNING_*` secrets.

Development certificates expire after a year. When one expires or is revoked, make a new
identity and replace both secrets in every repository that has them.

### The Google Play service account

1. In Google Cloud, in any project, enable the **Google Play Android Developer API** and
   create a service account; add a JSON key and download it.
2. In Play Console → Users and permissions, invite the service account's e-mail address
   and grant it, for the app, the permissions the switches you use need: release to testing
   tracks or production, and manage the store listing.
3. Store the JSON file as `PLAY_SERVICE_ACCOUNT_JSON`.

One service account can serve all of a developer account's apps.

## Per workflow

`tests/workflows/test_docs.py` checks this table against the `secrets:` each reusable
workflow declares.

| Workflow | Secret | Required | Used for |
|---|---|---|---|
| `ios-prepare.yml` | `APP_STORE_CONNECT_API_KEY_ID` | no | checked when the run builds or an App Store Connect switch is on: version creation, the read-only version preflight, the next build number |
| `ios-prepare.yml` | `APP_STORE_CONNECT_ISSUER_ID` | no | as above |
| `ios-prepare.yml` | `APP_STORE_CONNECT_API_KEY_P8` | no | as above |
| `ios-build.yml` | `APP_STORE_CONNECT_API_KEY_ID` | yes | cloud-managed distribution signing at export, `altool` validation, upload |
| `ios-build.yml` | `APP_STORE_CONNECT_ISSUER_ID` | yes | as above |
| `ios-build.yml` | `APP_STORE_CONNECT_API_KEY_P8` | yes | as above |
| `ios-build.yml` | `IOS_DEV_SIGNING_P12_BASE64` | yes | the Apple Development identity that signs the archive |
| `ios-build.yml` | `IOS_DEV_SIGNING_P12_PASSWORD` | yes | its password |
| `ios-wait-for-build.yml` | `APP_STORE_CONNECT_API_KEY_ID` | yes | waiting until this run's build is processed (read-only) |
| `ios-wait-for-build.yml` | `APP_STORE_CONNECT_ISSUER_ID` | yes | as above |
| `ios-wait-for-build.yml` | `APP_STORE_CONNECT_API_KEY_P8` | yes | as above |
| `ios-app-store-connect.yml` | `APP_STORE_CONNECT_API_KEY_ID` | yes | listing, What's New, screenshots, creative images, build selection, review |
| `ios-app-store-connect.yml` | `APP_STORE_CONNECT_ISSUER_ID` | yes | as above |
| `ios-app-store-connect.yml` | `APP_STORE_CONNECT_API_KEY_P8` | yes | as above |
| `ios-source-archive.yml` | none | | `id-token: write` for the keyless cosign signature and the provenance attestation |
| `ios-github-release.yml` | none | | the caller's `GITHUB_TOKEN` (`contents: write`) |
| `ios-store-plan.yml` | `APP_STORE_CONNECT_API_KEY_ID` | yes | the read-only plan |
| `ios-store-plan.yml` | `APP_STORE_CONNECT_ISSUER_ID` | yes | as above |
| `ios-store-plan.yml` | `APP_STORE_CONNECT_API_KEY_P8` | yes | as above |
| `android-gates.yml` | `PLAY_SERVICE_ACCOUNT_JSON` | no | checked when a Play track is on: the read-only check that the versionCode is new to Play |
| `android-publish.yml` | `ANDROID_KEYSTORE_BASE64` | yes | signing the AAB and APK with the upload key (validation runs too) |
| `android-publish.yml` | `ANDROID_KEYSTORE_PASSWORD` | yes | as above; with `android.build.signing = "gradle-properties"` it is also the key password |
| `android-publish.yml` | `ANDROID_KEY_ALIAS` | no | required for `keystore-properties` signing (the standard flow); `gradle-properties` takes the alias from `android.build.key_alias` |
| `android-publish.yml` | `ANDROID_KEY_PASSWORD` | no | required for `keystore-properties` signing (the standard flow) |
| `android-publish.yml` | `PLAY_SERVICE_ACCOUNT_JSON` | no | checked when a listing, screenshot or track switch is on: Play listing, uploads, track read-back |
| `android-store-plan.yml` | `PLAY_SERVICE_ACCOUNT_JSON` | no | the read-only plan; without it the Play part is skipped |
| `checks.yml` | none | | |

The key and issuer IDs are set only on the steps that need them; `altool` receives them on
its command line, the one place an ID is in an argv. Failure logs are redacted
(`ios redact-logs`) before they are uploaded as artifacts, and nothing is uploaded when
redaction fails.

## Handling

- Signing material is written only under `$RUNNER_TEMP`, with umask 077, and removed in
  `always()` steps. The Android keystore properties use Java properties escaping, so any
  password character works; the app checkout gets only a link for Gradle.
- The Android upload certificate is pinned per app (`android.upload_cert_sha256`).
  `android verify` fails when `ANDROID_KEYSTORE_BASE64` holds any other key, before
  anything is signed or published. After a Play upload-key reset, change the config value
  in the same change as the new keystore secret.
- App hooks run with every credential-like variable removed from their environment (names
  containing `TOKEN`, `SECRET`, `PASSWORD`, `PASSPHRASE`, `CREDENTIAL`, `_KEY`, `KEY_`,
  `P12` or `P8`, or starting with `ASC_`, `APP_STORE_CONNECT_`, `ANDROID_KEY`,
  `PLAY_SERVICE_ACCOUNT`, `GH_`, `ACTIONS_ID_TOKEN_REQUEST_` or `ACTIONS_RUNTIME_`).
- The app checkout keeps no credentials; job-setup fetches a job's files with the job's
  read-only `GITHUB_TOKEN`, given to that one git command as environment config.
- Runs started by Dependabot get no Actions secrets, so the release workflows cannot
  validate a Dependabot branch until someone dispatches them by hand.

## Sharing one development identity across your apps

One Apple Development identity can sign the archives of every app your team builds on
GitHub Actions. To give another repository the identity without the private key leaving
GitHub, copy both `IOS_DEV_SIGNING_*` secrets on GitHub's side:

1. Read the target repository's Actions public key:
   `gh api repos/<owner>/<repo>/actions/secrets/public-key` (`key` and `key_id`).
2. On a temporary branch of a repository that already has the secrets and whose workflows
   do not run on other branches, push the workflow below with `<key>` filled in. It prints
   only the sealed ciphertext, which only GitHub can decrypt for the target repository. A
   public repository's run log is public until step 4 deletes it, but it never contains a
   usable secret.
3. Store each sealed value in the target (`<run id>` is the finished temporary run). This
   stops on the first error and refuses anything but exactly two sealed values:

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
