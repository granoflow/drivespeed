"""Private, reported drive identity; never persist or log firmware identifiers.

The fingerprint supports duplicate matching, not proof of hardware ownership or
authenticity. Missing metadata never prevents a local benchmark.
"""

import hashlib
import json
import platform
import plistlib
import re
import shutil
import subprocess
import unicodedata

import windows_storage
from system_io import BenchError, check_target


DOMAIN = "drivespeed-device-v1"
SOURCE = "smartctl:serial_number"
MAX_OUTPUT_BYTES = 128 * 1024
QUERY_TIMEOUT = 5
ATTACHMENTS = {"internal", "external", "unknown"}


def validate_device_identity(value):
    """Return only the private metadata contract; reject accidental raw fields."""
    required = {"version", "fingerprint", "source"}
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - {"attachment"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("fingerprint"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
            or value.get("source") != SOURCE
            or ("attachment" in value and (not isinstance(value["attachment"], str)
                                           or value["attachment"] not in ATTACHMENTS))):
        raise ValueError("Invalid private device identity")
    return dict(value)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Ambiguous device metadata")
        value[key] = item
    return value


def _identifier(value):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value).strip()
    if (not value or len(value) > 256
            or any(unicodedata.category(char).startswith("C") for char in value)):
        return None
    return value


def _serial(value):
    value = _identifier(value)
    if value is None:
        return None
    marker = re.sub(r"[\s._:/-]", "", value).casefold()
    if (marker in {"unknown", "none", "null", "na", "notavailable", "notspecified",
                   "notapplicable", "defaultstring", "serialnumber", "systemserialnumber",
                   "tobefilledbyoem", "0123456789", "123456789", "1234567890",
                   "0123456789abcdef"}
            or len(set(marker)) < 2 or re.fullmatch(r"(.{1,4})\1+", marker)):
        return None
    return value


def _one_physical(target):
    devices = target.get("physical_devices") if isinstance(target, dict) else None
    return (isinstance(target, dict) and target.get("scope") == "physical"
            and isinstance(devices, list) and len(devices) == 1
            and isinstance(devices[0], str))


def _smart_device(target, system):
    disk = target["physical_devices"][0]
    if system == "Darwin" and re.fullmatch(r"disk[0-9]+", disk):
        return "/dev/" + disk
    if system == "Linux" and re.fullmatch(r"(?:sd[a-z]+|hd[a-z]+|nvme[0-9]+n[1-9][0-9]*)", disk):
        return "/dev/" + disk
    if system == "Windows":
        # UniqueId binds this fresh volume-to-disk lookup to the admitted target;
        # it is never a replacement for the firmware serial used in the hash.
        root, volume = windows_storage.volume_identity(target["path"])
        metadata = windows_storage.disk_metadata(root[0])
        number = metadata.get("number")
        if (target.get("device") == volume and type(number) is int and number >= 0
                and disk == "windows-disk:" + metadata["identity"]):
            return f"/dev/pd{number}"
    return None


def _query(command):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=QUERY_TIMEOUT, check=False)
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("Device metadata unavailable")
    return result.stdout


def _attachment(device, system):
    if system == "Darwin":
        try:
            value = plistlib.loads(_query(["diskutil", "info", "-plist", device]))
            if not isinstance(value, dict) or value.get("DeviceIdentifier") != device.removeprefix("/dev/"):
                return "unknown"
            internal = value.get("Internal")
            if type(internal) is bool:
                return "internal" if internal else "external"
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            pass
    return "unknown"


def collect_device_identity(target):
    """Read one exact ATA/NVMe drive through existing smartctl, or return None.

    No installation, elevation, shell, raw firmware value in errors, or fallback
    to enclosure/volume identifiers. Queries occur before background observation.
    """
    if not _one_physical(target):
        return None
    tool = shutil.which("smartctl")
    if tool is None:
        return None
    try:
        current = check_target(target)
        if not _one_physical(current) or current["physical_devices"] != target["physical_devices"]:
            return None
        system = platform.system()
        device = _smart_device(current, system)
        if device is None:
            return None
        value = json.loads(_query([tool, "-i", "-j", device]), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid metadata")))
        descriptor = value.get("device", {}) if isinstance(value, dict) else {}
        if (not isinstance(descriptor, dict) or descriptor.get("name") != device
                or descriptor.get("protocol") not in ("ATA", "NVMe")):
            return None
        model, serial = _identifier(value.get("model_name")), _serial(value.get("serial_number"))
        if model is None or serial is None or model.casefold() in {"unknown", "none", "n/a"}:
            return None
        attachment = _attachment(device, system)
        fresh = check_target(target)
        if (not _one_physical(fresh) or fresh["physical_devices"] != target["physical_devices"]
                or _smart_device(fresh, system) != device):
            return None
        material = json.dumps([DOMAIN, model, serial], ensure_ascii=False, separators=(",", ":"))
        return validate_device_identity(dict(version=1, fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
                                             source=SOURCE, attachment=attachment))
    except (BenchError, OSError, ValueError, TypeError, KeyError, AttributeError,
            windows_storage.WindowsStorageError, subprocess.SubprocessError, RecursionError):
        return None
