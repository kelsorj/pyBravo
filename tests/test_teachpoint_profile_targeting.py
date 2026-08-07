"""Teaching must go into the profile the operator thinks it is going into.

Teachpoints are saved to whichever profile is active. If the server quietly
falls back to `default` because no active profile was recorded, an operator can
teach an entire nine-position deck and only discover at the end that none of it
landed in the profile they meant. That costs the whole session, so the profile
name is reported on every teach and in the API response.
"""

from __future__ import annotations

import pytest
import yaml

from pybravo.profile.profile import BravoProfile
from pybravo.web import server


@pytest.fixture()
def profile_dir(tmp_path, monkeypatch):
    """Two real profiles on disk, with the server pointed at them."""
    for name in ("default", "other"):
        p = BravoProfile.default()
        p.name = name
        p.save(str(tmp_path / f"{name}.yaml"))
    monkeypatch.setattr(server, "_profile_dir", tmp_path)
    return tmp_path


def _locations(path):
    return yaml.safe_load(path.read_text()).get("teachpoints") or {}


@pytest.mark.asyncio
async def test_set_teachpoint_persists_to_the_active_profile(profile_dir, monkeypatch):
    bravo = server.Bravo(profile=profile_dir / "default.yaml")
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_profile_path", profile_dir / "default.yaml")

    res = await server.set_teachpoint(
        4, server.TeachpointSetRequest(x=111.0, y=222.0, z=133.0)
    )

    # The response names the profile so the UI can show it.
    assert res["profile"] == "default"

    # And it is on disk, not just in memory — a restart must not lose it.
    saved = _locations(profile_dir / "default.yaml")["4"]
    assert saved == pytest.approx({"x": 111.0, "y": 222.0, "z": 133.0})


@pytest.mark.asyncio
async def test_teaching_does_not_leak_into_a_different_profile(profile_dir, monkeypatch):
    """The profile that is NOT active must be left alone."""
    bravo = server.Bravo(profile=profile_dir / "default.yaml")
    monkeypatch.setattr(server, "_bravo", bravo)
    monkeypatch.setattr(server, "_profile_path", profile_dir / "default.yaml")

    before = _locations(profile_dir / "other.yaml")
    await server.set_teachpoint(4, server.TeachpointSetRequest(x=1.0, y=2.0, z=3.0))

    assert _locations(profile_dir / "other.yaml") == before


def test_startup_warns_when_it_falls_back_to_default(tmp_path, caplog):
    """A silent fallback is what makes a whole session land in the wrong file."""
    assert server._read_active_profile(tmp_path) is None

    (tmp_path / "real.yaml").write_text("name: real\n")
    server._write_active_profile(tmp_path, "real")
    assert server._read_active_profile(tmp_path) == "real"

    # A recorded profile whose file has gone must not be resumed silently.
    (tmp_path / "real.yaml").unlink()
    assert server._read_active_profile(tmp_path) is None


def test_active_profile_survives_a_load(tmp_path):
    """Loading a profile records it, so the next start resumes the same one."""
    (tmp_path / "384.yaml").write_text("name: '384'\n")
    server._write_active_profile(tmp_path, "384")
    assert (tmp_path / ".active_profile").read_text().strip() == "384"
    assert server._read_active_profile(tmp_path) == "384"
