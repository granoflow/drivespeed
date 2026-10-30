"""POSIX disk locks share a user namespace across per-run temporary roots."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import system_io


@unittest.skipIf(os.name == 'nt', 'POSIX directory locks; Windows uses named mutexes')
class DeviceLockTests(unittest.TestCase):
    def test_same_device_cannot_lock_under_different_temporary_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'run-a').mkdir()
            (root / 'run-b').mkdir()
            target = {'physical_devices': ['fixture-physical-disk']}
            with patch.object(system_io, 'CACHE', root / 'cache'):
                with patch.object(system_io.tempfile, 'gettempdir', return_value=str(root / 'run-a')):
                    first = system_io.DeviceLock(target)
                with patch.object(system_io.tempfile, 'gettempdir', return_value=str(root / 'run-b')):
                    second = system_io.DeviceLock(target)
                with first:
                    with self.assertRaises(system_io.BenchError) as raised:
                        with second:
                            self.fail('Same physical device locked twice')
                    self.assertEqual(raised.exception.key, 'locked')
                with second:
                    self.assertTrue(second.paths)
                self.assertEqual(second.paths, [])

    def test_current_temporary_root_still_excludes_older_runners(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = 'fixture-physical-disk'
            old_root = root / 'run' / 'external-disk-benchmark-locks'
            old_lock = old_root / hashlib.sha256(device.encode()).hexdigest()
            old_lock.mkdir(parents=True)
            with patch.object(system_io, 'CACHE', root / 'cache'), \
                    patch.object(system_io.tempfile, 'gettempdir', return_value=str(root / 'run')):
                lock = system_io.DeviceLock({'physical_devices': [device]})
                with self.assertRaises(system_io.BenchError):
                    with lock:
                        self.fail('Existing older lock ignored')
                self.assertTrue(old_lock.is_dir())
                self.assertEqual(lock.paths, [])
                stable = root / 'cache' / 'device-locks'
                self.assertFalse(stable.exists() and any(stable.iterdir()))
                old_lock.rmdir()
                with system_io.DeviceLock({'physical_devices': [device]}):
                    pass

    def test_untrusted_legacy_root_releases_already_acquired_stable_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'run'
            run.mkdir()
            destination = root / 'untrusted'
            destination.mkdir()
            (run / 'external-disk-benchmark-locks').symlink_to(destination, target_is_directory=True)
            with patch.object(system_io, 'CACHE', root / 'cache'), \
                    patch.object(system_io.tempfile, 'gettempdir', return_value=str(run)):
                lock = system_io.DeviceLock({'physical_devices': ['fixture-physical-disk']})
                with self.assertRaises(system_io.BenchError):
                    with lock:
                        self.fail('Symlink lock directory accepted')
                self.assertEqual(lock.paths, [])
                stable = root / 'cache' / 'device-locks'
                self.assertFalse(stable.exists() and any(stable.iterdir()))

    def test_second_device_conflict_releases_only_this_runs_first_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stable = root / 'cache' / 'device-locks'
            conflicting = stable / hashlib.sha256(b'b-device').hexdigest()
            conflicting.mkdir(parents=True)
            first_path = stable / hashlib.sha256(b'a-device').hexdigest()
            with patch.object(system_io, 'CACHE', root / 'cache'), \
                    patch.object(system_io.tempfile, 'gettempdir', return_value=str(root / 'run')):
                lock = system_io.DeviceLock({'physical_devices': ['b-device', 'a-device']})
                with self.assertRaises(system_io.BenchError):
                    with lock:
                        self.fail('Second physical device conflict ignored')
                self.assertFalse(first_path.exists())
                self.assertTrue(conflicting.is_dir())
                self.assertEqual(lock.paths, [])
