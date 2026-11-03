"""Synthetic Trash payloads only; no user files or disk benchmarks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from system_io import BenchError, trashed_workspace_identity, WORKSPACE_MARKER
from benchmark import ReuseSpaceGuard


class TrashReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.trash = self.home / '.Trash'
        self.trash.mkdir()
        self.target_dir = self.home / 'target'
        self.target_dir.mkdir()
        st = self.target_dir.stat()
        self.target = dict(path=str(self.target_dir), inode=st.st_ino, st_dev=st.st_dev)
        self.run = 'a' * 32
        self.work = self.trash / ('.diskbench-' + self.run)
        self.work.mkdir()
        self.payload = self.work / 'payload.bin'
        self.payload.write_bytes(b'a' * 8192)
        (self.work / 'README.txt').write_text('scratch')
        self.marker = dict(tool='drivespeed-workspace-v1', run_id=self.run,
                           target_path=self.target['path'], target_inode=self.target['inode'],
                           target_device=self.target['st_dev'], workspace_inode=self.work.stat().st_ino,
                           retain=False)
        self.write_marker()
        self.platform = patch('system_io.platform.system', return_value='Darwin')
        self.home_patch = patch('system_io.Path.home', return_value=self.home)
        self.platform.start(); self.home_patch.start()
        self.addCleanup(self.platform.stop); self.addCleanup(self.home_patch.stop)

    def write_marker(self):
        (self.work / WORKSPACE_MARKER).write_text(json.dumps(self.marker))

    def inspect(self):
        return trashed_workspace_identity(self.work, self.target, 8192)

    def test_owned_allocated_workspace_is_bound_to_inode(self):
        result = self.inspect()
        self.assertEqual(result['workspace_inode'], self.work.stat().st_ino)
        self.assertEqual(result['payload_inode'], self.payload.stat().st_ino)
        self.assertEqual(result['payload_bytes'], 8192)

    def test_mismatches_and_extra_contents_are_rejected(self):
        self.marker['target_inode'] += 1; self.write_marker()
        with self.assertRaises(BenchError): self.inspect()
        self.marker['target_inode'] -= 1; self.write_marker()
        (self.work / 'personal.txt').write_text('do not touch')
        with self.assertRaises(BenchError): self.inspect()
        self.assertEqual((self.work / 'personal.txt').read_text(), 'do not touch')

    def test_symlink_hardlink_sparse_and_wrong_size_are_rejected(self):
        with self.assertRaises(BenchError):
            trashed_workspace_identity(self.work, self.target, 4096)
        linked = self.home / 'linked'
        os.link(self.payload, linked)
        with self.assertRaises(BenchError): self.inspect()
        linked.unlink()
        self.payload.unlink()
        self.payload.symlink_to(self.home / 'elsewhere')
        with self.assertRaises(BenchError): self.inspect()
        self.payload.unlink()
        with self.payload.open('wb') as stream: stream.truncate(8192)
        with self.assertRaises(BenchError): self.inspect()


class ReuseSpaceGuardTests(unittest.TestCase):
    def test_low_space_or_failed_stat_stops_only_the_owned_process_group(self):
        proc = SimpleNamespace(pid=12345)
        for observation in (SimpleNamespace(f_bavail=2, f_frsize=100), OSError('unavailable')):
            with self.subTest(observation=observation), \
                 patch('benchmark.os.statvfs', side_effect=observation if isinstance(observation, Exception) else None,
                       return_value=observation), patch('benchmark.os.killpg') as kill:
                guard = ReuseSpaceGuard(proc, '/unused', 1000)
                guard.start()
                self.assertTrue(guard.failed.wait(1))
                guard.close()
                kill.assert_called_once_with(12345, __import__('signal').SIGTERM)


if __name__ == '__main__':
    unittest.main()
