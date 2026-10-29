"""Windows community state security, independent of benchmark/device access.

Private state requires a local filesystem with persistent Windows ACLs. New
objects get an owner-only DACL at creation; existing objects are checked, never
silently chmod'ed or repaired. Directory and database handles disallow deletion
while SQLite uses the path. No Win32 DLL is loaded when imported on POSIX.
"""

from contextlib import contextmanager
import ctypes as c
import hashlib
import ntpath
import re
import threading

from community_data import CommunityError

DWORD, WORD, HANDLE, PTR = c.c_uint32, c.c_uint16, c.c_void_p, c.c_void_p
READ_CONTROL, READ_ATTRIBUTES = 0x20000, 0x80
DIRECTORY, REPARSE = 0x10, 0x400
FILE_ALL, MUTEX_ALL = 0x1F01FF, 0x1F0001


class SecurityAttributes(c.Structure):
    _fields_ = [("length", DWORD), ("descriptor", PTR), ("inherit", c.c_int)]


class FileInfo(c.Structure):
    _fields_ = [("attributes", DWORD), ("creation", DWORD * 2),
                ("access", DWORD * 2), ("write", DWORD * 2),
                ("volume", DWORD), ("size_high", DWORD), ("size_low", DWORD),
                ("links", DWORD), ("index_high", DWORD), ("index_low", DWORD)]


class ACL(c.Structure):
    _fields_ = [("revision", c.c_ubyte), ("reserved", c.c_ubyte),
                ("size", WORD), ("count", WORD), ("reserved2", WORD)]


def _windows_path(value):
    path = str(value)
    drive, tail = ntpath.splitdrive(path)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith(("\\", "/")):
        raise CommunityError("unsafe_state_directory")
    parts = re.split(r"[\\/]", tail[1:])
    if any(not p or p in (".", "..") or p[-1:] in (".", " ") or
           re.search(r'[<>:"|?*\x00-\x1f]', p) or
           re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
           for p in parts):
        raise CommunityError("unsafe_state_directory")
    root = drive.upper() + "\\"
    paths = [root]
    for part in parts:
        paths.append(ntpath.join(paths[-1], part))
    return paths


