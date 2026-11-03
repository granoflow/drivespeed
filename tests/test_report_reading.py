"""Reader report composition; synthetic inputs, no disks, models or network."""
import copy
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).parent)]
from progress import messages
from report import render_report, render_references, render_connection_reference
from agent_usage import agent_usage
from test_community import local_result
from test_device_parameters import device_result


class Sections(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.parents[attrs['id']] = tuple(self.stack)
        if tag not in {'meta', 'br', 'hr', 'link', 'img', 'input'}:
            self.stack.append(attrs.get('id') or tag)

    def handle_endtag(self, tag):
        if tag not in {'meta', 'br', 'hr', 'link', 'img', 'input'}:
            self.stack.pop()


class ReportReadingTests(unittest.TestCase):
    def test_peak_connection_reference_uses_selected_qd16_measurement(self):
        from test_peak_analysis import case
        from workloads import PEAK_METHOD
        result = device_result()
        base = case(16, 100); base['name'] = 'seq_read_q'
        best = case(16, 130); best['name'] = 'peak_seq_read_16_1'
        result['tool'] = {'method_version': PEAK_METHOD}
        result['cases'] = [base, best]
        document, _ = render_connection_reference(result, result['references'], messages('zh-CN'))
        row = re.search(r'<tr data-path-direction="read".*?</tr>', document, re.S).group()
        self.assertIn('130.00 MB/s', row)
        self.assertNotIn('100.00 MB/s', row)

    def test_verified_stable_cache_keeps_source_facts_and_moves_followup_to_receipt(self):
        result = local_result()
        result.pop('user_input', None)
        result['evaluation'] = dict(report_policy='verified_only', run_id=result['run_id'], language='zh-CN', sections=[
            dict(kind=kind, title=kind, paragraphs=['Measured fact'], evidence_status='supported')
            for kind in ('bottleneck', 'upgrades', 'anomalies')])
        result['cache_assessment'] = dict(status='observed_drop_stable',
            cache_capacity_estimate_bytes=10 * 2**30, post_cache_mb_s=500)
        result['sustained_summary'] = dict(tail_mean_mb_s=500)
        result['references'] = dict(for_drive_model='Fixture SSD', checked_at='2025-01-01', items=[dict(
            component='drive', model='Fixture SSD', relationship='device', metric='cache_capacity_estimate_bytes',
            value=100, unit='GiB', source_kind='review', source_url='https://example.org/cache',
            source_label='Saved cache review', conditions='Same capacity')])
        result['reference_cache'] = dict(status='stale')
        before = copy.deepcopy(result)
        dialogue = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(dialogue):
            artifact = render_report(result, directory, 'zh-CN', messages('zh-CN'))
            document = Path(artifact['html']).read_text()
        self.assertIn('data-status="observed_drop_stable"', document)
        self.assertIn('data-metric="cache_capacity_estimate_bytes"', document)
        self.assertIn('https://example.org/cache', document)
        self.assertIn('2025-01-01', document)
        self.assertNotIn(messages('zh-CN')['reference_stale'], document)
        self.assertNotIn(messages('zh-CN')['cache_stable'], document)
        self.assertNotIn('data-cache-reference-warning="review"', document)
        self.assertIn(messages('zh-CN')['cache_stable'], dialogue.getvalue())
        self.assertIn(messages('zh-CN')['reference_stale'], dialogue.getvalue())
        self.assertNotIn('delivery_followup', artifact)
        self.assertEqual(result, before)

    def test_verified_capacity_keeps_coverage_and_returns_limits_for_dialogue(self):
        from report import render_capacity_check
        size = 2**20
        jobs = [dict(name='integrity_' + direction, rw=direction, verify=True,
                     bs=size, qd=16, io_bytes=size) for direction in ('write', 'read')]
        check = dict(outcome='complete', tool={'method_version': 'drivespeed-capacity-v1'},
            plan=dict(method_version='drivespeed-capacity-v1', profile='integrity', file_bytes=size, jobs=jobs),
            cases=[dict(job, status='completed', metrics_complete=True, metrics=dict(runtime_ms=1000,
                read={'bytes': size if job['rw'] == 'read' else 0},
                write={'bytes': size if job['rw'] == 'write' else 0})) for job in jobs])
        result = local_result()
        result.pop('user_input', None)
        result['evaluation'] = dict(report_policy='verified_only', run_id=result['run_id'], language='zh-CN', sections=[
            dict(kind=kind, title=kind, paragraphs=['Measured fact'], evidence_status='supported')
            for kind in ('bottleneck', 'upgrades', 'anomalies')])
        result['capacity_check'] = check
        dialogue = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(dialogue):
            artifact = render_report(result, directory, 'zh-CN', messages('zh-CN'))
            document = Path(artifact['html']).read_text()
        self.assertIn(messages('zh-CN')['capacity_ok'], document)
        self.assertIn('已完整校验', document)
        self.assertNotIn(messages('zh-CN')['capacity_limits'], document)
        self.assertIn(messages('zh-CN')['capacity_limits'], dialogue.getvalue())
        self.assertNotIn('delivery_followup', artifact)
        result['capacity_check'] = dict(outcome='partial', plan={'file_bytes': size}, cases=[])
        incomplete = render_capacity_check(result, messages('zh-CN'))
        self.assertIn(messages('zh-CN')['capacity_not_completed'], incomplete)
        self.assertIn(messages('zh-CN')['file_size'], incomplete)
        self.assertNotIn(messages('zh-CN')['actual_write'], incomplete)
        self.assertNotIn('—', incomplete)

    def test_verified_metadata_omits_missing_values_without_dropping_known_conditions(self):
        result = local_result()
        result.pop('user_input', None)
        result['evaluation'] = dict(report_policy='verified_only', run_id=result['run_id'], language='zh-CN', sections=[
            dict(kind=kind, title=kind, paragraphs=['Measured fact'], evidence_status='supported')
            for kind in ('bottleneck', 'upgrades', 'anomalies')])
        result['environment'].pop('engine')
        result['target'].pop('filesystem')
        for case in result['cases']:
            case['metrics'].pop('achieved_depth', None)
        with tempfile.TemporaryDirectory() as directory:
            document = Path(render_report(result, directory, 'zh-CN', messages('zh-CN'))['html']).read_text()
        metadata = re.search(r'<dl class="metadata">(.*?)</dl>', document, re.S).group(1)
        self.assertIn(messages('zh-CN')['free_space'], metadata)
        self.assertIn('macos', metadata)
        self.assertNotIn(messages('zh-CN')['unknown'], metadata)
        self.assertNotIn('—', metadata)
        self.assertNotIn(messages('zh-CN')['initial_temp'], metadata)
        self.assertNotIn(messages('zh-CN')['filesystem'], metadata)
        self.assertNotIn(messages('zh-CN')['depth'] + ': ' + messages('zh-CN')['unknown'], document)

    def test_legacy_single_bridge_candidate_is_escaped_without_confirming_identity(self):
        from report import render_hardware_assessment
        result = dict(cases=[], hardware=dict(drive={'model': 'Drive & B', 'source': 'system'},
            enclosure={'model': None, 'source': 'unavailable', 'bridge_candidates': ['Bridge <C>']}))
        document = render_hardware_assessment(result, messages('zh-CN'))
        self.assertIn('Bridge &lt;C&gt;', document)
        self.assertIn(messages('zh-CN')['controller_candidate'], document)
        self.assertNotIn('Bridge <C>', document)
        self.assertNotIn('data-device-origin="reference"', document)
        result['hardware']['enclosure']['bridge_candidates'].append('Other bridge')
        multiple = render_hardware_assessment(result, messages('zh-CN'))
        self.assertNotIn('Bridge &lt;C&gt;', multiple)
        self.assertNotIn('Other bridge', multiple)

    def test_verified_report_preserves_quick_facts_without_quick_disclaimer(self):
        for language, direction in (('ar', 'rtl'), ('zh-CN', 'ltr'), ('en', 'ltr')):
            with self.subTest(language=language):
                result = local_result()
                result.pop('user_input', None)
                result['evaluation'] = dict(report_policy='verified_only', run_id=result['run_id'], language=language, sections=[
                    dict(kind=kind, title=kind, paragraphs=['Measured fact'], evidence_status='supported')
                    for kind in ('bottleneck', 'upgrades', 'anomalies')])
                result['quick_measurement'] = {'cases': copy.deepcopy(result['cases'][:4])}
                before = copy.deepcopy(result)
                with tempfile.TemporaryDirectory() as directory:
                    document = Path(render_report(result, directory, language, messages(language))['html']).read_text()
                self.assertIn(f'<html lang="{language}" dir="{direction}">', document)
                self.assertIn('id="quick-measurement"', document)
                self.assertIn('quick_seq_read', document)
                self.assertNotIn(messages(language)['quick_notice'], document)
                self.assertEqual(result, before)

    def test_verified_daily_summary_only_describes_measured_directions(self):
        from report import render_readable_results
        for omitted in ('seq_read', 'seq_write', 'rand_read', 'rand_write'):
            with self.subTest(omitted=omitted):
                result = local_result()
                result['evaluation'] = {'report_policy': 'verified_only'}
                result['cases'] = [case for case in result['cases'] if case['name'] != omitted]
                document = render_readable_results(result, messages('zh-CN'))
                self.assertNotIn(messages('zh-CN')['reference_not_measured'], document)
                self.assertIn('MB/s', document)

    def test_one_device_identity_and_complete_html_appendix_preserve_measurements(self):
        result = local_result()
        result.pop("user_input", None)
        result['hardware']['controller'] = dict(model='ASMedia 246x', precision='series', source='ioreg:target-ancestor')
        result['hardware']['access_interface'] = dict(name='USB4 40 Gbps', source='ioreg:target-ancestor')
        before = copy.deepcopy(result)
        with tempfile.TemporaryDirectory() as directory:
            artifact = render_report(result, directory, 'zh-CN', messages('zh-CN'))
            document = Path(artifact['html']).read_text()
        sections = Sections(); sections.feed(document)
        self.assertIn('hardware-assessment', sections.parents)
        self.assertNotIn('hardware', sections.parents)
        for identifier in ('results', 'environment', 'method', 'peak-comparison'):
            if identifier in sections.parents:
                self.assertIn('technical-appendix', sections.parents[identifier])
        self.assertIn('ASMedia 246x', document)
        self.assertIn('USB4 40 Gbps', document)
        connection = re.search(r'<dl class="connection-facts">.*?</dl>', document, re.S).group()
        self.assertIn('USB4 40 Gbps', connection)
        self.assertNotIn('ASMedia 246x', connection)
        self.assertIn('href="#short-results"', document)
        self.assertIn('href="#reading-results"', document)
        self.assertIn('系统标识（系列）', document)
        self.assertEqual(result, before)

    def test_other_devices_and_unconfirmed_variants_are_not_current_report_parameters(self):
        result = device_result()
        original = copy.deepcopy(result['references']['items'][4])
        original.update(source_kind='review', conditions='Current-device review')
        peer = {**original, 'model': 'Other SSD', 'relationship': 'peer'}
        candidate = {**original, 'model': 'Unconfirmed chip', 'relationship': 'candidate'}
        result['references']['items'] = [original, peer, candidate]
        before = copy.deepcopy(result)
        document = render_references(result, messages('en'), {})
        self.assertIn('Current-device review', document)
        self.assertNotIn('Other SSD', document)
        self.assertNotIn('Unconfirmed chip', document)
        self.assertEqual(result, before)

    def test_known_series_is_not_erased_when_rated_throughput_is_unconfirmed(self):
        result = device_result()
        result['hardware']['controller'] = dict(model='ASMedia 246x', precision='series', source='ioreg:target-ancestor')
        result['hardware']['access_interface'] = dict(name='USB4 40 Gbps', source='ioreg:target-ancestor')
        document, _ = render_connection_reference(result, result['references'], messages('zh-CN'))
        controller = re.search(r'<tr data-path-component="controller">.*?</tr>', document, re.S).group()
        self.assertIn('ASMedia 246x', controller)
        self.assertIn('USB4 40 Gbps', controller)
        self.assertIn('芯片额定读写能力尚未确认', controller)
        self.assertNotIn('资料暂缺', controller)
        self.assertNotIn('ASM2464PD', controller)

    def test_unconfirmed_post_cache_speed_is_a_short_observation_not_a_prominent_result_card(self):
        result = local_result()
        result.pop('user_input', None)
        result['tool']['method_version'] = 'drivespeed-auto-full-v1'
        for case in result['cases']:
            if case['name'] == 'sustained':
                case.setdefault('metrics', {})['runtime_ms'] = 2_000
        with tempfile.TemporaryDirectory() as directory:
            document = Path(render_report(result, directory, 'en', messages('en'))['html']).read_text()
        self.assertIn('Write observation: 2.00 seconds.', document)
        self.assertNotIn('class="cards long-cards"', document)
        self.assertIn('id="sustained"', document)

    def test_localized_source_note_keeps_the_full_machine_readable_policy(self):
        result = local_result()
        result.pop("user_input", None)
        with tempfile.TemporaryDirectory() as directory:
            document = Path(render_report(result, directory, 'zh-CN', messages('zh-CN'))['html']).read_text()
        self.assertIn('数据使用协议', document)
        self.assertIn('DriveSpeed Data-use Agreement', document)
        self.assertIn('Citation agreement', document)
        self.assertIn('Agent usage agreement', document)
        self.assertIn('large-language-model training constitutes acceptance', document)
        self.assertNotIn('testing guarantees a diagnosis', document)
        self.assertNotIn('Reading or retrieving data alone does not establish agreement', document)
        self.assertNotIn('Data citation requirements and Agent guidance', document)
        self.assertIn('Skill 为可选工具', document)
        metadata = re.search(r'<script type="application/json" id="drivespeed-agent-usage">(.*?)</script>', document, re.S).group(1)
        self.assertEqual(json.loads(metadata), agent_usage())


if __name__ == '__main__':
    unittest.main()
