from pybravo.profile.profile import AccessoriesConfig, BravoProfile


def test_legacy_barcode_accessory_migrates_to_devices(tmp_path):
    path = tmp_path / "legacy.yaml"
    path.write_text(
        """
name: Legacy
accessories:
  barcode_reader:
    enabled: true
    device_type: ms3
    port: COM7
    side: west
    location: 6
""".strip()
    )

    profile = BravoProfile.load(path)

    assert len(profile.accessories.devices) == 1
    device = profile.accessories.devices[0]
    assert device.type == "barcode_reader"
    assert device.location == 6
    assert device.connection["port"] == "COM7"
    assert device.settings["side"] == "west"
    assert profile.accessories.barcode_reader.port == "COM7"


def test_accessory_devices_round_trip(tmp_path):
    profile = BravoProfile.default()
    profile.accessories = AccessoriesConfig.from_dict(
        {
            "devices": [
                {
                    "id": "shaker_1",
                    "type": "teleshake",
                    "name": "Teleshake",
                    "enabled": True,
                    "location": 4,
                    "holds_labware": True,
                    "connection": {"kind": "serial", "port": "COM4"},
                    "settings": {
                        "default_rpm": 500,
                        "default_direction": "NS",
                        "temperature_enabled": True,
                    },
                    "model": {"path": "accessories/teleshake.gltf"},
                    "teachpoint_hint": {"z_delta_mm": 12.4, "requires_validation": True},
                },
                {
                    "id": "barcode_1",
                    "type": "barcode_reader",
                    "name": "Barcode Reader",
                    "enabled": True,
                    "location": 6,
                    "connection": {"kind": "serial", "port": "COM5"},
                    "settings": {"device_type": "ms3", "side": "east"},
                },
            ]
        }
    )
    path = tmp_path / "profile.yaml"
    profile.save(path)

    loaded = BravoProfile.load(path)

    assert [device.id for device in loaded.accessories.devices] == ["shaker_1", "barcode_1"]
    shaker = loaded.accessories.devices[0]
    assert shaker.type == "teleshake"
    assert shaker.holds_labware is True
    assert shaker.model["path"] == "accessories/teleshake.gltf"
    assert shaker.teachpoint_hint["z_delta_mm"] == 12.4
    assert loaded.accessories.barcode_reader.location == 6
    assert loaded.accessories.barcode_reader.port == "COM5"
