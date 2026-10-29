"""Condition grouping and historical typed comparison facts, synthetic only."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from test_community import local_result, state_dir, ID
from test_community_network import receipt
from community_data import POLICY, CommunityError, project_result, digest
from community_store import CommunityStore
from community_comparison import build_comparison, read_comparison


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.store = CommunityStore(state_dir()); self.result = local_result()
        self.result["target"]["negotiated_bps"] = 10_000_000_000
        p = project_result(self.result, "https://x.com/fixture_user", submission_id=ID)
        self.store.create(p, self.result["run_id"], "fixture-only-path")
        self.local = self.store.payload(ID)
    def tearDown(self):
        self.store.close()
    def save(self, count=3, change=None):
        value = receipt(self.local)
        base = value["similar"]["items"][0]
        value["similar"]["items"] = []
        for index in range(count):
            item = copy.deepcopy(base); remote = f"{index+2:08d}-2222-4222-8222-222222222222"
            item["record"]["id"] = remote; item["record"]["url"] = POLICY["destination"]+"/benchmarks/"+remote
            p = item["record"]["submission"]; p["submission_id"] = remote
            p["environment"]["os_version"] = "15.9"
            p["cases"][0]["read"]["mb_s"] = self.local["cases"][0]["read"]["mb_s"] / 2
            p["cases"][0]["read"]["latency_us"]["p95"] = 1
            if change:
                change(p)
            value["similar"]["items"].append(item)
        value["similar"]["status"] = "ok" if count else "empty"
        self.store.save_receipt(ID, value)
    def facts(self):
        summary = build_comparison(self.store, self.result, ID)
        return summary, read_comparison(self.store, summary["comparison_id"])
    def test_three_records_compare_and_latency_delta_keeps_mathematical_sign(self):
        self.save(); summary, facts = self.facts()
        self.assertEqual(summary["comparable_group_count"], 11)
        metrics = {m["metric"]: m for m in facts["groups"][0]["metrics"]}
        self.assertEqual(metrics["mb_s"]["n"], 3)
        self.assertAlmostEqual(metrics["mb_s"]["delta_percent"], 100)
        self.assertAlmostEqual(metrics["p95"]["delta_percent"], 200)
        self.assertIsNone(metrics["mean"]["delta_percent"])
        self.assertTrue(all(r["verification"] == "unverified" for r in facts["groups"][0]["references"]))

    def test_finite_extreme_values_never_produce_infinite_percentage(self):
        self.save(change=lambda p:p["cases"][0]["read"].update(mb_s=5e-324))
        _, facts = self.facts()
        metric = next(m for m in facts["groups"][0]["metrics"] if m["metric"] == "mb_s")
        self.assertEqual(metric["n"],3)
        self.assertIsNone(metric["delta_percent"])
    def test_unknown_or_different_conditions_are_background_without_statistics(self):
        for change in (lambda p: p["storage"].update(negotiated_bps=None),
                       lambda p: p["storage"].update(capacity_bytes=999),
                       lambda p: p["storage"].update(scope="unresolved")):
            self.save(change=change); summary, facts = self.facts()
            self.assertEqual(summary["comparable_group_count"], 0)
            self.assertTrue(all(m["median"] is None and m["delta_percent"] is None for m in facts["groups"][0]["metrics"]))
            self.assertTrue(all(r["differences"] and not r["comparable"] for r in facts["groups"][0]["references"]))
    def test_less_than_three_and_repeated_cases_do_not_manufacture_sample_count(self):
        self.save(2, lambda p: p["cases"].append(copy.deepcopy(p["cases"][0])))
        summary, facts = self.facts()
        self.assertEqual(summary["sample_count"], 2); self.assertEqual(summary["comparable_group_count"], 0)
        self.assertTrue(all(m["n"] <= 2 and m["median"] is None for m in facts["groups"][0]["metrics"]))
    def test_comparison_requires_exact_original_result_and_stores_no_partial_facts(self):
        self.save(); different = copy.deepcopy(self.result); different["run_id"] = "other-run"
        with self.assertRaisesRegex(CommunityError, "integrity_mismatch"):
            build_comparison(self.store, different, ID)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0], 0)
    def test_reference_refresh_does_not_rewrite_historical_comparison(self):
        self.save(); summary, original = self.facts()
        self.save(0)
        self.assertEqual(self.store.samples(ID), [])
        self.assertEqual(read_comparison(self.store, summary["comparison_id"]), original)
        self.assertIn("historical reference snapshot", " ".join(original["limitations"]))
        self.assertTrue(original["context_complete"])
        source = original["groups"][0]["references"][0]
        self.assertEqual(source["conditions"]["os_version"], "15.9")
        self.assertEqual(source["conditions"]["x_profile_url"], "https://x.com/fixture_user")
        self.assertEqual(source["workload"]["qd"], self.local["cases"][0]["qd"])
        self.assertEqual(original["local"]["environment"]["os_version"], "15.0")
    def test_old_incomplete_context_is_marked_instead_of_rebuilt_from_current_samples(self):
        self.save(); summary, _ = self.facts()
        self.store.db.execute("DELETE FROM comparison_source_context"); self.store.db.commit()
        facts = read_comparison(self.store, summary["comparison_id"])
        self.assertFalse(facts["context_complete"])
        self.assertIsNone(facts["groups"][0]["references"][0]["conditions"])
    def test_recovery_only_report_is_not_eligible_for_upload(self):
        self.result["cases"][0]["name"] = "recovery_write"
        self.result["cases"][0]["idle_seconds"] = 30
        with self.assertRaises(CommunityError):
            project_result(self.result, "https://x.com/fixture_user", submission_id=ID)



if __name__ == "__main__":
    unittest.main()
