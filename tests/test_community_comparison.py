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
from community_comparison import build_comparison, read_comparison, _conditions


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
        self.assertTrue(all(r["conditions"]["write_budget_bytes"] == self.local["plan"]["write_budget_bytes"]
                            for r in facts["groups"][0]["references"]))

    def test_configured_budget_not_measured_writes_controls_comparability(self):
        for multiplier in (2, 4):
            with self.subTest(multiplier=multiplier):
                self.save(change=lambda p:p["plan"].update(write_budget_bytes=self.local["plan"]["write_budget_bytes"]*multiplier))
                summary, facts = self.facts()
                self.assertEqual(summary["comparable_group_count"], 0)
                for group in facts["groups"]:
                    self.assertTrue(all(m["n"] == 0 and m["median"] is None and m["delta_percent"] is None for m in group["metrics"]))
                    for reference in group["references"]:
                        self.assertFalse(reference["comparable"])
                        self.assertIn("write_budget_bytes", reference["differences"])
                        self.assertEqual(reference["conditions"]["write_budget_bytes"], self.local["plan"]["write_budget_bytes"]*multiplier)
                        # Only the configured limit changed; actual measurements stayed identical.
                        self.assertEqual(reference["values"]["iops"], self.local["cases"][group["case_index"]][group["direction"]]["iops"])

    def test_missing_and_invalid_budgets_are_unknown_in_either_direction(self):
        for value in (None, True, False, 0, -1, 1.5, float("inf"), float("nan"), 2**53, "16"):
            with self.subTest(value=value):
                sample = copy.deepcopy(self.local); sample["plan"]["write_budget_bytes"] = value
                self.assertIn("unknown_write_budget_bytes", _conditions(self.local, sample))
                self.assertIn("unknown_write_budget_bytes", _conditions(sample, self.local))
        sample = copy.deepcopy(self.local); del sample["plan"]["write_budget_bytes"]
        self.assertIn("unknown_write_budget_bytes", _conditions(self.local, sample))

    def test_legacy_snapshot_upgrade_preserves_facts_and_leaves_budget_unknown(self):
        self.save(); summary, _ = self.facts(); comparison_id = summary["comparison_id"]
        db = self.store.db
        before_metrics = [tuple(row) for row in db.execute("SELECT * FROM comparison_metrics ORDER BY case_index,direction,metric")]
        # Recreate the actual legacy column layout, retaining an unrelated future column.
        columns = [row[1] for row in db.execute("PRAGMA table_info(comparison_source_context)") if row[1] != "write_budget_bytes"]
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name='comparison_source_context'").fetchone()[0]
        definition = definition.replace("comparison_source_context(", "legacy_context(").replace("write_budget_bytes INTEGER,", "")
        db.execute(definition)
        names = ",".join(columns)
        db.execute(f"INSERT INTO legacy_context({names}) SELECT {names} FROM comparison_source_context")
        db.execute("DROP TABLE comparison_source_context")
        db.execute("ALTER TABLE legacy_context RENAME TO comparison_source_context")
        db.execute("ALTER TABLE comparison_source_context ADD COLUMN legacy_note TEXT DEFAULT 'sentinel'")
        before_context = [tuple(row) for row in db.execute("SELECT * FROM comparison_source_context ORDER BY case_index,direction,sample_id")]
        db.commit(); directory = self.store.directory; self.store.close()
        for _ in range(2):
            self.store = CommunityStore(directory); db = self.store.db
            info = list(db.execute("PRAGMA table_info(comparison_source_context)"))
            added = [row for row in info if row[1] == "write_budget_bytes"]
            self.assertEqual(len(added), 1); self.assertEqual(added[0][2], "INTEGER"); self.assertEqual(added[0][3], 0)
            self.assertEqual([tuple(row)[:-1] for row in db.execute("SELECT * FROM comparison_source_context ORDER BY case_index,direction,sample_id")], before_context)
            self.assertTrue(all(row[0] is None for row in db.execute("SELECT write_budget_bytes FROM comparison_source_context")))
            old = read_comparison(self.store, comparison_id)
            self.assertTrue(old["context_complete"])
            self.assertTrue(all(m["n"] == 0 and m["median"] is None and m["delta_percent"] is None for g in old["groups"] for m in g["metrics"]))
            self.assertTrue(all(not r["comparable"] and r["conditions"]["write_budget_bytes"] is None and "unknown_write_budget_bytes" in r["differences"] for g in old["groups"] for r in g["references"]))
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM comparison_metrics ORDER BY case_index,direction,metric")], before_metrics)
            self.store.close()
        self.store = CommunityStore(directory)
        new_summary, new = self.facts()
        self.assertEqual(new_summary["comparable_group_count"], 11)
        self.assertTrue(all(r["conditions"]["write_budget_bytes"] == self.local["plan"]["write_budget_bytes"] for g in new["groups"] for r in g["references"]))
        self.assertTrue(all(row[0] is None and row[1] == "sentinel" for row in self.store.db.execute(
            "SELECT write_budget_bytes,legacy_note FROM comparison_source_context WHERE comparison_id=?", (comparison_id,))))

    def test_known_historical_budget_difference_rechecks_only_its_frozen_values(self):
        self.save(); selected, _ = self.facts()
        self.store.db.execute("UPDATE comparison_source_context SET write_budget_bytes=write_budget_bytes*2")
        self.store.db.commit(); self.save(0)
        facts = read_comparison(self.store, selected["comparison_id"])
        self.assertTrue(all(not r["comparable"] and "write_budget_bytes" in r["differences"] for g in facts["groups"] for r in g["references"]))
        self.assertTrue(all(m["n"] == 0 and m["median"] is None for g in facts["groups"] for m in g["metrics"]))
        self.assertEqual(self.store.db.execute("SELECT MAX(n) FROM comparison_metrics").fetchone()[0], 3)

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
