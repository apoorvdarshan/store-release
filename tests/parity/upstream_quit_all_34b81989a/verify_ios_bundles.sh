#!/bin/bash
# Verifies the signed Quit All app and its widget before validation or upload.
#
#   verify_ios_bundles.sh <dir containing "Quit All.app"> archive|distribution
#
# Both: exactly the app and its one widget extension, each with the expected
# bundle id, MARKETING_VERSION and BUILD_NUMBER, signed for TEAM_ID with the
# shared app group (the widget reads the app's progress through it). The app
# must declare export compliance (ITSAppUsesNonExemptEncryption; YES also needs
# ITSEncryptionExportComplianceCode).
# distribution (App Store export): also a strict signature check, an Apple
# Distribution signature, an App Store provisioning profile (no device list)
# and no get-task-allow entitlement.
set -euo pipefail

apps_dir="${1:?usage: verify_ios_bundles.sh <apps dir> archive|distribution}"
kind="${2:?usage: verify_ios_bundles.sh <apps dir> archive|distribution}"
: "${BUNDLE_ID:?}" "${WIDGET_BUNDLE_ID:?}" "${APP_GROUP:?}" "${TEAM_ID:?}" "${MARKETING_VERSION:?}" "${BUILD_NUMBER:?}"
case "$kind" in archive|distribution) ;; *) echo "error: unknown kind $kind" >&2; exit 2 ;; esac

fail() { echo "error: $*" >&2; exit 1; }
plist_value() { /usr/libexec/PlistBuddy -c "Print :$2" "$1" 2>/dev/null || true; }

main="$apps_dir/Quit All.app"
widget="$main/PlugIns/Quit All Widget.appex"
[ -d "$main" ] || fail "no Quit All.app in $apps_dir"
apps="$(find "$apps_dir" -mindepth 1 -maxdepth 1 -name '*.app' | wc -l | tr -d ' ')"
[ "$apps" = 1 ] || fail "expected one app in $apps_dir, found $apps"
extensions="$(find "$main" -name '*.appex' -not -path '*/Frameworks/*' -print)"
[ "$extensions" = "$widget" ] || fail "expected only the widget extension, found: ${extensions:-none}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

check_bundle() {
  local bundle="$1" expected_id="$2" name
  name="$(basename "$bundle")"
  local info="$bundle/Info.plist"
  local id short build
  id="$(plist_value "$info" CFBundleIdentifier)"
  short="$(plist_value "$info" CFBundleShortVersionString)"
  build="$(plist_value "$info" CFBundleVersion)"
  [ "$id" = "$expected_id" ] || fail "$name is $id, expected $expected_id"
  [ "$short" = "$MARKETING_VERSION" ] && [ "$build" = "$BUILD_NUMBER" ] \
    || fail "$name is $short ($build), expected $MARKETING_VERSION ($BUILD_NUMBER)"

  local entitlements="$work/$name.entitlements.plist"
  codesign -d --entitlements - --xml "$bundle" > "$entitlements" 2>/dev/null \
    || fail "$name has no readable code signature"
  [ "$(plist_value "$entitlements" application-identifier)" = "$TEAM_ID.$expected_id" ] \
    || fail "$name application-identifier is $(plist_value "$entitlements" application-identifier)"
  [ "$(plist_value "$entitlements" com.apple.developer.team-identifier)" = "$TEAM_ID" ] \
    || fail "$name is not signed for team $TEAM_ID"
  plist_value "$entitlements" com.apple.security.application-groups | grep -qxF "    $APP_GROUP" \
    || fail "$name lost the $APP_GROUP app group entitlement"

  if [ "$kind" = distribution ]; then
    codesign --verify --deep --strict "$bundle" || fail "$name signature does not verify"
    codesign -dvv "$bundle" 2> "$work/$name.signature.txt" || true
    grep -qE '^Authority=(Apple|iPhone) Distribution' "$work/$name.signature.txt" \
      || fail "$name is not signed for distribution: $(grep -m1 '^Authority=' "$work/$name.signature.txt")"
    [ "$(plist_value "$entitlements" get-task-allow)" != true ] || fail "$name allows debugging (get-task-allow)"
    local profile="$work/$name.mobileprovision.plist"
    security cms -D -i "$bundle/embedded.mobileprovision" > "$profile" 2>/dev/null \
      || fail "$name has no readable embedded.mobileprovision"
    [ -z "$(plist_value "$profile" ProvisionedDevices)" ] || fail "$name has a development or ad hoc profile"
    [ "$(plist_value "$profile" Entitlements:application-identifier)" = "$TEAM_ID.$expected_id" ] \
      || fail "$name provisioning profile is for $(plist_value "$profile" Entitlements:application-identifier)"
    plist_value "$profile" Entitlements:com.apple.security.application-groups | grep -qxF "    $APP_GROUP" \
      || fail "$name provisioning profile does not grant $APP_GROUP"
  fi
  echo "  $name: $id $short ($build), app group $APP_GROUP"
}

check_bundle "$main" "$BUNDLE_ID"
check_bundle "$widget" "$WIDGET_BUNDLE_ID"

# Export compliance. Without ITSAppUsesNonExemptEncryption, App Store Connect
# asks about every build by hand and review submission fails until it is answered.
encryption="$(plist_value "$main/Info.plist" ITSAppUsesNonExemptEncryption)"
case "$encryption" in
  false) echo "  export compliance: no non-exempt encryption (ITSAppUsesNonExemptEncryption = NO)" ;;
  true)
    [ -n "$(plist_value "$main/Info.plist" ITSEncryptionExportComplianceCode)" ] \
      || fail "Quit All.app declares non-exempt encryption without ITSEncryptionExportComplianceCode"
    echo "  export compliance: non-exempt encryption with an export compliance code" ;;
  *) fail "Quit All.app does not declare ITSAppUsesNonExemptEncryption (export compliance)" ;;
esac
echo "verified the $kind app and widget for $MARKETING_VERSION ($BUILD_NUMBER)"
