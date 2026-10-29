"""Public evidence projection and frozen typed snapshots; fictional devices only."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).resolve().parent)]
import community_data as data
import community_assessment as assessment
from community_store import CommunityStore
from performance_rating import comparison_context
from test_community import local_result, ID


def assessed_result():
    result=local_result()
    result['hardware']['controller']=dict(model='ASM2362',source='sysfs:target-usb-label')
    result['hardware']['links']=[dict(kind='usb',state='negotiated',source='sysfs:target-ancestor',gbps=10,reported_name='PRIVATE-SERIAL',idVendor='PRIVATE-ID')]
    for case in result['cases']:case['metrics_complete']=True
    case=result['cases'][0]
    result['references']=dict(for_drive_model='Fixture SSD',checked_at='2026-10-29',items=[dict(component='drive',model='Fixture SSD',metric='seq_read',value=1.05,unit='MB/s',source_kind='review',relationship='device',source_url='https://example.com/fixture',source_label='Test evidence',conditions='Synthetic method',comparison=comparison_context(result,case),rating_bands=dict(thresholds_mb_s=[.5,.7,.9,1.0],upper_bound_mb_s=1.1,rationale='Synthetic test boundaries'))])
    return result


class CommunityAssessmentTests(unittest.TestCase):
    def project(self,result=None):return data.project_result(result or assessed_result(),'https://x.com/fixture_user',submission_id=ID)

    def test_projection_contains_only_public_evidence_and_never_a_claimed_level(self):
        result=assessed_result();before=copy.deepcopy(result);payload=self.project(result)
        self.assertEqual(result,before)
        self.assertNotIn('PRIVATE',json.dumps(payload))
        self.assertNotIn('level',json.dumps(payload['assessment']))
        ref=payload['assessment']['references'][0]
        self.assertEqual(ref['context_sha256'],assessment.condition_digest(payload,ref['metric']))
        self.assertEqual(payload['hardware']['controller']['model'],'ASM2362')
        self.assertNotIn('assessment',self.project(local_result()))

    def test_random_rating_evidence_is_projected_and_stored(self):
        result=assessed_result();original=result['references']['items'][0]
        for metric in ('rand_read','rand_write'):
            case=next(c for c in result['cases'] if c['name']==metric)
            ref=copy.deepcopy(original);ref.update(metric=metric,comparison=comparison_context(result,case));result['references']['items'].append(ref)
        p=self.project(result)
        self.assertEqual({r['metric'] for r in p['assessment']['references']},{'seq_read','rand_read','rand_write'})
        with tempfile.TemporaryDirectory() as tmp,CommunityStore(tmp) as store:
            store.create(p,'random-run','/fictional/result')
            self.assertEqual(store.payload(ID),data.normalise_payload(p))

    def test_hash_binds_conditions_but_not_attribution_or_transport_id(self):
        p=self.project();h=assessment.condition_digest(p,'seq_read')
        for update in [lambda x:x['hardware']['drive'].update(model='Different'),lambda x:x['plan'].update(file_bytes=2097152),lambda x:x['cases'][0].update(qd=16),lambda x:x['assessment']['links'][0].update(gbps=5)]:
            other=copy.deepcopy(p);update(other);self.assertNotEqual(assessment.condition_digest(other,'seq_read'),h)
        p['attribution']['intro']='Changed introduction';p['submission_id']='other'
        self.assertEqual(assessment.condition_digest(p,'seq_read'),h)

    def test_frozen_projection_does_not_expire_as_calendar_advances(self):
        result=assessed_result()
        with patch('performance_rating.date') as clock:
            clock.today.return_value=date(2030,1,1);clock.fromisoformat=date.fromisoformat
            self.assertEqual(self.project(result),self.project(result))
            self.assertEqual(len(self.project(result)['assessment']['references']),1)

    def test_owned_and_remote_samples_roundtrip_without_json_blobs(self):
        p=self.project()
        with tempfile.TemporaryDirectory() as tmp,CommunityStore(tmp) as store:
            store.create(p,'run','/fictional/result')
            self.assertEqual(store.payload(ID),data.normalise_payload(p))
            import community_samples
            remote=copy.deepcopy(p);remote['submission_id']='22222222-2222-4222-8222-222222222222'
            item=dict(record=dict(id=remote['submission_id'],url='https://example.com/report',created_at='2026-10-29T00:00:00Z',verification='unverified',submission=remote),matched_on=['drive'],differences=[])
            with store._transaction(write=True):community_samples.replace(store.db,ID,[item])
            self.assertEqual(community_samples.read(store.db,ID)[0]['record']['submission'],data.normalise_payload(remote))
            with store._transaction(write=True):community_samples.replace(store.db,ID,[])
            self.assertEqual(store.db.execute('SELECT count(*) FROM assessment_presence').fetchone()[0],1)
            self.assertNotIn('payload_json',store.db.execute("SELECT sql FROM sqlite_master WHERE name='assessment_references'").fetchone()[0])
            store.db.execute('UPDATE assessment_links SET gbps=5 WHERE owner_id=?',(ID,));store.db.commit()
            with self.assertRaises(data.CommunityError):store.payload(ID)

    def test_component_caps_are_official_facts_and_roundtrip_without_backfill(self):
        result=assessed_result();item=copy.deepcopy(result['references']['items'][0])
        item.pop('comparison');item.pop('rating_bands');item.update(source_kind='official',metric='seq_read_q',value=7000)
        result['references']['items'].append(item)
        p=self.project(result)
        self.assertEqual({c['component'] for c in p['assessment']['capabilities']},{'controller','drive'})
        self.assertNotIn('level',json.dumps(p['assessment']))
        old=copy.deepcopy(p);old['assessment'].pop('capabilities')
        with tempfile.TemporaryDirectory() as tmp,CommunityStore(tmp) as store:
            store.create(p,'new-run','/fictional/new')
            self.assertEqual(store.payload(ID),data.normalise_payload(p))
            self.assertNotIn('payload_json',store.db.execute("SELECT sql FROM sqlite_master WHERE name='assessment_capabilities'").fetchone()[0])
        with tempfile.TemporaryDirectory() as tmp,CommunityStore(tmp) as store:
            store.create(old,'old-run','/fictional/old')
            self.assertEqual(store.payload(ID),data.normalise_payload(old))
            self.assertNotIn('capabilities',store.payload(ID)['assessment'])
        for update in [lambda x:x['assessment']['capabilities'][0].update(level=5),
                       lambda x:x['assessment']['capabilities'][0].update(model='Different'),
                       lambda x:x['assessment']['capabilities'].append(copy.deepcopy(x['assessment']['capabilities'][0]))]:
            altered=copy.deepcopy(p);update(altered)
            with self.assertRaises(data.CommunityError):data.validate_payload(altered)

    def test_malformed_reference_and_claimed_level_are_rejected(self):
        for update in [lambda a:a.update(level=5),lambda a:a['references'][0].update(source_url='https://user:secret@example.com'),lambda a:a['references'][0].update(thresholds_mb_s=[1,1,2,3])]:
            p=self.project();update(p['assessment'])
            with self.assertRaises(data.CommunityError):data.validate_payload(p)


if __name__=='__main__':unittest.main()
