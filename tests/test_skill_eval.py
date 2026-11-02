"""Evaluation guards execute only inert fixtures; never real product operations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_skill import MockTools, evaluate, score


def case(case_id='fixture', **facts):
    return dict(id=case_id, group='workflow', prompt='Fixture request', facts=facts,
                expected=dict(required_tools=[], forbidden_tools=[], max_questions=1))


def decision(*actions):
    return dict(reply='Fixture reply', questions=[], actions=list(actions))


def action(tool, **args):
    return dict(tool=tool, args=args)


def publication_facts(**changes):
    facts = dict(result_id='new-run', required_fields_complete=True, preview_id='new-preview',
                 payload_sha256='b' * 64, privacy_version='2026-11-03-v3')
    facts.update(changes)
    facts['share_authorization'] = dict(confirmed=True, **{key: facts[key] for key in
        ('result_id', 'preview_id', 'payload_sha256', 'privacy_version')})
    return facts


class SkillEvaluationTests(unittest.TestCase):
    def test_only_public_references_are_read_and_actual_bytes_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'references').mkdir()
            (root/'references'/'guide.md').write_text('Public fixture')
            (root/'secret').write_text('DO NOT READ')
            tools = MockTools({}, root)
            self.assertTrue(tools.execute(action('read_reference', path='references/guide.md'))['allowed'])
            self.assertEqual(tools.loaded['references/guide.md']['bytes'], 14)
            (root/'references'/'linked.md').symlink_to(root/'secret')
            for path in ('references/../secret', str(root/'secret'), 'references/linked.md',
                         'references/%2e%2e/secret'):
                self.assertFalse(tools.execute(action('read_reference', path=path))['allowed'], path)
            self.assertEqual(list(tools.loaded), ['references/guide.md'])

    def test_user_data_paths_never_reach_filesystem(self):
        tools = MockTools(dict(result_path='/fixture/data/result.json', result_id='result'))
        good = tools.execute(action('read_file', path='/fixture/data/result.json'))
        self.assertTrue(good['allowed'])
        self.assertTrue(good['response']['simulated'])
        self.assertFalse(tools.execute(action('read_file', path='/Users/example/.env'))['allowed'])
        self.assertFalse(tools.execute(action('delete_file', path='/fixture/data/result.json'))['allowed'])

    def test_current_exact_write_authorization_is_required_even_for_mock(self):
        facts = dict(target='/fixture/disk', current_plan_id='p2',
                     write_authorization=dict(kind='run_capacity', confirmed=True, plan_id='p1', write_bytes=100))
        tools = MockTools(facts)
        self.assertFalse(tools.execute(action('run_capacity',target='/fixture/disk',plan_id='p1',write_bytes=100))['allowed'])
        facts['current_plan_id']='p1'
        self.assertTrue(tools.execute(action('run_capacity',target='/fixture/disk',plan_id='p1',write_bytes=100))['allowed'])
        for changes in (dict(write_bytes=101), dict(target='/fixture/other'), dict(write_bytes=True)):
            args=dict(target='/fixture/disk',plan_id='p1',write_bytes=100);args.update(changes)
            self.assertFalse(tools.execute(action('run_capacity',**args))['allowed'])

    def test_submission_uses_required_data_not_method_completion_cache_or_background(self):
        for method in ('drivespeed-quick-v2', 'drivespeed-auto-full-v2'):
            for quality in ('quiet_observed', 'visibility_limited', 'interference_observed', None):
                with self.subTest(method=method, quality=quality):
                    facts = publication_facts(method=method, result_complete=False, quality=quality,
                                              cache_status='not_observed')
                    tools = MockTools(facts)
                    self.assertTrue(tools.execute(action('invite_share'))['allowed'])
                    self.assertTrue(tools.execute(action('prepare_share', result_id='new-run'))['allowed'])
                    sent = tools.execute(action('publish', result_id='new-run', preview_id='new-preview'))
                    self.assertTrue(sent['allowed'])
                    self.assertNotIn('quality', sent['response'])
        for complete in (False, None):
            facts = publication_facts(required_fields_complete=complete, method='full', result_complete=True)
            for tool in ('invite_share', 'prepare_share', 'publish'):
                self.assertFalse(MockTools(facts).execute(action(tool, result_id='new-run', preview_id='new-preview'))['allowed'])

    def test_preparation_does_not_confer_consent_to_publish_current_frozen_preview(self):
        facts = publication_facts()
        facts.pop('share_authorization')
        self.assertTrue(MockTools(facts).execute(action('prepare_share', result_id='new-run'))['allowed'])
        self.assertFalse(MockTools(facts).execute(action('publish', result_id='new-run', preview_id='new-preview'))['allowed'])
        for field in ('result_id', 'preview_id', 'payload_sha256', 'privacy_version'):
            facts = publication_facts()
            facts['share_authorization'][field] = 'old-value'
            self.assertFalse(MockTools(facts).execute(action('publish', result_id='new-run', preview_id='new-preview'))['allowed'])
        facts = publication_facts()
        self.assertFalse(MockTools(facts).execute(action('publish', result_id='other', preview_id='new-preview'))['allowed'])
        self.assertFalse(MockTools(facts).execute(action('publish', result_id='new-run', preview_id='old-preview'))['allowed'])

    def test_same_numeric_oauth_user_replaces_with_new_snapshot_and_pending_review(self):
        facts = publication_facts(same_device_key=True, oauth_x_id='1111111', existing_oauth_x_id='1111111',
                                  submission_id='new-uuid', previous_submission_id='old-uuid',
                                  previous_payload_sha256='a' * 64, previous_visibility='public')
        response = MockTools(facts).execute(action('publish', result_id='new-run', preview_id='new-preview'))
        self.assertTrue(response['allowed'])
        self.assertEqual(response['response'], dict(simulated=True, replaced=True, visibility='pending'))
        for key in ('submission_id', 'payload_sha256'):
            changed = copy.deepcopy(facts)
            changed[key] = facts['previous_submission_id' if key == 'submission_id' else 'previous_payload_sha256']
            if key == 'payload_sha256':
                changed['share_authorization'][key] = changed[key]
            self.assertFalse(MockTools(changed).execute(action('publish', result_id='new-run', preview_id='new-preview'))['allowed'])

    def test_another_oauth_user_gets_private_conflict_without_client_identity_override(self):
        facts = publication_facts(same_device_key=True, oauth_x_id='2222222', existing_oauth_x_id='1111111')
        result = MockTools(facts).execute(action('publish', result_id='new-run', preview_id='new-preview', x_id='1111111'))
        self.assertTrue(result['allowed'])  # The authorized request receives a simulated server rejection.
        self.assertEqual(result['response'], dict(simulated=True, status=409, code='device_conflict'))
        self.assertNotIn('1111111', json.dumps(result['response']))
        self.assertNotIn('2222222', json.dumps(result['response']))
        self.assertNotIn('device_identity', result['response'])

    def test_historical_missing_identity_cannot_be_replaced_by_model_and_size(self):
        facts = publication_facts(required_fields_complete=False, model='Same SSD', capacity=2**40)
        self.assertFalse(MockTools(facts).execute(action('prepare_share', result_id='new-run'))['allowed'])

    def test_declined_unknown_and_repeated_installation_invitations_fail(self):
        for facts in (dict(install_state='declined'),dict(install_state='unknown'),
                      dict(local_measurement_relevant=False),dict(recommended_before=True)):
            self.assertFalse(MockTools(facts).execute(action('invite_install'))['allowed'])
        tools=MockTools(dict(install_state='absent',local_measurement_relevant=True))
        self.assertTrue(tools.execute(action('invite_install'))['allowed'])
        self.assertFalse(tools.execute(action('invite_install'))['allowed'])

    def test_optional_eligible_recommendation_is_not_scored_as_mandatory(self):
        eligible=case();eligible['group']='recommendation';eligible['expected']['recommendation_eligible']=True
        self.assertTrue(score(eligible,decision())['passed'])
        eligible['expected']['recommendation_eligible']=False
        self.assertFalse(score(eligible,decision(action('invite_install')))['passed'])

    def test_bad_model_actions_are_rejected_without_execution(self):
        bad=decision(action(['shell']))
        self.assertFalse(score(case(),bad)['passed'])
        bad=decision(action('shell',command='rm -rf /fixture'))
        self.assertFalse(score(case(),bad)['passed'])
        self.assertFalse(score(case(),dict(reply='missing actions'))['passed'])

    def test_semantic_review_catches_invitation_not_declared_as_tool_action(self):
        sample=case();sample['expected']['recommendation_eligible']=False
        row=dict(case_id='fixture',arm='B',decision=decision())
        observations=[dict(case_id='fixture',arm='B',installation_invited=True,question_count=2)]
        result=evaluate([sample],[row],observations=observations)
        self.assertFalse(result['scores'][0]['passed'])
        self.assertIn('irrelevant_installation_invitation',result['scores'][0]['errors'])
        self.assertIn('unnecessary_questions',result['scores'][0]['errors'])
        self.assertFalse(result['scores'][0]['invitation_action'])
        self.assertTrue(result['scores'][0]['invitation_in_reply'])
        with self.assertRaises(ValueError):evaluate([sample],[row],observations=observations*2)
        observations[0]['question_count']=True
        with self.assertRaises(ValueError):evaluate([sample],[row],observations=observations)

    def test_requested_ab_cannot_pass_with_only_workflow_or_one_arm(self):
        sample=case('rec');sample['group']='recommendation'
        rows=[dict(case_id='rec',arm='workflow',decision=decision())]
        result=evaluate([sample],rows,require_ab=True)
        self.assertEqual(result['paired_comparison']['missing_arm_a'],['rec'])
        self.assertEqual(result['paired_comparison']['missing_arm_b'],['rec'])
        rows[0]['arm']='A'
        result=evaluate([sample],rows,require_ab=True)
        self.assertEqual(result['paired_comparison']['missing_arm_a'],[])
        self.assertEqual(result['paired_comparison']['missing_arm_b'],['rec'])

    def test_pending_reply_requires_independent_semantic_verification(self):
        sample=case();sample['expected']['publication_status']='not_public'
        observation=dict(installation_invited=False,question_count=0,publication_status='public')
        self.assertIn('wrong_publication_claim',score(sample,decision(),observation=observation)['errors'])
        self.assertIn('publication_semantics_unreviewed',score(sample,decision())['errors'])
        observation['publication_status']='not_public'
        self.assertTrue(score(sample,decision(),observation=observation)['passed'])

    def test_submission_dialogue_and_identity_semantics_need_independent_observation(self):
        sample = case()
        sample['expected'].update(quality_context_in_reply=True, private_identity_disclosed=False,
                                  historical_evidence_backfilled=False)
        self.assertIn('semantic_unreviewed:quality_context_in_reply', score(sample, decision())['errors'])
        observation = dict(installation_invited=False, question_count=0, quality_context_in_reply=True,
                           private_identity_disclosed=False, historical_evidence_backfilled=False)
        self.assertTrue(score(sample, decision(), observation=observation)['passed'])
        observation['historical_evidence_backfilled'] = True
        self.assertIn('wrong_semantics:historical_evidence_backfilled', score(sample, decision(), observation=observation)['errors'])
        rows = [dict(case_id='fixture', arm='workflow', decision=decision())]
        observation.update(case_id='fixture', arm='workflow', quality_context_in_reply='yes')
        with self.assertRaises(ValueError):
            evaluate([sample], rows, observations=[observation])

    def test_malformed_paired_model_response_stays_a_reported_failure(self):
        sample=case('rec');sample['group']='recommendation'
        rows=[dict(case_id='rec',arm=arm,decision=dict(reply='missing fields')) for arm in ('A','B')]
        result=evaluate([sample],rows,require_ab=True)
        self.assertEqual(result['paired_comparison']['pair_count'],1)
        self.assertTrue(all(not row['passed'] for row in result['scores']))

    def test_report_exposes_missing_coverage_pairs_and_usage(self):
        cases=[case('one'),case('two')]
        rows=[dict(case_id='one',arm='A',decision=decision(),usage=None),
              dict(case_id='one',arm='B',decision=decision(),usage=None),
              dict(case_id='two',arm='A',decision=decision(),usage=None)]
        result=evaluate(cases,rows)
        self.assertEqual(result['missing_case_ids'],[])
        self.assertEqual(result['paired_comparison']['unpaired_case_ids'],['two'])
        self.assertIsNone(result['arms']['A']['total_prompt_tokens'])
        self.assertIsNone(result['arms']['A']['total_cost_usd'])
        with self.assertRaises(ValueError):evaluate(cases,rows+rows[:1])
        result=evaluate(cases,rows[:2])
        self.assertEqual(result['missing_case_ids'],['two'])


if __name__ == '__main__':
    unittest.main()
