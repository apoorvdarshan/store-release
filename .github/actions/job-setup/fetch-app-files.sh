#!/usr/bin/env bash
# job-setup: sparse-check out this job's files of the app (fetch-app-files.sh APP_DIR PATTERNS_FILE).
#
# The app checkout is blobless and keeps no credentials, so git fetches the blobs these patterns
# need here. FETCH_TOKEN (the job's read-only GITHUB_TOKEN) reaches only this git command, as
# environment config: never .git/config, never argv. That is what lets private app repositories
# work; for a public one the header changes nothing.
set -euo pipefail
app="$1"
patterns="$2"
if [ -n "${FETCH_TOKEN:-}" ]; then
  auth="$(printf 'x-access-token:%s' "$FETCH_TOKEN" | base64 | tr -d '\n')"
  echo "::add-mask::$auth"
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0="http.${GITHUB_SERVER_URL:-https://github.com}/.extraheader"
  export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $auth"
fi
unset FETCH_TOKEN
git -C "$app" sparse-checkout set --no-cone --stdin < "$patterns"
