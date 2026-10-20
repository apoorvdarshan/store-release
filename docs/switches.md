# Switches (repository variables)

Switches are the app repository's **Variables** (Settings → Secrets and variables →
Actions → Variables). A called workflow reads the caller repository's variables, so each
app keeps its own values; callers never pass switches as inputs. Every switch is off until
you set it, so a new app's first tag only builds and creates GitHub Releases. The names,
with comments, are in [`gates.env.example`](../gates.env.example); set one with the GitHub
CLI:

```bash
gh variable set IOS_RELEASE_AUTOMATION --body true
gh variable set IOS_UPLOAD_BUILD --body true
```

`tests/workflows/test_docs.py` checks that this page has a row, with its default, for every
switch the code reads.

**Nothing reaches a store unless its switch is on.**

- Missing or empty means off. On is `1/true/yes/on`, off is `0/false/no/off`
  (any case). Any other value fails the run, even when the platform's master switch
  is off.
- A tag push (`<tag_prefix><version>`) is the only release. A manual **Run
  workflow** only validates: every operation below is forced off, and the read-only
  `store-plan` job prints what a tag run would do with the current values
  ([plan.md](plan.md)).
- A switch that is on needs its config table ([config.md](config.md)); the gates fail
  and name the table otherwise.
- Print the values a run would read: `run.py print-switches --platform all`
  (an invalid value shows as `INVALID`).

## iOS

| Variable | Default | When on |
|---|---|---|
| `IOS_RELEASE_AUTOMATION` | off | Master switch. When off, every iOS store operation below is skipped; the tag still gets its GitHub Release |
| `IOS_CREATE_VERSION` | off | Create the tag's App Store version (Prepare for Submission) when App Store Connect does not have it, before the read-only version check. An existing version is left alone. Refuses, before anything is built and without writing, when another version is still on its way to the store (anything but Ready for Distribution, replaced or removed) or the tag is not newer than every existing version. Tag runs only |
| `IOS_UPLOAD_BUILD` | off | Build, sign and upload the binary to App Store Connect, then wait for processing |
| `IOS_UPLOAD_LISTING` | off | Description, keywords, promotional text, marketing and support URLs from `[ios.listing]`, checked before the build. Off: never read |
| `IOS_UPLOAD_WHATS_NEW` | off | What's New text from `[ios.whats_new]`, checked before the build. Off: never read |
| `IOS_UPLOAD_SCREENSHOTS` | off | The configured `[[ios.screenshots.sets]]`, staged as RGB without alpha. A set is replaced only when its files differ |
| `IOS_UPLOAD_CREATIVE_ASSETS` | off | Product page header and search results images from `[ios.creative]`, checked (size, format, no alpha) before the build. A new image is uploaded to the Asset Library once and sent to App Review in a submission of its own (never the version's); an approved image is placed on the version's page while the version can be edited. Off: never read |
| `IOS_SUBMIT_REVIEW` | off | Select this run's build and submit the version and the catalog's Ready-to-Submit in-app purchases and subscriptions for review. Requires `IOS_UPLOAD_BUILD` and `[ios.products]` |
| `IOS_PUBLIC_RELEASE` | off | Release automatically after approval (`AFTER_APPROVAL`). Off: you release it by hand (`MANUAL`). Requires `IOS_SUBMIT_REVIEW` |
| `IOS_BUILD_POLL_SECONDS` | 30 | Build readiness poll interval in seconds (10–600) |
| `IOS_BUILD_WAIT_MINUTES` | 120 | Total build wait from upload start, in minutes (10–300) |

The listing, What's New, screenshot, creative and review switches need the App Store
version to exist: turn on `IOS_CREATE_VERSION`, or create it in App Store Connect
first. `prepare` checks it (read-only) and stops before anything is built when it is
missing or already in review.

## Android

| Variable | Default | When on |
|---|---|---|
| `ANDROID_RELEASE_AUTOMATION` | off | Master switch. On: the tag builds and verifies the signed AAB and APK and creates the GitHub Release. Off: the tag builds and releases nothing (a green no-op), and every switch below is ignored |
| `ANDROID_UPLOAD_LISTING` | off | Play title, short and full description from `[android.listing]`, checked before the build. Off: never read |
| `ANDROID_UPLOAD_WHATS_NEW` | off | What's New (`<locale>` block) from `[android.whats_new]` on this run's Play release, checked before the build. Needs a track below; skipped with a note otherwise. Off: never read |
| `ANDROID_UPLOAD_SCREENSHOTS` | off | Play phone screenshots from `[android.screenshots]` (padded to 9:16, at most 8). The whole set is replaced |
| `ANDROID_INTERNAL_TESTING` | off | Upload this build to Play **internal testing** |
| `ANDROID_CLOSED_TESTING` | off | Upload this build to Play **closed testing** (the `alpha` track) |
| `ANDROID_PRODUCTION_ROLLOUT` | off | Upload this build to Play **production**, live, and mark the GitHub Release Latest (when it is the highest version). Off: the GitHub Release is a pre-release |

Track switches are independent: testing tracks never imply production, and
production never falls back to a testing track. The build is uploaded once to every
selected track, and each track is read back afterwards. The Play upload is never
called without a track (the action would default to production).

## Shared

| Variable | Default | When on |
|---|---|---|
| `STORE_SYNC_REVENUECAT` | off | **Fails the run**: RevenueCat write/sync is not implemented (the gate runs in iOS prepare and in the Android release job after the Play listing step). An invalid value fails every tag run before anything is built, even with the master switch off (iOS prepare, Android gates) |

## What each job sees

The resolved switches travel between jobs as plain `true`/`false` outputs, and every
job re-checks them ([workflows.md](workflows.md)). The store steps read these
environment flags, set by the workflows from the resolved switches, never directly
from variables:

| Flag | Read by | From |
|---|---|---|
| `UPLOAD_LISTING`, `UPLOAD_WHATS_NEW`, `UPLOAD_SCREENSHOTS`, `UPLOAD_CREATIVE_ASSETS`, `SUBMIT_IOS_REVIEW`, `PUBLIC_RELEASE` | `ios store-step` | the iOS switches above |
| `CREATE_IOS_VERSION` | `ios store-step --create-version` (prepare) | `IOS_CREATE_VERSION` |
| `UPLOAD_LISTING`, `UPLOAD_SCREENSHOTS` | `android play-listing` | the Android switches above |
| `STORE_UPLOAD_SCREENSHOTS` | `screenshots stage`: stage for upload, validating every frame (without Pillow, an App Store frame that stores alpha fails) | set to `true` by the jobs that upload screenshots |