class WindowsAPI:
    """Small Win32 boundary; fixtures replace this boundary, not ACL policy."""

    def __init__(self):
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.advapi = c.WinDLL("advapi32", use_last_error=True)
        def bind(dll, name, result, *arguments):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(arguments)
            return fn
        k, a = self.kernel, self.advapi
        self.close = bind(k, "CloseHandle", c.c_int, HANDLE)
        self.free = bind(k, "LocalFree", PTR, PTR)
        self.process = bind(k, "GetCurrentProcess", HANDLE)
        self.open_token = bind(a, "OpenProcessToken", c.c_int, HANDLE, DWORD, c.POINTER(HANDLE))
        self.token_info = bind(a, "GetTokenInformation", c.c_int, HANDLE, c.c_int, PTR, DWORD, c.POINTER(DWORD))
        self.sid_text = bind(a, "ConvertSidToStringSidW", c.c_int, PTR, c.POINTER(c.c_wchar_p))
        self.convert_sd = bind(a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", c.c_int,
                               c.c_wchar_p, DWORD, c.POINTER(PTR), c.POINTER(DWORD))
        self.mkdir = bind(k, "CreateDirectoryW", c.c_int, c.c_wchar_p, c.POINTER(SecurityAttributes))
        self.create_file = bind(k, "CreateFileW", HANDLE, c.c_wchar_p, DWORD, DWORD,
                                c.POINTER(SecurityAttributes), DWORD, DWORD, HANDLE)
        self.file_info = bind(k, "GetFileInformationByHandle", c.c_int, HANDLE, c.POINTER(FileInfo))
        self.file_type = bind(k, "GetFileType", DWORD, HANDLE)
        self.volume_info = bind(k, "GetVolumeInformationW", c.c_int, c.c_wchar_p,
                                c.c_wchar_p, DWORD, c.POINTER(DWORD), c.POINTER(DWORD),
                                c.POINTER(DWORD), c.c_wchar_p, DWORD)
        self.get_security = bind(a, "GetSecurityInfo", DWORD, HANDLE, c.c_int, DWORD,
                                 c.POINTER(PTR), PTR, c.POINTER(PTR), PTR, c.POINTER(PTR))
        self.sd_control = bind(a, "GetSecurityDescriptorControl", c.c_int, PTR, c.POINTER(WORD), c.POINTER(DWORD))
        self.get_ace = bind(a, "GetAce", c.c_int, PTR, DWORD, c.POINTER(PTR))
        self.create_mutex = bind(k, "CreateMutexW", HANDLE, c.POINTER(SecurityAttributes), c.c_int, c.c_wchar_p)
        self.wait = bind(k, "WaitForSingleObject", DWORD, HANDLE, DWORD)
        self.release = bind(k, "ReleaseMutex", c.c_int, HANDLE)
        self.read_file = bind(k, "ReadFile", c.c_int, HANDLE, PTR, DWORD, c.POINTER(DWORD), PTR)
        self.sid = self._current_sid()

    def _sid(self, pointer):
        text = c.c_wchar_p()
        if not self.sid_text(pointer, c.byref(text)):
            raise OSError("SID unavailable")
        try:
            return text.value
        finally:
            self.free(c.cast(text, PTR))

    def _current_sid(self):
        token = HANDLE()
        if not self.open_token(self.process(), 8, c.byref(token)):
            raise OSError("Token unavailable")
        try:
            size = DWORD()
            self.token_info(token, 1, None, 0, c.byref(size))
            if not 0 < size.value < 65536:
                raise OSError("Token unavailable")
            buffer = c.create_string_buffer(size.value)
            if not self.token_info(token, 1, buffer, size, c.byref(size)):
                raise OSError("Token unavailable")
            return self._sid(c.cast(buffer, c.POINTER(PTR))[0])
        finally:
            self.close(token)

    @contextmanager
    def attributes(self, directory=False, mutex=False):
        descriptor = PTR()
        flags, rights = ("", "0x001f0001") if mutex else ("OICI" if directory else "", "FA")
        sddl = "O:" + self.sid + "D:P(A;" + flags + ";" + rights + ";;;" + self.sid + ")"
        if not self.convert_sd(sddl, 1, c.byref(descriptor), None):
            raise OSError("Private ACL unavailable")
        try:
            yield SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
        finally:
            self.free(descriptor)

    def local_acl_volume(self, root, private=True):
        flags = DWORD()
        if (not self.volume_info(root, None, 0, None, None, c.byref(flags), None, 0)
                or private and not flags.value & 8):  # FILE_PERSISTENT_ACLS; FAT/exFAT cannot protect credentials.
            raise OSError("Persistent ACLs unavailable")
        drive_type = self.kernel.GetDriveTypeW
        drive_type.argtypes, drive_type.restype = [c.c_wchar_p], DWORD
        if drive_type(root) not in (2, 3):
            raise OSError("Local volume required")

    def directory(self, path, create=False):
        created = False
        if create:
            with self.attributes(directory=True) as sa:
                created = bool(self.mkdir(path, c.byref(sa)))
                if not created and c.get_last_error() != 183:
                    raise OSError("Private directory creation failed")
        return self.open(path, directory=True), created

    def open(self, path, directory=False, create=False, read=False):
        access = READ_ATTRIBUTES | READ_CONTROL | (0x80000000 if read else 0)
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT / BACKUP_SEMANTICS
        # No FILE_SHARE_DELETE: validated objects cannot be exchanged under SQLite.
        if create:
            with self.attributes() as sa:
                handle = self.create_file(path, access, 3, c.byref(sa), 1, flags, None)
                error = c.get_last_error()  # LocalFree below must not replace CreateFile's error.
            if handle != c.c_void_p(-1).value and handle is not None:
                return handle, True
            if error not in (80, 183):
                raise OSError("Private file creation failed")
        handle = self.create_file(path, access, 3, None, 3, flags, None)
        if handle is None or handle == c.c_void_p(-1).value:
            raise OSError("Safe file open failed")
        return (handle, False) if create else handle

    def check_file(self, handle, directory=False):
        info = FileInfo()
        if not self.file_info(handle, c.byref(info)) or self.file_type(handle) != 1:
            raise OSError("Not a disk file")
        if (info.attributes & REPARSE or bool(info.attributes & DIRECTORY) != directory
                or (not directory and info.links != 1)):
            raise OSError("Unsafe file type or link")
        return info

    def check_private(self, handle, directory=False, mutex=False):
        owner, dacl, sd = PTR(), PTR(), PTR()
        code = self.get_security(handle, 6 if mutex else 1, 5,
                                 c.byref(owner), None, c.byref(dacl), None, c.byref(sd))
        if code:
            raise OSError("Security descriptor unavailable")
        try:
            control, revision = WORD(), DWORD()
            if (not owner or self._sid(owner) != self.sid or not dacl or
                    not self.sd_control(sd, c.byref(control), c.byref(revision)) or
                    directory and not control.value & 0x1000):
                raise OSError("Private owner or DACL required")
            acl = c.cast(dacl, c.POINTER(ACL)).contents
            ace = PTR()
            if acl.count != 1 or not self.get_ace(dacl, 0, c.byref(ace)):
                raise OSError("Only the current user may access state")
            header = c.cast(ace, c.POINTER(c.c_ubyte))
            mask = c.cast(ace.value + 4, c.POINTER(DWORD))[0]
            if (header[0] != 0 or header[1] & 8 or
                    directory and header[1] & 3 != 3 or
                    mask != (MUTEX_ALL if mutex else FILE_ALL) or self._sid(ace.value + 8) != self.sid):
                raise OSError("Unexpected state ACL")
        finally:
            self.free(sd)

    def mutex(self, name, wait_ms):
        with self.attributes(mutex=True) as sa:
            handle = self.create_mutex(c.byref(sa), False, name)
        if not handle:
            raise OSError("Mutex unavailable")
        try:
            self.check_private(handle, mutex=True)
            result = self.wait(handle, wait_ms)
            if result == 0x102:
                raise CommunityError("operation_busy")
            if result not in (0, 0x80):
                raise OSError("Mutex wait failed")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def unlock(self, handle):
        try:
            if not self.release(handle):
                raise OSError("Mutex release failed")
        finally:
            self.close(handle)

    def read(self, handle, limit):
        info = self.check_file(handle)
        if info.size_high or info.size_low > limit:
            raise CommunityError("invalid_input_file")
        output = bytearray()
        while len(output) <= limit:
            buffer = c.create_string_buffer(min(65536, limit + 1 - len(output)))
            size = DWORD()
            if not self.read_file(handle, buffer, len(buffer), c.byref(size), None):
                raise OSError("Input unavailable")
            if not size.value:
                break
            output.extend(buffer.raw[:size.value])
        return bytes(output)


