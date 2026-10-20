# Store plan

Every validation dispatch (**Run workflow** on iOS Release or Android Release) runs a
`store-plan` job next to the validation build. It prints what a tag run of
`<tag_prefix><version>` would do in App Store Connect, Google Play and GitHub Releases
with the repository's current switch values, and sends no write. A clean plan on an
app's migration branch is part of the gate before the app moves to a new
store-release version.

## Contract

- **Report-only.** Exit 0 when the plan was computed. The last line is
  `store-plan: CLEAN` or `store-plan: <n> PROBLEM(S)`, and every problem is also a
  `::warning::` annotation, so the validation build's own result is unchanged.
- **Exit 1** only when the plan could not be computed: missing credentials, an App
  Store Connect 401 or 403, a Play auth error, or an unexpected exception (the
  traceback is printed, never a secret).
- A **problem** is anything that would stop a tag run (a failed precondition, a
  refusal from version creation or the preflight), with its exact message. Phases that
  depend on it are reported as "not planned (blocked by problem n)".
- **Output:** the report on stdout, the same report as Markdown in the job summary,
  and `<work dir>/store-plan.json` (not uploaded).

## iOS (`ios plan --version V --work-dir D`)

1. **Switches** a tag run would resolve now (`ios gates` with the event set to a tag
   push); an invalid value is a problem. With the master switch off: "a tag run would
   not touch App Store Connect", the GitHub Release title, and the plan ends.
2. **Local inputs,** loaded exactly as a tag run loads them: listing text, What's New,
   screenshot staging (Pillow) and the creative images, each only when its switch is
   on. A local failure is a problem.
3. **Version creation** (`IOS_CREATE_VERSION`): "exists (STATE)", "WOULD CREATE", or a
   refusal.
4. **Preflight** of the App Store version, when an App Store Connect switch is on.
5. **Build number** (`IOS_UPLOAD_BUILD`): the next build number over GETs.
6. **Store step:** the same code as the real `app-store-connect` job, run against a
   recording client (below). Build selection is simulated, because a validation run
   uploads no build.
7. **Report:** the switches and notes, each phase's outcome, the ordered writes a tag
   run would make with their purposes (listing fields with live → new lengths,
   screenshot sets to replace with the live and local `(name, size, md5)` lists,
   creative actions per slot, the review draft, product items, the build selection,
   `releaseType` and the submit), the GitHub Release it would create or update (title,
   `--latest` or `--latest=false`), the problems and the status line.

**The recording client** (`ios/plan.py` `PlanClient`) stands in for the App Store
Connect client. GETs go to a read-only client, which refuses anything else. Every
non-GET is recorded with its purpose and answered from an overlay, so later reads see
its effect: a created version appears in the version list (served from the newest
existing version, labelled "approximate (new version)"), a patched localization
returns the new text, created screenshot sets, screenshots, images, drafts and items
appear in their listings, deletes remove, uploads to `plan://` URLs are recorded and
never sent. A write the overlay does not know is listed as "unrecognised write". The
tests assert that the transport saw only GETs.

## Android (`android plan --work-dir D`)

1. **Switches** for a tag of the gradle `versionName`. Master off: "a tag run builds
   and releases nothing", and the plan ends.
2. **GitHub:** the versionCode check against earlier GitHub Releases, and the release
   it would create: title, `--prerelease`/`--latest` flags and asset names.
3. **Play,** when a listing, screenshot or track switch is on. Without
   `PLAY_SERVICE_ACCOUNT_JSON` it is "skipped: no Play credentials" (not a problem).
   Otherwise one throwaway edit, opened through a read-only publisher that allows only
   reads plus inserting and deleting that edit, compares the live listing with the
   prepared text (per field, same or differs, and the lengths), counts the live and
   staged phone screenshots (the whole set is always replaced), runs the upload
   decision for the planned tracks (upload, skip, or a problem) and checks the What's
   New length. The edit is then deleted.

## Example

Fud AI's current switches, with an illustrative App Store Connect state:

```
store-plan ios: Fud AI 7.2 (tag ios-v7.2), store-release v0.1.0 (<sha>)
switches a tag run would use: master=on create_version=on upload_build=on listing=off whats_new=off screenshots=off creative=off submit=off public_release=off
1. create-version: App Store version 7.2 is missing → WOULD CREATE (POST /appStoreVersions)
   refusal checks passed: 7.1.2 READY_FOR_SALE; 7.2 is newer than every version
2. build: next build number 41 (project 39, highest uploaded 40); xcodebuild uploads the binary
3. store step: not planned (no listing/What's New/screenshot/creative/review switch on)
4. GitHub Release: create the release ios-v7.2 titled 'iOS 7.2 (build 41)' with --latest
writes a tag run would make, in order:
  POST /appStoreVersions  create the App Store version: {platform: IOS, versionString: 7.2}
problems a tag run would hit: none
store-plan: CLEAN
```
