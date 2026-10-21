# store-release

[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15750/badge)](https://www.bestpractices.dev/projects/15750)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/apoorvdarshan/store-release/badge)](https://scorecard.dev/viewer/?uri=github.com/apoorvdarshan/store-release)

Release your iOS and Android apps to App Store Connect and Google Play from a git tag.
store-release is a set of reusable GitHub Actions workflows plus the Python tooling they
run: it builds, signs, verifies and uploads your app, edits the store listing, and submits
for review, but only the steps you switch on. Your repository keeps one config file and
three small caller workflows pinned to a release commit. No Fastlane and no Mac of your
own: everything runs on GitHub Actions.

## Features

- **App Store Connect and Google Play** from one config, for an iOS app, an Android app
  or both. App Store Connect is driven by a hand-written client for its REST API, Google
  Play through the Android Publisher API.
- **Switches for every store operation** (repository variables), all off until you set
  them: version creation, build upload, listing, What's New, screenshots, product page
  images, review submission and release on iOS; listing, What's New, screenshots and the
  internal, closed and production tracks on Android. An invalid value fails the run instead
  of reading as off ([switches](docs/switches.md)).
- **Validation runs.** **Run workflow** archives, signs, exports and validates the build
  exactly as a release would, and uploads nothing. Only a tag push releases.
- **A read-only store plan** on every validation run: what a tag run with the current
  switches would change in App Store Connect, Google Play and GitHub Releases, through
  clients that refuse every write ([plan](docs/plan.md)).
- **Screenshots staged as RGB** without an alpha channel (App Store Connect refuses alpha
  even when every pixel is opaque), fitted to the accepted sizes, and replaced only when
  they differ. iPhone, iPad, **iPhone Duo** and **Apple Watch** sets;
  Play phone frames padded to 9:16.
- **Product page header and search results images** (App Store creative assets): checked
  before the build, uploaded to the Asset Library once, sent to App Review in a submission
  of their own and placed on the version when approved.
- **Products and review drafts.** The catalog's in-app purchases and subscriptions go to
  review with the version; the review draft is picked and checked before the first write,
  so a re-run finishes a draft instead of creating another.
- **Checks before builds.** Tag, version, build number, export compliance, the switches,
  the store files and the App Store version are checked before anything is built; the
  build waits for this run's own processed build and never substitutes an older one.
- **Signed releases.** Android AABs and APKs are verified against your pinned upload
  certificate, then signed with Sigstore and attested (SLSA provenance). store-release's own
  releases are signed and attested the same way ([verification](docs/verification.md)).
- **Safe re-runs.** Each stage is its own job, so **Re-run failed jobs** resumes where it
  stopped ([rerun](docs/rerun.md)).
- **Pinned and reviewable.** Your callers pin a release commit SHA; Store checks on every
  pull request validate your store files, config and callers; Dependabot proposes updates.

## Install

The `store-release` command, the same CLI the workflows run, is on
[PyPI](https://pypi.org/project/store-release/) for Python 3.11 or later:

```bash
pipx run store-release init --help     # run the latest release without installing it
pipx install store-release             # or install the command
```

Without `--app-root` it works on the app in the current directory. `init`, `callers render`
and the `config`, `caller` and `pins` checks need only the standard library; the extras add
what the other commands import: `store-release[ios]` (PyJWT, for App Store Connect),
`[android]` (the Google API client, for Google Play), `[images]` (Pillow, for screenshots),
`[checks]` (PyYAML, for `caller check --pr`) or `[all]`, for example
`pipx install "store-release[all]"`. PyPI releases start after v0.3.0; for v0.3.0 itself, use
a checkout of the release (below). Your workflows never install the package: they run the
release commit you pinned, with hash-locked requirements.

## Quickstart

### 1. Check your app

- **iOS:** an `.xcodeproj` with a shared scheme and automatic signing for Release; the
  same `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in every configuration of the app
  target; `ITSAppUsesNonExemptEncryption` declared (in the Info.plist or as
  `INFOPLIST_KEY_ITSAppUsesNonExemptEncryption`). The app must exist in App Store Connect.
- **Android:** a Gradle Kotlin DSL `build.gradle.kts` with one `versionName = "X"` and
  one `versionCode = N` line, signing its release from `keystore.properties` (`storeFile`,
  `storePassword`, `keyAlias`, `keyPassword`; gitignored), and the app in Play Console.
- **Tests:** a workflow per platform that runs your tests with `on: workflow_call`, for
  example `.github/workflows/ios-tests.yml`. A tag releases only after it passes.

### 2. Add the config and the callers

Run `init` in your app repository:

```bash
pipx run store-release init --platform all \
  --name "My App" \
  --bundle-id com.example.myapp --asc-app-id 1234567890 --team-id ABCDE12345 \
  --project ios/MyApp.xcodeproj \
  --package com.example.myapp --upload-cert-sha256 <Play upload certificate SHA-256>
```

It writes `store/store-release.toml` ([every key](docs/config.md)), the callers
`.github/workflows/ios-release.yml`, `android-release.yml` and `store-checks.yml` pinned to
the commit of the release it comes from, and `.github/dependabot.yml` if you have none. Leave
out flags to be asked for them; `--platform ios` or `android` sets up one platform;
`init --help` lists the rest. The [templates](examples/templates/) show the result for
placeholder values. Check the files with:

```bash
pipx run store-release config check --files
```

Without pipx, run the same commands from a checkout of the
[latest release](https://github.com/apoorvdarshan/store-release/releases/latest) (`vX.Y.Z`):

```bash
git clone --depth 1 --branch vX.Y.Z https://github.com/apoorvdarshan/store-release /tmp/store-release
python3 -I -u /tmp/store-release/run.py --app-root . init --platform all --name "My App" ...
python3 -I -u /tmp/store-release/run.py --app-root . config check --files
```

### 3. Set the secrets

Each platform needs its credentials as repository secrets ([how to get each](docs/secrets.md)):

| iOS | Android |
|---|---|
| `APP_STORE_CONNECT_API_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`, `APP_STORE_CONNECT_API_KEY_P8` (an Admin API key) | `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` (the upload key) |
| `IOS_DEV_SIGNING_P12_BASE64`, `IOS_DEV_SIGNING_P12_PASSWORD` (an Apple Development identity) | `PLAY_SERVICE_ACCOUNT_JSON` (a Play service account) |

```bash
gh secret set APP_STORE_CONNECT_API_KEY_P8 < AuthKey_ABC123DEFG.p8
base64 -i upload.jks | gh secret set ANDROID_KEYSTORE_BASE64
```

### 4. Turn on the switches you want

Every switch is a repository variable and off until set ([all switches](docs/switches.md)).
Start small, for example a first iOS upload and an internal Play test:

```bash
gh variable set IOS_RELEASE_AUTOMATION --body true
gh variable set IOS_CREATE_VERSION --body true
gh variable set IOS_UPLOAD_BUILD --body true
gh variable set ANDROID_RELEASE_AUTOMATION --body true
gh variable set ANDROID_INTERNAL_TESTING --body true
```

A switch that edits the store needs its config table (`IOS_UPLOAD_LISTING` needs
`[ios.listing]`, ...): uncomment it in `store-release.toml` as you turn the switch on.

### 5. Validate

Commit, then **Run workflow** on **iOS Release** and **Android Release**. The validation
run builds and signs exactly as a release would, uploads nothing, and its `store-plan` job
prints what a tag run would change. Fix anything it reports ([troubleshooting](docs/troubleshooting.md)).

### 6. Release

Push a tag of the version in your project: `ios-v1.2` (the iOS `MARKETING_VERSION`) and
`android-v1.2` (the Android `versionName`), or `v1.2` for a single-platform app. Each tag
runs your tests, then every switched-on step in order, and creates a GitHub Release with
the tag's section of `RELEASE_NOTES.md` or GitHub's generated notes (on Android, only with
`ANDROID_RELEASE_AUTOMATION` on; off, an Android tag is a green no-op).

### 7. Keep it updated

The `store-release` Dependabot group (written by `init`, or add it yourself) opens one pull
request per new store-release release, moving all three callers to the new commit. Store
checks run the new version against your files; for changes that touch the release path,
run the validation dispatches on that branch before merging
([release process](docs/release-process.md#how-apps-receive-a-release)).

```yaml
# .github/dependabot.yml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    groups:
      store-release:              # first, so store-release gets a pull request of its own
        patterns: ["apoorvdarshan/store-release*"]
      github-actions:
        patterns: ["*"]
    cooldown:                     # these pins run with your signing credentials
      default-days: 7
      include: ["apoorvdarshan/store-release*"]
```

## How it fits together

```
your repository                                   apoorvdarshan/store-release @ <release sha>
  store/store-release.toml   ── read by every job ─┐
  .github/workflows/ios-release.yml      ── calls ──► ios-prepare → ios-build → ios-wait-for-build
                                                     → ios-app-store-connect → ios-github-release
  .github/workflows/android-release.yml  ── calls ──► android-gates → android-publish
  .github/workflows/store-checks.yml     ── calls ──► checks (pull requests)
```

Every job checks out store-release at `job.workflow_sha`, so the workflow and the Python it
runs are always the commit you pinned and reviewed ([workflows](docs/workflows.md)).

## Verifying store-release

Each release ships its source archive, sdist and wheel, each with a SHA-256 and a Sigstore
signature bundle, and SLSA provenance attestations; PyPI carries the same sdist and wheel,
with PEP 740 attestations from the same workflow:

```bash
v=X.Y.Z
gh release download "v$v" --repo apoorvdarshan/store-release
sha256sum -c "store-release-$v-source.tar.gz.sha256"
gh attestation verify "store-release-$v-source.tar.gz" --repo apoorvdarshan/store-release \
  --signer-workflow apoorvdarshan/store-release/.github/workflows/release.yml
cosign verify-blob "store-release-$v-source.tar.gz" --bundle "store-release-$v-source.tar.gz.sigstore.json" \
  --certificate-identity 'https://github.com/apoorvdarshan/store-release/.github/workflows/release.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The same commands check `store_release-$v.tar.gz` and `store_release-$v-py3-none-any.whl`
([verification](docs/verification.md#store-releases-own-releases), including PyPI's files).

Tags are created only by the Release workflow after CI passed, and never move; your Store
checks fail unless your pin is the commit its `# vX.Y.Z` tag points at. Verifying your
app's own signed files: [verification](docs/verification.md).

## Safety model

- **Nothing reaches a store unless its switch is on.** Missing means off; an invalid value
  fails the run, even with the master switch off.
- **Checks before builds.** Everything that can fail runs before anything is built or
  uploaded; a manual run never uploads.
- **One pinned commit.** Every job checks out store-release at `job.workflow_sha`, so the
  workflow YAML and the Python it runs are the commit you pinned and reviewed.
- **Least privilege.** Explicit secret mapping (never `secrets: inherit`), explicit
  permissions per job, credentials only under `RUNNER_TEMP` and removed afterwards, app
  hooks run with credentials scrubbed from their environment, failure logs redacted before
  upload, and the app checkout never keeps a credential.
- **Read-only by construction** where it should be: the build wait, the preflights and the
  store plan use clients that refuse any write.

## Documentation

| Page | What it covers |
|---|---|
| [config.md](docs/config.md) | every key of `store/store-release.toml`, with types, defaults and examples |
| [secrets.md](docs/secrets.md) | each secret, how to get it, and which workflow uses it |
| [switches.md](docs/switches.md) | every repository variable and what it turns on |
| [troubleshooting.md](docs/troubleshooting.md) | common errors and questions |
| [workflows.md](docs/workflows.md) | the reusable workflows, their inputs, outputs and checks |
| [plan.md](docs/plan.md) | the read-only store plan |
| [rerun.md](docs/rerun.md) | re-running failed jobs safely |
| [verification.md](docs/verification.md) | verifying signed files and pins |
| [release-process.md](docs/release-process.md) | how store-release is released and how apps update |
| [flows.md](docs/flows.md) | the ported flows of the apps listed below |
| [provenance.md](docs/provenance.md) | where the code came from, and its history |
| [roadmap.md](docs/roadmap.md) | planned work and known gaps |
| [yanked.md](docs/yanked.md) | withdrawn versions |

## Versioning

SemVer tags `vX.Y.Z`, created by the Release workflow only after CI passed on that commit,
and never moved. Apps pin the commit SHA with a `# vX.Y.Z` comment and receive updates
through Dependabot. Every release's impact on apps (`checks-only`, `validation`,
`release-path` or `config-breaking`) and any action it needs are in the
[CHANGELOG](CHANGELOG.md).

## Used by

| App | Platforms | Repository |
|---|---|---|
| [Fud AI](https://fud-ai.app) | iOS, Android | [apoorvdarshan/fud-ai](https://github.com/apoorvdarshan/fud-ai) |
| [Scowld](https://apps.apple.com/in/app/scowld-ai-voice-companion/id6760672848) | iOS | [apoorvdarshan/scowld](https://github.com/apoorvdarshan/scowld) |
| [Verceltics](https://verceltics.com) | iOS, Android | [apoorvdarshan/verceltics](https://github.com/apoorvdarshan/verceltics) |
| [ABYSM](https://apps.apple.com/app/abysm-dive-the-deep-sea/id6783505799) | iOS | [apoorvdarshan/abysm](https://github.com/apoorvdarshan/abysm) |
| [PCM Notes](https://pcm-notes.apoorvdarshan.com) | iOS | [apoorvdarshan/pcm-notes](https://github.com/apoorvdarshan/pcm-notes) |
| [Quit All](https://apps.apple.com/us/app/quit-all-break-every-habit/id6760978934) | iOS | [apoorvdarshan/quit-all](https://github.com/apoorvdarshan/quit-all) |

Their configs and callers are in [examples/](examples/). Using store-release for your app?
Open a pull request adding it here.

## Development

```bash
python3 -I -u run.py selftest --platform all            # stdlib only
python3 -m pip install --require-hashes -r requirements.txt -r requirements-checks.txt
python3 -I -u run.py selftest --platform all --suite unit --suite apps --suite parity \
  --suite workflows --require-pillow --strict
```

Building and checking the sdist and wheel: [CONTRIBUTING](CONTRIBUTING.md#set-up).

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT License](LICENSE)
