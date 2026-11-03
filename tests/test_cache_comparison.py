"""Verify that independent sustained observations retain their source and limits."""
import copy
import unittest

import test_counter_sampling as fixtures
from peak_comparison import compare_cache_run
from workloads import GIB, MIB, cache_supplement_plan


def bound_runs():
    target = {'path':'/fixture', 'st_dev':1, 'inode':2, 'device':'disk1', 'physical_devices':['disk1']}
    identity = {'version':1, 'fingerprint':'a'*64, 'source':'smartctl:serial_number', 'attachment':'internal'}
    environment = {'engine':'posixaio', 'fio':{'version':'fio-fixture'}}
    criteria = {'minimum_seconds':5, 'tolerance':0.05, 'minimum_depth_percent':20, 'minimum_half_depth_percent':90}
    source = {'run_id':'source', 'target':target, 'device_identity':identity, 'environment':environment,
              'tool':{'method_version':'drivespeed-peak-v1'},
              'plan':{'method_version':'drivespeed-peak-v1', 'file_bytes':8*GIB, 'peak_criteria':criteria}}
    plan = cache_supplement_plan(1000*GIB,700*GIB,80*GIB,8*GIB,300)
    plan.update(source_peak_sha256='c'*64, source_peak_run_id='source')
    case = fixtures.evidence()
    case.update(plan['jobs'][0], stop_reason='post_drop_stable')
    case['metrics']['write'].update(mb_s=400, iops=1, latency_us={'mean':1,'50':1,'95':1,'99':1})
    case['metrics']['read'] = {'bytes':0}
    supplement = {'target':target, 'device_identity':identity, 'environment':environment,
                  'tool':{'method_version':'drivespeed-cache-supplement-v1'}, 'plan':plan, 'cases':[case]}
    return copy.deepcopy(source), copy.deepcopy(supplement)


class CacheComparisonTests(unittest.TestCase):
    def test_valid_bound_observation_is_recomputed_without_merging(self):
        source, supplement = bound_runs()
        before = copy.deepcopy((source, supplement))
        result = compare_cache_run(source, supplement, 'c'*64)
        self.assertEqual(result['status'], 'qualified_observation')
        self.assertAlmostEqual(result['post_drop_mb_s'], 400)
        self.assertFalse(result['cause_confirmed'])
        self.assertEqual((source, supplement), before)

    def test_other_source_target_or_frozen_budget_is_rejected(self):
        source, supplement = bound_runs()
        for change in ('hash', 'run', 'device', 'budget', 'engine'):
            item = copy.deepcopy(supplement)
            if change == 'hash': item['plan']['source_peak_sha256'] = 'd'*64
            if change == 'run': item['plan']['source_peak_run_id'] = 'other'
            if change == 'device': item['device_identity']['fingerprint'] = 'b'*64
            if change == 'budget': item['plan']['max_write_bytes'] += MIB
            if change == 'engine': item['environment']['engine'] = 'other'
            with self.subTest(change=change):
                self.assertEqual(compare_cache_run(source, item, 'c'*64)['status'], 'incompatible')

    def test_claimed_stability_cannot_replace_missing_or_flat_counters(self):
        source, supplement = bound_runs()
        case = supplement['cases'][0]
        for i, row in enumerate(case['cache_write_counters_v1'], 1):
            row['bytes'] = i * 1000000000
        case['metrics']['write']['bytes'] = case['cache_write_counters_v1'][-1]['bytes']
        supplement['cache_assessment'] = {'status':'observed_drop_stable', 'post_cache_mb_s':400}
        result = compare_cache_run(source, supplement, 'c'*64)
        self.assertEqual(result['status'], 'unqualified')
        self.assertEqual(result['observation_status'], 'no_drop_observed')
        self.assertNotIn('post_drop_mb_s', result)


if __name__ == '__main__':
    unittest.main()
