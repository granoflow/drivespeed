"""Bounded, separately recorded sustained-write observations."""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark
import test_auto_full as fixtures
import test_counter_sampling as counter_fixtures
import test_benchmark as execution_fixtures
from progress import messages, Progress
from workloads import (CACHE_SUPPLEMENT_METHOD, GIB, MIB, cache_assessment,
                       cache_observation, cache_supplement_plan, case_core_missing)
from write_allowance import plan_hash


class CacheSupplementPlanTests(unittest.TestCase):
    def test_plan_charges_the_single_initializing_write_and_freezes_bounds(self):
        plan = cache_supplement_plan(1000*GIB,700*GIB,80*GIB,8*GIB,300)
        self.assertEqual(plan['method_version'],CACHE_SUPPLEMENT_METHOD)
        self.assertEqual(plan['max_write_bytes'],80*GIB)
        self.assertEqual(len(plan['jobs']),1)
        self.assertEqual(plan['jobs'][0]['initialize_bytes'],8*GIB)
        self.assertEqual(plan['jobs'][0]['io_bytes'],80*GIB)
        self.assertTrue(plan['jobs'][0]['cache_test'])

    def test_invalid_space_budget_alignment_and_duration_fail_closed(self):
        base=[1000*GIB,700*GIB,80*GIB,8*GIB,300]
        for index,bad in ((1,108*GIB),(2,7*GIB),(2,80*GIB+1),(3,MIB),(4,104),(4,86401)):
            args=list(base);args[index]=bad
            with self.subTest(index=index,value=bad),self.assertRaises(ValueError):
                cache_supplement_plan(*args)

    def test_undetected_drop_never_qualifies_as_post_cache(self):
        case=counter_fixtures.evidence();case.update(name='sustained',bs=MIB,qd=16)
        case['metrics']['write'].update(mb_s=1000,iops=1,latency_us={'mean':1,'50':1,'95':1,'99':1})
        case['metrics']['read']={'bytes':0}
        job={'name':'sustained','rw':'write','bs':MIB,'qd':16,
             'io_bytes':case['metrics']['write']['bytes'],'runtime':120}
        plan={'method_version':CACHE_SUPPLEMENT_METHOD,'file_bytes':GIB}
        self.assertEqual(case_core_missing(case,job,plan),[])
        changed=copy.deepcopy(case)
        for index,row in enumerate(changed['cache_write_counters_v1'],1):row['bytes']=index*1000000000
        changed['metrics']['write']['bytes']=changed['cache_write_counters_v1'][-1]['bytes']
        job['io_bytes']=changed['metrics']['write']['bytes']
        points,timing=cache_observation(changed)
        self.assertEqual(cache_assessment(points,timing)['status'],'no_drop_observed')
        self.assertIn('sustained.post_cache_stability',case_core_missing(changed,job,plan))


class CacheSupplementFlowTests(unittest.TestCase):
    def test_controlled_child_stop_keeps_cumulative_counter_evidence(self):
        work,output,script,target,config,job=execution_fixtures.ExecutionTests().setup_case()
        config['method_version']=CACHE_SUPPLEMENT_METHOD
        job.update(name='sustained',rw='write',bs=MIB,qd=16,cache_test=True,
                   initialize_bytes=MIB,io_bytes=128*GIB,runtime=1800)
        script.write_text('#!'+sys.executable+'\n'+'''
import json,sys,signal,time
from pathlib import Path
a=dict(x[2:].split('=',1) for x in sys.argv[1:] if '=' in x)
rows=[]; log=[]; total=0
for second in range(1,111):
    speed=1000 if second<=10 else 200; total+=speed*1000000
    write={'io_bytes':total,'runtime':second*1000,'bw_bytes':speed*1000000,'iops':1,
           'clat_ns':{'mean':1000,'percentile':{'50.000000':1000,'95.000000':1000,'99.000000':1000}}}
    rows.append({'jobs':[{'error':0,'eta':1,'job_runtime':second*1000,'write':write,'read':{'io_bytes':0}}]})
    log.append(f'{second*1000},{speed*1000000/1024},1')
def finish(signum,frame):
    rows[-1]['jobs'][0]['eta']=0
    with Path(a['output']).open('a') as stream: stream.write(json.dumps(rows[-1]))
    Path(a['write_bw_log']+'_bw.1.log').write_text('\\n'.join(log))
    sys.exit(0)
signal.signal(signal.SIGINT,finish)
Path(a['output']).write_text('\\n'.join(json.dumps(row) for row in rows)+'\\n')
time.sleep(5)
sys.exit(7)
''')
        # Compress the fake child's 110 seconds of records; cadence validation
        # itself is exercised separately with unmodified collection_status.
        with patch.object(benchmark,'check_target',return_value={}), \
             patch.object(benchmark,'collection_status',return_value='collected'), \
             patch('workloads.collection_status',return_value='collected'), \
             contextlib.redirect_stderr(io.StringIO()):
            case=benchmark.run_case({'path':str(script)},job,config,'posixaio',output,work,target,
                                    Progress(['sustained'],messages('en')))
        self.assertEqual(case['status'],'completed',case.get('detail'))
        self.assertEqual(case['stop_reason'],'post_drop_stable')
        self.assertEqual(len(case['cache_write_counters_v1']),110)
        reconstructed=dict(case);reconstructed.pop('elapsed_seconds')
        self.assertEqual(cache_assessment(*cache_observation(reconstructed))['status'],'observed_drop_stable')

    def test_separate_observation_is_never_a_complete_full_report(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();target=fixtures.AuthorizationTests().target()
            target.update(path=str(root),st_dev=root.stat().st_dev,inode=root.stat().st_ino)
            args=benchmark.parser().parse_args(['--disk',str(root),'--output',str(root/'out'),
                '--stage','cache-supplement','--propose-write-bytes',str(80*GIB),
                '--working-file-bytes',str(8*GIB),'--cache-runtime-seconds','300'])
            def produced(fio,job,*unused):
                case=counter_fixtures.evidence();case.update(job,status='completed',metrics_complete=True,stop_reason='post_drop_stable')
                case['metrics']['read']={'bytes':0}
                return case
            with contextlib.ExitStack() as stack:
                replacements={'target_info':dict(return_value=target),'check_target':dict(return_value=target),
                    'collect_hardware':dict(return_value={}),'load_reference_cache':dict(return_value=({},{})),
                    'apply_hardware_specs':{},'DeviceLock':dict(return_value=contextlib.nullcontext()),
                    'ensure_fio':dict(return_value={'path':'not-launched','engines':'posixaio'}),
                    'select_engine':dict(return_value='posixaio'),'observe_quality':dict(return_value={'status':'quiet_observed'}),
                    'confirm_write':dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)), 'trash_owned':{},
                    'clean_stale_workspaces':dict(return_value=[]),'run_case':dict(side_effect=produced)}
                mocked={k:stack.enter_context(patch.object(benchmark,k,**v)) for k,v in replacements.items()}
                stack.enter_context(patch('device_identity.collect_device_identity',return_value=None))
                stack.enter_context(patch('device_identity.collect_report_identity_macos',return_value=None,create=True))
                stack.enter_context(patch.object(benchmark.os,'ftruncate'))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                code=benchmark.execute(args,messages('en'),messages('en'))
            result=json.loads((root/'out/results.json').read_text())
        self.assertEqual(code,6)
        self.assertEqual(mocked['run_case'].call_count,1)
        self.assertEqual(result['cache_assessment']['status'],'observed_drop_stable')
        self.assertFalse(result['completeness']['complete'])
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual(result['report'],{})


if __name__ == '__main__':
    unittest.main()
