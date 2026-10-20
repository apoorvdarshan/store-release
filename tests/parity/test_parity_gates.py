"""Parity cases 1-3: iOS and Android switch resolution and the printed switch summary."""
from __future__ import annotations

import os
import random
import sys
import unittest
from unittest import mock

from store_release import switches
from store_release.android import release as android_release
from store_release.ios import release as ios_release
from support import apps as support_apps
from support.upstream import ParityCase, outcome

support_apps.configure_fud_ai_defaults()

IOS_SWITCHES = ("IOS_RELEASE_AUTOMATION", "IOS_CREATE_VERSION", "IOS_UPLOAD_BUILD", "IOS_UPLOAD_LISTING",
                "IOS_UPLOAD_WHATS_NEW", "IOS_UPLOAD_SCREENSHOTS", "IOS_UPLOAD_CREATIVE_ASSETS", "IOS_SUBMIT_REVIEW",
                "IOS_PUBLIC_RELEASE")
ANDROID_SWITCHES = ("ANDROID_RELEASE_AUTOMATION", "ANDROID_UPLOAD_LISTING", "ANDROID_UPLOAD_WHATS_NEW",
                    "ANDROID_UPLOAD_SCREENSHOTS", "ANDROID_INTERNAL_TESTING", "ANDROID_CLOSED_TESTING",
                    "ANDROID_PRODUCTION_ROLLOUT")


class IosGateParityTests(ParityCase):
    def test_every_switch_combination(self) -> None:
        mismatches = []
        for bits in range(2 ** len(IOS_SWITCHES)):
            env = {name: "true" if bits >> i & 1 else "false" for i, name in enumerate(IOS_SWITCHES)}
            for event, ref, override in (("push", "ios-v7.2", ""), ("push", "ios-v7.3", ""),
                                         ("workflow_dispatch", "main", ""), ("workflow_dispatch", "main", "7.3")):
                for timing in ({}, {"IOS_BUILD_POLL_SECONDS": "45", "IOS_BUILD_WAIT_MINUTES": "90"},
                               {"IOS_BUILD_POLL_SECONDS": "5"}):
                    case = {**env, **timing}
                    kwargs = dict(event=event, ref_name=ref, marketing_version="7.2", validation_version=override)
                    upstream = outcome(lambda: self.up.ios_release.resolve_gates(case, **kwargs))
                    package = outcome(lambda: ios_release.resolve_gates(case, **kwargs))
                    if upstream != package:
                        mismatches.append((case, kwargs, upstream, package))
        self.assertEqual(mismatches[:3], [])

    def test_invalid_values_and_events(self) -> None:
        cases = [({name: "maybe"}, "push") for name in IOS_SWITCHES]
        cases += [({"IOS_BUILD_WAIT_MINUTES": "999"}, "push"), ({}, "pull_request"), ({"IOS_UPLOAD_BUILD": "2"},
                                                                                       "workflow_dispatch")]
        for env, event in cases:
            kwargs = dict(event=event, ref_name="ios-v7.2", marketing_version="7.2")
            with self.subTest(env=env, event=event):
                upstream = outcome(lambda: self.up.ios_release.resolve_gates(env, **kwargs))
                self.assertEqual(upstream[0], "ReleaseError")
                self.assertEqual(outcome(lambda: ios_release.resolve_gates(env, **kwargs)), upstream)


class AndroidGateParityTests(ParityCase):
    def test_every_switch_combination(self) -> None:
        mismatches = []
        for bits in range(2 ** len(ANDROID_SWITCHES)):
            env = {name: "on" if bits >> i & 1 else "" for i, name in enumerate(ANDROID_SWITCHES)}
            for event, ref in (("push", "android-v7.2"), ("push", "android-v7.1"), ("workflow_dispatch", "main")):
                kwargs = dict(event=event, ref_name=ref, version_name="7.2")
                upstream = outcome(lambda: self.up.android_release.resolve_gates(env, **kwargs))
                package = outcome(lambda: android_release.resolve_gates(env, **kwargs))
                if upstream != package:
                    mismatches.append((env, kwargs, upstream, package))
        self.assertEqual(mismatches[:3], [])

    def test_invalid_values(self) -> None:
        for name in ANDROID_SWITCHES:
            env = {name: "sure"}
            kwargs = dict(event="workflow_dispatch", ref_name="main", version_name="7.2")
            with self.subTest(name=name):
                upstream = outcome(lambda: self.up.android_release.resolve_gates(env, **kwargs))
                self.assertEqual(upstream[0], "ReleaseError")
                self.assertEqual(outcome(lambda: android_release.resolve_gates(env, **kwargs)), upstream)


class PrintGatesParityTests(ParityCase):
    VALUES = ("", "true", "false", "maybe", " ON ", "0", "1", "yes", "INVALID", "off")

    def test_twenty_sampled_environments(self) -> None:
        rng = random.Random(20261020)
        names = IOS_SWITCHES + ANDROID_SWITCHES + ("STORE_SYNC_REVENUECAT",)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("IOS_", "ANDROID_", "STORE_"))}
        for index in range(20):
            env = {name: rng.choice(self.VALUES) for name in names if rng.random() < 0.6}
            if index == 0:
                env = {}
            if index == 1:
                env = {name: "INVALID" for name in names}
            platform = ("ios", "android", "all")[index % 3]
            with self.subTest(index=index, platform=platform), mock.patch.dict(os.environ, {**clean, **env}, clear=True):
                with mock.patch.object(sys, "argv", ["print_gates.py", "--platform", platform]):
                    upstream = outcome(self.up.print_gates.main)
                package = outcome(lambda: switches.main(["--platform", platform]))
                self.assertEqual(package, upstream)
                if index == 1:
                    self.assertIn("INVALID", upstream[2])


if __name__ == "__main__":
    unittest.main()
