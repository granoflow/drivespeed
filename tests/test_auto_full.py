"""Automatic full workloads and evidence qualification; no real disk workload."""

import copy
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workloads import GIB, MIB, auto_plan, core_completeness, collection_status, metrics, active_runtime_ms, case_core_missing
import benchmark
import system_io
from progress import messages
from write_allowance import WriteAllowance, plan_hash
import community_data
from community_store import CommunityStore


def complete_result(cap=16 * GIB):
    config = auto_plan(2_000 * GIB, 1_000 * GIB, cap, cap)
    cases = []
    for job in config["jobs"]:
        active = ("read", "write") if job["rw"] == "randrw" else (
            "read" if job["rw"] in ("read", "randread") else "write",
        )
        data = {"runtime_ms": 500}
        for direction in ("read", "write"):
            count = job["io_bytes"] // len(active) if direction in active else 0
            data[direction] = dict(bytes=count, mb_s=count / 500_000, iops=count / 4096 / .5,
                                   latency_us={k: 12.34 if count else 0 for k in ("mean", "50", "95", "99")})
        cases.append(dict(**job, status="completed", metrics=data, series=[], stop_reason="byte_limit"))
    return dict(plan=config, cases=cases)


class AutomaticPlanTests(unittest.TestCase):
    def test_space_budget_and_workload_boundaries(self):
        for cap in (8 * GIB, 16 * GIB, 26_040_606_720, 64 * GIB):
            config = auto_plan(2_000 * GIB, 1_000 * GIB, cap, cap)
            self.assertEqual(len(config["jobs"]), 11)
            self.assertEqual(len({j["name"] for j in config["jobs"]}), 11)
            self.assertEqual(config["jobs"][0]["name"], "sustained")
            self.assertNotIn("prepare", {j["name"] for j in config["jobs"]})
            self.assertLessEqual(config["file_bytes"] + config["reserve_bytes"] + 64 * MIB, 1_000 * GIB)
            total = sum(j["io_bytes"] for j in config["jobs"] if j["rw"] not in ("read", "randread"))
            self.assertEqual(total, config["max_write_bytes"])
            self.assertLessEqual(total, cap)
            self.assertGreaterEqual(config["jobs"][0]["io_bytes"], config["file_bytes"])
        self.assertEqual(auto_plan(100 * GIB, 20 * GIB, 64 * GIB, 16 * GIB)["write_budget"], 16 * GIB)

    def test_unavailable_space_budget_and_invalid_inputs_fail(self):
        for values in ((100 * GIB, 10 * GIB, 64 * GIB, 64 * GIB),
                       (100 * GIB, 20 * GIB, 7 * GIB, 64 * GIB),
                       (100 * GIB, 20 * GIB, 64 * GIB, 0)):
            with self.assertRaises(ValueError):
                auto_plan(*values)
        for value in (None, True, -1, 2**64, math.inf, "10"):
            for index in range(4):
                values = [100 * GIB, 20 * GIB, 64 * GIB, 64 * GIB]
                values[index] = value
                with self.subTest(value=value, index=index), self.assertRaises(ValueError):
                    auto_plan(*values)

    def test_integer_alignment_at_mib_space_boundary(self):
        for extra in (-1, 0, 1, MIB - 1):
            free = 15 * GIB + extra
            config = auto_plan(100 * GIB, free, 64 * GIB, 64 * GIB)
            self.assertEqual(config["file_bytes"] % MIB, 0)
            self.assertLessEqual(config["file_bytes"], 4 * (free - 10 * GIB - 64 * MIB) // 5)


class EvidenceTests(unittest.TestCase):
    def test_active_endpoint_runtime_preserves_direction_and_legacy_absence(self):
        data = dict(runtime_ms=29999,write=dict(bytes=4096,runtime_ms=30000),read=dict(bytes=0,runtime_ms=999999))
        before = copy.deepcopy(data)
        self.assertEqual(active_runtime_ms(data,"write"),30000)
        self.assertEqual(data,before)
        data["write"]["runtime_ms"] = 29999
        self.assertEqual(active_runtime_ms(data,"write"),29999)
        data["write"].pop("runtime_ms")
        self.assertEqual(active_runtime_ms(data,"write"),29999)
        data["read"].update(bytes=4096,runtime_ms=30000)
        self.assertEqual(active_runtime_ms(data,"randrw"),30000)
        for field in ("bytes","runtime_ms"):
            for value in (None,0,-1,True,math.nan,2**64):
                invalid = copy.deepcopy(data)
                invalid["write"][field] = value
                self.assertEqual(active_runtime_ms(invalid,"randrw"),0,(field,value))
        for value in (None,0,-1,True,math.nan):
            invalid = copy.deepcopy(data)
            invalid["runtime_ms"] = value
            self.assertEqual(active_runtime_ms(invalid,"write"),0)

    def test_core_time_endpoint_uses_valid_active_runtime(self):
        for name in ("seq_write","mixed"):
            result = complete_result()
            job = next(j for j in result["plan"]["jobs"] if j["name"]==name)
            case = next(c for c in result["cases"] if c["name"]==name)
            directions = ("read","write") if name=="mixed" else ("write",)
            case["metrics"]["runtime_ms"] = 29999
            case["series"] = []
            for direction in directions:
                case["metrics"][direction]["runtime_ms"] = 30000
                case["metrics"][direction]["bytes"] //= 2
                case["series"].extend(dict(seconds=t,mb_s=100,direction=direction) for t in range(1,31))
            self.assertEqual(case_core_missing(case,job,result["plan"]),[])
            case["metrics"]["runtime_ms"] = 30000
            for direction in directions:
                case["metrics"][direction]["runtime_ms"] = 29999
            self.assertIn(name+".endpoint",case_core_missing(case,job,result["plan"]))

    def test_real_mean_is_completion_latency_in_microseconds_once(self):
        source = {"error": 0, "job_runtime": 500, "read": {"io_bytes": 4096,
                  "bw_bytes": 8192, "iops": 2, "clat_ns": {"mean": 1234000,
                  "percentile": {"50.000000": 1000000, "95.000000": 2000000, "99.000000": 3000000}}}}
        result = metrics({"jobs": [source]})
        self.assertEqual(result["read"]["latency_us"], {"mean": 1234, "50": 1000, "95": 2000, "99": 3000})

    def test_legal_byte_endpoint_short_window_is_core_complete(self):
        for cap in (16 * GIB, 64 * GIB):
            result = complete_result(cap)
            self.assertEqual(core_completeness(result), {"complete": True, "missing_core": []})
            self.assertEqual(collection_status(result["cases"][0]), "short_window")

    def test_missing_expected_logs_are_partial_not_short_window(self):
        result = complete_result()
        result["cases"][0]["metrics"]["runtime_ms"] = 2100
        self.assertEqual(collection_status(result["cases"][0]), "collection_incomplete")
        self.assertIn("sustained.collection", core_completeness(result)["missing_core"])

    def test_timer_boundary_preserves_final_metrics_without_inventing_a_point(self):
        result = complete_result()
        case = result["cases"][0]
        case["metrics"]["runtime_ms"] = 3999
        case["metrics"]["write"]["runtime_ms"] = 4000
        case["series"] = [dict(seconds=t, mb_s=100, direction="write") for t in (1.005, 2.010, 3.019)]
        original = copy.deepcopy(case)
        self.assertEqual(collection_status(case), "collected")
        self.assertEqual(core_completeness(result), {"complete":True,"missing_core":[]})
        self.assertEqual(case, original)
        case["metrics"]["runtime_ms"] = 100000
        case["metrics"]["write"]["runtime_ms"] = 100000
        case["series"] = [dict(seconds=i*1.04, mb_s=100, direction="write") for i in range(1,97)]
        self.assertEqual(collection_status(case), "collected")
        case["series"].pop(48)
        self.assertEqual(collection_status(case), "collection_incomplete")
        for runtime, expected in ((999,"short_window"),(1000,"short_window"),(1004,"short_window"),(1050,"short_window"),(1051,"collection_incomplete")):
            case["metrics"]["runtime_ms"] = runtime
            case["metrics"]["write"]["runtime_ms"] = runtime
            case["series"] = []
            self.assertEqual(collection_status(case), expected)

    def test_timer_boundary_still_rejects_internal_holes_and_bad_samples(self):
        base = complete_result()["cases"][0]
        base["metrics"]["runtime_ms"] = 4000
        base["metrics"]["write"]["runtime_ms"] = 4000
        for times in ((2.010,3.019),(1.005,3.019),(1.005,2.010),(),
                      (1,2,4),(1,2,2,3),(3,2,1),(1,2,4.051)):
            case = copy.deepcopy(base)
            case["series"] = [dict(seconds=t, mb_s=100, direction="write") for t in times]
            self.assertEqual(collection_status(case), "collection_incomplete", times)
        for field,value in (("mb_s",math.nan),("mb_s",-1),("seconds",math.inf),("direction","read"),("direction","unknown")):
            case = copy.deepcopy(base)
            case["series"] = [dict(seconds=t, mb_s=100, direction="write") for t in (1.005,2.010,3.019)]
            case["series"][1][field] = value
            self.assertEqual(collection_status(case), "collection_incomplete", (field,value))
        case = copy.deepcopy(base)
        case["series"] = [dict(seconds=t, mb_s=100, direction="write") for t in (1.005,2.010,3.019)]
        case["collection_error"] = "invalid log record"
        self.assertEqual(collection_status(case), "collection_incomplete")
        case.pop("collection_error")
        case["series"].append(dict(seconds=1,mb_s=100,direction="read"))
        self.assertEqual(collection_status(case), "collection_incomplete")

    def test_log_millisecond_boundaries_do_not_expand_timer_tolerance(self):
        for runtime,times,expected in (
            (4200,[1.05,2.10,3.15,4.20],"collected"),
            (4201,[1.05,2.10,3.151,4.201],"collection_incomplete"),
            (2000,[1.05,2.0],"collected"),
            (2000,[1.051,2.0],"collection_incomplete"),
            (4200,[1.05,2.10,3.15],"collected"),
            (4201,[1.05,2.10,3.15],"collection_incomplete"),
            (2000,[1.0,2.05],"collected"),
            (2000,[1.001,2.051],"collection_incomplete"),
        ):
            case = complete_result()["cases"][0]
            case["metrics"]["runtime_ms"] = runtime
            case["metrics"]["write"]["runtime_ms"] = runtime
            case["series"] = [dict(seconds=t,mb_s=100,direction="write") for t in times]
            self.assertEqual(collection_status(case),expected,(runtime,times))

    def test_active_mean_percentile_zero_nan_and_mixed_missing_are_partial(self):
        for field in ("mean", "50", "95", "99"):
            for value in (None, math.nan, -1, True):
                result = complete_result()
                result["cases"][0]["metrics"]["write"]["latency_us"][field] = value
                self.assertFalse(core_completeness(result)["complete"])
        result = complete_result()
        mixed = next(c for c in result["cases"] if c["name"] == "mixed")
        mixed["metrics"]["read"]["bytes"] = 0
        self.assertIn("mixed.read.bytes", core_completeness(result)["missing_core"])
        result = complete_result()
        result["cases"].append(copy.deepcopy(result["cases"][0]))
        self.assertIn("sustained.identity", core_completeness(result)["missing_core"])


class AuthorizationTests(unittest.TestCase):
    def target(self):
        return dict(path="/owned/target", st_dev=1, inode=2, device="test-volume",
                    physical_devices=["test-disk"], total_bytes=100 * GIB, free_bytes=50 * GIB)

    def test_two_runs_share_allowance_unknown_tail_does_not_refund(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "state")
            target = self.target()
            ledger.import_existing(target, 16 * GIB, 0, 16 * GIB, "test-only explicit permission")
            allowance = ledger.read(target)
            config = auto_plan(target["total_bytes"], target["free_bytes"], allowance["run_cap"], allowance["remaining"])
            ledger.reserve(target, config, allowance, "first", plan_hash(target, config, allowance))
            self.assertEqual(ledger.read(target)["remaining"], 0)
            ledger.settle("first", 4 * GIB)
            second = ledger.read({**target, "path": "/another/target", "physical_devices": ["renamed-disk99"]})
            self.assertEqual(second["remaining"], 12 * GIB)
            config = auto_plan(target["total_bytes"], target["free_bytes"], second["run_cap"], second["remaining"])
            ledger.reserve(target, config, second, "second", plan_hash(target, config, second))
            ledger.settle("second", None)
            self.assertEqual(ledger.read(target)["remaining"], 0)
            with self.assertRaises(system_io.BenchError):
                ledger.settle("second", 0)
            self.assertEqual(ledger.read({**target, "physical_devices": ["another-disk"]})["remaining"], 0)

    def test_two_imported_authorizations_keep_their_caps_and_consumption(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "state")
            target = self.target()
            external = ledger.import_existing(target, 64 * GIB, 42_678_870_016, 16 * GIB, "fixture existing external permission")
            internal = ledger.import_existing({**target, "physical_devices": ["internal"]}, 16 * GIB, 0, 16 * GIB, "fixture existing internal permission")
            self.assertEqual(ledger.read(target)["scope_id"], external)
            self.assertEqual(ledger.read(target)["remaining"], 26_040_606_720)
            ledger.select_existing(internal, "fixture explicit selection of existing permission")
            self.assertEqual(ledger.read(target)["remaining"], 16 * GIB)
            ledger.select_existing(external, "fixture returning to existing permission")
            self.assertEqual(ledger.read(target)["remaining"], 26_040_606_720)

    def test_reimport_same_authorization_returns_original_without_refunding_consumption(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "state")
            target = self.target()
            record_id = "fixture-stable-original-permission-id"
            original = ledger.import_existing(target, 16 * GIB, 0, 16 * GIB, record_id)
            allowance = ledger.read(target)
            config = auto_plan(target["total_bytes"], target["free_bytes"], allowance["run_cap"], allowance["remaining"])
            ledger.reserve(target, config, allowance, "fixture", plan_hash(target, config, allowance))
            ledger.settle("fixture", 4 * GIB)
            self.assertEqual(ledger.import_existing(target, 16 * GIB, 0, 16 * GIB, record_id), original)
            self.assertEqual(ledger.read(target)["remaining"], 12 * GIB)
            with self.assertRaises(system_io.BenchError):
                ledger.import_existing(target, 32 * GIB, 0, 16 * GIB, record_id)

    def test_read_missing_state_is_read_only_and_confirmation_is_hash_bound(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "absent")
            target = self.target()
            allowance = ledger.read(target)
            self.assertFalse(ledger.directory.exists())
            config = auto_plan(target["total_bytes"], target["free_bytes"], allowance["run_cap"], allowance["remaining"])
            with contextlib.redirect_stderr(io.StringIO()), patch.object(benchmark.sys, "stdin", io.StringIO('{}\n')):
                with self.assertRaises(system_io.BenchError):
                    benchmark.confirm_write(target, config, allowance)
            digest = plan_hash(target, config, allowance)
            with contextlib.redirect_stderr(io.StringIO()), patch.object(benchmark.sys, "stdin", io.StringIO(json.dumps({"confirm_plan_sha256": digest}) + "\n")):
                self.assertEqual(benchmark.confirm_write(target, config, allowance), digest)
            self.assertFalse(ledger.directory.exists())

    def test_stale_revision_cannot_reserve_new_run(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "state")
            target = self.target()
            allowance = ledger.read(target)
            config = auto_plan(target["total_bytes"], target["free_bytes"], allowance["run_cap"], allowance["remaining"])
            ledger.reserve(target, config, allowance, "first", plan_hash(target, config, allowance))
            with self.assertRaises(system_io.BenchError):
                ledger.reserve(target, config, allowance, "second", plan_hash(target, config, allowance))

    def test_missing_or_corrupt_existing_state_cannot_become_new_allowance(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "state")
            ledger.directory.mkdir(mode=0o700)
            with self.assertRaises(system_io.BenchError):
                ledger.read(self.target())
            ledger.path.write_bytes(b"broken sqlite fixture")
            ledger.path.chmod(0o600)
            with self.assertRaises(system_io.BenchError):
                ledger.read(self.target())


class EntryAndQualityTests(unittest.TestCase):
    def test_removed_measurement_flags_reject_before_target_access(self):
        for flag, value in (("--profile", "full"), ("--file-size", "8GiB"),
                            ("--write-budget", "16GiB"), ("--seconds", "30"),
                            ("--sustained-seconds", "1800"), ("--recovery-seconds", "60"),
                            ("--cache-estimate-file", "estimate.json")):
            with patch.object(benchmark, "target_info") as target, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    benchmark.parser().parse_args(["--target", "/unused", flag, value])
                self.assertEqual(error.exception.code, 2)
                target.assert_not_called()

    def test_dry_run_does_not_create_ledger_or_run_fio(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "absent")
            args = benchmark.parser().parse_args(["--target", "/unused", "--dry-run"])
            target = AuthorizationTests().target()
            output = io.StringIO()
            with patch.object(benchmark, "WriteAllowance", return_value=ledger), \
                 patch.object(benchmark, "target_info", return_value=target), \
                 patch.object(benchmark, "collect_hardware", return_value={}), \
                 patch.object(benchmark, "load_reference_cache", return_value=({}, {})), \
                 patch.object(benchmark, "apply_hardware_specs"), \
                 patch.object(benchmark, "ensure_fio") as install, \
                 patch.object(benchmark, "DeviceLock") as lock, \
                 patch.object(benchmark, "observe_quality") as observe, \
                 patch.object(system_io, "capture") as command, contextlib.redirect_stdout(output):
                self.assertEqual(benchmark.execute(args, messages("en"), messages("en")), 0)
            self.assertFalse(ledger.directory.exists())
            self.assertIn("plan_sha256", json.loads(output.getvalue()))
            for operation in (install, lock, observe, command):
                operation.assert_not_called()

    def test_two_windows_and_boot_counter_quality(self):
        self.assertEqual(system_io.quality_from_windows([0, 0], "fixture")["status"], "quiet_observed")
        self.assertEqual(system_io.quality_from_windows([30, 30], "fixture")["status"], "visibility_limited")
        self.assertEqual(system_io.quality_from_windows([1, 0], "fixture", attributable=True)["status"], "interference_observed")
        self.assertEqual(system_io.quality_from_windows([], "unavailable")["status"], "visibility_limited")
        output = b"disk9\nKB/t xfrs MB\n32 22957312 99999\n0 0 0\n0 0 0\n"
        with patch.object(system_io.platform, "system", return_value="Darwin"), \
             patch.object(system_io.subprocess, "run") as process, \
             patch.object(system_io, "capture", return_value=output) as capture:
            process.return_value.returncode = 1
            result = system_io.observe_quality({"physical_devices": ["disk9"]})
        self.assertEqual(result["status"], "quiet_observed")
        self.assertEqual(capture.call_count, 1)

    def test_log_order_and_future_timestamps_are_not_repaired(self):
        case = complete_result()["cases"][0]
        case["metrics"]["runtime_ms"] = 2000
        for times in ([2, 1], [1, 4], [1, 1]):
            case["series"] = [dict(seconds=t, mb_s=100, direction="write") for t in times]
            self.assertEqual(collection_status(case), "collection_incomplete")

    def test_unreturned_case_after_io_attempt_never_refunds_unknown_tail(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = AuthorizationTests().target()
            target["path"] = str(root / "target")
            Path(target["path"]).mkdir()
            ledger = WriteAllowance(root / "state")
            ledger.import_existing(target, 16 * GIB, 0, 16 * GIB, "fixture-only permission")
            output = root / "output"
            args = benchmark.parser().parse_args(["--target", target["path"], "--output", str(output), "--language", "en"])
            with patch.object(benchmark, "WriteAllowance", return_value=ledger), \
                 patch.object(benchmark, "target_info", return_value=target), \
                 patch.object(benchmark, "check_target", return_value=target), \
                 patch.object(benchmark, "collect_hardware", return_value={}), \
                 patch.object(benchmark, "load_reference_cache", return_value=({}, {})), \
                 patch.object(benchmark, "apply_hardware_specs"), \
                 patch.object(benchmark, "DeviceLock", return_value=contextlib.nullcontext()), \
                 patch.object(benchmark, "ensure_fio", return_value={"path": "fixture-never-launched", "engines": "posixaio"}), \
                 patch.object(benchmark, "select_engine", return_value="posixaio"), \
                 patch.object(benchmark, "observe_quality", return_value={"status": "quiet_observed"}), \
                 patch.object(benchmark, "confirm_write", side_effect=lambda target, config, allowance, text: plan_hash(target, config, allowance)), \
                 patch.object(benchmark.os, "ftruncate") as allocate, \
                 patch.object(benchmark, "run_case", side_effect=OSError("simulated case-save ENOSPC after I/O")) as case, \
                 patch.object(benchmark, "trash_owned"), \
                 patch("report.render_report", return_value={"html": "fixture.html", "pdf": None}), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = benchmark.execute(args, messages("en"), messages("en"))
            self.assertEqual(code, 5)
            self.assertEqual(case.call_count, 1)
            self.assertEqual(allocate.call_count, 1)  # Mocked, no allocation or real fio.
            result = json.loads((output / "results.json").read_text())
            self.assertIsNone(result["actual_write_bytes"])
            self.assertFalse(result["completeness"]["complete"])
            self.assertEqual(ledger.read(target)["remaining"], 0)


def community_result(status="quiet_observed"):
    from test_community import local_result
    from workloads import fio_arguments
    result = local_result()
    result.update(complete_result())
    result["target"].update(total_bytes=2_000 * GIB, free_bytes=1_000 * GIB)
    result["tool"]["method_version"] = "drivespeed-auto-full-v1"
    result["measurement_quality"] = {"status": status, "private_process": "MUST-NOT-PUBLISH"}
    for case in result["cases"]:
        case["command"] = fio_arguments("fixture-fio", case, result["plan"]["file_bytes"], "posixaio", "fixture-raw", "fixture-log")
    return result


class CommunityContractTests(unittest.TestCase):
    def test_mean_and_quality_roundtrip_and_legacy_absence(self):
        from test_community import local_result, ID
        legacy = community_data.project_result(local_result(), "https://x.com/fixture_user", submission_id=ID)
        self.assertEqual(community_data.digest(legacy), "e0e0173b5b968a46798a03f926c5c21efe30713379b9d8712c16f23d00332129")
        self.assertNotIn("quality", legacy)
        self.assertTrue(all(c[d]["latency_us"]["mean"] is None for c in legacy["cases"] for d in ("read", "write")))
        for status in ("quiet_observed", "interference_observed", "visibility_limited"):
            payload = community_data.project_result(community_result(status), "https://x.com/fixture_user")
            self.assertEqual(payload["quality"], {"status": status})
            self.assertNotIn("MUST-NOT-PUBLISH", json.dumps(payload))
            self.assertEqual(payload["cases"][0]["write"]["latency_us"]["mean"], 12.34)
            with tempfile.TemporaryDirectory() as root, CommunityStore(Path(root) / "private") as store:
                identifier = store.create(payload, "fixture-auto", "fixture-results")
                self.assertEqual(store.payload(identifier), community_data.normalise_payload(payload))
                from community_samples import replace, read
                sample = copy.deepcopy(payload)
                sample["submission_id"] = "22222222-2222-4222-8222-222222222222"
                items = [{"record": {"id": sample["submission_id"], "url": "https://drivespeed.anz.tv/benchmarks/" + sample["submission_id"], "created_at": "2026-10-30T00:00:00Z", "submission": sample}, "matched_on": ["drive"], "differences": []}]
                replace(store.db, identifier, items)
                self.assertEqual(read(store.db, identifier)[0]["record"]["submission"], community_data.normalise_payload(sample))

    def test_partial_or_missing_quality_and_mean_cannot_publish(self):
        for mutate in (lambda r: r.pop("measurement_quality"),
                       lambda r: r["cases"][0]["metrics"]["write"]["latency_us"].pop("mean"),
                       lambda r: r["cases"][0]["metrics"].update(runtime_ms=2000)):
            result = community_result()
            mutate(result)
            result["completeness"] = {"complete": True, "missing_core": []}
            with self.assertRaises(community_data.CommunityError):
                community_data.project_result(result, "https://x.com/fixture_user")

    def test_new_comparisons_require_explicit_quiet_on_both_sides(self):
        from community_comparison import _conditions
        quiet = community_data.project_result(community_result(), "https://x.com/fixture_user")
        for status in (None, "visibility_limited", "interference_observed"):
            other = copy.deepcopy(quiet)
            if status is None:
                other.pop("quality")
                other["tool"]["method_version"] = "drivespeed-file-v1"
            else:
                other["quality"]["status"] = status
            self.assertIn("measurement_quality", _conditions(quiet, other))
            self.assertIn("measurement_quality", _conditions(other, other))


if __name__ == "__main__":
    unittest.main()
