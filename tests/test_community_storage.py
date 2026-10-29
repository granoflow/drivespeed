"""Windows security policy fixtures; no benchmark, network or real device I/O."""

from contextlib import contextmanager
import ctypes as c
import importlib.util
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import community
import community_storage as storage
import community_store
from community_data import CommunityError, digest
from test_community import payload, ID


class FakeAPI:
    sid = "S-1-5-21-123"

    def __init__(self):
        self.closed, self.unlocked = [], []
        self.directories, self.private = [], []
        self.fail = None
        self.kernel = SimpleNamespace(GetFileAttributesW=Mock(return_value=0xFFFFFFFF))

    def close(self, handle):
        self.closed.append(handle)

    def local_acl_volume(self, root, private=True):
        if self.fail == "volume":
            raise OSError("PRIVATE path")

    def directory(self, path, create=False):
        self.directories.append(path)
        return path, create

    def check_file(self, handle, directory=False):
        if self.fail == "file":
            raise OSError("PRIVATE path")
        return SimpleNamespace(volume=1, index_high=0, index_low=2)

    def check_private(self, handle, directory=False, mutex=False):
        self.private.append(handle)
        if self.fail == "acl":
            raise OSError("PRIVATE SID")

    def open(self, path, directory=False, create=False, read=False):
        return (path, True) if create else path

    def mutex(self, name, wait_ms):
        return name

    def unlock(self, handle):
        self.unlocked.append(handle)

    def read(self, handle, limit):
        return b'{"run_id":"fixture"}'


def acl_api(*, count=1, flags=3, mask=storage.FILE_ALL, owner=True, protected=True, ace_type=0, ace_owner=True):
    """Real ctypes ACL layout, fake OS calls: exercise the production validator."""
    api = object.__new__(storage.WindowsAPI)
    api.sid = FakeAPI.sid
    acl = storage.ACL(2, 0, 32, count, 0)
    ace = c.create_string_buffer(32)
    header = c.cast(ace, c.POINTER(c.c_ubyte))
    header[0], header[1] = ace_type, flags
    c.cast(c.addressof(ace) + 4, c.POINTER(storage.DWORD))[0] = mask
    def get_security(handle, kind, info, sid, group, dacl, sacl, sd):
        c.cast(sid, c.POINTER(storage.PTR))[0] = 1
        c.cast(dacl, c.POINTER(storage.PTR))[0] = c.addressof(acl)
        c.cast(sd, c.POINTER(storage.PTR))[0] = 2
        return 0
    def control(sd, output, revision):
        c.cast(output, c.POINTER(storage.WORD))[0] = 0x1000 if protected else 0
        return 1
    def get_ace(dacl, index, output):
        c.cast(output, c.POINTER(storage.PTR))[0] = c.addressof(ace)
        return 1
    api.get_security, api.sd_control, api.get_ace = get_security, control, get_ace
    def sid(ptr):
        is_owner = ptr == 1 or getattr(ptr, "value", None) == 1
        return api.sid if (owner if is_owner else ace_owner) else "OTHER-SID"
    api._sid = sid
    api.free = Mock()
    # Keep native buffers alive until the API fixture is discarded.
    api.buffers = (acl, ace)
    return api


