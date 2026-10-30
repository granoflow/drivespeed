"""fio identity/install and mounted-device guards. All writes use owned files."""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import stat as stat_module
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
import windows_storage

VERSION = "3.43"
SOURCE_SHA = "efa49b3f36eda9adf29294f27a87d7e457747d676cd0f73996d6365357832cfe"
WINDOWS_SHA = "8299e6e6fbc024d3b74aa1ce62fb94ef6960b45629fbfa678b720834b6eb34aa"
CACHE = Path.home() / ".cache" / "drivespeed"


class BenchError(Exception):
    def __init__(self, key, detail="", code=4):
        super().__init__(detail)
        self.key, self.detail, self.code = key, detail, code


def capture(args, timeout=15):
    return subprocess.run(args, capture_output=True, timeout=timeout, check=True).stdout


def fio_identity(path):
    try:
        version = capture([str(path), "--version"]).decode().strip()
        help_text = capture([str(path), "--help"]).decode(errors="replace")
        if not re.fullmatch(r"fio-3\.\d+(?:[^\s]*)?", version) or not all(
            marker in help_text
            for marker in (
                "--enghelp",
                "--output-format",
                "--readonly",
            )
        ):
            return None
        engines = capture([str(path), "--enghelp"]).decode(errors="replace")
        return dict(path=str(Path(path).resolve()), version=version, engines=engines)
    except (OSError, subprocess.SubprocessError):
        return None


def find_fio():
    candidates = [
        shutil.which("fio"),
        CACHE / "bin/fio",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "fio/fio.exe",
    ]
    for path in candidates:
        if path and (found := fio_identity(path)):
            return found
    return None


def stop_process(proc):
    """Stop the owned session, including descendants that outlive the group leader."""
    if platform.system() == "Windows":
        if proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
            proc.wait(5)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The leader exiting does not imply its descendants have exited. Escalate
        # for the original session even when wait() returned immediately.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def logged_command(args, output, progress, timeout=600, cwd=None):
    """Keep unknown install/build progress honest while retaining upstream logs."""
    with output.open("ab") as log:
        proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            start_new_session=platform.system() != "Windows",
        )
        start = time.monotonic()
        try:
            while proc.poll() is None:
                progress.emit("dependency")
                if time.monotonic() - start > timeout:
                    raise BenchError("dependency_failed", f"Timeout: {args[0]}", 3)
                time.sleep(0.25)
            if proc.returncode:
                raise BenchError(
                    "dependency_failed",
                    f"{args[0]}: exit {proc.returncode}; {output}",
                    3,
                )
        finally:
            stop_process(proc)


def download(url, dest, sha, progress):
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as response, dest.open("xb") as out:
        total = int(response.headers.get("Content-Length", 0))
        written = 0
        while block := response.read(256 * 1024):
            written += len(block)
            if written > 100 * 1024 * 1024:
                raise BenchError("dependency_failed", "Download exceeds 100 MiB", 3)
            digest.update(block)
            out.write(block)
            progress.emit(
                "dependency", percent=written / total * 100 if total else None
            )
    if digest.hexdigest() != sha:
        raise BenchError(
            "dependency_failed",
            "SHA256 mismatch; downloaded file retained, not executed",
            3,
        )


