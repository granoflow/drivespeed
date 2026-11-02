"""Staged measurement and whole-test shutdown using synthetic I/O only."""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark
import test_auto_full as fixtures
import test_benchmark as execution_fixtures
from progress import messages, Progress
from run_deadline import RunDeadline
from system_io import BenchError
from workloads import GIB, MIB, QUICK_METHOD, quick_plan, core_completeness, case_core_missing
from write_allowance import RunWriteProtection, plan_hash


class SingleRunTests(unittest.TestCase):
    def test_fixed_quick_working_set_is_never_silently_shrunk(self):
        target = fixtures.AuthorizationTests().target()
        target['free_bytes'] = max(10*GIB,(target['total_bytes']+9)//10) + GIB//2
        with self.assertRaises(ValueError):
            quick_plan(target)

    def test_new_runs_do_not_consume_or_read_old_quota(self):
        target = fixtures.AuthorizationTests().target()
        first, second = RunWriteProtection(), RunWriteProtection()
        before = first.read(target)
        config = benchmark.normal_plan(target, before)
        self.assertEqual(config["max_write_bytes"], 1024*GIB)
        self.assertEqual(config["total_timeout_seconds"], 2580)
        self.assertEqual(second.read(target), before)  # preview/execute are separate processes
        first.reserve(target, config, before, "one", plan_hash(target, config, before))
        first.settle("one", None)
        with self.assertRaises(BenchError):
            first.reserve(target, config, before, "retry", plan_hash(target, config, before))
        second.reserve(target, config, second.read(target), "two", plan_hash(target, config, before))

    def test_changed_target_and_overrun_are_rejected(self):
        target = fixtures.AuthorizationTests().target()
        guard = RunWriteProtection()
        before = guard.read(target)
        config = quick_plan(target)
        with self.assertRaises(BenchError):
            guard.reserve(target, config, before, "x", "wrong")
        guard.reserve(target, config, before, "x", plan_hash(target, config, before))
        with self.assertRaises(BenchError):
            guard.settle("x", config["max_write_bytes"]+1)
        with self.assertRaises(BenchError):
            guard.read(dict(target, inode=target["inode"]+1))


class DeadlineTests(unittest.TestCase):
    def test_watchdog_stops_owned_child_while_main_is_not_polling(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        deadline = RunDeadline(.1)
        try:
            deadline.attach(child)
            self.assertTrue(deadline.expired.wait(2))
            child.wait(timeout=3)
            with self.assertRaises(BenchError) as failure:
                deadline.check()
            self.assertEqual(failure.exception.key, "total_timeout")
        finally:
            deadline.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_deadline_does_not_restart_between_jobs(self):
        deadline = RunDeadline(.05)
        try:
            deadline.check()
            self.assertTrue(deadline.expired.wait(2))
            with self.assertRaises(BenchError):
                deadline.check()
        finally:
            deadline.close()

    def test_real_case_timeout_keeps_partial_artifacts(self):
        work, output, script, target, config, job = execution_fixtures.ExecutionTests().setup_case(sleep=True)
        deadline = RunDeadline(.1)
        try:
            with patch.object(benchmark, "check_target", return_value={}), contextlib.redirect_stderr(io.StringIO()):
                progress = Progress(["prepare"], messages("en"))
                progress.deadline = deadline
                case = benchmark.run_case({"path":str(script)},job,config,"posixaio",output,work,target,progress)
            self.assertEqual(case["status"], "partial")
            self.assertEqual(case["reason"], "total_timeout")
            self.assertFalse(case["metrics_complete"])
            self.assertTrue((output / "prepare.case.json").exists())
        finally:
            deadline.close()


class QuickFlowTests(unittest.TestCase):
    def test_new_result_keeps_private_device_identity_but_not_disk_activity(self):
        identity = dict(version=1, fingerprint='b' * 64,
                        source='smartctl:serial_number', attachment='external')
        with tempfile.TemporaryDirectory() as name, patch('device_identity.collect_device_identity', return_value=identity) as collect:
            code, result, mocked, renderer = self.execute_fixture(Path(name).resolve())
        self.assertEqual(code, 0)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(result['device_identity'], identity)
        self.assertNotIn('measurement_quality', result)
        self.assertNotIn('quiet_observed', json.dumps(result))

    def execute_fixture(self, root, timeout=False):
        target = fixtures.AuthorizationTests().target()
        target.update(path=str(root), st_dev=root.stat().st_dev, inode=root.stat().st_ino)
        args = benchmark.parser().parse_args(["--disk",str(root),"--stage","quick","--output",str(root/'out')])
        original = {c["name"]:c for c in fixtures.complete_result()["cases"]}
        def produced(fio, job, *unused):
            if timeout:
                raise BenchError("total_timeout", "fixture elapsed deadline", 6)
            case = copy.deepcopy(original[job["name"]])
            case.update(job)
            direction = "read" if job["rw"] in ("read", "randread") else "write"
            case["metrics"][direction]["bytes"] = job["io_bytes"]
            return case
        with contextlib.ExitStack() as stack:
            replacements={"target_info":dict(return_value=target),"check_target":dict(return_value=target),
                "collect_hardware":dict(return_value={}),"load_reference_cache":dict(return_value=({},{})),
                "apply_hardware_specs":{},"DeviceLock":dict(return_value=contextlib.nullcontext()),
                "ensure_fio":dict(return_value={"path":"never-launched","engines":"posixaio"}),
                "select_engine":dict(return_value="posixaio"),"observe_quality":dict(return_value={"status":"quiet_observed"}),
                "confirm_write":dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)),"trash_owned":{},
                "clean_stale_workspaces":dict(return_value=[]),"run_case":dict(side_effect=produced)}
            mocked={k:stack.enter_context(patch.object(benchmark,k,**v)) for k,v in replacements.items()}
            stack.enter_context(patch.object(benchmark.os,"ftruncate"))
            renderer=stack.enter_context(patch("report.render_report", return_value={"html":"fixture.html","pdf":None}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code=benchmark.execute(args,messages('en'),messages('en'))
        return code,json.loads((root/'out/results.json').read_text()),mocked,renderer

    def test_quick_is_four_case_workflow_not_complete_full_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            code,result,mocked,renderer=self.execute_fixture(Path(name).resolve())
        self.assertEqual(code,0)
        self.assertEqual(result['tool']['method_version'],QUICK_METHOD)
        self.assertEqual(mocked['run_case'].call_count,4)
        self.assertEqual(result['plan']['max_write_bytes'],GIB+16*MIB)
        self.assertEqual({c['name'] for c in result['cases']}, {'seq_write','seq_read','rand_read','rand_write'})
        self.assertEqual(sum(j['io_bytes'] for j in result['plan']['jobs'] if j['rw'] in ('write','randwrite')), result['plan']['max_write_bytes'])
        self.assertTrue(core_completeness(result)['complete'])
        renderer.assert_called_once()
        with tempfile.TemporaryDirectory() as name:
            from report import render_report
            page=Path(name)
            render_report(result,page,'zh-CN',messages('zh-CN'))
            source=(page/'report.html').read_text()
            self.assertIn('快速测速结果',source)
            self.assertIn(messages('zh-CN')['quick_notice'],source)
            self.assertIn('data-metric="rand_read"',source)
            self.assertIn('data-metric="rand_write"',source)

    def test_timeout_still_generates_report_and_never_runs_next_job(self):
        with tempfile.TemporaryDirectory() as name:
            code,result,mocked,renderer=self.execute_fixture(Path(name).resolve(),True)
        self.assertEqual(code,6)
        self.assertEqual(result['outcome'],'partial')
        self.assertEqual(result['stop_reason'],'total_timeout')
        self.assertEqual(mocked['run_case'].call_count,1)
        renderer.assert_called_once()

    def test_legacy_quick_stays_valid_without_inventing_random_measurements(self):
        with tempfile.TemporaryDirectory() as name:
            _, result, _, _ = self.execute_fixture(Path(name).resolve())
        result['tool']['method_version'] = result['plan']['method_version'] = 'drivespeed-quick-v1'
        result['cases'] = result['cases'][:2]
        result['plan']['jobs'] = result['plan']['jobs'][:2]
        self.assertTrue(core_completeness(result)['complete'])
        result['tool']['method_version'] = result['plan']['method_version'] = QUICK_METHOD
        self.assertIn('plan.jobs', core_completeness(result)['missing_core'])

    def test_partial_initialization_is_rejected_but_bounded_random_runtime_is_valid(self):
        with tempfile.TemporaryDirectory() as name:
            _, result, _, _ = self.execute_fixture(Path(name).resolve())
        job = result['plan']['jobs'][0]
        case = copy.deepcopy(result['cases'][0])
        case['metrics']['write']['bytes'] = GIB // 2
        case['metrics']['runtime_ms'] = case['metrics']['write']['runtime_ms'] = 30_000
        self.assertIn('seq_write.endpoint', case_core_missing(case, job, result['plan']))
        job = result['plan']['jobs'][-1]
        case = copy.deepcopy(result['cases'][-1])
        case['metrics']['write']['bytes'] = MIB
        case['metrics']['runtime_ms'] = case['metrics']['write']['runtime_ms'] = 5_000
        # No fabricated curve for a fixture whose active duration was changed.
        case['series'] = []
        self.assertNotIn('rand_write.endpoint', case_core_missing(case, job, result['plan']))

    def test_quick_source_is_bounded_regular_and_same_target(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve()
            _,result,_,_=self.execute_fixture(root)
            source=root/'quick.json'
            source.write_text(json.dumps(result))
            loaded=benchmark.load_quick_source(source,result['target'])
            self.assertEqual(loaded['run_id'],result['run_id'])
            with self.assertRaises(ValueError):
                benchmark.load_quick_source(source,dict(result['target'],inode=-1))
            link=root/'link.json';link.symlink_to(source)
            with self.assertRaises(ValueError):
                benchmark.load_quick_source(link,result['target'])
            if hasattr(os,'mkfifo'):
                pipe=root/'pipe.json';os.mkfifo(pipe)
                with self.assertRaises(ValueError):
                    benchmark.load_quick_source(pipe,result['target'])
            for raw in ('[]','null','{"plan": []}', 'not json'):
                source.write_text(raw)
                with self.assertRaises(ValueError):
                    benchmark.load_quick_source(source,result['target'])
            source.write_text(json.dumps(result))
            with source.open('r+b') as stream:
                stream.truncate(16*1024*1024+1)
            with self.assertRaises(ValueError):
                benchmark.load_quick_source(source,result['target'])

    def test_quick_source_rejects_invalid_run_id_before_preview_or_output(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            _, result, _, _ = self.execute_fixture(root)
            source = root / 'quick.json'
            rejected_output = root / 'rejected-output'
            invalid = [None, '', ' \t', 0, True, [], {}]
            for missing, value in [(True, None), *[(False, value) for value in invalid]]:
                current = copy.deepcopy(result)
                if missing:
                    current.pop('run_id')
                else:
                    current['run_id'] = value
                source.write_text(json.dumps(current))
                for dry_run in (True, False):
                    with self.subTest(missing=missing, value=value, dry_run=dry_run):
                        args = benchmark.parser().parse_args([
                            '--target', str(root), '--quick-from', str(source),
                            '--output', str(rejected_output),
                            *(['--dry-run'] if dry_run else []),
                        ])
                        with patch.object(benchmark, 'target_info', return_value=result['target']), \
                             patch.object(benchmark, 'collect_hardware', side_effect=AssertionError(
                                 'Invalid quick source must fail before hardware or output work')) as hardware:
                            with self.assertRaisesRegex(ValueError, 'Invalid or unavailable quick result'):
                                benchmark.execute(args, messages('en'), messages('en'))
                            hardware.assert_not_called()
                        self.assertFalse(rejected_output.exists())