class WindowsSecurityPolicyTests(unittest.TestCase):
    def test_only_owner_full_acl_with_inheritable_directory_rights_is_accepted(self):
        acl_api().check_private(12, directory=True)
        for change in ({"count": 2}, {"count": 0}, {"mask": 0x1F01FF | 0x80000000},
                       {"owner": False}, {"protected": False}, {"flags": 8},
                       {"flags": 0}, {"ace_type": 1}, {"ace_owner": False}):
            with self.subTest(change=change), self.assertRaises(OSError):
                acl_api(**change).check_private(12, directory=True)

    def test_inherited_private_file_acl_is_accepted_but_null_acl_is_not(self):
        api = acl_api(flags=16, protected=False)
        api.check_private(12)
        original = api.get_security
        def missing(*args):
            original(*args)
            c.cast(args[5], c.POINTER(storage.PTR))[0] = None
            return 0
        api.get_security = missing
        with self.assertRaises(OSError):
            api.check_private(12)
        self.assertEqual(api.free.call_count, 2)

    def test_reparse_hardlink_directory_and_non_disk_are_rejected_by_handle(self):
        api = object.__new__(storage.WindowsAPI)
        api.file_type = lambda handle: 1
        for attributes, links in ((storage.REPARSE, 1), (0, 2), (storage.DIRECTORY, 1)):
            def info(handle, output):
                value = c.cast(output, c.POINTER(storage.FileInfo)).contents
                value.attributes, value.links = attributes, links
                return 1
            api.file_info = info
            with self.subTest(attributes=attributes, links=links), self.assertRaises(OSError):
                api.check_file(12)
        api.file_type = lambda handle: 3
        with self.assertRaises(OSError):
            api.check_file(12, directory=True)

    def test_windows_paths_reject_unc_ads_dot_reserved_and_relative_names(self):
        self.assertEqual(storage._windows_path(r"C:\Users\fixture")[-1], r"C:\Users\fixture")
        for path in (r"\\host\share\state", r"\\?\C:\state", r"C:state", r"C:\x\..\state",
                     r"C:\state:stream", r"C:\NUL.txt", "C:\\state.", "C:\\state "):
            with self.subTest(path=path), self.assertRaises(CommunityError):
                storage._windows_path(path)

    def test_state_failure_closes_pinned_directories_and_hides_paths(self):
        for reason in ("volume", "file", "acl"):
            api = FakeAPI()
            api.fail = reason
            with patch.object(storage, "WindowsAPI", return_value=api), self.assertRaises(CommunityError) as caught:
                storage.WindowsPrivateState(r"C:\fixture\state")
            self.assertEqual(caught.exception.code, "unsafe_state_directory")
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(api.closed, list(reversed(api.directories)))

    def test_state_keeps_handles_and_network_mutex_is_not_recursive(self):
        api = FakeAPI()
        with patch.object(storage, "WindowsAPI", return_value=api):
            state = storage.WindowsPrivateState(r"C:\fixture\state")
        with state.lock(ID):
            with self.assertRaisesRegex(CommunityError, "operation_busy"):
                with state.lock(ID):
                    self.fail("recursive network operation")
        self.assertEqual(len(api.unlocked), 1)
        with self.assertRaisesRegex(ValueError, "fixture"):
            with state.lock(ID):
                raise ValueError("fixture")
        with state.lock(ID):
            pass
        self.assertEqual(len(api.unlocked), 3)
        self.assertEqual(api.closed, [])
        state.close()
        self.assertEqual(api.closed, list(reversed(api.directories)))

    def test_database_security_failure_does_not_enter_sqlite_and_releases_init_lock(self):
        api = FakeAPI()
        with patch.object(storage, "WindowsAPI", return_value=api):
            state = storage.WindowsPrivateState(r"C:\fixture\state")
        api.fail = "acl"
        with self.assertRaisesRegex(CommunityError, "unsafe_state_database"):
            with state.initialise():
                self.fail("untrusted database accepted")
        self.assertEqual(len(api.unlocked), 1)
        state.close()
        self.assertIn(r"C:\fixture\state\community.sqlite3", api.closed)

    def test_input_uses_handles_and_bounded_read_without_private_acl_requirement(self):
        api = FakeAPI()
        with patch.object(storage, "WindowsAPI", return_value=api):
            self.assertEqual(storage.read_windows_input(r"C:\fixture\results.json", 100), b'{"run_id":"fixture"}')
        self.assertEqual(api.private, [])
        self.assertEqual(api.closed[0], r"C:\fixture\results.json")
        self.assertEqual(len(api.closed), 3)

    def test_input_limit_is_checked_from_handle_and_reads_at_most_limit_plus_one(self):
        api = object.__new__(storage.WindowsAPI)
        api.check_file = lambda h: SimpleNamespace(size_high=0, size_low=101)
        with self.assertRaisesRegex(CommunityError, "invalid_input_file"):
            api.read(12, 100)
        api.check_file = lambda h: SimpleNamespace(size_high=0, size_low=0)
        calls = []
        def grow(handle, buffer, maximum, actual, overlapped):
            calls.append(maximum)
            c.memset(buffer, ord("x"), maximum)
            c.cast(actual, c.POINTER(storage.DWORD))[0] = maximum
            return 1
        api.read_file = grow
        self.assertEqual(api.read(12, 100), b"x" * 101)
        self.assertEqual(calls, [101])

    def test_win32_struct_layout_and_atomic_creation_descriptor(self):
        self.assertEqual(c.sizeof(storage.FileInfo), 52)
        self.assertEqual(c.sizeof(storage.ACL), 8)
        api = object.__new__(storage.WindowsAPI)
        api.sid = FakeAPI.sid
        descriptors = []
        def descriptor(text, revision, output, size):
            descriptors.append(text)
            c.cast(output, c.POINTER(storage.PTR))[0] = 42
            return 1
        api.convert_sd, api.free = descriptor, Mock()
        for kwargs in ({"directory": True}, {}, {"mutex": True}):
            with api.attributes(**kwargs) as sa:
                self.assertEqual(sa.descriptor, 42)
                self.assertFalse(sa.inherit)
        self.assertEqual(descriptors, [
            "O:" + api.sid + "D:P(A;OICI;FA;;;" + api.sid + ")",
            "O:" + api.sid + "D:P(A;;FA;;;" + api.sid + ")",
            "O:" + api.sid + "D:P(A;;0x001f0001;;;" + api.sid + ")"])
        self.assertEqual(api.free.call_count, 3)

    def test_native_mutex_timeout_and_abandoned_owner_have_safe_cleanup(self):
        api = object.__new__(storage.WindowsAPI)
        @contextmanager
        def attributes(**kwargs):
            yield storage.SecurityAttributes()
        api.attributes = attributes
        api.create_mutex, api.check_private, api.close = Mock(return_value=42), Mock(), Mock()
        api.wait = Mock(return_value=0x102)
        with self.assertRaisesRegex(CommunityError, "operation_busy"):
            api.mutex("fixture", 0)
        api.close.assert_called_once_with(42)
        api.wait.return_value = 0x80
        self.assertEqual(api.mutex("fixture", 0), 42)
        api.release = Mock(return_value=1)
        api.unlock(42)
        api.release.assert_called_once_with(42)
        self.assertEqual(api.close.call_count, 2)


