"""Community projection/integrity/cache tests: fixture files, no real benchmarks."""

import copy
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import community_data as data
import community
from community_store import CommunityStore
from workloads import fio_arguments, MIB


ID = "11111111-1111-4111-8111-111111111111"


def local_result():
    job = dict(name="seq_read", rw="read", bs=MIB, qd=1, runtime=1, io_bytes=MIB)
    command = fio_arguments("PRIVATE-FIO-PATH", job, MIB, "posixaio", "PRIVATE-RAW", "PRIVATE-LOG")
    result = {"schema_version": 1, "run_id": "private-run", "created": "2026-10-28T12:00:00.123456+00:00",
        "tool": {"version": "unknown", "method_version": "drivespeed-file-v1"},
        "target": {"path": "PRIVATE-PATH", "mount": "PRIVATE-MOUNT", "device": "PRIVATE-ID", "serial": "PRIVATE-SERIAL",
                   "total_bytes": 1000 * MIB, "free_bytes": 750 * MIB, "scope": "physical", "protocol": "USB", "filesystem": "apfs"},
        "environment": {"os": "PRIVATE-HOST-PLATFORM", "os_family": "macos", "os_version": "15.0", "architecture": "arm64",
                        "engine": "posixaio", "fio": {"version": "fio-3.43", "path": "PRIVATE-FIO-PATH"}},
        "plan": {"profile": "full", "file_bytes": MIB, "write_budget": 2 * MIB, "seconds": 1, "sustained": 1},
        "hardware": {"computer": {"model": "Display with PRIVATE-HOST", "matching_model": "Mac99,1", "source": "system_profiler"},
                     "drive": {"model": "Fixture SSD", "matching_model": "Fixture SSD", "source": "lsblk:physical-device"},
                     "controller": {"model": None, "source": "unavailable", "candidates": ["PRIVATE-BRIDGE"]},
                     "enclosure": {"model": "Fixture box", "source": "user"}},
        "outcome": "complete", "cases": [{**job, "status": "completed", "command": command,
            "metrics": {"runtime_ms": 1000, "read": {"bytes": MIB, "mb_s": MIB / 1e6, "iops": 1,
                "latency_us": {"50": 2, "95": 3, "99": 4}},
                "write": {"bytes": 0, "mb_s": 0, "iops": 0}, "achieved_depth": {"1": 100}}}],
        "notes": ["PRIVATE-NOTE"], "user_input": {"self_analysis": "PRIVATE-ANALYSIS"}}

    for name, (rw, bs, qd) in data.FULL_CASES.items():
        if name == "seq_read":
            continue
        item = copy.deepcopy(result["cases"][0])
        item.update(name=name, rw=rw, bs=bs, qd=qd)
        item["command"] = fio_arguments("PRIVATE-FIO-PATH", item, MIB, "posixaio", "PRIVATE-RAW", "PRIVATE-LOG")
        if rw in ("write", "randwrite"):
            item["metrics"]["write"] = item["metrics"]["read"]
            item["metrics"]["read"] = {"bytes": 0, "mb_s": 0, "iops": 0}
        result["cases"].append(item)
    return result



def payload():
    return data.project_result(local_result(), "https://x.com/fixture_user", submission_id=ID)


def state_dir():
    root = Path(tempfile.mkdtemp(prefix="drivespeed-community-fixture-")).resolve()
    if sys.platform == "win32":
        from community_storage import WindowsPrivateState
        root = root / "private"
        WindowsPrivateState(root).close()
        return root
    root.chmod(0o700)
    return root