def ensure_fio(output, progress):
    if found := find_fio():
        return found
    system = platform.system()
    brew = shutil.which("brew")
    if system == "Darwin" and brew:
        logged_command([brew, "install", "fio"], output / "install.log", progress)
    elif system == "Linux" and any(
        shutil.which(x) for x in ("apt-get", "dnf", "pacman")
    ):
        manager = next(x for x in ("apt-get", "dnf", "pacman") if shutil.which(x))
        args = {
            "apt-get": ["apt-get", "install", "-y", "fio"],
            "dnf": ["dnf", "install", "-y", "fio"],
            "pacman": ["pacman", "-S", "--needed", "--noconfirm", "fio"],
        }[manager]
        if os.geteuid() != 0:
            args = ["sudo", "-n"] + args
        logged_command(args, output / "install.log", progress)
    else:
        CACHE.mkdir(parents=True, exist_ok=True)
        # Unique workspace: keep failed artifacts for diagnosis, never delete unknown cache files.
        work = Path(tempfile.mkdtemp(prefix="install-", dir=CACHE))
        if system == "Windows":
            if platform.machine().lower() not in ("amd64", "x86_64"):
                raise BenchError(
                    "unsupported", "Windows automatic install: x64 only", 3
                )
            archive = work / "fio.msi"
            download(
                f"https://github.com/axboe/fio/releases/download/fio-{VERSION}/fio-{VERSION}-x64.msi",
                archive,
                WINDOWS_SHA,
                progress,
            )
            logged_command(
                ["msiexec", "/i", str(archive), "/qn", "/norestart"],
                output / "install.log",
                progress,
            )
        elif system in ("Darwin", "Linux"):
            if not all(shutil.which(x) for x in ("cc", "make")):
                raise BenchError(
                    "dependency_failed",
                    "No package manager or existing C compiler/make; install fio using official instructions",
                    3,
                )
            archive = work / "fio.tar.gz"
            download(
                f"https://github.com/axboe/fio/archive/refs/tags/fio-{VERSION}.tar.gz",
                archive,
                SOURCE_SHA,
                progress,
            )
            with tarfile.open(archive) as tar:
                for member in tar.getmembers():
                    target = (work / member.name).resolve()
                    if not target.is_relative_to(work) or not (
                        member.isfile() or member.isdir()
                    ):
                        raise BenchError(
                            "dependency_failed", "Unsafe archive member", 3
                        )
                tar.extractall(
                    work
                )  # Every member was validated above; compatible with Python 3.10.
            source = work / f"fio-fio-{VERSION}"
            logged_command(
                [str(source / "configure"), f"--prefix={CACHE}"],
                output / "install.log",
                progress,
                cwd=source,
            )
            # configure/make need source cwd, handled through -C and configure cwd below.
            logged_command(
                ["make", "-C", str(source), "-j2"], output / "install.log", progress
            )
            logged_command(
                ["make", "-C", str(source), "install"], output / "install.log", progress
            )
        else:
            raise BenchError("unsupported", system, 3)
    found = find_fio()
    if not found:
        raise BenchError(
            "dependency_failed", "fio identity/capability check failed after install", 3
        )
    return found


def _diskutil_info(identifier):
    value = plistlib.loads(capture(["diskutil", "info", "-plist", identifier]))
    if not isinstance(value, dict):
        raise BenchError("unsafe_target", "Invalid diskutil identity")
    return value


def _mac_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"disk\d+(?:s\d+)*", value):
        raise BenchError("unsafe_target", "Disk device identity unavailable")
    return value


def _backing_target(filename, seen):
    backing = Path(filename)
    if not backing.is_absolute() or backing.is_symlink() or not backing.is_file():
        raise BenchError("unsafe_target", "Local ordinary backing file unavailable")
    if backing.stat().st_dev != backing.parent.stat().st_dev:
        raise BenchError("unsafe_target", "File-mounted backing storage needs an explicit dependency mapping")
    return target_info(backing.parent, _seen=seen)


