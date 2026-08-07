"""Runtime accessory lifecycle management."""

from __future__ import annotations

import logging
from typing import Any

from pybravo.accessories.barcode_reader import BarcodeReader, config_for_device as barcode_config_for_device
from pybravo.profile.profile import AccessoryDeviceConfig, BravoProfile

logger = logging.getLogger(__name__)


class AccessoryManager:
    """Build and own accessory drivers for the active profile.

    Drivers are created lazily so selecting a profile does not open COM ports.
    """

    def __init__(self, profile: BravoProfile) -> None:
        self._profile = profile
        self._drivers: dict[str, Any] = {}

    @property
    def devices(self) -> list[AccessoryDeviceConfig]:
        return list(self._profile.accessories.devices)

    def reconfigure(self, profile: BravoProfile) -> None:
        self.close_all()
        self._profile = profile

    def close_all(self) -> None:
        for accessory_id, driver in list(self._drivers.items()):
            close = getattr(driver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Ignoring accessory close failure for %s", accessory_id, exc_info=True)
        self._drivers.clear()

    def enabled_devices(self, accessory_type: str | None = None) -> list[AccessoryDeviceConfig]:
        return [
            device
            for device in self._profile.accessories.devices
            if device.enabled and (accessory_type is None or device.type == accessory_type)
        ]

    def find_by_id(self, accessory_id: str) -> AccessoryDeviceConfig | None:
        for device in self._profile.accessories.devices:
            if device.id == accessory_id:
                return device
        return None

    def find_enabled(
        self,
        accessory_type: str,
        *,
        location: int | None = None,
    ) -> AccessoryDeviceConfig | None:
        candidates = self.enabled_devices(accessory_type)
        if location is not None:
            for device in candidates:
                if int(device.location or 0) == int(location):
                    return device
        for device in candidates:
            if int(device.location or 0) > 0:
                return device
        return candidates[0] if candidates else None

    def get_driver(self, device: AccessoryDeviceConfig) -> Any:
        if device.id not in self._drivers:
            self._drivers[device.id] = self._build_driver(device)
        return self._drivers[device.id]

    def get_barcode_reader(self, device: AccessoryDeviceConfig) -> BarcodeReader:
        driver = self.get_driver(device)
        if not isinstance(driver, BarcodeReader):
            raise TypeError(f"Accessory {device.id!r} is not a barcode reader")
        return driver

    def _build_driver(self, device: AccessoryDeviceConfig) -> Any:
        if device.type == "barcode_reader":
            device_type = str(device.settings.get("device_type") or "ms3")
            port = str(device.connection.get("port") or "COM5")
            return BarcodeReader(barcode_config_for_device(device_type, port))
        if device.type == "teleshake":
            from pybravo.accessories.teleshake import Teleshake, TeleshakeConfig

            settings = dict(device.settings or {})
            connection = dict(device.connection or {})
            return Teleshake(
                TeleshakeConfig(
                    port=str(connection.get("port") or "COM4"),
                    default_rpm=int(settings.get("default_rpm") or 100),
                    default_direction=str(settings.get("default_direction") or "NWSE"),
                )
            )
        raise ValueError(f"Unknown accessory type: {device.type!r}")
