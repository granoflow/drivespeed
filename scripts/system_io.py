"""fio identity/install and mounted-device guards. All writes use owned files."""

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

VERSION = "3.43"
SOURCE_SHA = "efa49b3f36eda9adf29294f27a87d7e457747d676cd0f73996d6365357832cfe"
WINDOWS_SHA = "8299e6e6fbc024d3b74aa1ce62fb94ef6960b45629fbfa678b720834b6eb34aa"
CACHE = Path.home() / ".cache" / "external-disk-benchmark"


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


def target_info(path):
    raw = Path(path).expanduser().absolute()
    if any(x.is_symlink() for x in (raw, *raw.parents)):
        raise BenchError("unsafe_target", "Symlink target components are not accepted")
    path = raw.resolve(strict=True)
    if not path.is_dir() or str(path).startswith("/dev/"):
        raise BenchError("unsafe_target", "Expected an existing mounted directory")
    stat = path.stat()
    mount = path
    while mount.parent != mount and mount.parent.stat().st_dev == stat.st_dev:
        mount = mount.parent
    info = dict(
        path=str(path),
        mount=str(mount),
        st_dev=stat.st_dev,
        inode=stat.st_ino,
        device=None,
        physical_devices=[],
        filesystem=None,
        protocol=None,
        negotiated_speed=None,
        temperature=None,
        smart=None,
    )
    if platform.system() == "Darwin":
        try:
            disk = plistlib.loads(capture(["diskutil", "info", "-plist", str(path)]))
            if (
                disk.get("Internal") is not False
                or not disk.get("MountPoint")
                or not disk.get("Writable")
            ):
                raise BenchError(
                    "unsafe_target", "Mounted writable external volume required"
                )
            physical = disk.get("APFSPhysicalStores") or []
            ids = [
                x.get("DeviceIdentifier") or x.get("APFSPhysicalStore")
                for x in physical
                if x.get("DeviceIdentifier") or x.get("APFSPhysicalStore")
            ]
            if not ids:
                ids = [disk.get("ParentWholeDisk") or disk.get("DeviceIdentifier")]
            roots = []
            for identifier in ids:
                parent = plistlib.loads(
                    capture(["diskutil", "info", "-plist", identifier])
                )
                roots.append(parent.get("ParentWholeDisk") or identifier)
            info.update(
                device=disk.get("DeviceIdentifier"),
                physical_devices=sorted(set(roots)),
                filesystem=disk.get("FilesystemType"),
                protocol=disk.get("BusProtocol"),
                model=disk.get("MediaName"),
                smart=disk.get("SMARTStatus"),
                temperature_c=(
                    disk.get("SMARTDeviceSpecificKeysMayVaryNotGuaranteed", {}).get(
                        "TEMPERATURE", 0
                    )
                    - 273.15
                )
                if disk.get("SMARTDeviceSpecificKeysMayVaryNotGuaranteed", {}).get(
                    "TEMPERATURE"
                )
                else None,
            )
        except (subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
            raise BenchError("unsafe_target", str(exc)) from exc
    elif platform.system() == "Linux":
        try:
            item = json.loads(
                capture(
                    ["findmnt", "-J", "-T", str(path), "-o", "SOURCE,FSTYPE,TARGET"]
                )
            )["filesystems"][0]
            source = item["source"].split("[")[0]
            nodes = json.loads(
                capture(["lsblk", "-s", "-J", "-o", "NAME,TYPE,TRAN", source])
            )["blockdevices"]
            devices, transports = [], []

            def visit(node):
                if node["type"] == "disk":
                    devices.append(node["name"])
                    transports.append(node.get("tran"))
                for child in node.get("children", []):
                    visit(child)

            for node in nodes:
                visit(node)
            if not devices or any(t != "usb" for t in transports):
                raise BenchError(
                    "unsafe_target",
                    "Linux requires a positively identified USB disk; other transports unverified",
                )
            info.update(
                device=source,
                physical_devices=sorted(set(devices)),
                filesystem=item["fstype"],
                protocol="USB",
            )
        except (KeyError, ValueError, subprocess.SubprocessError) as exc:
            raise BenchError("unsafe_target", str(exc)) from exc
    else:
        raise BenchError(
            "unsupported",
            "Device identity guards currently support macOS and Linux only",
        )
    if not info["physical_devices"] or any(x is None for x in info["physical_devices"]):
        raise BenchError("unsafe_target", "Physical device identity unavailable")
    usage = shutil.disk_usage(path)
    info.update(total_bytes=usage.total, free_bytes=usage.free)
    if stat.st_dev == Path("/").stat().st_dev:
        raise BenchError("unsafe_target", "System volume rejected")
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
        self.devices = target["physical_devices"]
        self.root = Path(tempfile.gettempdir()) / "external-disk-benchmark-locks"

    def __enter__(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid():
            raise BenchError("locked", "Untrusted lock directory")
        try:
            for device in sorted(self.devices):
                key = hashlib.sha256(device.encode()).hexdigest()
                path = self.root / key
                path.mkdir(mode=0o700)
                self.paths.append(path)
        except FileExistsError as exc:
            self.__exit__(None, None, None)
            raise BenchError(
                "locked", "Another run or stale lock exists; inspect before removing"
            ) from exc
        return self

    def __exit__(self, *args):
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
