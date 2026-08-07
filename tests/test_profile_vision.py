from pybravo.profile.profile import BravoProfile


def test_profile_vision_round_trip(tmp_path):
    profile = BravoProfile.default()
    profile.vision.enabled = True
    profile.vision.service_url = "http://127.0.0.1:8102"
    profile.vision.sdk_root = "external/custom_pyorbbecsdk"

    path = tmp_path / "vision_profile.yaml"
    profile.save(path)

    loaded = BravoProfile.load(path)
    assert loaded.vision.enabled is True
    assert loaded.vision.service_url == "http://127.0.0.1:8102"
    assert loaded.vision.sdk_root == "external/custom_pyorbbecsdk"

