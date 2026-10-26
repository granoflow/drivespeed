"""Read hardware model names once; never persist whole inventories or serial numbers."""

import json
import platform
import subprocess
from pathlib import Path


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def mac_hardware(profile, physical_devices):
    hardware = empty_hardware()
    hosts = profile.get("SPHardwareDataType", [])
    if hosts:
        host = hosts[0]
        names = [
            host.get(k)
            for k in ("machine_name", "chip_type", "machine_model", "physical_memory")
        ]
        hardware["computer"] = dict(
            model=" · ".join(str(x) for x in names if x) or None,
            source="system_profiler",
        )
    models = []
    for kind in ("SPNVMeDataType", "SPSerialATADataType", "SPStorageDataType"):
        for node in nodes(profile.get(kind, [])):
            if node.get("bsd_name") in physical_devices:
                name = node.get("device_model") or node.get("model")
                if name and name not in models:
                    models.append(name)
    if models:
        hardware["drive"] = dict(
            model=" / ".join(models), source="system_profiler:bsd_name"
        )
    # A bridge name is not a retail enclosure model. Even one candidate does
    # not establish which target is connected through it.
    candidates = []
    for kind in ("SPThunderboltDataType", "SPUSBDataType"):
        for node in nodes(profile.get(kind, [])):
            if kind == "SPThunderboltDataType" and node.get("route_string_key") not in (
                None,
                "0",
            ):
                parts = [node.get("vendor_name_key"), node.get("device_name_key")]
            elif (
                kind == "SPUSBDataType"
                and node.get("manufacturer")
                and node.get("_name")
            ):
                parts = [node["manufacturer"], node["_name"]]
            else:
                continue
            label = " ".join(str(x) for x in parts if x)
            if label and label not in candidates:
                candidates.append(label)
    hardware["enclosure"]["bridge_candidates"] = candidates
    return hardware


def empty_hardware():
    return dict(
        computer=dict(model=None, source="unavailable"),
        drive=dict(model=None, source="unavailable"),
        enclosure=dict(model=None, source="unavailable", bridge_candidates=[]),
    )


def collect_hardware(target, computer=None, drive=None, enclosure=None):
    hardware = empty_hardware()
    try:
        if platform.system() == "Darwin":
            output = subprocess.check_output(
                [
                    "system_profiler",
                    "SPHardwareDataType",
                    "SPNVMeDataType",
                    "SPSerialATADataType",
                    "SPStorageDataType",
                    "SPThunderboltDataType",
                    "SPUSBDataType",
                    "-json",
                ],
                timeout=45,
            )
            hardware = mac_hardware(json.loads(output), target["physical_devices"])
        elif platform.system() == "Linux":
            fields = []
            for name in ("sys_vendor", "product_name", "product_version"):
                path = Path("/sys/class/dmi/id") / name
                if path.is_file():
                    fields.append(path.read_text(encoding="utf-8").strip())
            hardware["computer"] = dict(
                model=" · ".join(x for x in fields if x) or None, source="sysfs:DMI"
            )
            output = subprocess.check_output(
                ["lsblk", "-J", "-o", "NAME,MODEL"], timeout=15
            )
            models = [
                n["model"].strip()
                for n in nodes(json.loads(output))
                if n.get("name") in target["physical_devices"] and n.get("model")
            ]
            hardware["drive"] = dict(
                model=" / ".join(models) or None, source="lsblk:physical-device"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        hardware["collection_error"] = type(exc).__name__
    if not hardware["drive"]["model"] and target.get("model"):
        hardware["drive"] = dict(
            model=target["model"], source="diskutil:reported-media-name"
        )
    for key, value in (
        ("computer", computer),
        ("drive", drive),
        ("enclosure", enclosure),
    ):
        if value is not None:
            if not value.strip():
                raise ValueError(f"{key} model override must not be empty")
            hardware[key].update(model=value.strip(), source="user")
    return hardware
