"""Synthetic hardware trees and measurements; no real drive benchmark."""
import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hardware
import performance_rating as rating
import reference_data
import report
import benchmark
from progress import messages


class HardwareRatingTests(unittest.TestCase):
    def fixture(self):
        case = dict(name='seq_read', status='completed', metrics_complete=True, rw='read', bs=1048576, qd=1,
                    runtime=30, io_bytes=8388608, metrics={'read': {'mb_s': 850}})
        result = dict(hardware={kind: {'model': name} for kind, name in [('computer', 'Test computer'), ('drive', 'Test SSD 2TB'), ('controller', 'ASM2362'), ('enclosure', None)]},
                      target=dict(scope='physical', filesystem='ext4', total_bytes=2000000000000, free_bytes=1000000000000),
                      tool={'method_version': 'drivespeed-file-v1'}, environment=dict(os_family='linux', os_version='test', engine='libaio'),
                      plan=dict(profile='full', file_bytes=8388608), cases=[case])
        result['hardware']['links'] = [dict(kind='usb', state='negotiated', gbps=10)]
        item = dict(component='drive', model='Test SSD 2TB', metric='seq_read', value=900, unit='MB/s', source_kind='review',
                    source_url='https://example.com/synthetic-review', source_label='Synthetic test fixture', conditions='Synthetic conditions only', relationship='device',
                    comparison=rating.comparison_context(result, case),
                    rating_bands=dict(thresholds_mb_s=[500,700,800,900], upper_bound_mb_s=1100, rationale='Synthetic boundaries for tests; not production thresholds'))
        result['references'] = dict(for_drive_model='Test SSD 2TB', checked_at=date.today().isoformat(), items=[item])
        return result

    def test_all_five_bands_and_outside_range_abstention(self):
        result = self.fixture()
        for value, score in [(400,1),(500,2),(700,3),(800,4),(900,5),(1100,5),(1101,None)]:
            result['cases'][0]['metrics']['read']['mb_s'] = value
            self.assertEqual(rating.performance_ratings(result)[0]['level'], score)
        self.assertEqual(rating.performance_ratings(result)[0]['reason'], 'above_reference_range')

    def test_no_score_from_peak_candidates_wrong_method_or_stale_reference(self):
        for mutate in [lambda r: r['references']['items'][0].pop('rating_bands'),
                       lambda r: r['references']['items'][0].update(relationship='candidate'),
                       lambda r: r['cases'][0].update(qd=16),
                       lambda r: r['target'].update(total_bytes=1000000000000),
                       lambda r: r['hardware']['links'][0].update(gbps=5),
                       lambda r: r['references'].update(checked_at='2000-01-01'),
                       lambda r: r['references'].update(for_drive_model='Different drive'),
                       lambda r: r['cases'][0].update(status='failed'),
                       lambda r: r['hardware']['controller'].update(model=None)]:
            result = self.fixture(); mutate(result)
            # Old reference peaks remain valid: remove both new optional fields.
            if 'rating_bands' not in result['references']['items'][0]:
                result['references']['items'][0].pop('comparison')
            self.assertIsNone(rating.performance_ratings(result)[0]['level'])

    def test_bands_need_complete_conditions_and_valid_sorted_cutoffs(self):
        for change in [lambda i: i['rating_bands'].update(thresholds_mb_s=[1,1,3,4]),
                       lambda i: i['rating_bands'].update(upper_bound_mb_s=1),
                       lambda i: i['comparison'].update(private_path='/secret'),
                       lambda i: i['comparison'].update(qd=True),
                       lambda i: i['comparison']['models'].update(serial='secret')]:
            result = self.fixture(); change(result['references']['items'][0])
            with self.assertRaises(ValueError): reference_data.validate_reference_data(result['references'])

    def test_above_known_link_bound_is_not_rewarded(self):
        result = self.fixture()
        result['cases'][0]['metrics']['read']['mb_s'] = 1400
        self.assertEqual(rating.performance_ratings(result)[0]['reason'], 'above_link_bound')

    def test_random_scores_use_4k_qd1_and_the_correct_direction(self):
        for metric,direction,rw in [('rand_read','read','randread'),('rand_write','write','randwrite')]:
            result=self.fixture();case=result['cases'][0]
            case.update(name=metric,rw=rw,bs=4096)
            case['metrics']={'read':{'mb_s':999},'write':{'mb_s':999}};case['metrics'][direction]['mb_s']=85
            ref=result['references']['items'][0];ref.update(metric=metric,value=90,comparison=rating.comparison_context(result,case),rating_bands=dict(thresholds_mb_s=[50,70,80,90],upper_bound_mb_s=110,rationale='Synthetic random I/O thresholds'))
            row=next(x for x in rating.performance_ratings(result) if x['metric']==metric)
            self.assertEqual((row['measured'],row['level']),(85,4))
            for field,value in [('bs',1048576),('qd',16)]:
                other=copy.deepcopy(result);other['cases'][0][field]=value
                row=next(x for x in rating.performance_ratings(other) if x['metric']==metric)
                self.assertIsNone(row['level'])
                self.assertEqual(row['reason'],'conditions_unknown')

    def test_interface_encoding_is_not_a_rating(self):
        pcie = rating.link_reference(dict(kind='pcie', gt_s=16, lanes=4))
        self.assertAlmostEqual(pcie['mb_s'], 7876.923, places=2)
        self.assertEqual(rating.link_reference(dict(kind='usb', gbps=40))['mb_s'], 5000)
        self.assertEqual(rating.link_reference(dict(kind='sata', gbps=6))['mb_s'], 600)
        self.assertIsNone(rating.link_reference(dict(kind='pcie', gt_s=64, lanes=4)))
        self.assertIsNone(rating.link_reference(dict(kind='pcie', gt_s=16, lanes=3)))
        self.assertTrue(all(row['level'] is None for row in rating.performance_ratings({'cases': []})))

    def test_mac_links_and_controller_are_target_bound(self):
        profile={'SPNVMeDataType':[dict(link_speed='16.0 GT/s', link_width='x4', _items=[dict(bsd_name='disk0',device_model='SSD')])],
                 'SPUSBDataType':[dict(_name='ASM2362',_items=[dict(bsd_name='disk6')]),dict(_name='ASM2464PD',_items=[dict(bsd_name='disk8')])]}
        self.assertEqual(hardware.mac_hardware(profile,['disk0'])['links'][0]['lanes'],4)
        self.assertEqual(hardware.mac_hardware(profile,['disk6'])['controller']['model'],'ASM2362')
        self.assertIsNone(hardware.mac_hardware(profile,['disk9'])['controller']['model'])
        self.assertEqual(hardware.mac_hardware(profile,['disk6'])['links'],[])
        self.assertIsNone(hardware.controller_from_label('ASM2464PDX'))

    def test_linux_target_ancestry_not_an_unrelated_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); usb=root/'devices/pci0000:00/0000:00:01.0/usb1/1-2'; disk=usb/'host0/target0/disk'
            disk.mkdir(parents=True); cls=root/'class/block/sda';cls.mkdir(parents=True);(cls/'device').symlink_to(disk)
            for key,value in [('idVendor','174c'),('idProduct','2362'),('speed','10000'),('product','ASM2362')]: (usb/key).write_text(value)
            pci=usb.parents[1]
            for key,value in [('current_link_speed','8.0 GT/s PCIe'),('current_link_width','1')]: (pci/key).write_text(value)
            links=hardware.linux_links(['sda'],root)
            self.assertEqual(links[0]['gbps'],10)
            self.assertEqual(links[0]['reported_name'],'ASM2362')
            self.assertEqual(links[1]['gt_s'],8)
            self.assertNotIn('serial',json.dumps(links))
            self.assertEqual(hardware.linux_links(['sda','sdb'],root),[])

    def test_official_enclosure_mapping_fills_only_unknown_controller(self):
        result=self.fixture(); hw=result['hardware']; hw['controller']={'model':None};hw['enclosure']={'model':'Branded box'}
        refs=result['references'];refs['hardware_specs']=[dict(component='controller',model='ASM2362',interface='USB 10 Gbps',source_url='https://example.com/product',source_label='Fixture official spec',conditions='Exact revision',enclosure_model='Branded box')]
        reference_data.apply_hardware_specs(hw,refs)
        self.assertEqual(hw['controller']['model'],'ASM2362');self.assertEqual(hw['controller']['source'],'reference')
        hw['controller']['model']='Existing chip';reference_data.apply_hardware_specs(hw,refs)
        self.assertEqual(hw['controller']['model'],'Existing chip')

    def test_reference_cache_preserves_rating_conditions(self):
        import os
        result=self.fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'XDG_CACHE_HOME':tmp}):
            reference_data.cache_reference_data(result['references'],result['hardware'])
            saved,_=reference_data.load_reference_cache(result['hardware'])
            self.assertEqual(saved['items'][0]['comparison'],result['references']['items'][0]['comparison'])
            result['references']=saved
            self.assertEqual(rating.performance_ratings(result)[0]['level'],4)

    def test_researched_controller_does_not_prevent_cache_discovery_next_run(self):
        import os
        result = self.fixture();hw=result['hardware'];hw['enclosure']['model']='Branded box';hw['controller']={'model':None}
        refs=result['references'];refs['hardware_specs']=[dict(component='controller',model='ASM2362',interface='USB 10 Gbps',source_url='https://example.com/product',source_label='Fixture',conditions='Exact revision',enclosure_model='Branded box')]
        unresolved=copy.deepcopy(hw)
        reference_data.apply_hardware_specs(hw,refs)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'XDG_CACHE_HOME':tmp}):
            reference_data.cache_reference_data(refs,hw)
            saved,info=reference_data.load_reference_cache(unresolved)
            self.assertEqual(info['status'],'fresh')
            reference_data.apply_hardware_specs(unresolved,saved)
            self.assertEqual(unresolved['controller']['model'],'ASM2362')

    def test_localized_render_escapes_sources_and_does_not_claim_authenticity(self):
        result=self.fixture();ref=result['references']['items'][0];ref.pop('rating_bands');ref.pop('comparison');ref.update(source_kind='official',conditions='<script>unsafe</script>')
        for lang in ('en','zh-CN','ar'):
            html=report.render_hardware_assessment(result,messages(lang))
            self.assertNotIn('4/5',html);self.assertIn('&lt;script&gt;',html);self.assertNotIn('<script>unsafe',html)
            self.assertIn('https://www.asmedia.com.tw/',html)
        source=report.render_hardware_assessment({},messages('zh-CN'))
        self.assertNotIn('无法评分',source);self.assertIn('测量背景未记录或存在干扰',source)
        self.assertNotIn('class="performance-ratings"',source)

    def test_windows_model_contains_no_unique_identifiers(self):
        raw=json.dumps({'Manufacturer':'Vendor','Model':'Model','SerialNumber':'SECRET'}).encode()
        with patch.object(hardware.subprocess,'check_output',return_value=raw):
            result=hardware.windows_computer()
        self.assertEqual(result['model'],'Vendor · Model');self.assertNotIn('SECRET',json.dumps(result))

    def test_dry_run_applies_researched_enclosure_without_test_writes(self):
        result=self.fixture();hw=result['hardware'];hw['enclosure']['model']='Branded box';hw['controller']={'model':None}
        refs=result['references'];refs['hardware_specs']=[dict(component='controller',model='ASM2362',interface='USB 10 Gbps',source_url='https://example.com/product',source_label='Fixture',conditions='Exact revision',enclosure_model='Branded box')]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'references.json';path.write_text(json.dumps(refs))
            args=benchmark.parser().parse_args(['--target',tmp,'--reference-file',str(path),'--dry-run'])
            output=io.StringIO()
            result['target'].update(path=tmp,st_dev=1,inode=2,device='fixture-volume',physical_devices=['fixture-disk'])
            with patch.object(benchmark,'target_info',return_value=result['target']), patch.object(benchmark,'collect_hardware',return_value=hw), patch.object(benchmark,'load_reference_cache',return_value=(None,{})), patch.object(benchmark.WriteAllowance,'read',return_value=dict(present=False,scope_id=None,revision=0,run_cap=64*1024**3,remaining=64*1024**3)), patch.object(benchmark,'ensure_fio') as install, contextlib.redirect_stdout(output):
                self.assertEqual(benchmark.execute(args,messages('en'),messages('en')),0)
            install.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['hardware']['controller']['source'],'reference')


if __name__ == '__main__': unittest.main()
