# Security Policy

## Reporting a vulnerability

Please report security issues in store-release **privately**, so they can be fixed
before public disclosure.

**Preferred:** GitHub's
[private vulnerability reporting](https://github.com/apoorvdarshan/store-release/security/advisories/new),
which routes directly to the maintainer.

**Alternative:** email either of:

- **apoorv@fud-ai.app**
- **ad13dtu@gmail.com**

Please include a description, steps to reproduce (a minimal workflow or input is
ideal), the store-release version or commit, and the potential impact. You can expect
an acknowledgement within **7 days** and a triage with an estimated fix timeline
within **14 days**. Please do not disclose the issue publicly until a fixed release
is out.

## Supported versions

Only the latest release is supported. Fixes ship as a new patch release, and an
affected version is listed in [docs/yanked.md](docs/yanked.md) so that apps pinned to
it fail their checks until they update.

## Scope

In scope: this repository, that is the reusable workflows, the `job-setup` action, the
`store_release` package (also on PyPI as `store-release`), `run.py` and the release
workflow. For example:

- a way for workflow inputs, app files or store responses to run commands, change
  what is uploaded or reach a secret
- a credential (App Store Connect key, signing identity, keystore, Play service
  account) that can leak into logs, artifacts, the app checkout or an app hook
- a write to App Store Connect, Google Play or GitHub Releases that happens with its
  switch off, on a validation run, or from the read-only plan
- a release path that skips a check it claims to run (signing certificate pin,
  version and build matching, image checks, the build wait)

Out of scope: vulnerabilities in the apps that use store-release (report them to
that app's repository), in GitHub Actions, App Store Connect or Google Play
themselves, and in third-party actions or Python packages (report them upstream; a
pin bump here follows).

## Safe harbor

Good-faith research that follows this policy will not be pursued legally. Do not
access other people's data or apps, and do not run workflows against store accounts
you do not own.

## Credit

Reporters of valid issues are credited in the release notes of the fix, unless they
prefer to stay anonymous.
