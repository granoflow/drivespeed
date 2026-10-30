import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import plistlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hardware
from mac_ancestry import mac_access_facts
from community_data import project_result
from test_community import local_result


def tree(disk='disk8'):
    return [{'IORegistryEntryName': 'host', 'IORegistryEntryChildren': [
        {'IOClass': 'FixtureUSB4Bridge', 'IORegistryEntryName': 'ASMedia 246x', 'Link Speed': '40 Gb/s',
         'Serial Number': 'DO-NOT-COPY', 'IORegistryEntryChildren': [{'BSD Name': disk}]}]}]


class MacAncestryTests(unittest.TestCase):
    def test_only_unique_target_ancestry_binds_series_and_external_link(self):
        value = mac_access_facts(tree(), ['disk8'])
        self.assertEqual(value['controller']['model'], 'ASMedia 246x')
        self.assertEqual(value['controller']['precision'], 'series')
        self.assertEqual(value['access_interface']['name'], 'USB4 40 Gbps')
        self.assertNotIn('DO-NOT-COPY', json.dumps(value))
        self.assertNotIn('ASM2464PD', json.dumps(value))
        self.assertEqual(mac_access_facts(tree(), ['disk9']), {})
        self.assertEqual(mac_access_facts(tree(), ['disk8', 'disk9']), {})
        self.assertEqual(mac_access_facts(tree() + tree(), ['disk8']), {})

    def test_connector_label_and_unitless_speed_are_not_protocol_evidence(self):
        value = tree(); bridge = value[0]['IORegistryEntryChildren'][0]
        bridge['Link Speed'] = 40
        self.assertEqual(mac_access_facts(value, ['disk8'])['access_interface']['name'], 'USB4')
        bridge['IOClass'] = 'USBConnector'
        self.assertEqual(mac_access_facts(value, ['disk8']), {})

    def test_collector_and_projection_keep_physical_protocol_and_internal_link(self):
        result = local_result(); result['target'].update(physical_devices=['disk8'], protocol='PCI-Express')
        profile = {'SPHardwareDataType': [], 'SPNVMeDataType': [{'bsd_name': 'disk8', 'spnvme_linkspeed': '16 GT/s', 'spnvme_linkwidth': 'x4'}]}
        with patch('hardware.platform.system', return_value='Darwin'), patch('hardware.subprocess.check_output',
                side_effect=[json.dumps(profile).encode(), plistlib.dumps(tree())]):
            result['hardware'] = hardware.collect_hardware(result['target'])
        result['hardware']['drive'] = {'model':'Fixture SSD', 'matching_model':'Fixture SSD', 'source':'user'}
        payload = project_result(result, 'https://x.com/fixture_user')
        self.assertEqual(payload['storage']['connection'], 'USB4 40 Gbps')
        self.assertEqual(result['target']['protocol'], 'PCI-Express')
        self.assertEqual(result['hardware']['links'][0]['kind'], 'pcie')
        self.assertEqual(result['hardware']['controller']['model'], 'ASMedia 246x')
        self.assertEqual(payload['assessment']['links'][-1]['source'], 'system')
