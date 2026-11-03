"""Synthetic PCIe-tunnel bindings; no real hardware inventory or benchmark."""
import copy
import json
import plistlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mac_ancestry import mac_access_facts
import hardware


def fixture():
    uid = 0x1234567890ABCDEF
    registry = [{'IORegistryEntryChildren': [
        {'Tunnel Endpoint GUID': uid.to_bytes(8, 'little'),
         'IORegistryEntryChildren': [{'BSD Name': 'disk8'}]}]}]
    device = {'switch_uid_key': f'0x{uid:016x}', 'vendor_name_key': 'ASMedia',
              'device_name_key': '246x', 'mode_key': 'usb_four', 'serial': 'PRIVATE-SERIAL',
              'receptacle_upstream_ambiguous_tag': {'current_speed_key': '40 Gb/s',
                                                 'receptacle_status_key': 'receptacle_connected'}}
    return registry, {'SPThunderboltDataType': [{'_items': [device]}]}


class MacTunnelTests(unittest.TestCase):
    def test_exact_tunnel_identity_projects_series_and_link_without_private_ids(self):
        registry, profile = fixture()
        result = mac_access_facts(registry, ['disk8'], profile)
        self.assertEqual(result['controller']['model'], 'ASMedia 246x')
        self.assertEqual(result['controller']['precision'], 'series')
        self.assertEqual(result['access_interface']['name'], 'USB4 40 Gbps')
        self.assertNotIn('PRIVATE-SERIAL', json.dumps(result))
        self.assertNotIn('1234567890abcdef', json.dumps(result))
        self.assertNotIn('ASM2464PD', json.dumps(result))

    def test_unrelated_duplicate_and_wrong_vendor_or_protocol_do_not_bind(self):
        registry, profile = fixture()
        for change in ('uid', 'duplicate', 'vendor', 'mode', 'missing_profile'):
            current = copy.deepcopy(profile)
            device = current['SPThunderboltDataType'][0]['_items'][0]
            if change == 'uid':
                device['switch_uid_key'] = '0x0000000000000001'
            elif change == 'duplicate':
                current['SPThunderboltDataType'][0]['_items'].append(copy.deepcopy(device))
            elif change == 'vendor':
                device['vendor_name_key'] = 'Another vendor'
            elif change == 'mode':
                device['mode_key'] = 'unknown'
            else:
                current = None
            with self.subTest(change=change):
                self.assertEqual(mac_access_facts(registry, ['disk8'], current), {})

    def test_unitless_or_disconnected_speed_does_not_become_measured_link_rate(self):
        registry, profile = fixture()
        port = profile['SPThunderboltDataType'][0]['_items'][0]['receptacle_upstream_ambiguous_tag']
        port['current_speed_key'] = 40
        self.assertEqual(mac_access_facts(registry, ['disk8'], profile)['access_interface']['name'], 'USB4')
        port.update(current_speed_key='40 Gb/s', receptacle_status_key='disconnected')
        self.assertIsNone(mac_access_facts(registry, ['disk8'], profile)['link']['gbps'])

    def test_collector_uses_the_same_profile_for_target_tunnel_join(self):
        registry, profile = fixture()
        with patch('hardware.platform.system', return_value='Darwin'), patch('hardware.subprocess.check_output',
                side_effect=[json.dumps(profile).encode(), plistlib.dumps(registry)]):
            result = hardware.collect_hardware({'physical_devices': ['disk8']})
        self.assertEqual(result['controller']['model'], 'ASMedia 246x')
        self.assertEqual(result['access_interface']['name'], 'USB4 40 Gbps')

    def test_large_registry_is_processed_with_a_bounded_limit(self):
        registry, profile = fixture()
        class SizedRegistry:
            def __init__(self, size): self.size = size
            def __len__(self): return self.size
        for size, matched in ((22 * 1024 * 1024, True), (65 * 1024 * 1024, False)):
            with self.subTest(size=size), patch('hardware.platform.system', return_value='Darwin'), \
                 patch('hardware.subprocess.check_output', side_effect=[json.dumps(profile).encode(), SizedRegistry(size)]), \
                 patch('hardware.plistlib.loads', return_value=registry):
                result = hardware.collect_hardware({'physical_devices': ['disk8']})
            self.assertEqual(result.get('access_interface', {}).get('name') == 'USB4 40 Gbps', matched)


if __name__ == '__main__':
    unittest.main()
