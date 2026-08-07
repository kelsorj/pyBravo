"""Profile configuration system using YAML files.

Replaces the legacy Windows registry-based configuration with portable
YAML serialisation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pybravo.deck.teachpoints import Teachpoints
from pybravo.motion.axes import AxisConfig, get_default_axis_config
from pybravo.tips import (
    get_default_tip_id_for_head,
    get_tip_capacity_ul,
    get_tip_id_for_capacity,
    get_tip_length_mm,
)
from pybravo.types import Axis, AxisRange, HeadType, SpeedLevel, SpeedProfile


@dataclass
class ConnectionConfig:
    use_ethernet: bool = True
    address: str = ""
    serial_port: str = ""
    controller_type: str = "simulation"
    machine_id: str = "SIM_BRAVO"


@dataclass
class HeadConfig:
    head_type: HeadType = HeadType.HT_96_D_70
    check_on_init: bool = True
    default_tip_capacity: float = 200.0
    teach_tip_capacity: float = 200.0
    default_tip_id: str | None = None
    teach_tip_id: str | None = None
    teach_tip_length_mm: float | None = None


@dataclass
class GripperConfig:
    grip_current: float = 0.5
    lid_grip_current: float = 0.3
    y_offset: float = 0.0
    gripper_position: float = 5.0  # G-axis position to grip plate (registry: Gripper position)

    # Plate-pad calibration. These two are a PAIR and are only meaningful
    # together: with a tip of `pad_reference_tip_length_mm` installed, the
    # gripper bottom sits in the plate-pad plane at Zg =
    # `pad_zg_reference_mm`. Pick/place shifts that reference by the taught
    # tip-length delta, so a machine taught with longer tips still lands on
    # the same physical plane.
    #
    # The defaults are the bench measurement this project was developed
    # against (Zg 7.0 mm with a 26.1 mm / 30 uL tip). Re-measure per machine
    # rather than assuming: get the gripper bottom touching the plate pad,
    # record Zg, and record the length of the tip that was on the head.
    pad_zg_reference_mm: float = 7.0
    pad_reference_tip_length_mm: float = 26.1


@dataclass
class SafetyConfig:
    ignore_plate_sensor: bool = False
    ignore_w_axis: bool = False
    simulation_mode: bool = False
    z_safe_position: float = 0.0
    approach_height: float = 10.0
    always_move_to_safe_z: bool = True
    prompt_home_w: bool = True
    run_medium_speed: bool = False
    enable_tips_off_tip_touch: bool = True
    is_srt: bool = False
    # Optional fields kept for parity with legacy registry profiles
    tips_off_w_position: float = -11.0
    tips_off_z_offset: float = 10.0
    tips_off_tip_touch_distance: float = 314.96
    head_tolerance: int = 25
    safe_location: int = 5
    prevent_bravo_during_robotic_access: bool = True
    tip_press_dwell_time: int = 0
    plate_sensor_transient_ms: int = 300
    allow_tos_fluid_handling: bool = False
    enable_tips_on_tip_touch: bool = False
    pin_tool_tip_type: str = "33 mm"


@dataclass
class VisionConfig:
    enabled: bool = False
    service_url: str = "http://127.0.0.1:8101"
    sdk_root: str = "external/pyorbbecsdk"


@dataclass
class BarcodeReaderAccessoryConfig:
    enabled: bool = False
    device_type: str = "ms3"   # key into barcode_reader.DEVICE_PRESETS
    port: str = "COM5"
    side: str = "east"         # "east" or "west" — which side of the plate the scanner reads
    location: int = 0          # deck location (1-9), 0 = not assigned


@dataclass
class AccessoryDeviceConfig:
    id: str = ""
    type: str = ""
    name: str = ""
    enabled: bool = True
    location: int = 0
    holds_labware: bool = True
    connection: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    teachpoint_hint: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "AccessoryDeviceConfig":
        known = {
            "id",
            "type",
            "name",
            "enabled",
            "location",
            "holds_labware",
            "connection",
            "settings",
            "model",
            "teachpoint_hint",
        }
        accessory_type = str(data.get("type") or "").strip()
        accessory_id = str(data.get("id") or "").strip()
        if not accessory_id:
            suffix = index + 1
            accessory_id = f"{accessory_type or 'accessory'}_{suffix}"
        name = str(data.get("name") or "").strip()
        if not name:
            name = accessory_type.replace("_", " ").title() if accessory_type else "Accessory"
        return cls(
            id=accessory_id,
            type=accessory_type,
            name=name,
            enabled=bool(data.get("enabled", True)),
            location=int(data.get("location") or 0),
            holds_labware=bool(data.get("holds_labware", True)),
            connection=dict(data.get("connection") or {}),
            settings=dict(data.get("settings") or {}),
            model=dict(data.get("model") or {}),
            teachpoint_hint=dict(data.get("teachpoint_hint") or {}),
            extra={k: v for k, v in data.items() if k not in known},
        )

    @classmethod
    def from_barcode_reader(cls, cfg: BarcodeReaderAccessoryConfig) -> "AccessoryDeviceConfig":
        return cls(
            id="barcode_reader",
            type="barcode_reader",
            name="Barcode Reader",
            enabled=cfg.enabled,
            location=int(cfg.location or 0),
            holds_labware=True,
            connection={"kind": "serial", "port": cfg.port},
            settings={"device_type": cfg.device_type, "side": cfg.side},
        )

    def to_barcode_reader(self) -> BarcodeReaderAccessoryConfig:
        return BarcodeReaderAccessoryConfig(
            enabled=self.enabled,
            device_type=str(self.settings.get("device_type") or "ms3"),
            port=str(self.connection.get("port") or "COM5"),
            side=str(self.settings.get("side") or "east"),
            location=int(self.location or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extra or {})
        out.update(
            {
                "id": self.id,
                "type": self.type,
                "name": self.name,
                "enabled": self.enabled,
                "location": self.location,
                "holds_labware": self.holds_labware,
                "connection": dict(self.connection or {}),
                "settings": dict(self.settings or {}),
            }
        )
        if self.model:
            out["model"] = dict(self.model)
        if self.teachpoint_hint:
            out["teachpoint_hint"] = dict(self.teachpoint_hint)
        return out


@dataclass
class AccessoriesConfig:
    devices: list[AccessoryDeviceConfig] = field(default_factory=list)
    barcode_reader: BarcodeReaderAccessoryConfig = field(default_factory=BarcodeReaderAccessoryConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccessoriesConfig":
        data = data or {}
        barcode_reader = BarcodeReaderAccessoryConfig()
        if "barcode_reader" in data:
            br = data["barcode_reader"] or {}
            barcode_reader = BarcodeReaderAccessoryConfig(
                **{k: br[k] for k in br if k in BarcodeReaderAccessoryConfig.__dataclass_fields__}
            )

        devices = [
            AccessoryDeviceConfig.from_dict(item or {}, index)
            for index, item in enumerate(data.get("devices") or [])
            if isinstance(item, dict)
        ]
        devices = cls._dedupe_device_ids(devices)
        if not devices and cls._legacy_barcode_has_values(barcode_reader):
            devices.append(AccessoryDeviceConfig.from_barcode_reader(barcode_reader))

        cfg = cls(devices=devices, barcode_reader=barcode_reader)
        cfg.sync_legacy_barcode_from_devices()
        return cfg

    @staticmethod
    def _legacy_barcode_has_values(cfg: BarcodeReaderAccessoryConfig) -> bool:
        return bool(
            cfg.enabled
            or cfg.location
            or cfg.port != "COM5"
            or cfg.device_type != "ms3"
            or cfg.side != "east"
        )

    @staticmethod
    def _dedupe_device_ids(devices: list[AccessoryDeviceConfig]) -> list[AccessoryDeviceConfig]:
        seen: set[str] = set()
        for index, device in enumerate(devices):
            base = device.id or f"{device.type or 'accessory'}_{index + 1}"
            candidate = base
            suffix = 2
            while candidate in seen:
                candidate = f"{base}_{suffix}"
                suffix += 1
            device.id = candidate
            seen.add(candidate)
        return devices

    def to_dict(self) -> dict[str, Any]:
        barcode_reader = self.barcode_reader_from_devices()
        return {
            "devices": [device.to_dict() for device in self.devices],
            "barcode_reader": {
                "enabled": barcode_reader.enabled,
                "device_type": barcode_reader.device_type,
                "port": barcode_reader.port,
                "side": barcode_reader.side,
                "location": barcode_reader.location,
            },
        }

    def barcode_reader_from_devices(self) -> BarcodeReaderAccessoryConfig:
        for device in self.devices:
            if device.type == "barcode_reader":
                return device.to_barcode_reader()
        return self.barcode_reader

    def sync_legacy_barcode_from_devices(self) -> None:
        self.barcode_reader = self.barcode_reader_from_devices()

    def upsert_barcode_reader_device(self) -> None:
        next_device = AccessoryDeviceConfig.from_barcode_reader(self.barcode_reader)
        for index, device in enumerate(self.devices):
            if device.type == "barcode_reader":
                self.devices[index] = next_device
                return
        if self._legacy_barcode_has_values(self.barcode_reader):
            self.devices.append(next_device)


@dataclass
class BravoProfile:
    name: str = "default"
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    head: HeadConfig = field(default_factory=HeadConfig)
    gripper: GripperConfig = field(default_factory=GripperConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    accessories: AccessoriesConfig = field(default_factory=AccessoriesConfig)
    axes: dict[str, AxisConfig] = field(default_factory=dict)
    teachpoints: Teachpoints | None = None
    # Optional: full parity with registry / profile384.reg
    current_limits: dict[str, dict[str, float]] | None = None  # e.g. {"LT": {"96 tips": 0.6}, "ST": {...}}
    locations: list[dict[str, Any]] | None = None  # e.g. [{"location": 1, "location_type": 0}, ...]
    external_robot_access: dict[str, Any] | None = None
    w_axis_motor_control: dict[str, Any] | None = None  # W-axis Motor Control Parameters
    extra: dict[str, Any] | None = None  # any remaining registry keys

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._to_dict()
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    @classmethod
    def load(cls, path: Path | str) -> BravoProfile:
        path = Path(path)
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls._from_dict(data)

    @classmethod
    def default(cls) -> BravoProfile:
        profile = cls()
        profile.head.default_tip_id = get_default_tip_id_for_head(profile.head.head_type)
        profile.head.teach_tip_id = profile.head.default_tip_id
        profile.head.default_tip_capacity = get_tip_capacity_ul(profile.head.head_type, profile.head.default_tip_id)
        profile.head.teach_tip_capacity = get_tip_capacity_ul(profile.head.head_type, profile.head.teach_tip_id)
        profile.head.teach_tip_length_mm = get_tip_length_mm(profile.head.head_type, profile.head.teach_tip_id)
        for axis in Axis:
            profile.axes[axis.name] = get_default_axis_config(axis)
        profile.teachpoints = Teachpoints()
        profile.teachpoints.set_default_teachpoints(profile.head.head_type)
        return profile

    def _to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "connection": {
                "use_ethernet": self.connection.use_ethernet,
                "address": self.connection.address,
                "serial_port": self.connection.serial_port,
                "controller_type": self.connection.controller_type,
                "machine_id": self.connection.machine_id,
            },
            "head": {
                "head_type": self.head.head_type.name,
                "check_on_init": self.head.check_on_init,
                "default_tip_capacity": self.head.default_tip_capacity,
                "teach_tip_capacity": self.head.teach_tip_capacity,
                "default_tip_id": self.head.default_tip_id,
                "teach_tip_id": self.head.teach_tip_id,
                "teach_tip_length_mm": self.head.teach_tip_length_mm,
            },
            "gripper": {
                "grip_current": self.gripper.grip_current,
                "lid_grip_current": self.gripper.lid_grip_current,
                "y_offset": self.gripper.y_offset,
                "gripper_position": self.gripper.gripper_position,
                "pad_zg_reference_mm": self.gripper.pad_zg_reference_mm,
                "pad_reference_tip_length_mm": self.gripper.pad_reference_tip_length_mm,
            },
            "safety": {
                "ignore_plate_sensor": self.safety.ignore_plate_sensor,
                "ignore_w_axis": self.safety.ignore_w_axis,
                "simulation_mode": self.safety.simulation_mode,
                "z_safe_position": self.safety.z_safe_position,
                "approach_height": self.safety.approach_height,
                "always_move_to_safe_z": self.safety.always_move_to_safe_z,
                "prompt_home_w": self.safety.prompt_home_w,
                "run_medium_speed": self.safety.run_medium_speed,
                "enable_tips_off_tip_touch": self.safety.enable_tips_off_tip_touch,
                "is_srt": self.safety.is_srt,
                "tips_off_w_position": self.safety.tips_off_w_position,
                "tips_off_z_offset": self.safety.tips_off_z_offset,
                "tips_off_tip_touch_distance": self.safety.tips_off_tip_touch_distance,
                "head_tolerance": self.safety.head_tolerance,
                "safe_location": self.safety.safe_location,
                "prevent_bravo_during_robotic_access": self.safety.prevent_bravo_during_robotic_access,
                "tip_press_dwell_time": self.safety.tip_press_dwell_time,
                "plate_sensor_transient_ms": self.safety.plate_sensor_transient_ms,
                "allow_tos_fluid_handling": self.safety.allow_tos_fluid_handling,
                "enable_tips_on_tip_touch": self.safety.enable_tips_on_tip_touch,
                "pin_tool_tip_type": self.safety.pin_tool_tip_type,
            },
            "vision": {
                "enabled": self.vision.enabled,
                "service_url": self.vision.service_url,
                "sdk_root": self.vision.sdk_root,
            },
            "accessories": self.accessories.to_dict(),
        }
        if self.axes:
            out["axes"] = {}
            for ax_name, cfg in self.axes.items():
                ax_dict: dict[str, Any] = {
                    "ticks_per_eng_unit": cfg.ticks_per_eng_unit,
                    "min_range": cfg.range.min_pos,
                    "max_range": cfg.range.max_pos,
                    "homing_offset": cfg.homing_offset,
                    "home_flag_register": cfg.home_flag_register,
                    "home_flag_bitmask": cfg.home_flag_bitmask,
                    "home_in_positive_direction": 1 if cfg.home_in_positive_direction else 0,
                    "home_complete_register": cfg.home_complete_register,
                    "homing_soft_stop_decel": cfg.homing_soft_stop_decel,
                    "min_move_full_accel": cfg.min_move_full_accel,
                    "check_for_alignment": 1 if cfg.check_for_alignment else 0,
                }
                for level in SpeedLevel:
                    if level in cfg.speeds:
                        sp = cfg.speeds[level]
                        ax_dict[f"{level.name.lower()}_velocity"] = sp.velocity
                        ax_dict[f"{level.name.lower()}_acceleration"] = sp.acceleration
                out["axes"][ax_name] = ax_dict
        if self.teachpoints is not None:
            out["teachpoints"] = {}
            for loc in self.teachpoints.locations:
                out["teachpoints"][str(loc)] = {
                    "x": self.teachpoints.get_teachpoint(loc, Axis.X),
                    "y": self.teachpoints.get_teachpoint(loc, Axis.Y),
                    "z": self.teachpoints.get_teachpoint(loc, Axis.Z),
                }
        if self.current_limits is not None:
            out["current_limits"] = self.current_limits
        if self.locations is not None:
            out["locations"] = self.locations
        if self.external_robot_access is not None:
            out["external_robot_access"] = self.external_robot_access
        if self.w_axis_motor_control is not None:
            out["w_axis_motor_control"] = self.w_axis_motor_control
        if self.extra is not None:
            out["extra"] = self.extra
        return out

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> BravoProfile:
        profile = cls()
        if "name" in data:
            profile.name = data["name"]
        if "connection" in data:
            c = data["connection"]
            profile.connection = ConnectionConfig(
                use_ethernet=c.get("use_ethernet", True),
                address=c.get("address", ""),
                serial_port=c.get("serial_port", ""),
                controller_type=c.get("controller_type", "simulation"),
                machine_id=c.get("machine_id", "SIM_BRAVO"),
            )
        if "head" in data:
            h = data["head"]
            ht = (
                HeadType[h["head_type"]]
                if isinstance(h.get("head_type"), str)
                else HeadType.HT_96_D_70
            )
            profile.head = HeadConfig(
                head_type=ht,
                check_on_init=h.get("check_on_init", True),
                default_tip_capacity=h.get("default_tip_capacity", 200.0),
                teach_tip_capacity=h.get("teach_tip_capacity", h.get("default_tip_capacity", 200.0)),
                default_tip_id=h.get("default_tip_id"),
                teach_tip_id=h.get("teach_tip_id"),
                teach_tip_length_mm=h.get("teach_tip_length_mm"),
            )
            if not profile.head.default_tip_id:
                profile.head.default_tip_id = (
                    get_tip_id_for_capacity(profile.head.head_type, profile.head.default_tip_capacity)
                    or get_default_tip_id_for_head(profile.head.head_type)
                )
            if not profile.head.teach_tip_id:
                profile.head.teach_tip_id = (
                    get_tip_id_for_capacity(profile.head.head_type, profile.head.teach_tip_capacity)
                    or profile.head.default_tip_id
                )
            profile.head.default_tip_capacity = get_tip_capacity_ul(
                profile.head.head_type,
                profile.head.default_tip_id or profile.head.default_tip_capacity,
            )
            profile.head.teach_tip_capacity = get_tip_capacity_ul(
                profile.head.head_type,
                profile.head.teach_tip_id or profile.head.teach_tip_capacity,
            )
            if profile.head.teach_tip_length_mm is None:
                profile.head.teach_tip_length_mm = get_tip_length_mm(
                    profile.head.head_type,
                    profile.head.teach_tip_id or profile.head.teach_tip_capacity,
                )
        if "gripper" in data:
            g = data["gripper"]
            profile.gripper = GripperConfig(
                **{k: g[k] for k in g if k in GripperConfig.__dataclass_fields__}
            )
        if "safety" in data:
            s = data["safety"]
            profile.safety = SafetyConfig(
                **{k: s[k] for k in s if k in SafetyConfig.__dataclass_fields__}
            )
        if "vision" in data:
            v = data["vision"] or {}
            profile.vision = VisionConfig(
                **{k: v[k] for k in v if k in VisionConfig.__dataclass_fields__}
            )
        if "accessories" in data:
            profile.accessories = AccessoriesConfig.from_dict(data["accessories"] or {})
        # Axes: merge from YAML or use defaults
        for axis in Axis:
            profile.axes[axis.name] = get_default_axis_config(axis)
        if "axes" in data:
            for ax_name, ax_data in data["axes"].items():
                try:
                    ax_enum = Axis[ax_name]
                except KeyError:
                    continue
                base = profile.axes.get(ax_name) or get_default_axis_config(ax_enum)
                r = base.range
                if "min_range" in ax_data or "max_range" in ax_data:
                    r = AxisRange(
                        ax_data.get("min_range", base.range.min_pos),
                        ax_data.get("max_range", base.range.max_pos),
                    )
                speeds = dict(base.speeds)
                for level in SpeedLevel:
                    v_key = f"{level.name.lower()}_velocity"
                    a_key = f"{level.name.lower()}_acceleration"
                    if v_key in ax_data and a_key in ax_data:
                        speeds[level] = SpeedProfile(
                            float(ax_data[v_key]),
                            float(ax_data[a_key]),
                        )
                profile.axes[ax_name] = AxisConfig(
                    axis=ax_enum,
                    ticks_per_eng_unit=float(ax_data.get("ticks_per_eng_unit", base.ticks_per_eng_unit)),
                    range=r,
                    homing_offset=float(ax_data.get("homing_offset", base.homing_offset)),
                    home_in_positive_direction=bool(ax_data.get("home_in_positive_direction", base.home_in_positive_direction)),
                    home_flag_bitmask=int(ax_data.get("home_flag_bitmask", base.home_flag_bitmask)),
                    home_flag_register=int(ax_data.get("home_flag_register", base.home_flag_register)),
                    home_complete_register=int(ax_data.get("home_complete_register", base.home_complete_register)),
                    homing_soft_stop_decel=float(ax_data.get("homing_soft_stop_decel", getattr(base, "homing_soft_stop_decel", 300.0))),
                    min_move_full_accel=float(ax_data.get("min_move_full_accel", getattr(base, "min_move_full_accel", 0.0))),
                    check_for_alignment=bool(ax_data.get("check_for_alignment", getattr(base, "check_for_alignment", True))),
                    speeds=speeds,
                )
        # Teachpoints: from YAML or defaults
        profile.teachpoints = Teachpoints()
        if "teachpoints" in data:
            for loc_str, tp in data["teachpoints"].items():
                loc = int(loc_str)
                profile.teachpoints.set_teachpoint(loc, Axis.X, float(tp.get("x", 0)))
                profile.teachpoints.set_teachpoint(loc, Axis.Y, float(tp.get("y", 0)))
                profile.teachpoints.set_teachpoint(loc, Axis.Z, float(tp.get("z", 0)))
        else:
            profile.teachpoints.set_default_teachpoints(profile.head.head_type)
        if "current_limits" in data:
            profile.current_limits = data["current_limits"]
        if "locations" in data:
            profile.locations = data["locations"]
        if "external_robot_access" in data:
            profile.external_robot_access = data["external_robot_access"]
        if "w_axis_motor_control" in data:
            profile.w_axis_motor_control = data["w_axis_motor_control"]
        if "extra" in data:
            profile.extra = data["extra"]
        return profile