class ProjectionTests(unittest.TestCase):
    def test_projection_is_whitelisted_and_does_not_mutate_source(self):
        original = local_result()
        before = copy.deepcopy(original)
        result = data.project_result(original, "https://x.com/fixture_user", submission_id=ID)
        self.assertEqual(original, before)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(result["hardware"]["computer"]["model"], "Mac99,1")
        self.assertIsNone(result["hardware"]["controller"]["model"])
        self.assertEqual(result["measured_at_device"], "2026-10-28T12:00:00.123456Z")
        self.assertIsNone(result["cases"][0]["read"]["latency_us"]["mean"])
        self.assertIsNone(result["cases"][0]["write"]["mb_s"])

    def test_missing_historical_parameters_are_not_filled_from_current_machine(self):
        for field in ("profile", "method_version", "os_version", "command"):
            result = local_result()
            if field == "profile":
                result["plan"].pop(field)
            elif field == "method_version":
                result["tool"].pop(field)
            elif field == "os_version":
                result["environment"].pop(field)
            else:
                result["cases"][0].pop(field)
            with self.subTest(field=field), patch("subprocess.run", side_effect=AssertionError("no commands")), self.assertRaises(data.CommunityError):
                data.project_result(result, "https://x.com/fixture_user")

    def test_changed_command_method_is_rejected(self):
        for original, replacement in (("--direct=1", "--direct=0"), ("--iodepth=1", "--iodepth=2"), ("--rw=read", "--rw=write")):
            result = local_result()
            result["cases"][0]["command"] = [replacement if value == original else value for value in result["cases"][0]["command"]]
            with self.subTest(replacement=replacement), self.assertRaises(data.CommunityError):
                data.project_result(result, "https://x.com/fixture_user")

    def test_invalid_or_missing_x_attribution_is_rejected(self):
        for address in ("https://example.com/user", "https://x.com/login", "https://x.com/name?x=1", "https://x.com/name/status/1", "http://x.com/user"):
            with self.subTest(address=address), self.assertRaises(data.CommunityError):
                data.project_result(local_result(), address)
        result = payload()
        result["attribution"] = None
        with self.assertRaises(data.CommunityError):
            data.validate_payload(result)

    def test_only_completed_full_report_is_projected(self):
        for change in (lambda r: r["plan"].update(profile="quick"),
                       lambda r: r.update(outcome="partial"),
                       lambda r: r["cases"].pop(),
                       lambda r: r["cases"][0].update(status="failed")):
            result = local_result(); change(result)
            with self.assertRaises(data.CommunityError):
                data.project_result(result, "https://x.com/fixture_user")

    def test_wrong_direction_does_not_count_as_completed_read(self):
        result = local_result()
        case = result["cases"][0]
        case["metrics"]["write"] = case["metrics"]["read"]
        case["metrics"]["read"] = {"bytes": 0, "mb_s": 0, "iops": 0}
        with self.assertRaises(data.CommunityError):
            data.project_result(result, "https://x.com/fixture_user")

    def test_intro_is_optional_but_plain_single_line(self):
        self.assertEqual(payload()["attribution"]["intro"], "")
        for intro in ("line\nbreak", "visit https://example.com", "line\u2028break", "<script>bad</script>", "x" * 81):
            with self.subTest(intro=intro), self.assertRaises(data.CommunityError):
                data.project_result(local_result(), "https://x.com/fixture_user", intro=intro)

    def test_completed_io_is_required_without_fabricating_missing_values(self):
        result = local_result()
        for case in result["cases"]:
            case["status"] = "failed"
        with self.assertRaises(data.CommunityError) as caught:
            data.project_result(result, "https://x.com/fixture_user")
        self.assertEqual(caught.exception.code, "no_measurements")

    def test_missing_or_invalid_measurement_time_is_not_invented(self):
        for seconds in (None, "PRIVATE-invalid", 0, float("inf")):
            result = local_result()
            result["cases"][0]["metrics"]["runtime_ms"] = seconds
            with self.subTest(seconds=seconds), self.assertRaises(data.CommunityError):
                data.project_result(result, "https://x.com/fixture_user")

    def test_ambiguous_fio_depth_bucket_is_not_a_precise_depth(self):
        result = local_result()
        result["cases"][0]["metrics"]["achieved_depth"] = {"1": 0, ">=64": 100}
        self.assertIsNone(data.project_result(result, "https://x.com/fixture_user")["cases"][0]["achieved_depth"])

    def test_invalid_zero_like_byte_counts_are_not_replaced_by_zero(self):
        for count in (None, False, 0.0, "", -1):
            result = local_result()
            result["cases"][0]["metrics"]["write"]["bytes"] = count
            with self.subTest(count=count), self.assertRaises(data.CommunityError):
                data.project_result(result, "https://x.com/fixture_user")
        result = local_result()
        result["cases"][0]["metrics"]["write"]["mb_s"] = 99
        with self.assertRaises(data.CommunityError):
            data.project_result(result, "https://x.com/fixture_user")

    def test_invalid_cache_assessment_has_a_safe_field_error(self):
        for assessment in (None, [], "PRIVATE"):
            result = local_result()
            result["cases"][0]["cache_test"] = True
            result["cache_assessment"] = assessment
            with self.subTest(assessment=assessment), self.assertRaises(data.CommunityError) as caught:
                data.project_result(result, "https://x.com/fixture_user")
            self.assertIn("cache_assessment", caught.exception.fields)

    def test_unknown_fields_bool_numbers_and_nonfinite_values_are_rejected(self):
        for value in (True, float("nan"), float("inf"), 10 ** 1000):
            item = payload()
            item["storage"]["capacity_bytes"] = value
            with self.subTest(kind=type(value).__name__), self.assertRaises(data.CommunityError):
                data.validate_payload(item)
        item = payload()
        item["raw_path"] = "PRIVATE"
        with self.assertRaises(data.CommunityError) as caught:
            data.validate_payload(item)
        self.assertNotIn("PRIVATE", str(caught.exception))