class WindowsStoreIntegrationTests(unittest.TestCase):
    def test_module_import_does_not_require_fcntl_on_windows(self):
        import builtins
        real_import = builtins.__import__
        def safe_import(name, *args, **kwargs):
            if name == "fcntl":
                raise AssertionError("Windows imported fcntl")
            return real_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("windows_store_fixture", community_store.__file__)
        with patch.object(sys, "platform", "win32"), patch.object(builtins, "__import__", side_effect=safe_import):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_windows_branch_preserves_typed_snapshot_hash_and_lock_delegation(self):
        root = Path(tempfile.mkdtemp(prefix="community-windows-seam-")).resolve()
        class State:
            def __init__(self, path):
                self.directory = path
                self.closed = False
            @contextmanager
            def initialise(self):
                yield not (self.directory / "community.sqlite3").exists()
            @contextmanager
            def lock(self, identifier):
                self.identifier = identifier
                yield
            def close(self):
                self.closed = True
        with patch.object(sys, "platform", "win32"), patch.object(storage, "WindowsPrivateState", State), \
                patch.object(community_store.os, "getuid", side_effect=AssertionError("POSIX owner check"), create=True):
            with community_store.CommunityStore(root) as store:
                store.create(payload(), "fixture", "/fixture/results.json")
                expected = digest(store.payload(ID))
                with store.network_lock(ID):
                    self.assertEqual(store._windows_state.identifier, ID)
            self.assertTrue(store._windows_state.closed)
            with community_store.CommunityStore(root) as reopened:
                self.assertEqual(digest(reopened.payload(ID)), expected)
                self.assertFalse(any(row[1] == "payload_json" for row in reopened.db.execute("PRAGMA table_info(reports)")))

    def test_cli_windows_input_branch_still_parses_limit_and_run_id(self):
        with patch.object(sys, "platform", "win32"), patch.object(storage, "read_windows_input", return_value=b'{"run_id":"fixture"}') as read:
            _, value = community.read_result("fixture.json")
            self.assertEqual(value, {"run_id": "fixture"})
            self.assertEqual(read.call_args.args[1], 8 * 1024 * 1024)
            read.return_value = b'{"wrong":true}'
            with self.assertRaisesRegex(CommunityError, "missing_parameters"):
                community.read_result("fixture.json")
            read.return_value = b"x" * 6
            with self.assertRaises(CommunityError):
                community._read_json_file("fixture.json", limit=5)


