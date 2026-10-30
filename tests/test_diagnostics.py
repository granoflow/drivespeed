"""Optional diagnostics: budgets, honest coverage and real CRC corruption detection."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import benchmark
from workloads import GIB, MIB, diagnostic_plan, add_recovery, plan, fio_arguments
from progress import messages
from report import render_diagnostics, render_report


class DiagnosticTests(unittest.TestCase):
    def test_finite_integrity_size_retains_reserve_and_requires_complete_readback(self):
        target = dict(total_bytes=200 * GIB, free_bytes=80 * GIB)
        config = diagnostic_plan('integrity', target, file_bytes=8 * GIB, write_budget=8 * GIB)
        self.assertEqual(config['file_bytes'], 8 * GIB)
        self.assertLessEqual(config['file_bytes'] + config['reserve_bytes'] + 64 * MIB, target['free_bytes'])
        self.assertEqual(config['max_write_bytes'], 8 * GIB)
        self.assertTrue(all(j['require_complete'] for j in config['jobs']))
        self.assertEqual([j['rw'] for j in config['jobs']], ['write', 'read'])
        with self.assertRaises(ValueError):
            diagnostic_plan('integrity', dict(total_bytes=200 * GIB, free_bytes=5 * GIB), file_bytes=8 * GIB)
        with self.assertRaises(ValueError):
            diagnostic_plan('integrity', target, file_bytes=8 * GIB, write_budget=GIB)

    def test_integrity_default_and_explicit_sizes_do_not_expand_with_free_space(self):
        for free in (80 * GIB, 120 * GIB):
            target = dict(total_bytes=200 * GIB, free_bytes=free)
            self.assertEqual(diagnostic_plan('integrity', target)['file_bytes'], 64 * GIB)
            self.assertEqual(diagnostic_plan('integrity', target, file_bytes=8 * GIB)['file_bytes'], 8 * GIB)

    def test_unknown_cli_option_fails_before_target_access(self):
        with contextlib.redirect_stderr(io.StringIO()) as error, \
             patch.object(benchmark, 'target_info') as inspect, self.assertRaises(SystemExit) as caught:
            benchmark.parser().parse_args(['--target', '/unused', '--profile', 'integrity', '--fill-free-space'])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('unrecognized arguments', error.getvalue())
        inspect.assert_not_called()

    def test_integrity_cli_is_not_an_automatic_test_option(self):
        with patch.object(benchmark, 'target_info') as inspect, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                benchmark.parser().parse_args(['--target', '/unused', '--profile', 'integrity', '--dry-run'])
        self.assertEqual(caught.exception.code, 2)
        inspect.assert_not_called()

    def test_mapping_repeats_sequence_and_never_reads_uninitialized_space(self):
        config = diagnostic_plan('mapping', dict(total_bytes=1000 * GIB, free_bytes=600 * GIB))
        self.assertTrue(config['jobs'][0]['require_complete'])
        for first, second in zip(config['jobs'][1::2], config['jobs'][2::2]):
            self.assertEqual(first['random_seed'], second['random_seed'])
            self.assertEqual(first['region_bytes'], second['region_bytes'])
            self.assertLessEqual(first['region_bytes'], config['file_bytes'])
            args = fio_arguments('fio', first, config['file_bytes'], 'posixaio', '/tmp/raw', '/tmp/log')
            self.assertIn('--readonly', args)
            self.assertIn('--randrepeat=1', args)

    def test_recovery_is_bounded_and_counted(self):
        config = plan('cache', file_bytes=16 * GIB, write_budget=16 * GIB)
        previous = config['max_runtime_seconds']
        add_recovery(config, 60)
        self.assertEqual(config['max_write_bytes'], 24 * GIB)
        self.assertEqual(config['max_runtime_seconds'], previous + 135)
        self.assertEqual(config['jobs'][-1]['idle_seconds'], 60)
        with self.assertRaises(ValueError):
            add_recovery(config, 0)

    def test_failed_verification_cannot_render_pass(self):
        result = dict(plan=dict(file_bytes=MIB), cases=[
            dict(name='integrity_write', status='completed', metrics=dict(write=dict(bytes=MIB))),
            dict(name='integrity_read', status='failed', metrics=dict(read=dict(bytes=MIB)))])
        text = messages('zh-CN')
        html = render_diagnostics(result, text)
        self.assertIn(text['integrity_incomplete'], html)
        self.assertNotIn(text['integrity_pass'], html)
        result['cases'][1]['status'] = 'completed'
        self.assertIn(text['integrity_pass'], render_diagnostics(result, text))

    def test_short_read_is_failed_even_when_fio_exits_successfully(self):
        with tempfile.TemporaryDirectory(prefix='drivespeed-short-') as directory:
            root = Path(directory)
            work, output = root / 'work', root / 'output'
            work.mkdir()
            output.mkdir()
            (work / 'payload.bin').write_bytes(b'')
            job = dict(name='integrity_read', rw='read', bs=MIB, qd=1,
                       io_bytes=MIB, runtime=10, verify=True, require_complete=True)
            snapshot = dict(jobs=[dict(error=0, eta=0, job_runtime=1000,
                                      read=dict(io_bytes=4096), write=dict(io_bytes=0))])
            (output / 'integrity_read.fio.json-stream').write_text(json.dumps(snapshot))
            process = Mock(returncode=0)
            process.poll.return_value = 0
            with patch.object(benchmark, 'check_target', return_value={}), \
                 patch.object(benchmark.subprocess, 'Popen', return_value=process), \
                 patch.object(benchmark, 'stop_process'):
                case = benchmark.run_case(dict(path='fio'), job,
                                          dict(file_bytes=MIB, reserve_bytes=0),
                                          'posixaio', output, work, dict(path=str(work)), Mock())
            self.assertEqual(case['status'], 'failed')
            self.assertFalse(case['metrics_complete'])

    def test_archived_diagnostic_reports_remain_renderable(self):
        target = dict(total_bytes=1000 * GIB, free_bytes=600 * GIB)
        for profile in ('mapping', 'integrity'):
            config = diagnostic_plan(profile, target)
            cases = [dict(**job, status='skipped') for job in config['jobs']]
            with tempfile.TemporaryDirectory(prefix='drivespeed-report-') as directory:
                result = dict(plan=config, cases=cases, outcome='partial', synthetic=True)
                paths = render_report(result, directory, 'zh-CN', messages('zh-CN'))
                html = Path(paths['html']).read_text()
                self.assertIn('id="diagnostics"', html)
                self.assertNotIn('id="sustained"', html)

    def test_archived_cache_recovery_plan_counts_both_writes(self):
        config = plan('cache', file_bytes=8 * GIB, write_budget=8 * GIB)
        add_recovery(config, 60)
        self.assertEqual(config['max_write_bytes'], 16 * GIB)

    @unittest.skipUnless(os.environ.get('DRIVESPEED_RUN_FIO_FIXTURES') == '1' and shutil.which('fio'), 'real fio fixture requires explicit opt-in')
    def test_real_crc_write_read_and_corruption(self):
        # Only 1 MiB in a private system temporary directory, never the external target.
        fio = shutil.which('fio')
        version = subprocess.run([fio, '--version'], capture_output=True, text=True)
        if not version.stdout.startswith('fio-'):
            self.skipTest('Unrelated fio program')
        with tempfile.TemporaryDirectory(prefix='drivespeed-crc-') as directory:
            folder = Path(directory)
            config = diagnostic_plan('integrity', dict(total_bytes=100 * GIB, free_bytes=50 * GIB), file_bytes=MIB)
            def run(job):
                args = fio_arguments(fio, job, MIB, 'posixaio', folder / 'raw.json', folder / job['name'])
                return subprocess.run(args, cwd=folder, capture_output=True, timeout=15)
            self.assertEqual(run(config['jobs'][0]).returncode, 0)
            self.assertEqual(run(config['jobs'][1]).returncode, 0)
            with (folder / 'payload.bin').open('r+b') as stream:
                stream.seek(4096)
                value = stream.read(1)
                stream.seek(4096)
                stream.write(bytes([value[0] ^ 255]))
            self.assertNotEqual(run(config['jobs'][1]).returncode, 0)


if __name__ == '__main__':
    unittest.main()
