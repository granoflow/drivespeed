"""Platform admission and process safety fixtures; never access a real disk."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import benchmark
import system_io
import windows_storage
from progress import messages
from workloads import MIB


class WindowsSupportTests(unittest.TestCase):
    def setUp(self):
        self.disk = dict(number=2, unique='fixture-private-id', bus='USB', style='GPT',
                         readonly=False, offline=False, clustered=False, partition_readonly=False,
                         partition_type='Basic', filesystem='NTFS', model='Example SSD')

    def test_positive_disk_metadata_is_minimized_and_stable(self):
        first = windows_storage.validate_disk_metadata(self.disk)
        self.assertEqual(first['model'], 'Example SSD')
        self.assertEqual(len(first['identity']), 64)
        self.assertNotIn('fixture-private-id', json.dumps(first))
        self.assertEqual(first, windows_storage.validate_disk_metadata(dict(self.disk)))

    def test_uncertain_remote_virtual_or_unwritable_devices_fail_closed(self):
        for key, values in {'bus':['iSCSI', 'File Backed Virtual', 'RAID', None],
                            'style':['RAW', None], 'readonly':[True, None],
                            'offline':[True, None], 'clustered':[True, None],
                            'partition_readonly':[True, None], 'unique':['', None],
                            'partition_type':['LDM', None],
                            'number':[-1, None, True], 'filesystem':['ReFS', '']}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(windows_storage.WindowsStorageError):
                    windows_storage.validate_disk_metadata({**self.disk, key:value})

    def test_powershell_returns_utf8_and_only_accepts_validated_letter(self):
        reply = Mock(stdout=json.dumps(self.disk, ensure_ascii=False).encode('utf-8'))
        with patch.object(windows_storage.subprocess, 'run', return_value=reply) as run:
            windows_storage.disk_metadata('C')
        self.assertIn('-EncodedCommand', run.call_args.args[0])
        with self.assertRaises(windows_storage.WindowsStorageError):
            windows_storage.disk_metadata("C'; invalid")

    def test_windows_target_binds_volume_and_physical_disk(self):
        info = {}
        disk = windows_storage.validate_disk_metadata(self.disk)
        with patch.object(windows_storage, 'volume_identity', return_value=('C:\\', 'volume-guid')), \
             patch.object(windows_storage, 'disk_metadata', return_value=disk) as query:
            system_io._windows_target('C:\\Users\\test', info)
        query.assert_called_once_with('C')
        self.assertEqual(info['device'], 'volume-guid')
        self.assertEqual(info['physical_devices'], ['windows-disk:' + disk['identity']])
        self.assertEqual(info['scope'], 'physical')
        self.assertNotIn('fixture-private-id', json.dumps(info))

    def test_failed_mapping_is_never_accepted(self):
        with patch.object(windows_storage, 'volume_identity', side_effect=windows_storage.WindowsStorageError('remote')):
            with self.assertRaises(system_io.BenchError):
                system_io._windows_target('Z:\\test', {})

    def test_volume_identity_rejects_network_and_nested_mounts(self):
        class Function:
            def __init__(self, fn): self.fn=fn
            def __call__(self, *args): return self.fn(*args)
        def kernel(mount, kind):
            def resolve(path, out, size): out.value=mount; return 1
            def guid(path, out, size):
                out.value='\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\'; return 1
            return type('Kernel',(),dict(GetVolumePathNameW=Function(resolve),
                        GetDriveTypeW=Function(lambda p:kind),
                        GetVolumeNameForVolumeMountPointW=Function(guid)))()
        with patch.object(windows_storage.os.path, 'splitdrive', return_value=('C:', '\\dir')):
            with patch.object(windows_storage, '_kernel', return_value=kernel('C:\\', 3)):
                root, volume=windows_storage.volume_identity('C:\\dir')
                self.assertEqual(root,'C:\\')
                self.assertIn('volume{12345678',volume)
            for mount, kind in [('C:\\', 4), ('C:\\mount\\', 3)]:
                with patch.object(windows_storage, '_kernel', return_value=kernel(mount, kind)), \
                     self.assertRaises(windows_storage.WindowsStorageError):
                    windows_storage.volume_identity('C:\\dir')

    def test_windows_mutex_covers_disk_and_releases_after_failure(self):
        with patch.object(system_io.platform, 'system', return_value='Windows'), \
             patch.object(windows_storage, 'acquire_mutex', side_effect=[123, windows_storage.WindowsStorageError('locked')]) as acquire, \
             patch.object(windows_storage, 'release_mutex') as release:
            with self.assertRaises(system_io.BenchError):
                with system_io.DeviceLock({'physical_devices':['b', 'a']}):
                    self.fail('a locked disk was accepted')
        self.assertEqual([c.args[0] for c in acquire.call_args_list], ['a', 'b'])
        release.assert_called_once_with(123)

    def test_windows_engine_is_required_not_silently_replaced(self):
        with patch.object(benchmark.platform, 'system', return_value='Windows'):
            self.assertEqual(benchmark.select_engine({'engines':'sync windowsaio libaio'}), 'windowsaio')
            with self.assertRaises(system_io.BenchError):
                benchmark.select_engine({'engines':'sync libaio'})

    def test_reparse_attribute_is_rejected_even_when_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'junction'
            target.mkdir()
            original_lstat = Path.lstat
            class Reparse:
                st_file_attributes = 0x400
            def lstat(p):
                return Reparse() if p == target else original_lstat(p)
            with patch.object(Path, 'lstat', lstat), patch.object(Path, 'is_symlink', return_value=False):
                self.assertTrue(system_io.unsafe_path_component(target))

    def test_windows_fio_completion_uses_no_posix_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); work = root/'work'; output = root/'output'
            work.mkdir(); output.mkdir(); (work/'payload.bin').write_bytes(b'fixture')
            job=dict(name='prepare', rw='write', bs=MIB, qd=1, io_bytes=MIB, runtime=10)
            snapshot={'jobs':[{'error':0,'eta':0,'job_runtime':1000,
                                'write':{'io_bytes':MIB,'bw_bytes':MIB,'runtime':1000},'read':{'io_bytes':0}}]}
            (output/'prepare.fio.json-stream').write_text(json.dumps(snapshot))
            proc=Mock(returncode=0); proc.poll.return_value=0
            with patch.object(benchmark.platform,'system',return_value='Windows'), \
                 patch.object(benchmark,'check_target',return_value={}), \
                 patch.object(benchmark.subprocess,'Popen',return_value=proc) as spawn, \
                 patch.object(benchmark,'stop_process'):
                case=benchmark.run_case({'path':'fio'}, job, {'file_bytes':MIB,'reserve_bytes':0},
                                        'windowsaio', output, work, {'path':str(work)}, Mock())
            self.assertEqual(case['status'],'completed',case.get('detail'))
            self.assertFalse(spawn.call_args.kwargs['start_new_session'])


if __name__ == '__main__':
    unittest.main()
