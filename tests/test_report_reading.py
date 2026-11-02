"""Reader report composition; synthetic inputs, no disks, models or network."""
import copy
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
        self.assertIn('数据来源与引用', document)
        self.assertIn('Skill 为可选工具', document)
        metadata = re.search(r'<script type="application/json" id="drivespeed-agent-usage">(.*?)</script>', document, re.S).group(1)
        self.assertEqual(json.loads(metadata), agent_usage())


if __name__ == '__main__':
    unittest.main()
