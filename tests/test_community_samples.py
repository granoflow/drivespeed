"""Private legacy digests and redacted public reference snapshots; offline fixtures."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_community import payload, state_dir, ID
from test_community_network import receipt, REMOTE_ID
from community_data import CommunityError, digest, normalise_payload, without_private_metadata
from community_store import CommunityStore


class SamplePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.store = CommunityStore(state_dir())
        self.owner = payload()
        self.owner["quality"] = {"status": "quiet_observed"}
        self.owner["device_identity"] = {"version": 1, "fingerprint": "b" * 64, "source": "smartctl:serial_number"}
        self.store.create(self.owner, "fixture-run", "fixture-only-path")

    def tearDown(self):
        self.store.close()

    def test_new_remote_snapshots_drop_private_metadata_and_preserve_owner(self):
        incoming = receipt(self.owner)
        incoming["similar"]["items"][0]["differences"] = ["measurement_quality", "unknown_condition", "file_bytes"]
        before = copy.deepcopy(incoming)
        self.store.save_receipt(ID, incoming)
        sample = self.store.samples(ID)[0]
        self.assertEqual(sample["record"]["submission"], normalise_payload(without_private_metadata(before["similar"]["items"][0]["record"]["submission"])))
        self.assertEqual(sample["differences"], ["unknown_condition", "file_bytes"])
        self.assertNotIn('"quality"', json.dumps(sample)); self.assertNotIn('"device_identity"', json.dumps(sample))
        self.assertNotIn("b" * 64, json.dumps(sample))
        self.assertIsNone(self.store.db.execute("SELECT status FROM measurement_quality WHERE owner_id=? AND report_id=?", (ID, REMOTE_ID)).fetchone())
        self.assertEqual(self.store.payload(ID), normalise_payload(self.owner))
        self.assertEqual(incoming, before)

    def legacy(self):
        incoming = receipt(self.owner)
        self.store.save_receipt(ID, incoming)
        old = copy.deepcopy(incoming["similar"]["items"][0]["record"]["submission"])
        old.pop("device_identity")  # Original sample schema never persisted private disk identity.
        db = self.store.db
        db.execute("INSERT INTO measurement_quality VALUES(?,?,?)", (ID, REMOTE_ID, old["quality"]["status"]))
        db.execute("UPDATE samples SET expected_hash=? WHERE parent_id=? AND id=?", (digest(old), ID, REMOTE_ID))
        db.execute("INSERT INTO sample_differences VALUES(?,?,?,?)", (ID, REMOTE_ID, "measurement_quality", 0))
        db.commit()
        return old

    def test_legacy_digest_is_checked_before_redaction_without_mutating_snapshot(self):
        old = self.legacy(); db = self.store.db
        tables = ("samples", "measurement_quality", "sample_differences")
        before = {table: [tuple(r) for r in db.execute("SELECT * FROM " + table)] for table in tables}
        public = self.store.samples(ID)[0]
        self.assertEqual(public["record"]["submission"], normalise_payload(without_private_metadata(old)))
        self.assertEqual(public["differences"], ["unknown_condition"])
        for table, rows in before.items():
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM " + table)], rows)

    def test_corrupt_legacy_private_metadata_still_fails_integrity(self):
        self.legacy()
        self.store.db.execute("UPDATE measurement_quality SET status='interference_observed' WHERE owner_id=? AND report_id=?", (ID, REMOTE_ID))
        self.store.db.commit()
        with self.assertRaisesRegex(CommunityError, "integrity_mismatch"):
            self.store.samples(ID)


if __name__ == "__main__":
    unittest.main()
