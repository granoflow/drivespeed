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
import test_benchmark as execution_fixtures
from progress import messages, Progress
from system_io import BenchError
from write_allowance import plan_hash
from peak_comparison import compare_peak_runs


def qualified_baseline():
    case = peak_fixtures.case(16,200)
    case['name'] = 'seq_read_q'
    case['metrics']['achieved_depth'] = {'8':75,'16':25,'32':0,'>=64':0}
    case['metrics']['read'].update(iops=100, latency_us={'mean':1,'50':1,'95':1,'99':1})
    job = {'name':case['name'], 'rw':'read', 'bs':MIB, 'qd':16,
           'io_bytes':case['metrics']['read']['bytes'], 'runtime':5}
    return case, job


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
    def execute_fixture(self, root, fail=False, with_source=False, wrong_identity=False):
        target = fixtures.AuthorizationTests().target()
        target.update(path=str(root), st_dev=root.stat().st_dev, inode=root.stat().st_ino)
        cli = ['--disk', str(root), '--output', str(root/'out'),
            '--stage', 'peak-supplement', '--propose-write-bytes', str(8*GIB),
            '--working-file-bytes', str(8*GIB), '--peak-io-bytes', str(GIB),
            '--supplement-workload', 'seq_read', '--supplement-depths', '16,32']
        identity = {'version':1, 'fingerprint':'a'*64, 'source':'smartctl:serial_number', 'attachment':'internal'}
        if with_source:
            criteria = {'minimum_seconds':5,'tolerance':.05,'minimum_depth_percent':20,'minimum_half_depth_percent':90}
            baseline,job=qualified_baseline()
            source={'target':target,'device_identity':identity,'environment':{'engine':'posixaio','fio':{'version':'fio-fixture'}},'run_id':'old',
                    'tool':{'method_version':'drivespeed-peak-v1'},'cases':[baseline],
                    'plan':{'method_version':'drivespeed-peak-v1','file_bytes':8*GIB,'peak_criteria':criteria,'jobs':[job]}}
            path=root/'source.json';path.write_text(json.dumps(source));cli+=['--peak-source',str(path)]
        args = benchmark.parser().parse_args(cli)
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
            if job['name'] != 'prepare':
                case['metrics']['runtime_ms'] = 20000
                case['metrics']['read'].update(runtime_ms=20000, iops=100,
                    latency_us={'mean':1,'50':1,'95':1,'99':1})
                case['series'] = [{'seconds':n,'direction':'read','mb_s':100} for n in range(1,21)]
            return case
        with contextlib.ExitStack() as stack:
            replacements = {'target_info':dict(return_value=target), 'check_target':dict(return_value=target),
                'collect_hardware':dict(return_value={}), 'load_reference_cache':dict(return_value=({},{})),
                'apply_hardware_specs':{}, 'DeviceLock':dict(return_value=contextlib.nullcontext()),
                'ensure_fio':dict(return_value={'path':'not-launched','engines':'posixaio','version':'fio-fixture'}),
                'select_engine':dict(return_value='posixaio'), 'observe_quality':dict(return_value={'status':'quiet_observed'}),
                'confirm_write':dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)), 'trash_owned':{},
                'clean_stale_workspaces':dict(return_value=[]), 'run_case':dict(side_effect=produced)}
            mocked = {key:stack.enter_context(patch.object(benchmark,key,**value)) for key,value in replacements.items()}
            detected = dict(identity, fingerprint='b'*64) if wrong_identity else identity if with_source else None
            stack.enter_context(patch('device_identity.collect_device_identity', return_value=detected))
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

    def test_source_digest_is_bound_and_earlier_high_sample_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as name:
            code,result,_ = self.execute_fixture(Path(name).resolve(), with_source=True)
        self.assertEqual(code, 6)
        self.assertEqual(len(result['plan']['source_peak_sha256']),64)
        self.assertEqual(result['plan']['source_peak_run_id'],'old')
        self.assertTrue(result['peak_comparison']['workloads']['seq_read']['earlier_higher'])
        self.assertEqual(result['completeness']['complete'],False)

    def test_source_identity_change_stops_before_any_test_write(self):
        with tempfile.TemporaryDirectory() as name:
            code,result,mocked = self.execute_fixture(Path(name).resolve(), with_source=True, wrong_identity=True)
        self.assertEqual(code, 4)
        self.assertEqual(result['error']['key'],'unsafe_target')
        self.assertEqual(mocked['run_case'].call_count,0)

    def test_real_child_output_failures_cannot_qualify_a_supplement_sample(self):
        for fault in ('short', 'overrun', 'truncated'):
            with self.subTest(fault=fault):
                work, output, script, target, config, job = execution_fixtures.ExecutionTests().setup_case(short=fault == 'short')
                config['method_version'] = 'drivespeed-peak-supplement-v1'
                job.update(name='peak_seq_write_16_1', qd=16, peak_workload='seq_write')
                if fault == 'overrun':
                    script.write_text(script.read_text().replace("count=int(a['io_size'])", "count=int(a['io_size'])+1048576"))
                elif fault == 'truncated':
                    script.write_text(script.read_text() + "with Path(a['output']).open('a') as stream: stream.write('{')\n")
                with patch.object(benchmark, 'check_target', return_value={}), contextlib.redirect_stderr(io.StringIO()):
                    case = benchmark.run_case({'path':str(script)}, job, config, 'posixaio', output, work,
                                              target, Progress([job['name']], messages('en')))
                self.assertEqual(case['status'], 'failed')
                self.assertFalse(case['metrics_complete'])
                self.assertTrue((output/(job['name']+'.case.json')).exists())

    def test_separate_comparison_flags_an_earlier_higher_qualified_sample(self):
        criteria = {'minimum_seconds':5, 'tolerance':.05, 'minimum_depth_percent':20, 'minimum_half_depth_percent':90}
        target = {'path':'/fixture','st_dev':1,'inode':2,'device':'disk1','physical_devices':['disk1']}
        identity = {'version':1,'fingerprint':'a'*64,'source':'smartctl:serial_number','attachment':'internal'}
        common = {'target':target,'device_identity':identity,'environment':{'engine':'posixaio','fio':{'version':'fio-fixture'}}}
        baseline,job = qualified_baseline()
        source = {**common,'run_id':'old','tool':{'method_version':'drivespeed-peak-v1'},
                  'plan':{'file_bytes':8*GIB,'peak_criteria':criteria,'jobs':[job]},'cases':[baseline]}
        cases=[];jobs=[]
        for depth in (16,32):
            for repeat in (1,2):
                case=peak_fixtures.case(depth,100);case['name']=f'peak_seq_read_{depth}_{repeat}'
                case['metrics']['achieved_depth']=({'8':75,'16':25,'32':0,'>=64':0} if depth==16 else {'16':75,'32':25,'>=64':0})
                case['metrics']['read'].update(iops=100,latency_us={'mean':1,'50':1,'95':1,'99':1})
                cases.append(case)
                jobs.append({'name':case['name'],'rw':'read','bs':MIB,'qd':depth,'runtime':5,'io_bytes':1000000})
        supplement={**common,'tool':{'method_version':'drivespeed-peak-supplement-v1'},
                    'plan':{'file_bytes':8*GIB,'peak_criteria':criteria,'jobs':jobs},'cases':cases,
                    'peak_supplement':{'seq_read':{'status':'plateau','depths':[16,32]}}}
        compared=compare_peak_runs(source,supplement)
        self.assertTrue(compared['workloads']['seq_read']['earlier_higher'])
        changed=copy.deepcopy(supplement);changed['device_identity']['fingerprint']='b'*64
        self.assertEqual(compare_peak_runs(source,changed)['status'],'incompatible')
        changed=copy.deepcopy(supplement);changed['environment']['fio']['version']='other'
        self.assertEqual(compare_peak_runs(source,changed)['status'],'incompatible')
        changed=copy.deepcopy(supplement);changed['cases'][1]['metrics']['read']['mb_s']=150
        self.assertEqual(compare_peak_runs(source,changed)['workloads'],{})
        changed=copy.deepcopy(supplement);changed['cases'][0]['metrics']['read'].pop('latency_us')
        self.assertEqual(compare_peak_runs(source,changed)['workloads'],{})
        changed=copy.deepcopy(supplement);changed['cases'][0]['bs']=4096
        self.assertEqual(compare_peak_runs(source,changed)['workloads'],{})
        changed=copy.deepcopy(source);changed['cases'][0]['metrics']['read'].pop('latency_us')
        self.assertEqual(compare_peak_runs(changed,supplement)['workloads'],{})


if __name__ == '__main__':
    unittest.main()
