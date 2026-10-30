"""Local display privacy and immutable upload data; synthetic inputs only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_community import local_result, ID, state_dir
from test_community_network import receipt, ORIGIN
from community_data import project_result, transport, read_transport, digest, CommunityError
from community_network import check_receipt, NetworkError
from community_store import CommunityStore
from community_comparison import build_comparison, read_comparison
from progress import messages
from report import render_report

PRIVATE = "TEST-COMPUTER-PRIVATE-42"


class ReportPrivacyTests(unittest.TestCase):
    def test_conversational_computer_name_is_hidden_even_when_detection_is_unknown_or_different(self):
        for detected in (None,"DETECTED-COMPUTER-PRIVATE-77"):
            result=local_result()
            result["hardware"]["computer"].update(model=detected,matching_model=detected)
            result["user_input"]={"run_id":result["run_id"],"language":"en","device_models":{
                "电脑":"USER-COMPUTER-PRIVATE-88","Drive":"EXTRA-DRIVE-KEEP-99"},"test_notes":"Keep this user note."}
            original=json.dumps(result,sort_keys=True)
            for show in (False,True):
                with self.subTest(detected=detected,show=show),tempfile.TemporaryDirectory() as directory:
                    artifact=render_report(result,directory,"en",messages("en"),show_computer_model=show)
                    source=Path(artifact["html"]).read_text()
                self.assertEqual("USER-COMPUTER-PRIVATE-88" in source,show)
                if detected:self.assertEqual(detected in source,show)
                self.assertIn("EXTRA-DRIVE-KEEP-99",source)
                self.assertIn("Keep this user note.",source)
                self.assertEqual(json.dumps(result,sort_keys=True),original)

    def test_display_toggle_changes_both_reports_without_changing_facts_or_upload(self):
        result = local_result(); result.pop("user_input", None)
        result["hardware"]["computer"].update(model=PRIVATE, matching_model=PRIVATE)
        result["target"]["negotiated_bps"] = 10_000_000_000
        result["evaluation"] = {"run_id": result["run_id"], "language": "en", "sections": [
            {"kind": kind, "title": title, "paragraphs": ["Valid measured result.", PRIVATE+" automatic model commentary."],
             "sources": [{"label": PRIVATE+" source", "url": "https://example.com/"+PRIVATE, "checked_at": "2026-10-30"}]}
            for kind,title in (("bottleneck","Bottleneck"),("upgrades","Upgrades"),("anomalies","Anomalies"))]}
        payload = project_result(result,"https://x.com/fixture_user",submission_id=ID)
        original = json.dumps(result,sort_keys=True); upload = transport(payload)
        with CommunityStore(state_dir()) as store:
            store.create(payload,result["run_id"],"synthetic-only")
            remote = receipt(store.payload(ID)); remote["similar"]["items"][0]["record"]["submission"]["hardware"]["computer"]["model"] = "REMOTE-COMPUTER-PRIVATE-99"
            store.save_receipt(ID,remote)
            selected = build_comparison(store,result,ID); facts = read_comparison(store,selected["comparison_id"])
            for show in (False,True):
                with self.subTest(show=show), tempfile.TemporaryDirectory() as folder:
                    artifacts = render_report(result,folder,"en",messages("en"),comparison=facts,show_computer_model=show)
                    for key in ("html","comparison_html"):
                        source = Path(artifacts[key]).read_text()
                        self.assertEqual(PRIVATE in source,show)
                        self.assertNotIn("REMOTE-COMPUTER-PRIVATE-99",source)
                        self.assertIn("Valid measured result.",source)
                        self.assertIn("MB/s",source)
                self.assertEqual(json.dumps(result,sort_keys=True),original)
                self.assertEqual(transport(project_result(result,"https://x.com/fixture_user",submission_id=ID)),upload)
        tampered=json.loads(upload)
        tampered["payload_json"]=tampered["payload_json"].replace(PRIVATE,"OTHER-COMPUTER-42")
        with self.assertRaisesRegex(CommunityError,"integrity_mismatch"):
            read_transport(json.dumps(tampered).encode())

    def test_network_budget_codes_pass_through_typed_samples_and_comparison(self):
        result = local_result(); result["target"]["negotiated_bps"] = 10_000_000_000
        result["measurement_quality"] = {"status": "quiet_observed"}  # Explicit synthetic condition.
        payload = project_result(result,"https://x.com/fixture_user",submission_id=ID)
        for difference in (None,"write_budget_bytes","unknown_write_budget_bytes"):
            with self.subTest(difference=difference), CommunityStore(state_dir()) as store:
                store.create(payload,result["run_id"],"synthetic-only")
                value = receipt(payload); reference = value["similar"]["items"][0]
                reference["record"]["submission"]["hardware"]["computer"] = {"model":None,"source":"unknown"}
                if difference == "write_budget_bytes":
                    reference["record"]["submission"]["plan"]["write_budget_bytes"] *= 2
                reference["differences"] = [difference] if difference else []
                checked = check_receipt(value,ID,digest(payload),ORIGIN)
                store.save_receipt(ID,checked)
                samples = store.samples(ID)
                self.assertEqual(samples[0]["differences"],reference["differences"])
                self.assertIsNone(samples[0]["record"]["submission"]["hardware"]["computer"]["model"])
                sample_hash=store.db.execute("SELECT expected_hash FROM samples").fetchone()[0]
                self.assertEqual(sample_hash,digest(samples[0]["record"]["submission"]))
                self.assertNotEqual(sample_hash,digest(payload))
                selected = build_comparison(store,result,ID); facts = read_comparison(store,selected["comparison_id"])
                if difference == "unknown_write_budget_bytes":
                    store.db.execute("UPDATE comparison_source_context SET write_budget_bytes=NULL")
                    store.db.commit(); facts = read_comparison(store,selected["comparison_id"])
                first = facts["groups"][0]["references"][0]
                self.assertEqual(first["comparable"],difference is None)
                if difference:
                    self.assertIn(difference,first["differences"])
                    self.assertTrue(all(m["n"] == 0 for g in facts["groups"] for m in g["metrics"]))
                store.db.execute("UPDATE sample_hardware SET model='TAMPERED-DRIVE' WHERE kind='drive'")
                store.db.commit()
                with self.assertRaisesRegex(CommunityError,"integrity_mismatch"):
                    store.samples(ID)
                store.db.execute("UPDATE comparisons SET content_sha256=?",("0"*64,))
                store.db.commit()
                with self.assertRaisesRegex(CommunityError,"integrity_mismatch"):
                    read_comparison(store,selected["comparison_id"])
        invalid = receipt(payload); invalid["similar"]["items"][0]["differences"] = ["arbitrary_budget_code"]
        with self.assertRaisesRegex(NetworkError,"invalid_response"):
            check_receipt(invalid,ID,digest(payload),ORIGIN)
