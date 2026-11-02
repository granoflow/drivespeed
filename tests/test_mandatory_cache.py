"""Mandatory full-v2 qualification using synthetic measurements; no disk workload."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from test_auto_full import complete_result, community_result
from workloads import AUTO_METHOD, GIB, auto_plan, cache_assessment, core_completeness, fio_arguments
from progress import messages
from report import render_report
import community_data


def stable_result():
    result = community_result()
    result.update(complete_result(64 * GIB))
    result['plan']['method_version'] = AUTO_METHOD
    result['tool']['method_version'] = AUTO_METHOD
    case = result['cases'][0]
    case['series'] = [dict(seconds=i, mb_s=1000 if i <= 10 else 200, direction='write') for i in range(1, 121)]
    written = 32_000_000_000
    case['metrics'].update(runtime_ms=120000)
    case['metrics']['write'].update(bytes=written, runtime_ms=120000, mb_s=written / 120_000_000)
    case.update(elapsed_seconds=121, stop_reason='post_drop_stable')
    for item in result['cases']:
        item['command'] = fio_arguments('fixture-fio', item, result['plan']['file_bytes'], 'posixaio', 'fixture-raw', 'fixture-log')
    # Omit cached assessments: v2 projection must recompute from the real series.
    result.pop('cache_assessment', None)
    return result


class MandatoryCacheTests(unittest.TestCase):
    def test_explicit_larger_authorization_reuses_bounded_file(self):
        config = auto_plan(2000 * GIB, 1000 * GIB, 256 * GIB, 128 * GIB)
        self.assertEqual(config['method_version'], AUTO_METHOD)
        self.assertEqual(config['file_bytes'], 8 * GIB)
        self.assertEqual(config['max_write_bytes'], 128 * GIB)
        self.assertEqual(config['jobs'][0]['runtime'], 1800)
        self.assertGreater(config['jobs'][0]['io_bytes'], config['file_bytes'])

    def test_short_byte_endpoint_cannot_be_full_v2(self):
        result = complete_result()
        result['plan']['method_version'] = AUTO_METHOD
        result['cache_assessment'] = dict(status='observed_drop_stable', post_cache_mb_s=999)
        missing = core_completeness(result)['missing_core']
        self.assertIn('sustained.post_cache_stability', missing)

    def test_real_stable_curve_qualifies_without_cached_flags(self):
        result = stable_result()
        self.assertEqual(core_completeness(result), dict(complete=True, missing_core=[]))
        payload = community_data.project_result(result, 'https://x.com/fixture_user')
        cache = payload['cases'][0]['cache']
        self.assertEqual(cache['status'], 'observed')
        self.assertEqual(cache['post_cache_mb_s'], 200)
        self.assertFalse(cache['cause_confirmed'])
        # Physical cache capacity is not an obligatory measurement field.
        cache['capacity_estimate_bytes'] = None
        community_data.validate_payload(payload)

    def test_gaps_flat_curve_and_failed_final_do_not_qualify(self):
        for failure in ('gap', 'flat', 'cancelled', 'partial', 'invalid_sample'):
            result = stable_result()
            case = result['cases'][0]
            if failure == 'gap':
                del case['series'][55]
            elif failure == 'flat':
                for point in case['series']:
                    point['mb_s'] = 200
            elif failure == 'invalid_sample':
                case['series'][55]['mb_s'] = math.nan
            else:
                case['status'] = failure
            self.assertFalse(core_completeness(result)['complete'], failure)

    def test_v2_tool_cannot_downgrade_plan_qualification(self):
        result = complete_result()
        result['tool'] = dict(method_version=AUTO_METHOD)
        self.assertIn('plan.method_version', core_completeness(result)['missing_core'])

    def test_public_cache_is_mandatory_and_positive(self):
        valid = community_data.project_result(stable_result(), 'https://x.com/fixture_user')
        for bad in (None, 0, -1, math.nan, math.inf):
            payload = copy.deepcopy(valid)
            payload['cases'][0]['cache']['post_cache_mb_s'] = bad
            with self.assertRaises(community_data.CommunityError):
                community_data.validate_payload(payload)
        payload = copy.deepcopy(valid)
        payload['cases'][0]['cache'] = None
        with self.assertRaises(community_data.CommunityError):
            community_data.require_full_report(payload)

    def test_incomplete_report_does_not_trust_complete_flag(self):
        result = community_result()
        result['plan']['method_version'] = AUTO_METHOD
        result['tool']['method_version'] = AUTO_METHOD
        result.pop('user_input', None)
        result['outcome'] = 'complete'
        result['completeness'] = dict(complete=True)
        with tempfile.TemporaryDirectory() as directory:
            paths = render_report(result, directory, 'en', messages('en'))
            document = Path(paths['html']).read_text()
        self.assertIn('<h1>Incomplete test record</h1>', document)
        self.assertIn('data-required-cache="incomplete"', document)
        self.assertNotIn(messages('en')['write_observation_brief'].split('{')[0], document)


if __name__ == '__main__':
    unittest.main()
