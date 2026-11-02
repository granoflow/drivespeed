"""Device-specific report advice with synthetic identities; no benchmark or network."""
import copy
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from performance_rating import component_summary, upgrade_parameters
from progress import messages
from report import render_hardware_assessment


def device_result(controller=3_200, drive=14_000, interface=20, measured=2_200):
    hardware = {component: {'model': 'Fixture ' + component}
                for component in ('computer', 'enclosure', 'controller', 'drive')}
    hardware['links'] = [{'kind': 'usb', 'gbps': interface, 'state': 'negotiated'}]
    items = []
    for component, metric, value in (
            ('computer', 'interface_gbps', 40), ('enclosure', 'interface_gbps', 40),
            ('controller', 'seq_read', controller), ('controller', 'seq_write', controller),
            ('drive', 'seq_read', drive), ('drive', 'seq_write', drive)):
        items.append(dict(component=component, model=hardware[component]['model'], metric=metric,
                          value=value, unit='Gb/s' if metric == 'interface_gbps' else 'MB/s',
                          relationship='device', source_kind='official', conditions='Synthetic specification',
                          source_url='https://example.com/spec', source_label='Fixture specification'))
    cases = [dict(name='seq_' + direction + '_q', rw=direction, qd=16, bs=1_048_576,
                  status='completed', metrics_complete=True, metrics={direction: {'mb_s': measured}})
             for direction in ('read', 'write')]
    return dict(hardware=hardware, target={'scope': 'physical'}, cases=cases,
                measurement_quality={'status': 'quiet_observed'},
                references={'for_drive_model': hardware['drive']['model'],
                            'checked_at': date.today().isoformat(), 'items': items})


class DeviceParameterTests(unittest.TestCase):
    def test_only_current_parameters_and_one_supported_interface_target_are_displayed(self):
        result = device_result()
        before = copy.deepcopy(result)
        advice = upgrade_parameters(result, component_summary(result))
        self.assertEqual(advice['interface']['label'], 'USB 40 Gbps')
        page = render_hardware_assessment(result, messages('zh-CN'))
        self.assertIn('Fixture drive', page)
        self.assertIn('USB 20 Gbps', page)
        self.assertIn('USB 40 Gbps', page)
        for irrelevant in ('PCIe 1.0', 'USB 80 Gbps', 'SATA 6 Gbps', '接口参考上限'):
            self.assertNotIn(irrelevant, page)
        self.assertEqual(result, before)

    def test_unknown_background_and_identity_keep_facts_but_never_recommend_replacement(self):
        result = device_result()
        result['measurement_quality']['status'] = 'visibility_limited'
        result['hardware']['controller']['model'] = None
        page = render_hardware_assessment(result, messages('zh-CN'))
        self.assertIn('Fixture drive', page)
        self.assertIn('尚未确认', page)
        self.assertIn('暂不推荐更换设备', page)
        self.assertNotIn('建议匹配的参数', page)
        self.assertIsNone(upgrade_parameters(result, component_summary(result)))

    def test_reported_links_stale_references_and_unsupported_host_block_interface_advice(self):
        original = device_result()
        for change in ('reported', 'stale', 'host'):
            result = copy.deepcopy(original)
            if change == 'reported':
                result['hardware']['links'][0]['state'] = 'reported'
            elif change == 'stale':
                result['references']['checked_at'] = '2020-01-01'
            else:
                result['references']['items'][0]['value'] = 10
            with self.subTest(change=change):
                self.assertIsNone(upgrade_parameters(result, component_summary(result)))

    def test_tied_constraints_do_not_suggest_upgrading_one_component(self):
        result = device_result(controller=2_500)
        summary = component_summary(result)
        self.assertEqual(summary['status'], 'bottleneck')
        self.assertIsNone(upgrade_parameters(result, summary))

    def test_another_remaining_link_constraint_blocks_single_segment_upgrade(self):
        result = device_result(measured=900)
        result['hardware']['links'].append(dict(kind='pcie', state='negotiated', gt_s=8, lanes=1,
                                              max_gt_s=16, max_lanes=4))
        summary = component_summary(result)
        self.assertEqual((summary['status'], summary['bottleneck']), ('bottleneck', 'interface'))
        self.assertIsNone(upgrade_parameters(result, summary))

    def test_controller_and_drive_targets_match_remaining_known_capacities(self):
        for component, result in (
                ('controller', device_result(controller=1_000, measured=900)),
                ('drive', device_result(drive=1_000, measured=900))):
            with self.subTest(component=component):
                advice = upgrade_parameters(result, component_summary(result))
                self.assertEqual(advice['component'], component)
                self.assertEqual((advice['read_mb_s'], advice['write_mb_s']), (2_500, 2_500))
                self.assertNotIn('interface', advice)

    def test_candidates_cannot_supply_missing_exact_device_capabilities(self):
        result = device_result()
        result['references']['items'][1]['relationship'] = 'candidate'
        self.assertIsNone(upgrade_parameters(result, component_summary(result)))

    def test_localized_sections_keep_computer_identity_private_and_escape_device_text(self):
        result = device_result()
        result['hardware']['enclosure']['model'] = '<script>unsafe</script>'
        for language in ('en', 'zh-CN', 'ar'):
            text = messages(language)
            page = render_hardware_assessment(result, text)
            with self.subTest(language=language):
                self.assertIn(text['hardware_assessment'], page)
                self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', page)
                self.assertNotIn('<script>unsafe</script>', page)
                self.assertNotIn('Fixture computer', page)
                self.assertNotIn(text['device_upgrade'], page)


if __name__ == '__main__':
    unittest.main()