_held_mutexes = set()
_mutex_guard = threading.Lock()


class WindowsPrivateState:
    def __init__(self, directory):
        self.api, self.handles = WindowsAPI(), []
        try:
            paths = _windows_path(directory)
            self.api.local_acl_volume(paths[0])
            for index, path in enumerate(paths):
                handle, created = self.api.directory(path, create=index > 0)
                self.handles.append(handle)
                info = self.api.check_file(handle, directory=True)
                if created or index == len(paths) - 1:
                    self.api.check_private(handle, directory=True)
            self.directory = paths[-1]
            self.key = self.api.sid + ":" + ":".join(str(n) for n in (info.volume, info.index_high, info.index_low))
        except BaseException as error:
            self.close()
            if isinstance(error, CommunityError):
                raise
            raise CommunityError("unsafe_state_directory") from None

    def close(self):
        while self.handles:
            self.api.close(self.handles.pop())

    @contextmanager
    def lock(self, identifier, wait_ms=0):
        name = "Global\\DriveSpeed-Community-" + hashlib.sha256((self.key + ":" + identifier).encode()).hexdigest()
        # Win32 mutexes are recursive in one thread; network operations are not.
        with _mutex_guard:
            if name in _held_mutexes:
                raise CommunityError("operation_busy")
            _held_mutexes.add(name)
        handle = None
        try:
            handle = self.api.mutex(name, wait_ms)
            yield
        except OSError:
            raise CommunityError("unsafe_state_lock") from None
        finally:
            try:
                if handle is not None:
                    self.api.unlock(handle)
            finally:
                with _mutex_guard:
                    _held_mutexes.remove(name)

    @contextmanager
    def initialise(self):
        with self.lock("init", wait_ms=5000):
            try:
                handle, created = self.api.open(ntpath.join(self.directory, "community.sqlite3"), create=True)
                self.handles.append(handle)
                self.api.check_file(handle)
                self.api.check_private(handle)
                # Pre-existing SQLite sidecars must not bypass the private DB.
                for suffix in ("-journal", "-wal", "-shm"):
                    path = ntpath.join(self.directory, "community.sqlite3" + suffix)
                    # GetFileAttributesW is only an existence hint; the handle
                    # below supplies all security/type checks without following links.
                    attrs = self.api.kernel.GetFileAttributesW
                    attrs.argtypes, attrs.restype = [c.c_wchar_p], DWORD
                    if attrs(path) == 0xFFFFFFFF:
                        if c.get_last_error() in (2, 3):
                            continue
                        raise OSError("Sidecar unavailable")
                    sidecar = self.api.open(path)
                    try:
                        self.api.check_file(sidecar)
                        self.api.check_private(sidecar)
                    finally:
                        self.api.close(sidecar)
            except OSError:
                raise CommunityError("unsafe_state_database") from None
            yield created


def read_windows_input(path, limit):
    """Read an existing ordinary result without granting permissions or writes."""
    handles = []
    try:
        api = WindowsAPI()
        paths = _windows_path(path)
        api.local_acl_volume(paths[0], private=False)
        for parent in paths[:-1]:
            handle, _ = api.directory(parent)
            handles.append(handle)
            api.check_file(handle, directory=True)
        handle = api.open(paths[-1], read=True)
        handles.append(handle)
        return api.read(handle, limit)
    except CommunityError as error:
        if error.code == "unsafe_state_directory":
            raise CommunityError("unsafe_input_path") from None
        raise
    except OSError:
        raise CommunityError("input_unavailable") from None
    finally:
        for handle in reversed(handles):
            api.close(handle)