@unittest.skipIf(sys.platform == "win32", "POSIX permissions and link protection")
class PosixSafetyRegressionTests(unittest.TestCase):
    def test_linked_state_database_or_lock_is_still_rejected(self):
        root = Path(tempfile.mkdtemp(prefix="community-posix-links-")).resolve()
        root.chmod(0o700)
        state = root / "state"
        with community_store.CommunityStore(state):
            pass
        link = root / "alias"
        link.symlink_to(state, target_is_directory=True)
        with self.assertRaisesRegex(CommunityError, "unsafe_state_directory"):
            community_store.CommunityStore(link)
        os.link(state / "community.sqlite3", root / "database-hardlink")
        with self.assertRaisesRegex(CommunityError, "unsafe_state_database"):
            community_store.CommunityStore(state)
        other = root / "other"
        other.mkdir(mode=0o700)
        os.link(state / ".init.lock", other / ".init.lock")
        with self.assertRaisesRegex(CommunityError, "unsafe_state_lock"):
            community_store.CommunityStore(other)


@unittest.skipUnless(sys.platform == "win32", "Windows machine required for native ACL/SQLite acceptance")
class NativeWindowsStateTests(unittest.TestCase):
    def test_native_private_creation_reopen_mutex_and_typed_digest(self):
        root = Path(tempfile.mkdtemp(prefix="community-native-windows-")) / "private"
        with community_store.CommunityStore(root) as store:
            store.create(payload(), "fixture", "fixture-results.json")
            expected = digest(store.payload(ID))
            with store.network_lock(ID):
                with community_store.CommunityStore(root) as second:
                    with self.assertRaisesRegex(CommunityError, "operation_busy"):
                        with second.network_lock(ID):
                            self.fail("concurrent lock")
                script = """import sys
from community_store import CommunityStore
from community_data import CommunityError
with CommunityStore(sys.argv[1]) as store:
    try:
        with store.network_lock(sys.argv[2]): pass
    except CommunityError as error:
        sys.exit(77 if error.code == 'operation_busy' else 78)
sys.exit(79)
"""
                environment = dict(os.environ, PYTHONPATH=str(Path(community_store.__file__).parent))
                child = subprocess.run([sys.executable, "-c", script, str(root), ID],
                                       env=environment, capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 77)
        with community_store.CommunityStore(root) as store:
            self.assertEqual(digest(store.payload(ID)), expected)

    def test_native_nonprivate_existing_directory_is_not_repaired(self):
        api = storage.WindowsAPI()
        root = Path(tempfile.mkdtemp(prefix="community-native-untrusted-"))
        # Typical inherited temporary-directory ACL is not protected/owner-only.
        handle, _ = api.directory(str(root))
        try:
            try:
                api.check_private(handle, directory=True)
            except OSError:
                with self.assertRaisesRegex(CommunityError, "unsafe_state_directory"):
                    community_store.CommunityStore(root)
            else:
                self.skipTest("Temporary-directory ACL already satisfies private policy")
        finally:
            api.close(handle)


if __name__ == "__main__":
    unittest.main()