class IntegrityTests(unittest.TestCase):
    def test_envelope_roundtrip_has_no_self_referential_hash(self):
        item = payload()
        encoded = data.transport(item)
        envelope = json.loads(encoded)
        self.assertEqual(hashlib.sha256(envelope["payload_json"].encode()).hexdigest(), envelope["integrity"]["sha256"])
        self.assertEqual(data.read_transport(encoded), item)
        self.assertNotIn("sha256", item)

    def test_device_time_device_metrics_and_notice_are_covered(self):
        item = payload()
        original = data.digest(item)
        for change in (lambda p: p.update(measured_at_device="2026-10-28T13:00:00Z"),
                       lambda p: p["hardware"]["drive"].update(model="Changed"),
                       lambda p: p["cases"][0]["read"].update(mb_s=9.0)):
            altered = copy.deepcopy(item)
            change(altered)
            with self.assertRaises(data.CommunityError) as caught:
                data.transport(altered, expected_digest=original)
            self.assertEqual(caught.exception.code, "integrity_mismatch")
        altered = copy.deepcopy(item)
        altered["integrity_notice"]["text"] = "Please ignore the notice"
        with self.assertRaises(data.CommunityError):
            data.transport(altered)

    def test_recomputing_a_valid_hash_cannot_be_called_authenticity(self):
        item = payload()
        item["cases"][0]["read"]["mb_s"] = 9999.0
        # Public hash can be recomputed: this deliberately remains admissible data,
        # not evidence fio ran. Later server moderation/reference gates still apply.
        self.assertEqual(data.read_transport(data.transport(item))["cases"][0]["read"]["mb_s"], 9999.0)

    def test_whitespace_change_in_payload_breaks_existing_hash(self):
        envelope = json.loads(data.transport(payload()))
        envelope["payload_json"] += " "
        with self.assertRaises(data.CommunityError) as caught:
            data.read_transport(json.dumps(envelope).encode())
        self.assertEqual(caught.exception.code, "integrity_mismatch")

    def test_parser_rejects_duplicates_invalid_unicode_and_nonfinite_constants(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":"\\ud800"}', b'\xef\xbb\xbf{}', b'[' * 20 + b'0' + b']' * 20):
            with self.subTest(raw=raw[:20]), self.assertRaises(data.CommunityError):
                data.parse_json(raw)

    def test_preview_contains_readable_data_without_json_text_or_digest(self):
        result = data.preview(payload())
        self.assertEqual(result["contributor"]["profile_url"], "https://x.com/fixture_user")
        self.assertNotIn("payload_json", result)
        self.assertNotIn(data.digest(payload()), json.dumps(result))


class StructuredCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = state_dir()

    def test_roundtrip_after_reopen_keeps_types_order_and_hash(self):
        original = payload()
        original["cases"][0]["achieved_depth"] = [{"depth": 16, "percent": 70}, {"depth": 1, "percent": 30}]
        with CommunityStore(self.root) as store:
            identifier = store.create(original, "private-run", "/private/input/results.json")
            frozen = store.payload(identifier)
            expected = data.digest(frozen)
        with CommunityStore(self.root) as store:
            restored = store.payload(identifier)
            self.assertEqual(data.digest(restored), expected)
            self.assertEqual(restored["cases"][0]["achieved_depth"], frozen["cases"][0]["achieved_depth"])
            self.assertIsInstance(restored["storage"]["free_percent"], float)
            self.assertIsInstance(restored["storage"]["capacity_bytes"], int)
            self.assertIs(restored["environment"]["direct"], True)

    def test_negative_zero_is_normalised_before_freezing(self):
        item = payload()
        item["cases"][0]["read"]["latency_us"]["p50"] = -0.0
        with CommunityStore(self.root) as store:
            store.create(item, "run", "/fixture/results.json")
            self.assertEqual(store.payload(ID)["cases"][0]["read"]["latency_us"]["p50"], 0.0)

    def test_sqlite_contains_structured_fields_not_serialized_request(self):
        with CommunityStore(self.root) as store:
            store.create(payload(), "run", "/fixture/results.json")
            for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                columns = store.db.execute("PRAGMA table_info(" + row[0] + ")").fetchall()
                self.assertFalse(any(column[2] == "BLOB" for column in columns))
                self.assertFalse(any(column[1] in ("payload_json", "raw_json", "envelope_json") for column in columns))
        self.assertNotIn(b'"integrity_notice":', (self.root / "community.sqlite3").read_bytes())
        self.assertFalse(list(self.root.glob("*.json")))
        if sys.platform != "win32":
            self.assertEqual((self.root / "community.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_mutated_cached_measurement_fails_before_send(self):
        with CommunityStore(self.root) as store:
            store.create(payload(), "run", "/fixture/results.json")
            with store.db:
                store.db.execute("UPDATE measurements SET mb_s=88 WHERE direction='read' AND ordinal=0")
            with self.assertRaises(data.CommunityError) as caught:
                store.payload(ID)
            self.assertEqual(caught.exception.code, "integrity_mismatch")

    def test_snapshot_cannot_be_overwritten_and_consent_binds_current_version(self):
        with CommunityStore(self.root) as store:
            store.create(payload(), "run", "/fixture/results.json")
            with self.assertRaises(data.CommunityError):
                store.require_consent(ID)
            with self.assertRaises(data.CommunityError):
                store.consent(ID, "old-optional-account-version")
            store.consent(ID, data.POLICY["notice_version"])
            restored, consent = store.require_consent(ID)
            self.assertEqual(data.digest(restored), consent["content_sha256"])
            with self.assertRaises(data.CommunityError):
                store.create(payload(), "run", "/fixture/results.json")
            self.assertEqual(len(store.list_reports()), 1)

    def test_refusal_preserves_history_and_revokes_unsent_consent(self):
        with CommunityStore(self.root) as store:
            store.create(payload(), "run", "/fixture/results.json")
            store.consent(ID, data.POLICY["notice_version"])
            store.remember("run", "/fixture/results.json")
            self.assertEqual(store.list_reports("declined")[0]["submission_id"], ID)
            with self.assertRaises(data.CommunityError):
                store.require_consent(ID)
            # A later explicit decision can approve the frozen preview again.
            store.consent(ID, data.POLICY["notice_version"])
            self.assertEqual(store.require_consent(ID)[0]["submission_id"], ID)
            self.assertEqual(store.list_reports()[0]["decision"], "approved")

    def test_unsafe_directory_and_unrelated_database_are_not_modified(self):
        if sys.platform != "win32":
            self.root.chmod(0o755)
            with self.assertRaises(data.CommunityError):
                CommunityStore(self.root)
            self.root.chmod(0o700)
        path = self.root / "community.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE user_content(value TEXT)")
            db.execute("INSERT INTO user_content VALUES ('preserve')")
        if sys.platform != "win32":
            path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(data.CommunityError):
            CommunityStore(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_multiple_store_instances_do_not_mix_distinct_snapshots(self):
        with CommunityStore(self.root) as first, CommunityStore(self.root) as second:
            first.create(payload(), "first", "/fixture/first")
            other = payload()
            other["submission_id"] = "22222222-2222-4222-8222-222222222222"
            second.create(other, "second", "/fixture/second")
            self.assertEqual(len(first.list_reports()), 2)
            self.assertEqual(second.payload(ID)["submission_id"], ID)


class OfflineCLITests(unittest.TestCase):
    def setUp(self):
        self.root = state_dir()
        self.source = self.root / "results.json"
        result = local_result()
        result["measurement_quality"] = {"status": "quiet_observed"}
        self.source.write_text(json.dumps(result), encoding="utf-8")
        self.cache = self.root / "cache"

    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()), \
             patch("subprocess.run", side_effect=AssertionError("offline operation must not launch processes")):
            code = community.main(list(args))
        return code, json.loads(output.getvalue())

    def test_unqualified_background_blocks_prepare_without_creating_state(self):
        for status in (None, "visibility_limited", "interference_observed"):
            from test_auto_full import community_result
            result = community_result(status)
            if status is None:
                result.pop("measurement_quality")
            self.source.write_text(json.dumps(result))
            before = self.source.read_bytes()
            code, output = self.call("prepare", "--input", str(self.source),
                "--x-profile", "https://x.com/fixture_user", "--state-dir", str(self.cache),
                "--ui-language", "zh")
            self.assertNotEqual(code, 0)
            self.assertIn("measurement_quality", json.dumps(output))
            self.assertIn("重新测量", json.dumps(output, ensure_ascii=False))
            self.assertFalse(self.cache.exists())
            self.assertEqual(self.source.read_bytes(), before)

    def test_old_unqualified_snapshot_cannot_receive_new_publication_consent(self):
        with CommunityStore(self.cache) as store:
            store.create(payload(), "old-run", self.source)
        code, output = self.call("consent", "--submission-id", ID, "--decision", "publish",
            "--notice-version", data.POLICY["notice_version"], "--state-dir", str(self.cache))
        self.assertEqual(code, 2)
        self.assertEqual(output["error"]["code"], "measurement_quality")
        code, output = self.call("preview", "--submission-id", ID, "--state-dir", str(self.cache))
        self.assertEqual(code, 0)
        code, output = self.call("consent", "--submission-id", ID, "--decision", "declined",
            "--state-dir", str(self.cache))
        self.assertEqual(code, 0)

    def test_prepare_and_consent_are_offline_readable_and_do_not_write_json_cache(self):
        before = self.source.read_bytes()
        code, prepared = self.call("prepare", "--input", str(self.source), "--x-profile", "https://x.com/fixture_user", "--state-dir", str(self.cache))
        self.assertEqual(code, 0)
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["preview"]["contributor"]["profile_url"], "https://x.com/fixture_user")
        self.assertNotIn("PRIVATE", json.dumps(prepared))
        self.assertNotIn("payload_json", prepared)
        self.assertFalse(list(self.cache.glob("*.json")))
        self.assertEqual(self.source.read_bytes(), before)
        code, consented = self.call("--state-dir", str(self.cache), "consent", "--submission-id", prepared["submission_id"], "--decision", "publish", "--notice-version", prepared["notice_version"])
        self.assertEqual(code, 0)
        self.assertEqual(consented["status"], "consented")

    def test_frozen_preview_and_refusal_do_not_depend_on_original_result(self):
        _, prepared = self.call("prepare", "--input", str(self.source), "--x-profile", "https://x.com/fixture_user", "--state-dir", str(self.cache))
        identifier = prepared["submission_id"]
        self.call("consent", "--submission-id", identifier, "--decision", "publish", "--notice-version", prepared["notice_version"], "--state-dir", str(self.cache))
        with patch.object(community, "read_result", side_effect=AssertionError("original result unavailable")):
            code, displayed = self.call("preview", "--submission-id", identifier, "--state-dir", str(self.cache))
            self.assertEqual(code, 0)
            self.assertEqual(displayed["preview"], prepared["preview"])
            self.assertEqual(displayed["privacy_url"], data.POLICY["destination"] + "/privacy")
            self.assertEqual(displayed["notice_version"], prepared["notice_version"])
            code, declined = self.call("consent", "--submission-id", identifier, "--decision", "declined", "--state-dir", str(self.cache))
            self.assertEqual((code, declined["status"], declined["remote_unchanged"]), (0, "declined", True))
        with CommunityStore(self.cache) as store:
            with self.assertRaises(data.CommunityError):
                store.require_consent(identifier)

    def test_list_and_remember_stay_local(self):
        code, remembered = self.call("remember", "--input", str(self.source), "--decision", "declined", "--state-dir", str(self.cache))
        self.assertEqual((code, remembered["decision"]), (0, "declined"))
        code, listing = self.call("list", "--state-dir", str(self.cache))
        self.assertEqual(code, 0)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["decision"], "declined")
        self.assertEqual(listing["items"][0]["result_path"], str(self.source))
        with CommunityStore(self.cache) as store:
            self.assertEqual(store.db.execute("SELECT decision FROM decisions").fetchone()[0], "declined")

    def test_duplicate_and_unknown_arguments_do_not_echo_private_values(self):
        for args in (("prepare", "--input", "PRIVATE-A", "--input", "PRIVATE-B"),
                     ("prepare", "--input", "PRIVATE", "--x-profile", "https://x.com/user", "--unexpected", "PRIVATE")):
            code, error = self.call(*args)
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "invalid_arguments")
            self.assertNotIn("PRIVATE", json.dumps(error))

    def test_abbreviated_options_cannot_override_full_options(self):
        for args in (("prepare", "--input", str(self.source), "--in", "PRIVATE"),
                     ("list", "--state-dir", str(self.cache), "--state-d", "PRIVATE")):
            code, error = self.call(*args)
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "invalid_arguments")
            self.assertFalse(self.cache.exists())

    def test_nonregular_input_is_rejected_without_opening_it(self):
        with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
             patch.object(community.os, "open", side_effect=AssertionError("must not open FIFO/device")):
            with self.assertRaises(data.CommunityError) as caught:
                community.read_result(self.source)
        self.assertEqual(caught.exception.code, "invalid_input_file")

    def test_symlink_result_is_rejected_before_cache_creation(self):
        link = self.root / "link.json"
        link.symlink_to(self.source)
        code, error = self.call("prepare", "--input", str(link), "--x-profile", "https://x.com/user", "--state-dir", str(self.cache))
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "unsafe_input_path")
        self.assertFalse(self.cache.exists())

    def test_missing_metadata_is_not_guessed_or_persisted(self):
        raw = local_result()
        raw["measurement_quality"] = {"status": "quiet_observed"}
        raw["plan"].pop("profile")
        self.source.write_text(json.dumps(raw))
        code, error = self.call("prepare", "--input", str(self.source), "--x-profile", "https://x.com/user", "--state-dir", str(self.cache))
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "missing_parameters")
        self.assertFalse(self.cache.exists())

    def test_localized_preview_changes_notice_without_changing_snapshot(self):
        code, prepared = self.call("prepare", "--input", str(self.source), "--x-profile", "https://x.com/fixture_user",
                                   "--state-dir", str(self.cache), "--ui-language", "zh-CN")
        self.assertEqual(code, 0)
        self.assertIn("本人X链接", prepared["preview"]["notice"])
        identifier = prepared["submission_id"]
        with CommunityStore(self.cache) as store:
            original_digest = data.digest(store.payload(identifier))
        code, displayed = self.call("preview", "--submission-id", identifier, "--state-dir", str(self.cache), "--ui-language", "ar")
        self.assertEqual(code, 0)
        self.assertNotEqual(displayed["preview"]["notice"], prepared["preview"]["notice"])
        with CommunityStore(self.cache) as store:
            self.assertEqual(data.digest(store.payload(identifier)), original_digest)

    def test_consent_error_explains_recovery_and_does_not_grant_consent(self):
        _, prepared = self.call("prepare", "--input", str(self.source), "--x-profile", "https://x.com/fixture_user", "--state-dir", str(self.cache))
        code, failed = self.call("consent", "--submission-id", prepared["submission_id"], "--decision", "publish", "--state-dir", str(self.cache), "--ui-language", "zh")
        self.assertEqual(code, 2)
        error = failed["error"]
        self.assertEqual(error["code"], "consent_required")
        self.assertIn("preview", error["next_action"])
        self.assertIn("明确同意", error["next_action"])
        self.assertTrue(error["docs"].endswith("#consent_required"))
        self.assertEqual(error["side_effects"]["remote"], "none")
        with CommunityStore(self.cache) as store:
            with self.assertRaises(data.CommunityError):
                store.require_consent(prepared["submission_id"])

    def test_unknown_locale_or_incomplete_custom_pack_does_not_create_state(self):
        code, failed = self.call("list", "--state-dir", str(self.cache), "--ui-language", "unsupported-fixture")
        self.assertEqual((code, failed["error"]["code"]), (2, "unsupported_language"))
        messages = self.root / "messages.json"
        messages.write_text('{"share_notice":"PRIVATE incomplete translation"}')
        code, failed = self.call("list", "--state-dir", str(self.cache), "--ui-messages", str(messages))
        self.assertEqual((code, failed["error"]["code"]), (2, "invalid_messages"))
        self.assertNotIn("PRIVATE", json.dumps(failed))
        self.assertFalse(self.cache.exists())

    def test_unexpected_storage_error_has_safe_trace_and_action(self):
        with patch.object(community, "CommunityStore", side_effect=sqlite3.OperationalError("PRIVATE SQL and path")):
            code, failed = self.call("list", "--state-dir", str(self.cache), "--ui-language", "zh")
        error = failed["error"]
        self.assertEqual(code, 5)
        self.assertEqual(error["context"]["cause_type"], "OperationalError")
        self.assertTrue(error["trace"])
        self.assertTrue(error["next_action"])
        self.assertEqual(error["side_effects"], {"remote": "none", "local": "check_history"})
        self.assertNotIn("PRIVATE", json.dumps(failed))
        for frame in error["trace"]:
            self.assertNotIn("/", frame["file"])


if __name__ == "__main__":
    unittest.main()
