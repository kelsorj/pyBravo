"""A mode override must not change the profile it was given.

Seen 2026-10-22: the designer's simulate built `Bravo(profile=live.profile,
mode="simulation")`, which switched the live instrument's profile to
simulation. The live accessories then read the simulator (a fake level) and
Execute refused to run on hardware.
"""
from __future__ import annotations

from pybravo.bravo import Bravo
from pybravo.profile.profile import BravoProfile


def test_mode_override_leaves_the_given_profile_alone():
    profile = BravoProfile.default()
    profile.connection.controller_type = "darwin_native"

    sim = Bravo(profile=profile, mode="simulation")

    assert sim.profile.connection.controller_type == "simulation"
    assert profile.connection.controller_type == "darwin_native"


def test_without_a_mode_the_profile_is_shared():
    profile = BravoProfile.default()
    bravo = Bravo(profile=profile)
    assert bravo.profile is profile
