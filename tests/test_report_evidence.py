import copy
import contextlib
import html
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from report_evidence import render_evidence, evidence_labels
from report import render_report
from progress import messages, ASSETS
from agent_usage import agent_usage


class ReportEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.labels = json.loads((ASSETS / 'report-templates/zh.json').read_text())['evidence']
        review = dict(url='https://example.org/review', label='Original <review>', checked_at='2026-11-02',
                      drive_model='Fixture SSD', capacity='2 TB', interface='USB4 40 Gb/s',
                      controller='unknown', tool='fio', conditions='8 GiB; other details unknown',
                      results='3000 MB/s', comparison='Different host; not a quality verdict')
        self.result = dict(run_id='run-a', hardware={'drive': {'model': 'Fixture SSD'}},
                           evaluation=dict(run_id='run-a', language='zh-CN', sections=[
                               dict(kind=k, title=k, paragraphs=['Evidence-limited judgment'], sources=[])
                               for k in ('bottleneck', 'upgrades', 'anomalies')], evidence=dict(
                               device_health=dict(status='collected', model='Fixture SSD', tool='smartctl 7.5',
                                                  captured_at='2026-11-02T11:00:00+08:00', scope='Later snapshot',
                                                  rows=[dict(field='model', value='<unsafe>')]),
                               review_selection='One source; no same-enclosure match', online_reviews=[review])))

    def test_top_table_appendix_and_print_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            render_report(self.result, Path(directory), 'zh-CN', messages('zh-CN'))
            html = (Path(directory) / 'report.html').read_text()
        self.assertLess(html.index('id="device-health"'), html.index('id="evaluation-bottleneck"'))
        self.assertLess(html.index('id="online-reviews"'), html.index('id="technical-appendix"'))
        self.assertIn('&lt;unsafe&gt;', html)
        self.assertIn('Original &lt;review&gt;', html)
        self.assertIn('https://example.org/review', html)
        self.assertNotIn('可能被修改', html)
        self.assertNotIn('Later snapshot', html)

    def test_legacy_and_unavailable(self):
        self.assertEqual(render_evidence({}, 'en', self.labels), ('', '', ''))
        health = self.result['evaluation']['evidence']['device_health']
        health.update(status='unavailable', rows=[], scope='Bridge does not forward SMART')
        top, _, _ = render_evidence(self.result, 'zh-CN', self.labels)
        self.assertEqual(top, '')

    def test_review_uses_site_list_structure_and_only_supplied_measurements(self):
        review = self.result['evaluation']['evidence']['online_reviews'][0]
        review['measurements'] = [dict(label='TLC write <source>', value=3.9, unit='GB/s', qualifier='approximate')]
        _, appendix, _ = render_evidence(self.result, 'zh-CN', self.labels)
        self.assertIn('class="online-review result"', appendix)
        self.assertIn('class="result-device"', appendix)
        self.assertIn('class="device-path"', appendix)
        self.assertIn('class="result-contributor"', appendix)
        self.assertEqual(appendix.count('<strong>'), 1)
        self.assertIn('≈ 3.9 GB/s', appendix)
        self.assertIn('TLC write &lt;source&gt;', appendix)
        for value in (-1, float('nan'), True):
            with self.subTest(value=value):
                review['measurements'][0]['value'] = value
                with self.assertRaises(ValueError):
                    render_evidence(self.result, 'zh-CN', self.labels)

    def test_further_testing_advice_is_explicit_escaped_and_does_not_change_status(self):
        self.result['evaluation']['evidence']['further_testing'] = dict(
            decision='not_recommended', reason='Path may mask cache changes <not proof>',
            remaining_unknowns='Cache boundary remains unknown',
            when_to_continue='Revisit for sustained-workload requirements')
        _, _, advice = render_evidence(self.result, 'zh-CN', self.labels)
        self.assertIn('建议到此为止', advice)
        self.assertIn('&lt;not proof&gt;', advice)
        self.assertNotIn('outcome', self.result)
        self.result['evaluation']['evidence']['further_testing']['decision'] = 'passed'
        with self.assertRaises(ValueError):
            render_evidence(self.result, 'zh-CN', self.labels)

    def test_different_sources_can_have_different_optional_blocks_without_empty_slots(self):
        minimal = dict(url='https://example.org/text-review', label='Original text review', checked_at='2026-11-02')
        minimal['sections'] = [dict(title='Temperature <test>', paragraphs=['A & B'], evidence_status='supported')]
        ranged = dict(url='https://example.org/range-review', label='Transfer review', checked_at='2026-11-02',
                      context=[dict(label='Workload', value='Whole-drive write')],
                      measurements=[dict(label='Observed rate', value='900–3900', unit='MB/s'),
                                    dict(label='Readback errors', value=0)])
        evidence = self.result['evaluation']['evidence']
        self.result['evaluation']['report_policy'] = 'verified_only'
        evidence['online_reviews'] = [minimal, ranged]
        evidence.pop('review_selection')
        _, appendix, _ = render_evidence(self.result, 'zh-CN', self.labels)
        first, second = appendix.split('<article class="online-review result">')[1:]
        self.assertIn('Temperature &lt;test&gt;', first)
        self.assertIn('A &amp; B', first)
        self.assertNotIn('device-path', first)
        self.assertNotIn('result-performance', first)
        self.assertNotIn('result-capacity', first)
        self.assertIn('900–3900 MB/s', second)
        self.assertIn('<bdi>0</bdi>', second)
        self.assertNotIn('review-details', second)
        for absent in ('硬盘盒主控', '与本次测试的关系', '原文测量结果'):
            self.assertNotIn(absent, appendix)

    def test_verified_report_moves_missing_work_to_delivery_and_blocks_share_advice(self):
        self.result.update(outcome='partial', measurement_quality={'status': 'visibility_limited'})
        evaluation = self.result['evaluation']
        evaluation.update(report_policy='verified_only', followup=[dict(
            issue='missing-cache-marker', reason='Path masks a transition',
            solution='Use an appropriate connection', retest_plan='Prepare a new finite plan')])
        evaluation['sections'][0].update(evidence_status='needs_followup', title='Unconfirmed bottleneck')
        evaluation['sections'][1].update(evidence_status='supported', title='Measured fact')
        before = copy.deepcopy(self.result)
        dialogue = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(dialogue):
            artifact = render_report(self.result, Path(directory), 'zh-CN', messages('zh-CN'))
            document = Path(artifact['html']).read_text()
        self.assertNotIn('missing-cache-marker', document)
        self.assertNotIn('Unconfirmed bottleneck', document)
        self.assertIn('Measured fact', document)
        self.assertNotIn('id="evaluation-anomalies"', document)
        self.assertNotIn('data-required-cache="incomplete"', document)
        self.assertNotIn('Later snapshot', document)
        self.assertNotIn('以上为设备固件报告', document)
        self.assertNotIn(messages('zh-CN')['privacy'], document)
        self.assertIn('class="agent-usage"', document)
        self.assertIn('id="agent-usage" class="agent-guidance"', document)
        self.assertIn(html.escape(agent_usage()['install_command']), document)
        for requirement in agent_usage()['requirements'].values():
            self.assertIn(html.escape(requirement), document)
        self.assertNotIn(messages('zh-CN')['measurement_quality'], document)
        self.assertNotIn(messages('zh-CN')['quality_visibility_limited'], document)
        self.assertNotIn(messages('zh-CN')['cache_method'], document)
        self.assertNotIn(messages('zh-CN')['window_method'], document)
        self.assertNotIn('delivery_followup', artifact)
        self.assertIn(messages('zh-CN')['cache_method'], dialogue.getvalue())
        self.assertIn(messages('zh-CN')['quality_visibility_limited'], dialogue.getvalue())
        self.assertIn('Later snapshot', dialogue.getvalue())
        self.assertFalse(artifact['share_recommendation_allowed'])
        self.assertIn('missing-cache-marker', dialogue.getvalue())
        self.assertEqual(self.result, before)

    def test_reject_wrong_target_run_language_identifiers_and_urls(self):
        mutations = [
            lambda r: r['evaluation'].update(run_id='other'),
            lambda r: r['evaluation']['evidence']['device_health'].update(model='Other drive'),
            lambda r: r['evaluation']['evidence']['device_health']['rows'].append(dict(field='serial', value='secret')),
            lambda r: r['evaluation']['evidence']['online_reviews'][0].update(url='javascript:alert(1)'),
            lambda r: r['evaluation']['evidence']['online_reviews'][0].update(checked_at='invented'),
            lambda r: r['evaluation']['evidence'].update(online_reviews=r['evaluation']['evidence']['online_reviews'] * 11),
            lambda r: r['evaluation']['evidence'].update(online_reviews=r['evaluation']['evidence']['online_reviews'] * 2),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                result = copy.deepcopy(self.result)
                mutate(result)
                with self.assertRaises(ValueError):
                    render_evidence(result, 'zh-CN', self.labels)
        self.result['evaluation']['language'] = 'fr'
        with self.assertRaises(ValueError):
            evidence_labels(self.result, 'fr', ASSETS)

    def test_english_and_complete_localized_evidence_labels(self):
        self.result['evaluation']['language'] = 'en'
        labels = evidence_labels(self.result, 'en', ASSETS)
        top, appendix, _ = render_evidence(self.result, 'en', labels)
        self.assertIn('Drive information and SMART status', top)
        self.assertIn('Read original review', appendix)
        self.assertNotIn('硬盘信息', top)
        self.result['evaluation']['language'] = 'fr'
        translated = copy.deepcopy(labels)
        translated['health_title'] = 'Informations du disque et état SMART'
        self.result['evaluation']['evidence']['labels'] = translated
        self.assertEqual(evidence_labels(self.result, 'fr', ASSETS), translated)
        translated['fields'].pop('firmware')
        with self.assertRaises(ValueError):
            evidence_labels(self.result, 'fr', ASSETS)
