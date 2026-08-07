"""Removing an accessory must actually remove it from the profile.

Accessories live in two places for backwards compatibility: the `devices` list,
and a legacy `barcode_reader` block that predates it. Loading a profile migrates
the legacy block into a device, and the UI recreates a device from the legacy
block whenever `devices` is empty.

That means clearing `devices` is not enough on its own — if the legacy block is
left enabled, the barcode reader reappears the next time the profile is read.
These tests pin both halves.
"""

from __future__ import annotations


from pybravo.profile.profile import AccessoriesConfig


def _configured_barcode() -> dict:
    return {
        "devices": [
            {
                "id": "barcode_reader",
                "type": "barcode_reader",
                "name": "Barcode Reader",
                "enabled": True,
                "location": 6,
                "holds_labware": True,
                "connection": {"kind": "serial", "port": "COM5"},
                "settings": {"device_type": "ms3", "side": "east"},
            }
        ],
        "barcode_reader": {
            "enabled": True,
            "device_type": "ms3",
            "port": "COM5",
            "side": "east",
            "location": 6,
        },
    }


def test_clearing_devices_also_clears_the_legacy_block():
    """Otherwise the UI resurrects the accessory from the legacy config."""
    cfg = AccessoriesConfig.from_dict({"devices": []})

    assert cfg.devices == []
    assert cfg.barcode_reader.enabled is False
    assert cfg.barcode_reader.location == 0


def test_removal_does_not_resurrect_from_a_stale_legacy_block():
    """A remove sends only `devices`; the stale legacy block must not win."""
    before = AccessoriesConfig.from_dict(_configured_barcode())
    assert [d.id for d in before.devices] == ["barcode_reader"]
    assert before.barcode_reader.enabled is True

    # This is exactly what the UI sends when you press Remove.
    after = AccessoriesConfig.from_dict({"devices": []})

    assert after.devices == []
    assert after.barcode_reader.enabled is False, (
        "legacy barcode block still enabled — the accessory will come back"
    )


def test_a_legacy_only_profile_still_migrates():
    """Old profiles carry only the legacy block; that must still produce a device."""
    cfg = AccessoriesConfig.from_dict({
        "barcode_reader": {
            "enabled": True, "device_type": "ms3", "port": "COM7",
            "side": "west", "location": 3,
        }
    })

    assert len(cfg.devices) == 1
    device = cfg.devices[0]
    assert device.type == "barcode_reader"
    assert device.location == 3
    assert cfg.barcode_reader.port == "COM7"


def test_round_trip_preserves_a_configured_accessory():
    """Saving and reloading must not drop or duplicate the device."""
    cfg = AccessoriesConfig.from_dict(_configured_barcode())
    again = AccessoriesConfig.from_dict(cfg.to_dict())

    assert [d.id for d in again.devices] == ["barcode_reader"]
    assert again.devices[0].location == 6


def test_removing_one_of_two_accessories_keeps_the_other():
    payload = _configured_barcode()
    payload["devices"].append({
        "id": "teleshake", "type": "teleshake", "name": "Teleshake",
        "enabled": True, "location": 4,
        "connection": {"kind": "serial", "port": "COM4"},
        "settings": {},
    })
    cfg = AccessoriesConfig.from_dict(payload)
    assert len(cfg.devices) == 2

    remaining = [d for d in cfg.to_dict()["devices"] if d["type"] != "barcode_reader"]
    after = AccessoriesConfig.from_dict({"devices": remaining})

    assert [d.type for d in after.devices] == ["teleshake"]
    assert after.barcode_reader.enabled is False
