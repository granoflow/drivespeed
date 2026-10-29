"""Report interfaces, source privacy and no hidden online effects."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from test_community import local_result, state_dir, ID
from test_community_network import receipt
from community_data import POLICY, project_result, CommunityError
from community_store import CommunityStore
from community_comparison import build_comparison, read_comparison
from community_report import render_comparison, report_messages
from progress import messages
import report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.store = CommunityStore(state_dir()); self.result = local_result(); self.result.pop("user_input",None)
        self.result["synthetic"] = True; self.result["target"]["negotiated_bps"] = 10_000_000_000
        p = project_result(self.result,"https://x.com/fixture_user",submission_id=ID)
        self.store.create(p,self.result["run_id"],"fixture-only-result")
        self.local = self.store.payload(ID)
    def tearDown(self):
        self.store.close()
    def facts(self, n=3):
        value = receipt(self.local); base = value["similar"]["items"][0]; value["similar"]["items"] = []
        for index in range(n):
            item = copy.deepcopy(base); remote = f"{index+2:08d}-2222-4222-8222-222222222222"
            item["record"]["id"] = remote; item["record"]["url"] = POLICY["destination"]+"/benchmarks/"+remote
            item["record"]["submission"]["submission_id"] = remote
            item["record"]["submission"]["cases"][0]["read"]["mb_s"] /= 2
            value["similar"]["items"].append(item)
        value["similar"]["status"] = "ok" if n else "empty"
        self.store.save_receipt(ID,value)
        selected = build_comparison(self.store,self.result,ID)
        return read_comparison(self.store,selected["comparison_id"])
    def test_a_and_b_share_facts_without_changing_original_or_loading_remote_resources(self):
        before = json.dumps(self.result,sort_keys=True); facts = self.facts()
        with tempfile.TemporaryDirectory() as directory:
            result = report.render_report(self.result,directory,"zh-CN",messages("zh-CN"),comparison=facts)
            a,b = Path(result["html"]).read_text(),Path(result["comparison_html"]).read_text()
        self.assertEqual(json.dumps(self.result,sort_keys=True),before)
        self.assertIn("硬盘测速报告",a); self.assertIn("横向测评报告",b)
        for private in ("PRIVATE-PATH","PRIVATE-MOUNT","PRIVATE-ID","PRIVATE-SERIAL","PRIVATE-HOST-PLATFORM","PRIVATE-FIO-PATH","payload_json"):
            self.assertNotIn(private,a); self.assertNotIn(private,b)
        for source in (a,b):
            self.assertNotIn("<script src=",source); self.assertNotIn("<iframe",source); self.assertNotIn("fetch(",source)
        self.assertIn("https://x.com/fixture_user",b); self.assertIn("速率高100.00%",b)
        self.assertIn("历史快照",b); self.assertIn("不是独立设备",b)
    def test_usage_survives_offline_samples_comparisons_and_both_local_reports(self):
        from agent_usage import agent_usage
        from html.parser import HTMLParser

        class UsageParser(HTMLParser):
            def __init__(self):
                super().__init__(); self.in_usage = False; self.metadata = ""
            def handle_starttag(self, tag, attrs):
                self.in_usage = tag == "script" and dict(attrs).get("id") == "drivespeed-agent-usage"
            def handle_data(self, value):
                if self.in_usage:
                    self.metadata += value
            def handle_endtag(self, tag):
                if tag == "script":
                    self.in_usage = False

        original = json.dumps(self.result, sort_keys=True)
        facts = self.facts()
        samples = self.store.samples(ID)
        self.assertEqual(samples[0]["record"]["agent_usage"], agent_usage(samples[0]["record"]["url"]))
        source = facts["groups"][0]["references"][0]
        self.assertEqual(source["agent_usage"], agent_usage(source["url"]))
        with tempfile.TemporaryDirectory() as directory:
            result = report.render_report(self.result, directory, "zh-CN", messages("zh-CN"), comparison=facts)
            for key in ("html", "comparison_html"):
                document = Path(result[key]).read_text()
                parser = UsageParser(); parser.feed(document)
                metadata = json.loads(parser.metadata)
                self.assertEqual(metadata, agent_usage())
                self.assertNotIn("/benchmarks/private-run", document)
                if key == "comparison_html":
                    self.assertIn('href="'+source["url"]+'"', document)
                else:
                    self.assertIn('href="comparison.html"', document)
                self.assertIn('id="agent-usage"', document)
                self.assertIn(metadata["skill_url"], document)
                self.assertNotIn("fetch(", document)
        self.assertEqual(json.dumps(self.result, sort_keys=True), original)

    def test_mismatched_run_does_not_replace_existing_a(self):
        facts = self.facts(); bad = copy.deepcopy(self.result); bad["run_id"] = "wrong"
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)/"report.html"; p.write_text("kept original")
            with self.assertRaisesRegex(CommunityError,"integrity_mismatch"):
                report.render_report(bad,directory,"en",messages("en"),comparison=facts)
            self.assertEqual(p.read_text(),"kept original")
    def test_less_than_three_or_empty_has_no_statistical_advantage_text(self):
        for count in (0,1,2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                result = render_comparison(self.facts(count),self.result,directory,"zh-CN",messages("zh-CN"))
                source = Path(result["comparison_html"]).read_text()
                self.assertNotIn("速率高100",source); self.assertIn("不足3份只列个例",source)
                if count:
                    self.assertIn('<section class="comparison-group">',source)
                    self.assertIn('class="comparison-example"',source)
                    self.assertIn("吞吐（MB/s）: 0.52",source)
                else:
                    self.assertNotIn('<section class="comparison-group">',source)
                    self.assertIn('class="comparison-empty"',source)
    def test_empty_snapshot_keeps_context_evaluation_and_sources_without_repeating_a(self):
        facts = self.facts(0)
        self.result['evaluation'] = {'run_id': self.result['run_id'], 'language': 'zh-CN', 'sections': [
            {'kind': kind, 'title': title, 'paragraphs': ['缺少参考，仅依据本次结果判断。'],
             'sources': [{'label': 'Fixture source', 'url': 'https://example.com/spec', 'checked_at': '2026-10-30'}]}
            for kind,title in (('bottleneck','瓶颈判断'),('upgrades','升级建议'),('anomalies','异常检查'))]}
        before_facts, before_result = copy.deepcopy(facts), copy.deepcopy(self.result)
        with tempfile.TemporaryDirectory() as directory:
            result = render_comparison(facts,self.result,directory,'zh-CN',messages('zh-CN'))
            source = Path(result['comparison_html']).read_text()
        self.assertIn('0份相似记录，0项读写测试可进行统计比较',source)
        self.assertIn('本次测试条件',source)
        self.assertIn('本项没有可用参考记录。',source)
        self.assertIn('href="https://x.com/fixture_user"',source)
        for value in (facts['sample_as_of'],facts['samples_fetched_at']):
            self.assertIn('datetime="'+value+'"',source)
        for kind in ('bottleneck','upgrades','anomalies'):
            self.assertIn('id="evaluation-'+kind+'"',source)
        self.assertIn('href="https://example.com/spec"',source)
        self.assertIn('<div class="comparison-ending"><section class="comparison-method">',source)
        self.assertIn('id="agent-usage"',source)
        self.assertIn('href="report.html"',source)
        self.assertNotIn('<section class="comparison-group">',source)
        self.assertNotIn('<table class="comparison-metrics">',source)
        self.assertNotIn('fetch(',source)
        self.assertEqual(facts,before_facts)
        self.assertEqual(self.result,before_result)
    def test_nonempty_background_sample_retains_example_and_condition_differences(self):
        value = receipt(self.local)
        value['similar']['items'][0]['record']['submission']['storage']['capacity_bytes'] += 1048576
        self.store.save_receipt(ID,value)
        selected = build_comparison(self.store,self.result,ID)
        facts = read_comparison(self.store,selected['comparison_id'])
        self.assertEqual(facts['sample_count'],1)
        self.assertTrue(all(metric['n'] == 0 for group in facts['groups'] for metric in group['metrics']))
        with tempfile.TemporaryDirectory() as directory:
            result = render_comparison(facts,self.result,directory,'zh-CN',messages('zh-CN'))
            source = Path(result['comparison_html']).read_text()
        self.assertIn('<section class="comparison-group">',source)
        self.assertIn('class="comparison-example"',source)
        self.assertIn('class="condition-differences"',source)
        self.assertIn('只作背景：条件不同或缺失',source)
        self.assertNotIn('class="comparison-empty"',source)

    def test_write_budget_difference_and_historical_unknown_have_readable_conditions(self):
        for budget in (None, self.local["plan"]["write_budget_bytes"]*2):
            with self.subTest(budget=budget):
                initial = self.facts()
                self.store.db.execute("UPDATE comparison_source_context SET write_budget_bytes=? WHERE comparison_id=?", (budget, initial["comparison_id"]))
                self.store.db.commit()
                facts = read_comparison(self.store, initial["comparison_id"])
                for language in ("zh-CN", "en", "ar"):
                    with self.subTest(language=language), tempfile.TemporaryDirectory() as directory:
                        labels = messages(language); text = report_messages(language)
                        output = render_comparison(facts,self.result,directory,language,labels)
                        source = Path(output["comparison_html"]).read_text()
                    self.assertIn("<th>"+labels["write_budget"]+"</th><td>"+f'{self.local["plan"]["write_budget_bytes"]:,}'+"</td><td>"+
                                  (text["unknown"] if budget is None else f"{budget:,}")+"</td>",source)
                    self.assertNotIn("write_budget_bytes",source)
                    self.assertNotIn("unknown_write_budget_bytes",source)
                    self.assertIn(text["background"],source)
                    self.assertNotIn(text["rate_more"].format(value="100.00"),source)
                    self.assertTrue(all(m["n"] == 0 and m["median"] is None for g in facts["groups"] for m in g["metrics"]))
    def test_bad_source_or_missing_historical_context_fails_without_inventing_links(self):
        facts = self.facts(); facts["groups"][0]["references"][0]["conditions"]["x_profile_url"] = "javascript:alert(1)"
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(CommunityError):
            render_comparison(facts,self.result,directory,"en",messages("en"))
        facts = self.facts(); facts["context_complete"] = False
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(CommunityError,"unsupported_comparison_version"):
            render_comparison(facts,self.result,directory,"en",messages("en"))
    def test_arabic_and_complete_translation_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            result = render_comparison(self.facts(),self.result,directory,"ar",messages("ar"))
            self.assertIn('dir="rtl"',Path(result["comparison_html"]).read_text())
            p = Path(directory)/"translation.json"; selected = report_messages("en"); selected["summary"] = "{bad}"
            p.write_text(json.dumps(selected))
            with self.assertRaisesRegex(ValueError,"placeholders"):
                report_messages("fr",p)

    def test_evaluation_stdin_binds_run_without_json_cache_or_original_mutation(self):
        evaluation = {"run_id": self.result["run_id"], "language": "zh-CN", "sections": [
            {"kind": kind, "title": title, "paragraphs": ["模拟数据，仅验证报告输入。"], "sources": []}
            for kind, title in (("bottleneck","瓶颈判断"),("upgrades","升级建议"),("anomalies","异常检查"))]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"original.json"; source.write_text(json.dumps(self.result))
            before = source.read_bytes(); output = Path(directory)/"report"
            command = [sys.executable, str(Path(report.__file__)), "--input", str(source),
                       "--output", str(output), "--language", "zh-CN", "--evaluation-stdin"]
            result = subprocess.run(command, input=json.dumps(evaluation), text=True, capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            rendered = (output/"report.html").read_text()
            self.assertIn('id="evaluation-anomalies"',rendered)
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(list(output.glob("*.json")),[])
            evaluation["run_id"] = "another-run"
            result = subprocess.run(command, input=json.dumps(evaluation), text=True, capture_output=True)
            self.assertEqual(result.returncode,2)
            self.assertEqual((output/"report.html").read_text(),rendered)


if __name__ == "__main__":
    unittest.main()
