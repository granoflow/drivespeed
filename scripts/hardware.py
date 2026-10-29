"""Read hardware model names once; never persist whole inventories or serial numbers."""

import json
import base64
import math
import platform
import re
import subprocess
from pathlib import Path


def positive_number(value):
    """Parse only numeric metadata, never infer a transport from throughput."""
    match = re.fullmatch(r"\s*x?([0-9]+(?:\.[0-9]+)?)\s*(?:GT/s|Gb/s|Gbit/s|Mbps)?(?:\s+PCIe)?\s*", str(value or ""), re.I)
    number = float(match[1]) if match else None
    return number if number and math.isfinite(number) else None


def linux_links(devices, sys_root=Path("/sys")):
    """Read only ancestors of a uniquely identified disk, without serials or IDs."""
    if len(devices) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+", devices[0]):
        return []
    root = Path(sys_root).resolve()
    device = (root / "class/block" / devices[0] / "device").resolve()
    if not device.is_relative_to(root / "devices"):
        return []
    links = []
    for node in (device, *device.parents):
        if node == root:
            break
        def read(name):
            try:
                return (node / name).read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                return None
        if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", node.name):
            item = {"kind": "pcie", "source": "sysfs:target-ancestor", "state": "negotiated"}
            for name, field in (("current_link_speed", "gt_s"), ("current_link_width", "lanes"),
                                ("max_link_speed", "max_gt_s"), ("max_link_width", "max_lanes")):
                item[field] = positive_number(read(name))
            if item["gt_s"] or item["max_gt_s"]:
                links.append(item)
        elif (node / "idVendor").is_file() and (node / "idProduct").is_file():
            speed = positive_number(read("speed"))
            link = dict(kind="usb", source="sysfs:target-ancestor", state="negotiated", gbps=speed / 1000 if speed else None,
                        reported_name=read('product'))
            for name in ('idVendor', 'idProduct'):
                value = read(name)
                if value and re.fullmatch(r'[0-9a-fA-F]{4}', value):
                    link[name] = value.lower()
            links.append(link)
    return links


def windows_computer():
    script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    raw = subprocess.check_output(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=20)
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Ambiguous Windows computer metadata")
    fields = [data.get(key) for key in ("Manufacturer", "Model")]
    model = " · ".join(value.strip() for value in fields if isinstance(value, str) and value.strip())
    return dict(model=model or None, matching_model=model or None, source="windows:Win32_ComputerSystem")


def mac_links(value, physical_devices):
    if len(physical_devices) != 1:
        return []
    for node in nodes(value):
        descendants = {item["bsd_name"] for item in nodes(node) if re.fullmatch(r'disk\d+', str(item.get('bsd_name', '')))}
        if descendants != set(physical_devices):
            continue
        speed = positive_number(node.get("link_speed") or node.get("spnvme_linkspeed"))
        width = positive_number(node.get("link_width") or node.get("spnvme_linkwidth"))
        if speed or width:
            return [dict(kind="pcie", state="reported", gt_s=speed, lanes=width, source="system_profiler:bsd_name")]
    return []


def controller_from_label(label):
    """Recognize documented chip labels; this is reported identity, not proof."""
    catalog = json.loads((Path(__file__).resolve().parent.parent / 'assets/interface-reference.json').read_text(encoding='utf-8'))
    for item in catalog.get('controllers', []):
        if re.search(r'(?<![A-Za-z0-9])' + re.escape(item['model']) + r'(?![A-Za-z0-9])', label, re.I):
            return item['model']
    return None


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
            matching_model=host.get("machine_model"),
        )
    models = []
    for kind in ("SPNVMeDataType", "SPSerialATADataType", "SPStorageDataType"):
        for node in nodes(profile.get(kind, [])):
            if node.get("bsd_name") in physical_devices:
                name = node.get("device_model") or node.get("model")
                if name and name not in models:
                    models.append(name)
    # Match a single NVMe subtree to the target. Unrelated bridges cannot
    # supply this disk's PCIe generation or a negotiated path rate.
    hardware["links"] = mac_links(profile.get("SPNVMeDataType", []), physical_devices)
    if models:
        hardware["drive"] = dict(
            model=" / ".join(models), source="system_profiler:bsd_name",
            matching_model=models[0] if len(models) == 1 and len(physical_devices) == 1 else None
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
    hardware["controller"]["candidates"] = candidates
    if len(physical_devices) == 1:
        for node in nodes(profile.get("SPUSBDataType", [])):
            descendants = {item["bsd_name"] for item in nodes(node) if re.fullmatch(r'disk\d+', str(item.get('bsd_name', '')))}
            if descendants != set(physical_devices):
                continue
            label = str(node.get('_name') or '')
            if label:
                hardware['controller']['reported_name'] = label
            for field in ('vendor_id', 'product_id'):
                match = re.match(r'0x([0-9a-fA-F]{4})(?:\s|$)', str(node.get(field, '')))
                if match:
                    hardware['controller'][field] = match[1].lower()
            chip = controller_from_label(label)
            if chip:
                hardware['controller'].update(model=chip, source='system_profiler:target-label')
                break
    return hardware


def empty_hardware():
    return dict(
        computer=dict(model=None, source="unavailable"),
        drive=dict(model=None, source="unavailable"),
        enclosure=dict(model=None, source="unavailable", bridge_candidates=[]),
        controller=dict(model=None, source="unavailable", candidates=[]),
        links=[],
    )


def collect_hardware(
    target, computer=None, drive=None, enclosure=None, controller=None, access_interface=None
):
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
                model=" · ".join(x for x in fields if x) or None, source="sysfs:DMI",
                matching_model=" · ".join(x for x in fields[:2] if x) or None
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
                model=" / ".join(models) or None, source="lsblk:physical-device",
                matching_model=models[0] if len(models) == 1 and len(target["physical_devices"]) == 1 else None
            )
            hardware["links"] = linux_links(target["physical_devices"])
            for link in hardware['links']:
                chip = controller_from_label(link.get('reported_name') or '')
                if chip:
                    hardware['controller'].update(model=chip, source='sysfs:target-usb-label')
                    break
        elif platform.system() == "Windows":
            hardware["computer"] = windows_computer()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        hardware["collection_error"] = type(exc).__name__
    if not hardware["drive"]["model"] and target.get("model"):
        hardware["drive"] = dict(
            model=target["model"],
            source="windows:disk-model" if platform.system() == "Windows" else "diskutil:reported-media-name",
            matching_model=target["model"] if platform.system() == "Windows" else None,
        )
    # Some unmodified bridge firmware exposes the chip name as the disk name.
    # Preserve it as a controller clue rather than claiming it is an SSD model.
    chip = controller_from_label(hardware['drive'].get('model') or '')
    if chip and str(target.get('protocol', '')).lower() == 'usb':
        hardware['controller'].update(model=chip, source=hardware['drive']['source'])
        hardware['drive'] = dict(model=None, source='unavailable')
    for key, value in (
        ("computer", computer),
        ("drive", drive),
        ("enclosure", enclosure),
        ("controller", controller),
    ):
        if value is not None:
            if not value.strip():
                raise ValueError(f"{key} model override must not be empty")
            hardware[key].update(model=value.strip(), source="user")
            if key in ("computer", "drive"):
                hardware[key]["matching_model"] = value.strip()
    if access_interface is not None:
        from interface_types import confirmed_interface
        hardware["access_interface"] = confirmed_interface(access_interface)
    return hardware
