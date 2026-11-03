"""Cache evidence from fio cumulative snapshots; synthetic data only."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from workloads import cache_assessment, cache_observation, collection_status, write_counter_samples


def evidence():
    counters, total = [], 0
    for second in range(1, 121):
        total += (1000 if second <= 10 else 400) * 1000 * 1000
        counters.append({'runtime_ms': second * 1000, 'bytes': total})
    return {'rw': 'write', 'status': 'completed',
            'metrics': {'runtime_ms': 120000, 'write': {'runtime_ms': 120000, 'bytes': total}},
            'series': [{'seconds': 1, 'direction': 'write', 'mb_s': 1000},
                       {'seconds': 120, 'direction': 'write', 'mb_s': 400}],
            'cache_write_counters_v1': counters}


class CounterSamplingTests(unittest.TestCase):
    def test_valid_cumulative_counters_provide_continuous_window(self):
        case = evidence()
        self.assertEqual(collection_status(case), 'collected_irregular')
        points, status = cache_observation(case)
        self.assertEqual(status, 'collected')
        result = cache_assessment(points, status)
        self.assertEqual(result['status'], 'observed_drop_stable')
        self.assertAlmostEqual(result['post_cache_mb_s'], 400)

    def test_counter_gap_reset_or_final_mismatch_never_qualifies(self):
        for alteration in ('gap', 'reset', 'time', 'final', 'noninteger'):
            case = evidence()
            if alteration == 'gap':
                case['cache_write_counters_v1'].pop(20)
            elif alteration == 'reset':
                case['cache_write_counters_v1'][20]['bytes'] = 1
            elif alteration == 'time':
                case['cache_write_counters_v1'][20]['runtime_ms'] = 20000
            elif alteration == 'final':
                case['cache_write_counters_v1'][-1]['bytes'] -= 1
            else:
                case['cache_write_counters_v1'][20]['bytes'] = True
            points, status = cache_observation(case)
            with self.subTest(alteration=alteration):
                self.assertNotEqual(status, 'collected')
                self.assertNotEqual(cache_assessment(points, status)['status'], 'observed_drop_stable')

    def test_legacy_result_never_changes_its_sampling_source(self):
        case = evidence(); case.pop('cache_write_counters_v1')
        before = copy.deepcopy(case)
        points, status = cache_observation(case)
        self.assertEqual((points, status), (case['series'], 'collected_irregular'))
        self.assertEqual(case, before)

    def test_snapshot_parser_rejects_reset_and_ignores_exact_final_duplicate(self):
        values = [dict(write={'runtime_ms': t, 'bytes': b}) for t, b in
                  [(1000, 100), (2000, 200), (2000, 200)]]
        with patch('workloads.metrics', side_effect=lambda value: value):
            self.assertEqual(write_counter_samples(values),
                             [{'runtime_ms': 1000, 'bytes': 100}, {'runtime_ms': 2000, 'bytes': 200}])
            with self.assertRaises(ValueError):
                write_counter_samples(values[:2] + [dict(write={'runtime_ms': 3000, 'bytes': 150})])


if __name__ == '__main__':
    unittest.main()
