"""Data-based publication and private identity; no network or device access."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).parent)]
import community_data as data
from community_store import CommunityStore
from progress import messages
from report import render_report
from test_auto_full import community_result
from workloads import AUTO_METHOD, QUICK_METHOD, quick_plan, fio_arguments


def quick_result():
    result = community_result('visibility_limited')
    result.pop('user_input', None)
    result['plan'] = quick_plan(result['target'])
    result['tool']['method_version'] = QUICK_METHOD
    result['device_identity'] = dict(version=1, fingerprint='a' * 64,
                                     source='smartctl:serial_number', attachment='external')
    result['hardware']['controller'] = dict(model='ASMedia 246x', source='user')
    result['target']['protocol'] = 'USB4 40 Gbps'
    original = {case['name']: case for case in result['cases']}
    cases = []
    for job in result['plan']['jobs']:
        case = copy.deepcopy(original[job['name']])
        case.update(job)
        direction = 'read' if job['rw'] in ('read', 'randread') else 'write'
        case['metrics'][direction]['bytes'] = job['io_bytes']
        case['metrics'][direction]['mb_s'] = job['io_bytes'] / 500_000
        case['metrics'][direction]['iops'] = job['io_bytes'] / job['bs'] / .5
        case['command'] = fio_arguments('fixture-fio', job, result['plan']['file_bytes'],
                                        'posixaio', 'fixture-raw', 'fixture-log')
        cases.append(case)
    result['cases'] = cases
    return result


class ReportDataEligibilityTests(unittest.TestCase):
    def test_quick_core_data_and_private_snapshot_roundtrip(self):
        result = quick_result()
        original = copy.deepcopy(result)
        payload = data.project_result(result, 'https://x.com/fixture_user')
        data.require_publication_identity(payload)
        self.assertEqual(len(payload['cases']), 4)
        self.assertEqual(payload['plan']['sustained_seconds'], 0)
        self.assertNotIn('quality', payload)
        with tempfile.TemporaryDirectory() as directory, CommunityStore(Path(directory).resolve()) as store:
            identifier = store.create(payload, result['run_id'], 'fixture-results')
            self.assertEqual(store.payload(identifier), data.normalise_payload(payload))
        self.assertEqual(result, original)

    def test_profile_does_not_replace_core_data_checks(self):
        payload = data.project_result(quick_result(), 'https://x.com/fixture_user')
        for profile in ('quick', 'full', 'cache', 'mapping', 'integrity'):
            candidate = copy.deepcopy(payload)
            candidate['plan']['profile'] = profile
            data.require_report_data(candidate)
        for name in data.REPORT_CASES:
            candidate = copy.deepcopy(payload)
            candidate['cases'] = [case for case in candidate['cases'] if case['name'] != name]
            with self.subTest(missing=name), self.assertRaises(data.CommunityError):
                data.require_report_data(candidate)

    def test_partial_full_without_cache_keeps_its_original_outcome(self):
        result = community_result()
        result['tool']['method_version'] = result['plan']['method_version'] = AUTO_METHOD
        result['outcome'] = 'partial'
        before = copy.deepcopy(result)
        payload = data.project_result(result, 'https://x.com/fixture_user')
        self.assertEqual(payload['outcome'], 'partial')
        cache = next(case for case in payload['cases'] if case['name'] == 'sustained')['cache']
        self.assertTrue(cache is None or cache['post_cache_mb_s'] is None)
        self.assertEqual(result, before)

    def test_invalid_active_core_and_duplicate_names_are_rejected(self):
        valid = data.project_result(quick_result(), 'https://x.com/fixture_user')
        for mutate in (
            lambda p: p['cases'][0].update(status='failed'),
            lambda p: p['cases'][0]['write'].update(bytes=0),
            lambda p: p['cases'][0]['write'].update(mb_s=None),
            lambda p: p['cases'][0]['write']['latency_us'].update(mean=None),
            lambda p: p['cases'].append(copy.deepcopy(p['cases'][0])),
        ):
            candidate = copy.deepcopy(valid)
            mutate(candidate)
            with self.assertRaises(data.CommunityError):
                data.validate_payload(candidate)

    def test_new_publication_requires_private_identity_and_resolved_environment(self):
        valid = data.project_result(quick_result(), 'https://x.com/fixture_user')
        for mutate in (
            lambda p: p.pop('device_identity'),
            lambda p: p['storage'].update(connection='Unknown interface'),
            lambda p: p['hardware']['controller'].update(model=None, source='unknown'),
            lambda p: p['hardware']['controller'].update(model=' UNKNOWN ', source='user'),
            lambda p: p['storage'].update(scope='virtual'),
        ):
            candidate = copy.deepcopy(valid)
            mutate(candidate)
            with self.assertRaises(data.CommunityError):
                data.require_publication_identity(candidate)
        valid['hardware']['controller'] = dict(model=None, source='unknown')
        valid['device_identity']['attachment'] = 'internal'
        with self.assertRaises(data.CommunityError):
            data.require_publication_identity(valid)  # USB is not a confirmed native internal path.
        valid['storage']['connection'] = 'PCIe 4.0 x4'
        data.require_publication_identity(valid)

    def test_interference_is_dialogue_only_and_does_not_revoke_complete_data(self):
        result = quick_result()
        result['evaluation'] = dict(run_id=result['run_id'], language='zh-CN', report_policy='verified_only',
            sections=[dict(kind=kind, title=kind, paragraphs=['Measured fact'], evidence_status='supported')
                      for kind in ('bottleneck', 'upgrades', 'anomalies')],
            followup=[dict(issue='DISK-ACTIVITY-CONTEXT', reason='DIALOGUE-REASON',
                           solution='User may choose a new run', retest_plan='New bounded consent')])
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(stderr):
            receipt = render_report(result, directory, 'zh-CN', messages('zh-CN'))
            document = Path(receipt['html']).read_text()
        self.assertTrue(receipt['share_recommendation_allowed'])
        self.assertIn('DISK-ACTIVITY-CONTEXT', stderr.getvalue())
        self.assertNotIn('DISK-ACTIVITY-CONTEXT', document + json.dumps(receipt))
        self.assertNotIn('measurement_quality', json.dumps(receipt))
        exported = data.without_private_metadata(data.project_result(result, 'https://x.com/fixture_user'))
        self.assertNotIn('quality', exported)
        self.assertNotIn('device_identity', exported)


if __name__ == '__main__':
    unittest.main()
