import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import report
from progress import messages

class UserInputTests(unittest.TestCase):
    def render(self, data):
        return report.render_user_input({'run_id': 'one', 'user_input': data}, 'zh-CN', messages('zh-CN'))

    def test_skip_and_empty(self):
        self.assertEqual(self.render(None), '')
        self.assertEqual(self.render({'run_id': 'one', 'language': 'zh-CN'}), '')

    def test_partial_and_escaped_user_opinion(self):
        rendered = self.render({'run_id': 'one', 'language': 'zh-CN',
            'self_analysis': '怀疑 <script>盒子</script>\n还需对照'})
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('\n还需对照', rendered)
        self.assertIn(messages('zh-CN')['user_self_analysis'], rendered)
        self.assertNotIn('<table>', rendered)

    def test_all_fields_and_arbitrary_device(self):
        rendered = self.render({'run_id': 'one', 'language': 'zh-CN',
            'device_models': {'转接器': 'Brand A & B'},
            'test_notes': '没有贴硅脂', 'self_analysis': '可能散热不足'})
        self.assertIn('Brand A &amp; B', rendered)
        self.assertIn('没有贴硅脂', rendered)
        self.assertEqual(rendered.count('<h3>'), 3)

    def test_mismatch_and_invalid_input_rejected(self):
        for extra in [{'run_id': 'two'}, {'language': 'en'}, {'test_notes': []},
                      {'device_models': {'硬盘盒': 1}}]:
            data = {'run_id': 'one', 'language': 'zh-CN', **extra}
            with self.assertRaises(ValueError):
                self.render(data)
