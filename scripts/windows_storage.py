"""Conservative Windows volume identification and machine-wide disk mutexes.

Only metadata APIs are used; no raw device is opened or written.
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import subprocess
from ctypes import wintypes


class WindowsStorageError(Exception):
    pass


def _kernel():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def volume_identity(path):
    """Resolve a drive-letter root and GUID; refuse remote or mounted folders."""
    drive = os.path.splitdrive(path)[0]
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise WindowsStorageError("A local drive-letter path is required")
    kernel = _kernel()
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    volume_path.restype = wintypes.BOOL
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    get_name = kernel.GetVolumeNameForVolumeMountPointW
    get_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_name.restype = wintypes.BOOL
    mount = ctypes.create_unicode_buffer(32768)
    if not volume_path(path, mount, len(mount)):
        raise WindowsStorageError("Windows volume mount could not be resolved")
    root = drive.upper() + "\\"
    if mount.value.upper() != root or drive_type(mount.value) not in (2, 3):
        raise WindowsStorageError("Only local drive-letter volumes are supported")
    guid = ctypes.create_unicode_buffer(128)
    if not get_name(mount.value, guid, len(guid)) or not re.fullmatch(
        r"\\\\\?\\Volume\{[0-9a-fA-F-]{36}\}\\", guid.value
    ):
        raise WindowsStorageError("Stable Windows volume identity unavailable")
    return root, guid.value.lower()


def disk_metadata(letter):
    if not re.fullmatch(r"[A-Z]", letter):
        raise WindowsStorageError("Invalid drive letter")
    # Letter is constrained above; no user path is interpreted as PowerShell.
    script = f'''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$p = @(Get-Partition -DriveLetter '{letter}')
$v = @(Get-Volume -DriveLetter '{letter}')
if ($p.Count -ne 1 -or $v.Count -ne 1) {{ throw 'ambiguous volume' }}
$d = @(Get-Disk -Number $p[0].DiskNumber)
if ($d.Count -ne 1) {{ throw 'ambiguous disk' }}
@{{number=$d[0].Number; unique=[string]$d[0].UniqueId; bus=[string]$d[0].BusType;
style=[string]$d[0].PartitionStyle; readonly=$d[0].IsReadOnly; offline=$d[0].IsOffline;
clustered=$d[0].IsClustered; partition_readonly=$p[0].IsReadOnly;
partition_type=[string]$p[0].Type;
filesystem=[string]$v[0].FileSystem; model=[string]$d[0].FriendlyName}} | ConvertTo-Json -Compress'''
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                                 "-EncodedCommand", encoded], capture_output=True, check=True, timeout=20)
        data = json.loads(result.stdout.decode("utf-8-sig"))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise WindowsStorageError("Windows Storage metadata unavailable") from exc
    return validate_disk_metadata(data)


def validate_disk_metadata(data):
    if not isinstance(data, dict):
        raise WindowsStorageError("Invalid disk metadata")
    if (not isinstance(data.get("number"), int) or isinstance(data["number"], bool)
        or data["number"] < 0 or not isinstance(data.get("unique"), str)
        or not data["unique"].strip() or data.get("bus") not in ("USB", "SATA", "ATA", "NVMe", "SD", "MMC")
        or data.get("style") not in ("GPT", "MBR")
        or data.get("partition_type") not in ("Basic", "IFS", "FAT32", "FAT32 LBA", "FAT16", "Huge")
        or any(data.get(k) is not False for k in ("readonly", "offline", "clustered", "partition_readonly"))
        or data.get("filesystem") not in ("NTFS", "exFAT", "FAT32")):
        raise WindowsStorageError("Unrecognized, virtual, remote or read-only disk")
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        model = None
    return {"number": data["number"], "identity": hashlib.sha256(data["unique"].encode("utf-8")).hexdigest(),
            "bus": data["bus"], "filesystem": data["filesystem"], "model": model}


def acquire_mutex(device):
    kernel = _kernel()
    create = kernel.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    name = "Global\\DriveSpeed-" + hashlib.sha256(device.encode()).hexdigest()
    handle = create(None, False, name)
    if not handle:
        raise WindowsStorageError("Cannot create system-wide device lock")
    result = wait(handle, 0)
    if result not in (0, 0x80):
        close(handle)
        raise WindowsStorageError("Device is locked or lock access is unavailable")
    return handle


def release_mutex(handle):
    kernel = _kernel()
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        if not release(handle):
            raise WindowsStorageError("Cannot release device lock")
    finally:
        close(handle)
