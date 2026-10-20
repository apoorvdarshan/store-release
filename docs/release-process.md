# Releasing store-release

## Versions

- SemVer tags `vX.Y.Z`, created only by the **Release** workflow and never moved.
  Every fix is a new patch.
- While on 0.x: MINOR for a new app's features or config keys (with defaults that
  keep current behaviour) and for any `config-breaking` change; PATCH for fixes and
  pin bumps.
- `store_release.__version__` must equal the tag.

## CHANGELOG

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/): an `## [Unreleased]`
section, then one `## [X.Y.Z] - YYYY-MM-DD` section per tag. Each pull request adds
its entry under `[Unreleased]`. Each release section has:

- `Impact:` one of
  - `checks-only`: PR validation or docs only
  - `validation`: build, sign or verify paths that validation dispatches exercise
  - `release-path`: App Store Connect, Play or GitHub Release writes, or other
    tag-only paths
  - `config-breaking`: an app must edit its config
- `### Added` / `### Changed` / `### Fixed`
- `### App actions required`: exact config or caller edits
- `### Verification`: run URLs for CI, the canary, the re-run probe (when the
  workflows changed), and the app validation dispatches with their store-plan results

Dependabot bumps ship as patches: an action bump has Impact `release-path` or
`validation` depending on which jobs use the action, a runtime lock bump
(`requirements.txt`) `release-path`, a `requirements-checks.txt` bump `checks-only`.
The locks are regenerated with `pip-compile --generate-hashes` (pip-tools 7.6.2) on
Python 3.13. The Apple WWDR G3 and bundletool checksums are updated by hand, in a PR
with Impact `validation`.

## Cutting a release

1. A PR sets `__version__`, turns `[Unreleased]` into `## [X.Y.Z] - <today>` with its
   Impact line, and adds the verification URLs. Merge it once CI is green.
2. Dispatch **Release** on `main` with `version=X.Y.Z`
   (`gh workflow run release.yml -f version=X.Y.Z`). `scripts/release_check.py` fails
   unless:
   1. the run is on `refs/heads/main`;
   2. the version is X.Y.Z and equals `__version__`;
   3. no tag `vX.Y.Z` exists and the version is greater than every `v*` tag;
   4. `CHANGELOG.md` has exactly one dated section for it, non-empty, with an
      `Impact:` line (its body becomes the GitHub Release notes);
   5. [yanked.md](yanked.md) does not list it;
   6. the newest check run of `test (stdlib)`, `test (strict)`, `test (macos)`,
      `lint`, `workflows`, `Analyze (python)` and `Analyze (actions)` on the commit
      concluded `success` (still running counts as a failure: wait, then dispatch
      again). The canary is advisory and not checked.
3. Only then does the workflow create the tag on that commit and the GitHub Release,
   marked Latest.

## After the first tag

Protect the tags (creating tags stays allowed, for `release.yml`):

```bash
gh api -X POST repos/apoorvdarshan/store-release/rulesets --input - <<'JSON'
{
  "name": "Release tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "update"}, {"type": "non_fast_forward"}],
  "bypass_actors": []
}
JSON
```

Immutable releases are enabled before the first release, so every release is
published immutable; confirm with `gh api repos/apoorvdarshan/store-release/immutable-releases`.

## How apps receive a release

1. Dependabot (weekly, grouped as `store-release`) opens one PR per app that moves
   every store-release reference in its three callers to the new SHA and comment.
2. That PR's Store checks run the new version against the app's real data, including
   the pins and callers.
3. By Impact:
   - `checks-only`: merge.
   - `validation` or `release-path`: dispatch the app's iOS Release (with the next
     unapproved `marketing_version`) and Android Release on the Dependabot branch.
     Both must be green and both store-plans clean. A manual dispatch gets the normal
     Actions secrets; a Dependabot-started run never does.
   - `config-breaking`: push the listed config edits onto the branch first, then
     validate.
4. Merge with `--match-head-commit`.
5. Rollback is reverting the bump PR. The old SHA stays valid, because tags never move
   and pins are SHAs.
6. Push an app's `ios-v*` and `android-v*` tags one at a time: GitHub creates no push
   events when more than three tags are pushed at once.

## Yanking

1. Add `- vX.Y.Z — YYYY-MM-DD — <reason> (use vX.Y.Z+1)` to [yanked.md](yanked.md)
   through a normal PR.
2. Ship the fixed patch.

From then on, apps still pinned to the yanked version fail their Store checks, their
release runs print a warning (a release never depends on mutable remote state), and
Dependabot's bump PRs to the fixed version pass.
