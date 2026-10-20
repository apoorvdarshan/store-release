# Re-runs

Each stage is its own caller job calling one single-job reusable workflow, so
**Re-run failed jobs** re-runs the failed stage and the stages after it, and the
stages that passed keep their outputs. Every re-run uses the first attempt's
store-release commit (the pin is a SHA anyway).

## iOS

- **`wait-for-build` is its own job.** It outputs the processed build's id to
  `app-store-connect`. If a listing, screenshot or review step fails, re-running reuses
  that build id instead of waiting again; a second wait would always time out, because
  the wait budget counts from the original upload.
- **`app-store-connect` is safe to re-run in every state a first attempt can leave.**
  Listing and What's New fields are sent only when they differ from App Store Connect,
  and screenshots are replaced only when the files differ. Header and search results
  images already in the Asset Library are never uploaded again, an image already
  placed is left alone, and an interrupted image review is finished in the draft it
  left.
  - After a failed submit the version is `READY_FOR_REVIEW`: its text can still change,
    its screenshots cannot (they are compared, and the run fails only if they differ).
  - After a submit that went through but whose job failed, the version is
    `WAITING_FOR_REVIEW`: nothing can change, so the re-run checks that App Store
    Connect already matches the repository and that this run's build is the one in
    review, then finishes.
- **`github-release`** updates the release if an earlier attempt created it.
- A version the run created stays in Prepare for Submission if a later step fails,
  and the next run for the same tag reuses it.

## Android

- The GitHub Release is created last, so a failed Play upload or read-back never
  leaves a release behind.
- Re-running `release` rebuilds and re-signs the app. If an earlier attempt of the
  same run already got this versionCode onto every selected track, the upload is
  skipped (Play would reject the duplicate), and the read-back and GitHub Release go
  ahead; an existing GitHub Release for the tag gets this attempt's signed files. Play
  then holds the first attempt's AAB; both come from the same commit and upload key.
  Inside a called workflow the jobs API names the job `release / release`, which the
  re-run detector matches.
- A versionCode Play already has from anything else (an older release, another run)
  fails the run before it builds, and so does one that is not higher than every
  earlier GitHub Release's for the tag prefix.

## The re-run probe

Before v0.1.0 is tagged, a probe checks that GitHub re-runs called workflows the way
the callers rely on. `probe-rerun.yml` (dispatch only) runs `a` → `b` → `c` → `d`:
`b` and `c` call [`probe-inner.yml`](../.github/workflows/probe-inner.yml) through the
same SHA-pinned path the apps use, `c` receives `b`'s `id` output and fails on
attempt 1, and the probe reads the repository variable `PROBE_VAR`. `id` is
`<run_id>.<attempt>` of the attempt that ran `b`, so `c` can show which attempt
produced the value it received.

Procedure:

1. Dispatch **Re-run probe** and wait for `c` to fail.
2. `gh run rerun <run id> --failed`.
3. Pass when attempt 2 re-runs `c` and `d` only, `c` prints `b`'s attempt-1 `id`,
   `job.workflow_sha` is the pinned SHA in both attempts, and `PROBE_VAR` resolves.
4. Repeat with a new dispatch and `gh run rerun <run id> --job <c's job id>`.

If any criterion fails, nothing is tagged: the result changes the caller template for
every app.

### Results

Pending: recorded here once `probe-rerun.yml` has run.
