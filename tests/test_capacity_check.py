"""Capacity planning and qualification only; never invokes fio or a device probe."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).parent)]
import capacity_check
from capacity_check import METHOD, capacity_plan, completeness, load_source
from test_mandatory_cache import stable_result
from workloads import GIB, MIB


class CapacityCheckTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        info = self.directory.stat()
        self.target = dict(path=str(self.directory), st_dev=info.st_dev, inode=info.st_ino,
                           physical_devices=['disk8'], total_bytes=200 * GIB, free_bytes=100 * GIB)
        self.allowance = dict(present=True, scope_id='fixture', revision=3, remaining=64 * GIB, run_cap=64 * GIB)
        self.source = stable_result()
        self.source.update(run_id='normal-fixture', outcome='complete', target=copy.deepcopy(self.target))
        self.path = self.directory / 'normal.json'
        self.path.write_text(json.dumps(self.source))

    def loaded(self):
        return load_source(self.path, self.target)

    def result(self):
        plan = capacity_plan(self.target, self.allowance, self.loaded())
        cases = copy.deepcopy(plan['jobs'])
        for case in cases:
            data = {direction: dict(bytes=plan['file_bytes'] if case['rw'] == direction else 0)
                    for direction in ('read', 'write')}
            data['runtime_ms'] = 1000
            case.update(status='completed', metrics_complete=True, metrics=data)
        return dict(tool={'method_version': METHOD}, plan=plan, outcome='complete', cases=cases)

    def test_source_requalifies_measurements_and_binds_digest_and_directory(self):
        loaded = self.loaded()
        self.assertEqual(loaded['_source_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotIn('_source_sha256', self.source)
        for field, value in [('st_dev', -1), ('inode', -1), ('physical_devices', ['disk9'])]:
            target = {**self.target, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_source(self.path, target)
        other = self.directory / 'other'
        other.mkdir()
        info = other.stat()
        with self.assertRaises(ValueError):
            load_source(self.path, {**self.target, 'path': str(other), 'inode': info.st_ino})

    def test_synthetic_incomplete_nonfull_and_fake_complete_sources_are_refused(self):
        for failure in ('synthetic', 'outcome', 'profile', 'measurement', 'missing_run'):
            source = copy.deepcopy(self.source)
            if failure == 'synthetic':
                source['synthetic'] = True
            elif failure == 'outcome':
                source['outcome'] = 'partial'
            elif failure == 'profile':
                source['plan']['profile'] = 'integrity'
            elif failure == 'measurement':
                source['cases'][0]['status'] = 'partial'
                source['completeness'] = {'complete': True}
            else:
                source.pop('run_id')
            self.path.write_text(json.dumps(source))
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.loaded()

    def test_source_is_bounded_regular_json_without_symlinks_or_nonfinite_values(self):
        link = self.directory / 'linked.json'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            load_source(link, self.target)
        with patch.object(capacity_check, 'MAX_SOURCE_BYTES', 8), self.assertRaises(ValueError):
            self.loaded()
        for invalid in ('{"x":1,"x":2}', '{"x":NaN}', '[]'):
            self.path.write_text(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.loaded()

    def test_plan_uses_all_safe_free_space_without_shrinking_to_remaining_allowance(self):
        source = self.loaded()
        before = copy.deepcopy((self.target, self.allowance, source))
        plan = capacity_plan(self.target, self.allowance, source)
        size = 90 * GIB - 64 * MIB
        self.assertEqual(plan['file_bytes'], size)
        self.assertEqual(plan['max_write_bytes'], size)
        self.assertEqual(plan['additional_authorized_bytes'], size - 64 * GIB)
        self.assertEqual(plan['capacity_run_cap_bytes'], size)
        self.assertEqual(plan['capacity_allowance'], self.allowance)
        self.assertEqual(plan['authorization_status'], 'proposal_requires_explicit_plan_confirmation')
        self.assertEqual((plan['source_run_id'], plan['source_sha256']), ('normal-fixture', source['_source_sha256']))
        self.assertEqual([job['io_bytes'] for job in plan['jobs']], [size, size])
        self.assertEqual([job['status_interval'] for job in plan['jobs']], [10, 10])
        self.assertEqual(plan['metadata_headroom_bytes'], 64 * MIB)
        self.assertEqual((self.target, self.allowance, source), before)

    def test_existing_allowance_and_signed_integer_limits_are_mandatory(self):
        for allowance in ({}, {**self.allowance, 'present': False}, {**self.allowance, 'remaining': -1},
                          {**self.allowance, 'run_cap': 2**63}, {**self.allowance, 'remaining': True}):
            with self.subTest(allowance=allowance), self.assertRaises(ValueError):
                capacity_plan(self.target, allowance, self.loaded())
        allowance = {**self.allowance, 'run_cap': 200 * GIB, 'remaining': 200 * GIB}
        self.assertEqual(capacity_plan(self.target, allowance, self.loaded())['additional_authorized_bytes'], 0)

    def test_estimate_uses_real_stable_write_and_preferred_read_with_bounded_runtime(self):
        source = self.loaded()
        plan = capacity_plan(self.target, self.allowance, source)
        rate = next(case for case in source['cases'] if case['name'] == 'sustained_read')['metrics']['read']['mb_s']
        baseline = plan['file_bytes'] / 1_000_000 * (1 / 200 + 1 / rate)
        self.assertEqual(plan['estimate']['write_mb_s'], 200)
        self.assertEqual(plan['estimate']['read_case'], 'sustained_read')
        self.assertAlmostEqual(plan['estimate']['lower_seconds'], baseline * 1.2)
        self.assertAlmostEqual(plan['estimate']['upper_seconds'], baseline * 1.8)
        self.assertTrue(all(0 < job['runtime'] <= 7 * 86400 for job in plan['jobs']))
        self.assertIn('unknown', plan['estimate']['verification_overhead'])
        with patch.object(capacity_check, '_source_rates', return_value=(0.001, 0.001, 'seq_read')):
            with self.assertRaisesRegex(ValueError, 'seven-day'):
                capacity_plan(self.target, self.allowance, source)

    def test_flat_curve_cannot_borrow_cached_post_cache_speed_and_space_is_not_reused(self):
        source = self.loaded()
        for point in source['cases'][0]['series']:
            point['mb_s'] = 200
        source['cache_assessment'] = dict(status='observed_drop_stable', post_cache_mb_s=999)
        with self.assertRaises(ValueError):
            capacity_plan(self.target, self.allowance, source)
        small = {**self.target, 'free_bytes': 10 * GIB + 64 * MIB}
        with self.assertRaisesRegex(ValueError, 'range fits'):
            capacity_plan(small, self.allowance, self.loaded())

    def test_only_two_complete_verified_exact_file_passes_qualify(self):
        result = self.result()
        self.assertEqual(completeness(result), dict(complete=True, missing_core=[]))
        for failure in ('partial', 'verify', 'short', 'extra', 'read_write', 'runtime', 'active_runtime', 'duplicate', 'metrics'):
            current = copy.deepcopy(result)
            if failure == 'partial':
                current['outcome'] = 'partial'
            elif failure == 'verify':
                current['cases'][0]['verify'] = False
            elif failure in ('short', 'extra'):
                current['cases'][1]['metrics']['read']['bytes'] += -1 if failure == 'short' else 1
            elif failure == 'read_write':
                current['cases'][1]['metrics']['write']['bytes'] = 1
            elif failure == 'runtime':
                current['cases'][0]['metrics']['runtime_ms'] = 0
            elif failure == 'active_runtime':
                current['cases'][1]['metrics']['read']['runtime_ms'] = 0
            elif failure == 'duplicate':
                current['cases'][1] = copy.deepcopy(current['cases'][0])
            else:
                current['cases'][0]['metrics'] = None
            with self.subTest(failure=failure):
                self.assertFalse(completeness(current)['complete'])


if __name__ == '__main__':
    unittest.main()
