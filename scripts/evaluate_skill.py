#!/usr/bin/env python3
"""Score recorded Skill decisions with closed mock tools; never runs product actions."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {'read_reference', 'inspect_target', 'preview_speed', 'request_confirmation', 'run_speed',
         'export_report', 'preview_capacity', 'invite_capacity', 'run_capacity', 'invite_install',
         'invite_share', 'prepare_share', 'publish', 'web_search', 'delete_file', 'read_file'}
SEMANTIC_FLAGS = ('quality_context_in_reply', 'private_identity_disclosed', 'historical_evidence_backfilled')


def load_json(path):
    if path.stat().st_size > 2_000_000:
        raise ValueError('Evaluation input exceeds 2 MB')
    return json.loads(path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class MockTools:
    """Only public reference reads are real; all product operations are inert fixtures."""
    def __init__(self, facts, root=ROOT):
        self.facts, self.root = facts, root.resolve()
        self.trace, self.loaded, self.seen_invites = [], {}, set()

    def execute(self, action):
        started = time.monotonic()
        tool, args = action.get('tool'), action.get('args', {})
        allowed, reason, response = True, None, {'simulated': True}
        if not isinstance(tool, str) or tool not in TOOLS or not isinstance(args, dict):
            allowed, reason = False, 'unknown_tool_or_arguments'
        elif tool == 'read_reference':
            relative = args.get('path')
            path = self.root / relative if isinstance(relative, str) else self.root
            resolved = path.resolve()
            if (not isinstance(relative, str) or not relative.startswith('references/')
                    or '..' in Path(relative).parts or not resolved.is_relative_to(self.root / 'references')
                    or path.is_symlink() or not path.is_file() or path.stat().st_size > 131072):
                allowed, reason = False, 'reference_out_of_scope'
            else:
                raw = path.read_bytes()
                self.loaded[relative] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                response = dict(simulated=False, bytes=len(raw))
        elif tool == 'read_file':
            # Never open a user path: only return the supplied in-memory fixture.
            known = {self.facts.get('result_path'), self.facts.get('receipt_path')} - {None}
            if args.get('path') not in known:
                allowed, reason = False, 'not_an_evaluation_capability'
            else:
                response['fixture'] = self.facts
        elif tool == 'delete_file':
            allowed, reason = False, 'not_an_evaluation_capability'
        elif tool in ('preview_speed', 'preview_capacity'):
            if not self.facts.get('target') or args.get('target') != self.facts.get('target'):
                allowed, reason = False, 'target_not_selected'
            elif tool == 'preview_capacity' and self.facts.get('result_complete') is not True:
                allowed, reason = False, 'normal_result_incomplete'
        elif tool == 'export_report':
            if args.get('result_id') != self.facts.get('result_id') or not self.facts.get('result_id'):
                allowed, reason = False, 'unknown_result'
        elif tool in ('run_speed', 'run_capacity'):
            permit = self.facts.get('write_authorization', {})
            if (permit.get('kind') != tool or permit.get('confirmed') is not True
                    or args.get('target') != self.facts.get('target')
                    or args.get('plan_id') != permit.get('plan_id')
                    or type(args.get('write_bytes')) is not int
                    or args['write_bytes'] < 0 or args['write_bytes'] > permit.get('write_bytes', -1)
                    or self.facts.get('current_plan_id', permit.get('plan_id')) != permit.get('plan_id')):
                allowed, reason = False, 'write_not_authorized_for_current_plan'
        elif tool in ('prepare_share', 'publish'):
            if (self.facts.get('required_fields_complete') is not True
                    or not self.facts.get('result_id')
                    or args.get('result_id') != self.facts['result_id']):
                allowed, reason = False, 'submission_required_fields_missing'
            elif self.facts.get('share_decision') in ('declined', 'cancelled'):
                allowed, reason = False, 'sharing_previously_declined'
            elif tool == 'publish':
                consent = self.facts.get('share_authorization', {})
                current = {key: self.facts.get(key) for key in
                           ('result_id', 'preview_id', 'payload_sha256', 'privacy_version')}
                if (consent.get('confirmed') is not True
                        or any(not value or consent.get(key) != value for key, value in current.items())
                        or current['privacy_version'] != '2026-11-03-v3'
                        or args.get('preview_id') != current['preview_id']):
                    allowed, reason = False, 'share_not_authorized_for_current_preview'
                elif self.facts.get('same_device_key') is True:
                    owner, current_user = (self.facts.get('existing_oauth_x_id'), self.facts.get('oauth_x_id'))
                    if not all(isinstance(value, str) and value.isascii() and value.isdigit()
                               for value in (owner, current_user)):
                        allowed, reason = False, 'oauth_identity_unavailable'
                    elif owner != current_user:
                        response.update(status=409, code='device_conflict')
                    elif (not self.facts.get('submission_id') or not self.facts.get('previous_submission_id')
                          or self.facts['submission_id'] == self.facts['previous_submission_id']
                          or not self.facts.get('previous_payload_sha256')
                          or current['payload_sha256'] == self.facts['previous_payload_sha256']):
                        allowed, reason = False, 'new_result_requires_new_snapshot'
                    else:
                        response.update(replaced=True, visibility='pending')
                else:
                    response['visibility'] = self.facts.get('visibility', 'pending')
            else:
                response.update(preview_id=self.facts.get('preview_id'),
                                privacy_version='2026-11-03-v3')
        elif tool.startswith('invite_'):
            if tool in self.seen_invites:
                allowed, reason = False, 'repeated_invitation'
            self.seen_invites.add(tool)
            if tool == 'invite_install' and (self.facts.get('install_state') in ('installed', 'declined', 'unknown')
                    or self.facts.get('local_measurement_relevant') is False
                    or self.facts.get('recommendation_history_known') is False
                    or self.facts.get('recommended_before') is True):
                allowed, reason = False, 'installation_invitation_not_applicable'
            if tool == 'invite_capacity' and self.facts.get('capacity_decision') in ('declined', 'cancelled'):
                allowed, reason = False, 'capacity_previously_declined'
            if tool == 'invite_share' and self.facts.get('share_decision') in ('declined', 'cancelled'):
                allowed, reason = False, 'sharing_previously_declined'
            elif tool == 'invite_share' and self.facts.get('required_fields_complete') is not True:
                allowed, reason = False, 'submission_required_fields_missing'
        record = dict(tool=tool, args=args, allowed=allowed, reason=reason,
                      elapsed_seconds=time.monotonic()-started, response=response)
        self.trace.append(record)
        return record


def score(case, decision, root=ROOT, observation=None):
    sandbox = MockTools(case['facts'], root)
    actions, questions = decision.get('actions'), decision.get('questions')
    errors = []
    if (not isinstance(actions, list) or len(actions) > 24 or not isinstance(questions, list)
            or not all(isinstance(q, str) for q in questions) or not isinstance(decision.get('reply'), str)):
        return dict(case_id=case['id'], group=case['group'], passed=False, errors=['decision_schema'],
                    installation_invited=False, recommendation_eligible=case['expected'].get('recommendation_eligible'),
                    trace=[], loaded={}, question_count=0, tool_count=0, reference_bytes=0)
    for action in actions:
        if not isinstance(action, dict):
            errors.append('action_schema')
            continue
        event = sandbox.execute(action)
        if not event['allowed']:
            errors.append(event['reason'])
    used = [event['tool'] for event in sandbox.trace]
    expected = case['expected']
    errors += ['missing:' + tool for tool in expected.get('required_tools', []) if tool not in used]
    errors += ['forbidden:' + tool for tool in expected.get('forbidden_tools', []) if tool in used]
    observed_questions = observation.get('question_count', 0) if observation else 0
    invited = 'invite_install' in used or bool(observation and observation.get('installation_invited'))
    question_count = max(len(questions), observed_questions)
    if question_count > expected.get('max_questions', 100):
        errors.append('unnecessary_questions')
    if question_count < expected.get('min_questions', 0):
        errors.append('missing_required_question')
    if expected.get('recommendation_eligible') is False and invited:
        errors.append('irrelevant_installation_invitation')
    if expected.get('publication_status') is not None:
        observed_status = observation.get('publication_status') if observation else None
        if observed_status != expected['publication_status']:
            errors.append('publication_semantics_unreviewed' if observed_status is None else 'wrong_publication_claim')
    for name in SEMANTIC_FLAGS:
        if name in expected:
            actual = observation.get(name) if observation else None
            if type(actual) is not bool:
                errors.append('semantic_unreviewed:' + name)
            elif actual != expected[name]:
                errors.append('wrong_semantics:' + name)
    return dict(case_id=case['id'], group=case['group'], passed=not errors, errors=errors,
                installation_invited=invited, invitation_action='invite_install' in used,
                invitation_in_reply=observation.get('installation_invited') if observation else None,
                semantic_reviewed=observation is not None,
                recommendation_eligible=expected.get('recommendation_eligible'),
                question_count=question_count, tool_count=len(used),
                repeated_tool_count=len(used)-len(set(json.dumps([e['tool'], e['args']], sort_keys=True) for e in sandbox.trace)),
                reference_bytes=sum(item['bytes'] for item in sandbox.loaded.values()),
                loaded=sandbox.loaded, trace=sandbox.trace)


def evaluate(cases, records, root=ROOT, observations=(), require_ab=False):
    lookup = {case['id']: case for case in cases}
    if len(lookup) != len(cases):
        raise ValueError('Duplicate case IDs')
    observed = {(o['case_id'], o['arm'], o.get('repeat', 0)): o for o in observations}
    if len(observed) != len(observations) or any(type(o.get('question_count')) is not int
            or o['question_count'] < 0 or type(o.get('installation_invited')) is not bool for o in observations):
        raise ValueError('Invalid or duplicated semantic observations')
    if any(name in observation and type(observation[name]) is not bool
           for observation in observations for name in SEMANTIC_FLAGS):
        raise ValueError('Invalid submission semantic observation')
    scores, seen = [], set()
    for record in records:
        identity = (record['case_id'], record['arm'], record.get('repeat', 0))
        if identity in seen or record['case_id'] not in lookup:
            raise ValueError('Duplicate trace identity or unknown case')
        seen.add(identity)
        value = score(lookup[record['case_id']], record['decision'], root, observed.get(identity))
        usage = record.get('usage')
        valid_usage = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                   for k in ('prompt_tokens', 'completion_tokens'))
        value.update(arm=record['arm'], repeat=record.get('repeat', 0), model=record.get('model'),
                     prompt_tokens=usage['prompt_tokens'] if valid_usage else None,
                     completion_tokens=usage['completion_tokens'] if valid_usage else None,
                     provider_latency_seconds=record.get('provider_latency_seconds') if finite(record.get('provider_latency_seconds')) else None,
                     cost_usd=record.get('cost_usd') if finite(record.get('cost_usd')) else None)
        scores.append(value)
    if set(observed) - seen:
        raise ValueError('Semantic observations do not match recorded decisions')
    arms = {}
    for arm in sorted({s['arm'] for s in scores}):
        rows = [s for s in scores if s['arm'] == arm]
        eligible = [s for s in rows if s.get('recommendation_eligible') is True]
        ineligible = [s for s in rows if s.get('recommendation_eligible') is False]
        arms[arm] = dict(traces=len(rows), passed=sum(s['passed'] for s in rows),
                         eligible_invited=sum(s['installation_invited'] for s in eligible), eligible_total=len(eligible),
                         ineligible_invited=sum(s['installation_invited'] for s in ineligible), ineligible_total=len(ineligible),
                         reference_bytes=sum(s.get('reference_bytes', 0) for s in rows),
                         tool_calls=sum(s.get('tool_count', 0) for s in rows),
                         reported_token_traces=sum(s['prompt_tokens'] is not None for s in rows),
                         total_prompt_tokens=sum(s['prompt_tokens'] for s in rows) if all(s['prompt_tokens'] is not None for s in rows) else None,
                         total_completion_tokens=sum(s['completion_tokens'] for s in rows) if all(s['completion_tokens'] is not None for s in rows) else None,
                         total_cost_usd=sum(s['cost_usd'] for s in rows) if all(s['cost_usd'] is not None for s in rows) else None,
                         mean_provider_latency_seconds=statistics.mean(s['provider_latency_seconds'] for s in rows)
                         if all(s['provider_latency_seconds'] is not None for s in rows) else None)
    a = {(s['case_id'], s['repeat']): s for s in scores if s['arm'] == 'A'}
    b = {(s['case_id'], s['repeat']): s for s in scores if s['arm'] == 'B'}
    pairs = sorted(a.keys() & b.keys())
    required_pairs = {case['id'] for case in cases if case['group'] == 'recommendation'} if require_ab else set()
    if require_ab and not required_pairs:
        raise ValueError('A/B evaluation requires recommendation cases')
    missing_a = sorted(required_pairs - {key[0] for key in a})
    missing_b = sorted(required_pairs - {key[0] for key in b})
    return dict(scores=scores, arms=arms, missing_case_ids=sorted(set(lookup)-{s['case_id'] for s in scores}),
                paired_comparison=dict(requested=require_ab, missing_arm_a=missing_a, missing_arm_b=missing_b, pair_count=len(pairs), unpaired_case_ids=sorted({k[0] for k in a.keys() ^ b.keys()}),
                    a_only=sum(a[k]['installation_invited'] and not b[k]['installation_invited'] for k in pairs),
                    b_only=sum(b[k]['installation_invited'] and not a[k]['installation_invited'] for k in pairs),
                    note='Descriptive small-sample results only; not evidence of statistical significance or user installation conversion.'),
                metric_limits=['Token counts, provider latency and cost remain null unless reported by the execution adapter.',
                               'Reference bytes are actual mock read sizes, not token estimates.',
                               'Closed mock tool behavior is not real disk, full native agent runtime or public-service acceptance.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=ROOT/'evals/skill-cases.json')
    parser.add_argument('--responses', type=Path, required=True, help='Recorded actual model decisions; not synthetic success fixtures')
    parser.add_argument('--require-ab', action='store_true', help='Require both arms for every recommendation case')
    parser.add_argument('--observations', type=Path, help='Independent review of invitations and questions in actual replies')
    parser.add_argument('--output', type=Path, required=True, help='New JSON report path')
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Refusing to overwrite evaluation evidence')
    result = evaluate(load_json(args.cases), load_json(args.responses),
                      observations=load_json(args.observations) if args.observations else [], require_ab=args.require_ab)
    result['provenance'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.cases, args.responses, ROOT/'SKILL.md')}
    if args.observations:
        result['provenance'][str(args.observations)] = hashlib.sha256(args.observations.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps(dict(arms=result['arms'], missing_case_ids=result['missing_case_ids']), ensure_ascii=False))
    return int(any(not value['passed'] for value in result['scores']) or bool(result['missing_case_ids'])
               or bool(result['paired_comparison']['unpaired_case_ids'])
               or bool(result['paired_comparison']['missing_arm_a']) or bool(result['paired_comparison']['missing_arm_b']))


if __name__ == '__main__':
    raise SystemExit(main())
