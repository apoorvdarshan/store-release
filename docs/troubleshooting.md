# Troubleshooting and FAQ

Every run says what it checked and why it stopped. This page explains the messages people
meet most, and answers the common questions. Each heading quotes the fixed part of a
message, with `<...>` where the run fills in a value; `tests/workflows/test_docs.py` fails
when a quoted message no longer exists in the code.

## Before anything is built

### `release runs need an <prefix>* tag, got <ref>`

A release run started from something other than a tag with the platform's prefix: a branch
push, or an iOS tag on the Android workflow. Push a tag `<tag_prefix><version>`
(`ios.tag_prefix`, `android.tag_prefix`). To check a build without releasing, use **Run
workflow** instead: a validation run.

### `does not match MARKETING_VERSION`

The tag is not the project's version, so the release would ship the wrong number. Bump
`MARKETING_VERSION` (the same in every configuration of `ios.app_target`) or fix the tag.
Tags never move: delete a wrong tag that started nothing, or push the next one.

### `does not match versionName`

The same for Android: the tag must be `<android.tag_prefix><versionName>` from
`android.gradle_file`.

### `is not a valid switch value; use true/false (or leave it unset)`

A repository variable holds something other than `true/false`, `yes/no`, `on/off` or
`1/0`. Invalid values fail the run even when the master switch is off, so a typo can never
read as "off" ([switches.md](switches.md)).

### `is on, but <config> has no [<table>]`

A switch is on but the config has no table for it, for example `IOS_UPLOAD_LISTING` without
`[ios.listing]`. Add the table ([config.md](config.md)) or turn the switch off.
`IOS_SUBMIT_REVIEW` needs `[ios.products]` (an empty table is enough).

### `IOS_SUBMIT_REVIEW needs IOS_UPLOAD_BUILD`

Review submission only ever uses the build this run uploaded. Turn on `IOS_UPLOAD_BUILD`
too; an older build is never submitted.

### `turn on IOS_CREATE_VERSION, or create the version in App Store Connect first`

The listing, What's New, screenshot, creative and review steps edit an App Store version,
and the tag's version does not exist yet. Turn on `IOS_CREATE_VERSION` or add the version
in App Store Connect.

### `not creating App Store version <version> (nothing was changed)`

`IOS_CREATE_VERSION` refused, before anything was built. The lines below it say why:
another version is still on its way to the store (`is still <state>; release or remove it
in App Store Connect before <version> can be created`), or the tag is `not newer than the
existing version`. App Store Connect allows one version in flight at a time.

### `missing secret(s): `

A secret the job needs is not set in the app repository. The message names it; the
reference is [secrets.md](secrets.md). Secrets only some switches need are checked once
the switches show they are needed.

### `the caller workflows differ from what store-release v`

Store checks found a caller that is not what this store-release version renders for your
config (each differing key is listed). Callers are generated: re-render them with
`run.py callers render` (or `run.py init --force`) at the pinned release instead of
editing them by hand. Comments don't count.

### `has no release tag v`

A caller pins a commit that is not a store-release release. Pin the commit the `vX.Y.Z`
tag points at (what `init`, `callers render` and Dependabot do). `is <sha>, not the pinned
<sha>` means the comment's version and the commit disagree.

### `is yanked (docs/yanked.md); update the pin to the fixed release`

The pinned version was withdrawn ([yanked.md](yanked.md)). Merge the Dependabot bump (or
re-render at the fixed release). Release runs only warn; Store checks fail.

### `is not a folder in the app repository`

`config check --files` checks every file and folder the config names, including gate
workflows, hooks, the project and the listing documents. Fix the path in the config, or
add the file. `does not exist in the app repository` is the same for a path of any kind.

### `set ios.info_plist = `

The app target has an `INFOPLIST_FILE` but the config names none, so the jobs would not
check it out. Copy the suggested value into `ios.info_plist`. Leave the key out only when
Xcode generates the whole Info.plist (`nor GENERATE_INFOPLIST_FILE = YES` means the project
has neither).

### `App Store builds need a released Xcode`

`ios.build.developer_dir` points at a beta Xcode. App Store Connect rejects builds from
beta toolchains; point it at the released Xcode on the runner (`ios.build.runner`).

## During the build

### `does not declare export compliance`

Every configuration of the app target must declare `ITSAppUsesNonExemptEncryption`, in
the Info.plist or as `INFOPLIST_KEY_ITSAppUsesNonExemptEncryption`. Without it App Store
Connect asks about every build by hand and review submission fails. `NO` covers apps that
use only HTTPS, the Keychain and Apple's system crypto; `YES` also needs
`ITSEncryptionExportComplianceCode`. Pin the answer with `ios.export_compliance`.

