"""Bounded planning for separately recorded, selected peak measurements."""
import sys
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from workloads import GIB, MIB, peak_supplement_plan
import benchmark
import test_auto_full as fixtures
import test_peak_analysis as peak_fixtures
from progress import messages
from system_io import BenchError
from write_allowance import plan_hash


class PeakSupplementPlanTests(unittest.TestCase):
    def test_selected_pair_reserves_initialization_and_all_possible_writes(self):
        config = peak_supplement_plan(1000*GIB, 700*GIB, 50*GIB, 8*GIB,
                                      10*GIB, ['seq_read', 'rand_write'], [32, 64])
        self.assertEqual(config['max_write_bytes'], 48*GIB)
        self.assertEqual(len(config['jobs']), 9)
        self.assertEqual(config['jobs'][0]['name'], 'prepare')
        self.assertEqual([job['qd'] for job in config['jobs'][1:]], [32,32,64,64]*2)
        self.assertNotIn('sustained', [job['name'] for job in config['jobs']])
        self.assertEqual(config['method_version'], 'drivespeed-peak-supplement-v1')

    def test_read_only_scan_still_charges_full_initialization(self):
        config = peak_supplement_plan(1000*GIB, 700*GIB, 8*GIB, 8*GIB,
                                      GIB, ['rand_read'], [16, 32])
        self.assertEqual(config['max_write_bytes'], 8*GIB)
        self.assertEqual(config['reserve_bytes'], 100*GIB)

    def test_confirmation_digest_changes_with_depth_or_selected_workload(self):
        target = fixtures.AuthorizationTests().target()
        common = (1000*GIB, 700*GIB, 50*GIB, 8*GIB, 10*GIB)
        guard = benchmark.WriteAllowance()
        allowance = guard.read(target)
        first = peak_supplement_plan(*common, ['rand_write'], [16, 32])
        different_depth = peak_supplement_plan(*common, ['rand_write'], [32, 64])
        different_workload = peak_supplement_plan(*common, ['seq_write'], [16, 32])
        self.assertNotEqual(plan_hash(target,first,allowance), plan_hash(target,different_depth,allowance))
        self.assertNotEqual(plan_hash(target,first,allowance), plan_hash(target,different_workload,allowance))

    def test_rejects_incomplete_or_unsafe_plans(self):
        args = [1000*GIB, 700*GIB, 50*GIB, 8*GIB, 10*GIB, ['rand_write'], [32, 64]]
        for index, bad in ((1, 108*GIB), (2, 47*GIB), (3, MIB), (4, MIB+1),
                           (5, ['rand_write', 'rand_write']), (5, ['unknown']),
                           (6, [16, 64]), (6, [32, 16])):
            candidate = list(args)
            candidate[index] = bad
            with self.subTest(index=index, value=bad), self.assertRaises(ValueError):
                peak_supplement_plan(*candidate)
        with self.assertRaises(ValueError):
            peak_supplement_plan(*args, seconds=4)
        with self.assertRaises(ValueError):
            peak_supplement_plan(*args, seconds=86401)


class PeakSupplementFlowTests(unittest.TestCase):
    def execute_fixture(self, root, fail=False):
        target = fixtures.AuthorizationTests().target()
        target.update(path=str(root), st_dev=root.stat().st_dev, inode=root.stat().st_ino)
        args = benchmark.parser().parse_args(['--disk', str(root), '--output', str(root/'out'),
            '--stage', 'peak-supplement', '--propose-write-bytes', str(8*GIB),
            '--working-file-bytes', str(8*GIB), '--peak-io-bytes', str(GIB),
            '--supplement-workload', 'seq_read', '--supplement-depths', '16,32'])
        def produced(fio, job, *unused):
            if fail:
                raise BenchError('total_timeout', 'fixture deadline', 6)
            case = copy.deepcopy(peak_fixtures.case(job['qd'], 100))
            case.update(job)
            if job['name'] == 'prepare':
                case['metrics']['write']['bytes'] = job['io_bytes']
                case['metrics']['read']['bytes'] = 0
            elif job['qd'] == 16:
                case['metrics']['achieved_depth'] = {'8':75, '16':25, '32':0, '>=64':0}
            else:
                case['metrics']['achieved_depth'] = {'16':75, '32':25, '>=64':0}
            return case
        with contextlib.ExitStack() as stack:
            replacements = {'target_info':dict(return_value=target), 'check_target':dict(return_value=target),
                'collect_hardware':dict(return_value={}), 'load_reference_cache':dict(return_value=({},{})),
                'apply_hardware_specs':{}, 'DeviceLock':dict(return_value=contextlib.nullcontext()),
                'ensure_fio':dict(return_value={'path':'not-launched','engines':'posixaio'}),
                'select_engine':dict(return_value='posixaio'), 'observe_quality':dict(return_value={'status':'quiet_observed'}),
                'confirm_write':dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)), 'trash_owned':{},
                'clean_stale_workspaces':dict(return_value=[]), 'run_case':dict(side_effect=produced)}
            mocked = {key:stack.enter_context(patch.object(benchmark,key,**value)) for key,value in replacements.items()}
            stack.enter_context(patch('device_identity.collect_device_identity', return_value=None))
            stack.enter_context(patch('device_identity.collect_report_identity_macos', return_value=None, create=True))
            stack.enter_context(patch.object(benchmark.os,'ftruncate'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = benchmark.execute(args,messages('en'),messages('en'))
        return code, json.loads((root/'out/results.json').read_text()), mocked

    def test_supplement_is_separate_and_cannot_claim_full_completion(self):
        with tempfile.TemporaryDirectory() as name:
            code, result, mocked = self.execute_fixture(Path(name).resolve())
        self.assertEqual(code, 6)
        self.assertEqual(mocked['run_case'].call_count, 5)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['completeness'], {'complete':False, 'missing_core':['full_run']})
        self.assertEqual(result['peak_supplement']['seq_read']['status'], 'plateau')
        self.assertEqual(result['report'], {})

    def test_timeout_stops_remaining_jobs_and_keeps_partial_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            code, result, mocked = self.execute_fixture(Path(name).resolve(), fail=True)
        self.assertEqual(code, 6)
        self.assertEqual(mocked['run_case'].call_count, 1)
        self.assertEqual(result['stop_reason'], 'total_timeout')
        self.assertTrue(all(case['status']=='skipped' for case in result['cases']))


if __name__ == '__main__':
    unittest.main()
