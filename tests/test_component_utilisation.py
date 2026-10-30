"""Synthetic component capacities, not hardware performance acceptance."""
import copy
import sys
import unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
from performance_rating import utilisation_summary, component_summary, component_capabilities
import test_hardware_rating as hardware_tests


def caps():
    return [dict(component='controller',read_mb_s=3200,write_mb_s=3000),
            dict(component='drive',read_mb_s=14000,write_mb_s=12000)]


class ComponentUtilisationTests(unittest.TestCase):
    def test_three_axes_explain_a_healthy_but_limited_drive(self):
        result=utilisation_summary(dict(read=2200,write=2000),caps(),[2500])
        self.assertEqual([r['level'] for r in result['rows']],[4,5,1])
        self.assertEqual((result['status'],result['bottleneck']),('bottleneck','interface'))
        self.assertAlmostEqual(result['rows'][2]['ratio'],(2200/14000+2000/12000)/2)

    def test_display_boundaries_are_project_utilisation_bands(self):
        for ratio,level in [(0.01,1),(.1999,1),(.2,2),(.4,3),(.6,4),(.8,5),(1.01,5)]:
            result=utilisation_summary(dict(read=1000*ratio,write=1000*ratio),[dict(component='drive',read_mb_s=1000,write_mb_s=1000)],[])
            self.assertEqual(result['rows'][2]['level'],level)

    def test_high_low_balanced_and_incomplete_are_distinct(self):
        same=[dict(component=c,read_mb_s=1000,write_mb_s=1000) for c in ('drive','controller')]
        for value,status in [(900,'balanced'),(1200,'high'),(300,'low')]:
            self.assertEqual(utilisation_summary(dict(read=value,write=value),same,[1000])['status'],status)
        result=utilisation_summary(dict(read=900),same,[1000])
        self.assertEqual(result['status'],'incomplete')
        self.assertTrue(all(r['level']==5 for r in result['rows']))
        self.assertTrue(all(r['level'] is None for r in utilisation_summary(dict(read=900),same,[1000],False)['rows']))

    def test_ties_are_deterministic_and_opposite_direction_limits_do_not_blame_one_component(self):
        tied=[dict(component='controller',read_mb_s=1250,write_mb_s=1250),dict(component='drive',read_mb_s=7000,write_mb_s=6000)]
        result=utilisation_summary(dict(read=1100,write=1050),tied,[1250])
        self.assertEqual((result['status'],result['bottleneck']),('bottleneck','interface'))
        crossed=[dict(component='controller',read_mb_s=1000,write_mb_s=5000),dict(component='drive',read_mb_s=5000,write_mb_s=1000)]
        result=utilisation_summary(dict(read=900,write=900),crossed,[10000])
        self.assertEqual(result['status'],'normal')

    def test_official_exact_capability_and_valid_workload_required(self):
        fixture=hardware_tests.HardwareRatingTests().fixture();fixture['cases']=[]
        fixture['measurement_quality']={'status':'quiet_observed'}
        for direction,value in [('read',2200),('write',2000)]:
            fixture['cases'].append(dict(name='seq_'+direction+'_q',rw=direction,qd=16,bs=1048576,status='completed',metrics_complete=True,metrics={direction:dict(mb_s=value)}))
        ref=fixture['references']['items'][0]
        ref.pop('comparison');ref.pop('rating_bands');ref.update(source_kind='official',metric='seq_read_q',value=14000)
        second=copy.deepcopy(ref);second.update(metric='seq_write_q',value=12000);fixture['references']['items'].append(second)
        self.assertEqual(component_summary(fixture)['rows'][2]['level'],1)
        for change in [lambda r:r['references']['items'][0].update(relationship='candidate'),
                       lambda r:r['references']['items'][0].update(model='Wrong SSD'),
                       lambda r:r['references']['items'][0].update(source_kind='review')]:
            altered=copy.deepcopy(fixture);change(altered)
            self.assertEqual(len(component_summary(altered)['rows'][2]['directions']),1)
        other=copy.deepcopy(fixture);other['references']['items'].append({**ref,'value':7000})
        self.assertEqual(len(component_summary(other)['rows'][2]['directions']),1)
        fixture['cases'][0]['qd']=1;fixture['cases'][1]['metrics_complete']=False
        self.assertTrue(all(r['level'] is None for r in component_summary(fixture)['rows']))


if __name__=='__main__':unittest.main()