def _device_records(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise BenchError("unsafe_target", "Invalid structured device records")
    return value


def _mac_physical(identifier, seen):
    identifier = _mac_identifier(identifier)
    key = "mac-device:" + identifier
    if key in seen or len(seen) >= 32:
        raise BenchError("unsafe_target", "Cyclic physical-device mapping")
    seen = set(seen) | {key}
    disk = _diskutil_info(identifier)
    stores = _device_records(disk.get("APFSPhysicalStores") or [])
    if stores:
        roots, virtual = [], False
        for store in stores:
            child = store.get("DeviceIdentifier") or store.get("APFSPhysicalStore")
            devices, image = _mac_physical(child, seen)
            roots.extend(devices)
            virtual = virtual or image
        return roots, virtual
    whole = _mac_identifier(disk.get("ParentWholeDisk") or identifier)
    if whole != identifier:
        return _mac_physical(whole, seen)
    if disk.get("VirtualOrPhysical") == "Virtual" or disk.get("DiskImage") is True:
        images = plistlib.loads(capture(["hdiutil", "info", "-plist"]))
        if not isinstance(images, dict):
            raise BenchError("unsafe_target", "Invalid disk-image inventory")
        for image in _device_records(images.get("images", [])):
            entities = _device_records(image.get("system-entities", []))
            if any(entry.get("dev-entry") == "/dev/" + whole for entry in entities):
                backing = image.get("image-path")
                if not isinstance(backing, str):
                    break
                host = _backing_target(backing, seen)
                return host["physical_devices"], True
        raise BenchError("unsafe_target", "Virtual disk physical dependencies unavailable")
    # Apple Silicon reports its whole internal Apple Fabric device as
    # "Unknown"; APFS synthetic containers still report Virtual and have stores.
    local_apple = (disk.get("VirtualOrPhysical") == "Unknown"
                   and disk.get("Internal") is True
                   and disk.get("BusProtocol") == "Apple Fabric"
                   and disk.get("SystemImage") is False)
    if (disk.get("WholeDisk", disk.get("Whole")) is not True
            or not (disk.get("VirtualOrPhysical") == "Physical" or local_apple)):
        raise BenchError("unsafe_target", "Whole physical disk identity unavailable")
    return [whole], False


def _mac_target(path, info, seen):
    # diskutil accepts a mount/device, not arbitrary directories. stat's fixed
    # device format also resolves APFS Data firmlinks without parsing df tables.
    identifier = _mac_identifier(capture(["stat", "-f", "%Sd", str(path)]).decode().strip())
    disk = _diskutil_info(identifier)
    if not disk.get("MountPoint") or disk.get("WritableVolume", disk.get("Writable")) is not True:
        raise BenchError("unsafe_target", "Mounted writable local volume required")
    stores = _device_records(disk.get("APFSPhysicalStores") or [])
    if disk.get("APFSContainerReference") and not stores:
        container = _mac_identifier(disk["APFSContainerReference"])
        listing = plistlib.loads(capture(["diskutil", "apfs", "list", "-plist", container]))
        if not isinstance(listing, dict):
            raise BenchError("unsafe_target", "Invalid APFS container inventory")
        matches = [item for item in _device_records(listing.get("Containers", []))
                   if item.get("ContainerReference") == container]
        if len(matches) != 1:
            raise BenchError("unsafe_target", "APFS physical stores unavailable")
        stores = _device_records(matches[0].get("PhysicalStores") or [])
        if not stores:
            raise BenchError("unsafe_target", "APFS physical stores unavailable")
    identifiers = [item.get("DeviceIdentifier") or item.get("APFSPhysicalStore") for item in stores]
    if not identifiers:
        identifiers = [disk.get("ParentWholeDisk") or identifier]
    roots, virtual = [], False
    for physical in identifiers:
        devices, image = _mac_physical(physical, seen)
        roots.extend(devices)
        virtual = virtual or image
    info.update(
        mount=disk["MountPoint"], device=identifier,
        physical_devices=sorted(set(roots)), filesystem=disk.get("FilesystemType"),
        protocol=disk.get("BusProtocol"), model=disk.get("MediaName"),
        internal=disk.get("Internal"), smart=disk.get("SMARTStatus"),
        scope="virtual" if virtual else ("multi_device" if len(set(roots)) > 1 else "physical"),
    )
    temperature = disk.get("SMARTDeviceSpecificKeysMayVaryNotGuaranteed", {}).get("TEMPERATURE")
    info["temperature_c"] = temperature - 273.15 if temperature else None


def _linux_local_disk(node):
    name, transport = node["name"], node.get("tran")
    if re.fullmatch(r"(?:nbd|rbd|drbd)\d+", name):
        raise BenchError("unsafe_target", "Network block storage is not a local target")
    if transport == "nvme":
        controller = re.fullmatch(r"(nvme\d+)(?:c\d+)?n\d+", name)
        if not controller:
            raise BenchError("unsafe_target", "NVMe controller identity unavailable")
        connection = (Path("/sys/class/nvme") / controller[1] / "transport").read_text().strip()
        if connection != "pcie":
            raise BenchError("unsafe_target", "Only positively local PCIe NVMe is supported")
    elif transport is None:
        device = str((Path("/sys/class/block") / name / "device").resolve(strict=True))
        if not device.startswith("/sys/devices/pci") or any(
            marker in device for marker in ("/session", "/rport-", "/virtio", "/net/")
        ):
            raise BenchError("unsafe_target", "Local physical-device transport unavailable")
    elif transport not in ("ata", "sata", "sas", "spi", "usb", "mmc", "pcie", "thunderbolt"):
        raise BenchError("unsafe_target", "Remote or unknown device transport is not supported")


def _linux_target(path, info, seen):
    item = json.loads(capture([
        "findmnt", "-J", "-T", str(path), "-o", "SOURCE,FSTYPE,TARGET,OPTIONS"
    ]))["filesystems"][0]
    source = item["source"].split("[")[0]
    if not source.startswith("/dev/") or "ro" in item["options"].split(","):
        raise BenchError("unsafe_target", "Mounted writable local block storage required")
    nodes = json.loads(capture([
        "lsblk", "-s", "-J", "-o", "NAME,TYPE,TRAN,RO", source
    ]))["blockdevices"]
    devices, transports, virtual = [], [], False

    def visit(node):
        nonlocal virtual
        kind = node["type"]
        if not isinstance(kind, str):
            raise BenchError("unsafe_target", "Invalid block device type")
        name = node["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", name):
            raise BenchError("unsafe_target", "Invalid block device identity")
        if node.get("ro") not in (False, 0):
            raise BenchError("unsafe_target", "Read-only or unknown block-device access")
        if kind == "disk":
            _linux_local_disk(node)
            devices.append(name)
            if node.get("tran"):
                transports.append(node["tran"])
        elif kind == "loop":
            backing = (Path("/sys/class/block") / name / "loop/backing_file").read_text().strip()
            host = _backing_target(backing, seen)
            devices.extend(host["physical_devices"])
            virtual = True
        elif kind in ("part", "lvm", "crypt", "md", "mpath", "dm") or kind.startswith("raid"):
            if not node.get("children"):
                raise BenchError("unsafe_target", "Incomplete physical-device dependency tree")
        else:
            raise BenchError("unsafe_target", "Unsupported local block-device mapping")
        for child in node.get("children", []):
            visit(child)

    for node in nodes:
        visit(node)
    if not devices:
        raise BenchError("unsafe_target", "Physical device identity unavailable")
    info.update(
        mount=item["target"], device=source, physical_devices=sorted(set(devices)),
        filesystem=item["fstype"], protocol=" / ".join(sorted(set(transports))) or None,
        scope="virtual" if virtual else ("multi_device" if len(set(devices)) > 1 else "physical"),
    )


def unsafe_path_component(path):
    """Reject symlinks and Windows junctions/mount points, including ancestors."""
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if item.is_symlink() or getattr(metadata, "st_file_attributes", 0) & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    return False


def _windows_target(path, info):
    try:
        root, volume = windows_storage.volume_identity(str(path))
        disk = windows_storage.disk_metadata(root[0])
    except windows_storage.WindowsStorageError as exc:
        raise BenchError("unsafe_target", str(exc)) from exc
    info.update(mount=root, device=volume,
                physical_devices=["windows-disk:" + disk["identity"]],
                filesystem=disk["filesystem"], protocol=disk["bus"],
                model=disk["model"], scope="physical")


def target_info(path, _seen=None):
    raw = Path(path).expanduser().absolute()
    if unsafe_path_component(raw):
        raise BenchError("unsafe_target", "Symlink target components are not accepted")
    path = raw.resolve(strict=True)
    if not path.is_dir() or str(path).startswith("/dev/"):
        raise BenchError("unsafe_target", "Expected an existing mounted directory")
    seen = set() if _seen is None else set(_seen)
    if str(path) in seen or len(seen) >= 32:
        raise BenchError("unsafe_target", "Cyclic or unresolved backing storage")
    seen.add(str(path))
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise BenchError("unsafe_target", "Target directory must be readable and writable")
    stat = path.stat()
    if platform.system() == "Windows" and not stat.st_ino:
        raise BenchError("unsafe_target", "Stable directory identity unavailable")
    mount = path
    while mount.parent != mount and mount.parent.stat().st_dev == stat.st_dev:
        mount = mount.parent
    info = dict(
        path=str(path), mount=str(mount), st_dev=stat.st_dev, inode=stat.st_ino,
        device=None, physical_devices=[], filesystem=None, protocol=None,
        negotiated_speed=None, temperature=None, smart=None,
    )
    try:
        if platform.system() == "Darwin":
            _mac_target(path, info, seen)
        elif platform.system() == "Linux":
            _linux_target(path, info, seen)
        elif platform.system() == "Windows":
            _windows_target(path, info)
        else:
            raise BenchError("unsupported", "Unsupported device identity platform")
    except (OSError, KeyError, ValueError, TypeError, IndexError, subprocess.SubprocessError) as exc:
        raise BenchError("unsafe_target", "Local storage identity could not be established") from exc
    if not info["physical_devices"] or any(x is None for x in info["physical_devices"]):
        raise BenchError("unsafe_target", "Physical device identity unavailable")
    usage = shutil.disk_usage(path)
    info.update(total_bytes=usage.total, free_bytes=usage.free)
    return info


def check_target(original, reserve=0):
    current = target_info(original["path"])
    for key in ("st_dev", "inode", "mount", "device", "physical_devices"):
        if current[key] != original[key]:
            raise BenchError("unsafe_target", f"Target identity changed: {key}")
    if current["free_bytes"] < reserve:
        raise BenchError("space_failed", f"Required free bytes: {reserve}")
    return current


class DeviceLock:
    def __init__(self, target):
        self.paths = []
        self.mutexes = []
        self.devices = target["physical_devices"]
        # Per-run TMPDIR values must not split the same user's physical-disk locks.
        self.root = CACHE / "device-locks"
        # Also exclude older runners in this temporary root. Older versions using
        # a different TMPDIR cannot participate in the new shared namespace.
        self.legacy_root = Path(tempfile.gettempdir()) / "external-disk-benchmark-locks"

    def __enter__(self):
        if platform.system() == "Windows":
            try:
                for device in sorted(self.devices):
                    self.mutexes.append(windows_storage.acquire_mutex(device))
            except windows_storage.WindowsStorageError as exc:
                self.__exit__(None, None, None)
                raise BenchError("locked", str(exc)) from exc
            return self
        try:
            for root in (self.root, self.legacy_root):
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                if root.is_symlink() or root.stat().st_uid != os.getuid():
                    raise BenchError("locked", "Untrusted lock directory")
                for device in sorted(self.devices):
                    key = hashlib.sha256(device.encode()).hexdigest()
                    path = root / key
                    path.mkdir(mode=0o700)
                    self.paths.append(path)
        except FileExistsError as exc:
            self.__exit__(None, None, None)
            raise BenchError(
                "locked", "Another run or stale lock exists; inspect before removing"
            ) from exc
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        for handle in reversed(self.mutexes):
            windows_storage.release_mutex(handle)
        self.mutexes = []
        for path in self.paths:
            path.rmdir()  # Only this process's empty lock directories.
        self.paths = []


def trash_owned(directory, target):
    check_target(target)
    path = Path(directory)
    if (
        path.is_symlink()
        or path.parent != Path(target["path"])
        or not path.name.startswith(".diskbench-")
    ):
        raise BenchError("unsafe_target", "Refusing cleanup outside owned directory")
    if platform.system() == "Darwin":
        script = (
            "function run(argv) { ObjC.import('Foundation'); var error = Ref(); "
            "var ok = $.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError("
            "$.NSURL.fileURLWithPath(argv[0]), null, error); "
            "if (!ok) throw new Error(ObjC.unwrap(error[0].localizedDescription)); }"
        )
        capture(["osascript", "-l", "JavaScript", "-e", script, str(path)], timeout=30)
    elif shutil.which("gio"):
        capture(["gio", "trash", str(path)], timeout=30)
    else:
        raise BenchError(
            "cleanup_failed", "No system Trash integration; files retained"
        )
