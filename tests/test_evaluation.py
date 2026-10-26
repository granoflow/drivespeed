import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import report

class EvaluationTests(unittest.TestCase):
    def fixture(self):
        return {'run_id': 'current', 'evaluation': {'run_id': 'current', 'language': 'zh-CN', 'sections': [
            {'kind': kind, 'title': '<unsafe>', 'paragraphs': ['A & B'], 'sources': []}
            for kind in ('bottleneck', 'upgrades', 'anomalies')
        ]}}

    def test_optional_and_escaped(self):
        self.assertEqual(report.render_evaluation({}, 'en'), '')
        rendered = report.render_evaluation(self.fixture(), 'zh-CN')
        self.assertIn('&lt;unsafe&gt;', rendered)
        self.assertIn('A &amp; B', rendered)
        self.assertEqual(rendered.count('<section'), 3)

    def test_other_run_or_language_rejected(self):
        for field, value in [('run_id', 'old'), ('language', 'en')]:
            result = self.fixture()
            result['evaluation'][field] = value
            with self.assertRaises(ValueError):
                report.render_evaluation(result, 'zh-CN')

    def test_incomplete_evaluation_and_unsafe_source_rejected(self):
        result = self.fixture()
        result['evaluation']['sections'].pop()
        with self.assertRaises(ValueError):
            report.render_evaluation(result, 'zh-CN')
        result = self.fixture()
        result['evaluation']['sections'][0]['sources'] = [
            {'url': 'javascript:alert(1)', 'label': 'source', 'checked_at': '2026-10-26'}]
        with self.assertRaises(ValueError):
            report.render_evaluation(result, 'zh-CN')

    def test_delivery_notices_are_not_in_report(self):
        import tempfile
        from progress import messages
        text = messages('zh-CN')
        result = {'cases': [], 'outcome': 'complete',
                  'cache_assessment': {'status': 'no_drop_observed'}, 'notes': [
            'trash_note', 'retain_note', 'cleanup_failed', 'cache_incomplete']}
        with tempfile.TemporaryDirectory() as directory:
            report.render_report(result, Path(directory), 'zh-CN', text)
            rendered = (Path(directory) / 'report.html').read_text()
        for key in result['notes']:
            self.assertNotIn(text[key], rendered)
        self.assertIn(text['cache_no_drop'], rendered)

    def test_peak_uses_same_direction_and_does_not_invent_missing_samples(self):
        from progress import messages
        result = {'cases': [
            {'name': 'seq_read', 'status': 'completed',
             'series': [{'direction': 'read', 'mb_s': 120},
                        {'direction': 'write', 'mb_s': 999}],
             'metrics': {'read': {'mb_s': 100}}},
            {'name': 'seq_write', 'status': 'completed', 'series': [],
             'metrics': {'write': {'mb_s': 80}}}]}
        rendered = report.render_peak_comparison(result, messages('zh-CN'))
        self.assertIn('120.00', rendered)
        self.assertIn('100.00', rendered)
        self.assertIn('80.00', rendered)
        self.assertNotIn('999.00', rendered)
        self.assertIn('—', rendered)