### `no valid signing identity in the CI keychain`

`IOS_DEV_SIGNING_P12_BASE64` does not hold a usable Apple Development identity (a
certificate with its private key), or `IOS_DEV_SIGNING_P12_PASSWORD` does not open it.
Export it again ([secrets.md](secrets.md#the-ios-development-identity)).

### `archive failed (full log in artifact)`

`xcodebuild archive` failed. The job prints the errors and the last 60 lines; the redacted
full log is the `ios-release-logs-*` artifact. The usual causes are a scheme that is not
shared, a target without automatic signing for Release, or files the build needs outside
`ios.build.checkout`.

### `the keystore's upload certificate is <sha>, not the pinned <sha>`

`ANDROID_KEYSTORE_BASE64` holds a different key than `android.upload_cert_sha256` pins.
Nothing was signed or published. After a Play upload-key reset, change the config value in
the same pull request as the new keystore secret.

### `is not new to Google Play (highest versionCode already uploaded: `

Play already has this versionCode or a higher one. Bump `versionCode` in
`android.gradle_file` and tag again. The check runs before the build.

### `timed out waiting for <version> (<build>) to finish processing`

App Store Connect had not finished processing the upload within `IOS_BUILD_WAIT_MINUTES`.
Re-run the failed jobs: `wait-for-build` is its own job, so the upload is not repeated
([rerun.md](rerun.md)).

### `got no tool_sha from this run's prepare/gates job (was the output dropped?)`

A job output arrived empty. GitHub drops an output that contains a secret's value, so a
secret whose value is short and common (for example `1` or `true`) can blank outputs.
Use real secret values; outputs here are only versions, numbers and switch states.

## Questions

### Which platforms and project types work?

Native iOS apps built from an `.xcodeproj` and scheme with `xcodebuild archive`
(Swift Package Manager dependencies are cached), and Android apps built with Gradle Kotlin
DSL, whose `build.gradle.kts` declares `versionName = "X"` and `versionCode = N`. Not
supported in this version: `.xcworkspace` (CocoaPods) projects, Groovy `build.gradle`
version lines, and macOS, tvOS or watchOS-only apps. An iOS-only or Android-only app
leaves out the other table.

### Does it work for private repositories?

Yes, from v0.3.0. The jobs fetch the app's files with the run's read-only token. On
private repositories GitHub bills macOS minutes at a higher rate, and the iOS build runs
on macOS.

### Which runner builds the iOS app?

`xcode-27` by default. Set `ios.build.runner` to another GitHub-hosted macOS label or a
self-hosted runner, and `ios.build.developer_dir` to its Xcode.

### Can I upload to TestFlight or Play testing only?

Yes. `IOS_UPLOAD_BUILD` alone uploads the build and waits until App Store Connect has
processed it; nothing is submitted for review. On Android, turn on only
`ANDROID_INTERNAL_TESTING` or `ANDROID_CLOSED_TESTING`: production is a separate switch
that the testing tracks never imply.

### What does a validation run do?

**Run workflow** archives, signs and verifies the build exactly as a tag run would, then
validates the IPA with App Store Connect (iOS) or verifies the signed AAB and APK
(Android), and uploads nothing. Its `store-plan` job prints, read-only, what a tag run
with the current switches would change ([plan.md](plan.md)).

### How is the iOS build number chosen?

Prepare reads the app's uploaded builds from App Store Connect and uses the higher of the
project's `CURRENT_PROJECT_VERSION` and the highest uploaded build number plus one. The archive
gets it as a build setting; the project file is not changed.

### Why does a Dependabot pull request not run the release workflows?

GitHub gives runs started by Dependabot no Actions secrets. Store checks still run on the
bump; dispatch the release workflows on its branch by hand to validate it
([release-process.md](release-process.md#how-apps-receive-a-release)).

### A run shows no jobs at all ("Invalid workflow file")

GitHub rejected the caller before any job started: a pin that does not exist, a secret
passed that the reusable workflow does not declare (or a required one left out), or a job
that asks for more permissions than its caller grants. Re-render the callers; the run
page names the line ([workflows.md](workflows.md#startup-failures)).

### How do I stop or roll back an upgrade?

Revert the pull request that moved the pin. The old commit stays valid: tags never move
and pins are commit SHAs.

### Where do I report a problem?

Open an issue with the run's log (the logs are redacted, but check before you paste) and
your store-release version. Security problems go privately ([SECURITY.md](../SECURITY.md)).
