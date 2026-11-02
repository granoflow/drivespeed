"""Capacity opt-in integration and authorization; synthetic I/O only."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_auto_full import AuthorizationTests
from test_mandatory_cache import stable_result
import benchmark
from capacity_check import capacity_plan
from progress import messages
from report import render_capacity_check
from system_io import BenchError
from workloads import GIB, fio_arguments
from write_allowance import WriteAllowance, plan_hash


def capacity_fixture(directory):
    target = AuthorizationTests().target()
    path = Path(directory).resolve()
    target.update(path=str(path), st_dev=path.stat().st_dev, inode=path.stat().st_ino)
    source = stable_result()
    source['target'] = copy.deepcopy(target)
    source['_source_sha256'] = 'a' * 64
    return target, source


class CapacityAuthorizationTests(unittest.TestCase):
    def setup_ledger(self, root):
        ledger = WriteAllowance(Path(root) / 'ledger')
        target = AuthorizationTests().target()
        ledger.import_existing(target, 16 * GIB, 12 * GIB, 8 * GIB, 'fixture prior authorization')
        allowance = ledger.read(target)
        config = dict(method_version='drivespeed-capacity-v1', max_write_bytes=20 * GIB,
                      write_budget=20 * GIB, additional_authorized_bytes=16 * GIB,
                      capacity_run_cap_bytes=20 * GIB)
        return ledger, target, allowance, config

    def test_confirmation_extends_finite_budget_without_resetting_history(self):
        with tempfile.TemporaryDirectory() as root:
            ledger, target, allowance, config = self.setup_ledger(root)
            ledger.reserve(target, config, allowance, 'capacity', plan_hash(target, config, allowance))
            reserved = ledger.read(target)
            self.assertEqual(reserved['scope_id'], allowance['scope_id'])
            self.assertEqual(reserved['remaining'], 0)
            self.assertEqual(reserved['revision'], allowance['revision'] + 1)
            ledger.settle('capacity', 10 * GIB)
            final = ledger.read(target)
            self.assertEqual(final['remaining'], 10 * GIB)
            self.assertEqual(final['run_cap'], 20 * GIB)
            with ledger._connection(create=False) as db:
                cap, charged = db.execute('SELECT cap,charged FROM scopes WHERE active=1').fetchone()
            self.assertEqual((cap, charged), (32 * GIB, 22 * GIB))

    def test_omitted_confirmation_or_changed_proposal_cannot_extend(self):
        with tempfile.TemporaryDirectory() as root:
            ledger, target, allowance, config = self.setup_ledger(root)
            digest = plan_hash(target, config, allowance)
            modified = dict(config, additional_authorized_bytes=17 * GIB)
            for plan, confirmation in ((config, 'wrong'), (modified, digest),
                                       (modified, plan_hash(target, modified, allowance))):
                with self.assertRaises(BenchError):
                    ledger.reserve(target, plan, allowance, 'no-write', confirmation)
                self.assertEqual(ledger.read(target), allowance)

    def test_normal_plan_cannot_expand_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ledger, target, allowance, config = self.setup_ledger(root)
            config['method_version'] = 'drivespeed-auto-full-v2'
            with self.assertRaises(BenchError):
                ledger.reserve(target, config, allowance, 'normal', plan_hash(target, config, allowance))
            self.assertEqual(ledger.read(target), allowance)

    def test_stale_revision_rejects_capacity_extension(self):
        with tempfile.TemporaryDirectory() as root:
            ledger, target, allowance, config = self.setup_ledger(root)
            ledger.reserve(target, config, allowance, 'first', plan_hash(target, config, allowance))
            after = ledger.read(target)
            with self.assertRaises(BenchError):
                ledger.reserve(target, config, allowance, 'duplicate', plan_hash(target, config, allowance))
            self.assertEqual(ledger.read(target), after)


class CapacityFlowTests(unittest.TestCase):
    def test_dry_run_does_not_write_extend_or_launch_fio(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target, source = capacity_fixture(directory.name)
        allowance = dict(present=True, scope_id='fixture', revision=1, run_cap=8*GIB, remaining=GIB)
        args = benchmark.parser().parse_args(['--target', target['path'], '--capacity-from', 'fixture.json', '--dry-run'])
        captured = io.StringIO()
        with patch.object(benchmark, 'target_info', return_value=target), \
             patch('capacity_check.load_source', return_value=source), \
             patch.object(benchmark, 'WriteAllowance') as ledger, \
             patch.object(benchmark, 'collect_hardware', return_value={}), \
             patch.object(benchmark, 'load_reference_cache', return_value=({}, {})), \
             patch.object(benchmark, 'apply_hardware_specs'), \
             patch.object(benchmark, 'ensure_fio') as fio, \
             patch.object(benchmark, 'DeviceLock') as lock, contextlib.redirect_stdout(captured):
            ledger.return_value.read.return_value = allowance
            self.assertEqual(benchmark.execute(args, messages('en'), messages('en')), 0)
            ledger.return_value.reserve.assert_not_called()
            fio.assert_not_called()
            lock.assert_not_called()
        plan = json.loads(captured.getvalue())['plan']
        self.assertEqual(plan['method_version'], 'drivespeed-capacity-v1')
        self.assertGreater(plan['additional_authorized_bytes'], 0)
        self.assertGreater(plan['file_bytes'], allowance['remaining'])

    def test_success_and_read_failure_preserve_speed_and_export_combined_report(self):
        for fail_read in (False, True):
            with self.subTest(fail_read=fail_read), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                target, source = capacity_fixture(root)
                source.pop('user_input', None)
                original = copy.deepcopy(source)
                output = root / 'output'
                ledger = WriteAllowance(root / 'ledger')
                ledger.import_existing(target, 16*GIB, 12*GIB, 8*GIB, 'fixture approved original session')
                args = benchmark.parser().parse_args(['--target', target['path'], '--capacity-from', 'fixture.json',
                                                       '--output', str(output), '--language', 'en'])
                def produced(fio, job, config, *unused):
                    direction = job['rw']
                    size = config['file_bytes']
                    data = {d:dict(bytes=size if d == direction else 0, runtime_ms=1000, mb_s=100,
                                   iops=100, latency_us={k:1 for k in ('mean','50','95','99')})
                            for d in ('read','write')}
                    data['runtime_ms'] = 1000
                    failed = fail_read and direction == 'read'
                    return dict(**job, status='failed' if failed else 'completed', metrics=data,
                                metrics_complete=not failed, series=[])
                with contextlib.ExitStack() as stack:
                    replacements = {
                        'target_info': dict(return_value=target),
                        'WriteAllowance': dict(return_value=ledger),
                        'collect_hardware': dict(return_value={}),
                        'load_reference_cache': dict(return_value=({}, {})),
                        'apply_hardware_specs': {},
                        'DeviceLock': dict(return_value=contextlib.nullcontext()),
                        'ensure_fio': dict(return_value=dict(path='fixture-never-launched', engines='posixaio')),
                        'select_engine': dict(return_value='posixaio'),
                        'observe_quality': dict(return_value=dict(status='quiet_observed')),
                        'check_target': dict(return_value=target),
                        'confirm_write': dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)),
                        'trash_owned': {},
                    }
                    for name, options in replacements.items():
                        stack.enter_context(patch.object(benchmark, name, **options))
                    stack.enter_context(patch('capacity_check.load_source', return_value=source))
                    stack.enter_context(patch.object(benchmark.os, 'ftruncate'))
                    jobs = stack.enter_context(patch.object(benchmark, 'run_case', side_effect=produced))
                    stack.enter_context(patch.object(benchmark.platform, 'platform', return_value='fixture-platform'))
                    child = stack.enter_context(patch.object(benchmark.subprocess, 'Popen'))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    code = benchmark.execute(args, messages('en'), messages('en'))
                self.assertEqual(code, 5 if fail_read else 0)
                self.assertEqual(jobs.call_count, 2)
                child.assert_not_called()
                self.assertEqual(source, original)
                combined = json.loads((output/'combined-results.json').read_text())
                self.assertEqual(combined['outcome'], 'complete')
                self.assertEqual(combined['capacity_check']['tool']['method_version'], 'drivespeed-capacity-v1')
                document = (output/'combined-report'/'report.html').read_text()
                self.assertIn(messages('en')['capacity_not_completed'] if fail_read else messages('en')['capacity_ok'], document)
                self.assertTrue((output/'report.html').is_file())

    def test_rejection_does_not_imply_capacity_consent(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target, source = capacity_fixture(directory.name)
        allowance = dict(present=True, scope_id='fixture', revision=1, run_cap=8*GIB, remaining=GIB)
        plan = capacity_plan(target, allowance, source)
        with patch.object(benchmark.sys, 'stdin', io.StringIO('no\n')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(BenchError):
                benchmark.confirm_write(target, plan, allowance, messages('en'))

    def test_report_recomputes_readback_and_does_not_call_it_bad_block_scan(self):
        size = GIB
        check = dict(tool=dict(method_version='drivespeed-capacity-v1'), plan=dict(method_version='drivespeed-capacity-v1', profile='integrity', file_bytes=size, jobs=[
            dict(name='integrity_write', rw='write', bs=1048576, qd=16, verify=True, io_bytes=size),
            dict(name='integrity_read', rw='read', bs=1048576, qd=16, verify=True, io_bytes=size)]),
            outcome='complete', completeness=dict(complete=True), cases=[])
        for name, direction in (('integrity_write', 'write'), ('integrity_read', 'read')):
            check['cases'].append(dict(name=name, rw=direction, bs=1048576, qd=16, verify=True, io_bytes=size, status='completed', metrics_complete=True,
                metrics=dict(runtime_ms=1000, read=dict(bytes=size if direction=='read' else 0, runtime_ms=1000),
                             write=dict(bytes=size if direction=='write' else 0, runtime_ms=1000))))
        text = messages('en')
        html = render_capacity_check(dict(capacity_check=check), text)
        self.assertIn(text['capacity_ok'], html)
        self.assertIn(text['capacity_limits'], html)
        check['cases'][-1]['status'] = 'failed'
        html = render_capacity_check(dict(capacity_check=check), text)
        self.assertNotIn(text['capacity_ok'], html)
        self.assertIn(text['capacity_not_completed'], html)
        self.assertIn('—', html)

    def test_capacity_crc_read_preserves_file_and_rejects_alias_offsets(self):
        job = dict(name='integrity_read', rw='read', bs=1048576, qd=16, runtime=120,
                   io_bytes=GIB, verify=True, status_interval=10)
        command = fio_arguments('fio-fixture', job, GIB, 'posixaio', 'raw', 'log')
        for required in ('--verify=crc32c', '--readonly', '--allow_file_create=0', '--size=1073741824',
                         '--io_size=1073741824', '--status-interval=10', '--log_avg_msec=10000'):
            self.assertIn(required, command)
        self.assertNotIn('--verify_only=1', command)
        self.assertNotIn('--time_based', command)


if __name__ == '__main__':
    unittest.main()
