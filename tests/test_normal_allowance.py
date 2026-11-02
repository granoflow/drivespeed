"""Exact finite normal-speed proposals preserve prior accounting; synthetic only."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_auto_full as fixtures
import benchmark
from progress import messages
from system_io import BenchError
from workloads import GIB
from write_allowance import WriteAllowance, plan_hash


class NormalAllowanceTests(unittest.TestCase):
    def test_proposal_is_read_only_and_reserve_preserves_history(self):
        with tempfile.TemporaryDirectory() as root:
            target = fixtures.AuthorizationTests().target()
            ledger = WriteAllowance(Path(root) / "ledger")
            ledger.import_existing(target, 16*GIB, 16*GIB, 8*GIB, "fixture exhausted")
            before = ledger.read(target)
            config = benchmark.normal_plan(target, before, 64*GIB)
            self.assertEqual(ledger.read(target), before)
            self.assertEqual(config["additional_authorized_bytes"], 64*GIB)
            for proposed, digest in ((config, "wrong"),
                                      (dict(config, additional_authorized_bytes=65*GIB),
                                       plan_hash(target, config, before))):
                with self.assertRaises(BenchError):
                    ledger.reserve(target, proposed, before, "rejected", digest)
                self.assertEqual(ledger.read(target), before)
            ledger.reserve(target, config, before, "approved", plan_hash(target, config, before))
            self.assertEqual(ledger.read(target)["scope_id"], before["scope_id"])
            ledger.settle("approved", 12*GIB)
            with ledger._connection(create=False) as db:
                self.assertEqual(db.execute("SELECT cap,charged FROM scopes WHERE active=1").fetchone(),
                                 (80*GIB, 28*GIB))

    def test_new_session_explicit_proposal_and_changed_revision(self):
        with tempfile.TemporaryDirectory() as root:
            target = fixtures.AuthorizationTests().target()
            ledger = WriteAllowance(Path(root) / "ledger")
            before = ledger.read(target)
            config = benchmark.normal_plan(target, before, 128*GIB)
            digest = plan_hash(target, config, before)
            ledger.reserve(target, config, before, "first", digest)
            with self.assertRaises(BenchError):
                ledger.reserve(target, config, before, "stale", digest)
            ledger.settle("first", 0)
            self.assertEqual(ledger.read(target)["remaining"], 128*GIB)

    def test_first_proposal_below_default_grants_only_confirmed_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            target = fixtures.AuthorizationTests().target()
            ledger = WriteAllowance(Path(root) / "ledger")
            before = ledger.read(target)
            config = benchmark.normal_plan(target, before, 32*GIB)
            ledger.reserve(target, config, before, "small-first", plan_hash(target, config, before))
            ledger.settle("small-first", 4*GIB)
            self.assertEqual(ledger.read(target)["remaining"], 28*GIB)
            self.assertEqual(ledger.read(target)["run_cap"], 32*GIB)

    def test_bad_proposal_and_wrong_method_cannot_extend(self):
        target = fixtures.AuthorizationTests().target()
        allowance = dict(present=False, scope_id=None, revision=0, run_cap=64*GIB, remaining=64*GIB)
        for value in (-1, 0, True, 2**63):
            with self.assertRaises(ValueError):
                benchmark.normal_plan(target, allowance, value)
        with tempfile.TemporaryDirectory() as root:
            ledger = WriteAllowance(Path(root) / "ledger")
            config = benchmark.normal_plan(target, allowance, 128*GIB)
            config["method_version"] = "drivespeed-auto-full-v1"
            with self.assertRaises(BenchError):
                ledger.reserve(target, config, allowance, "wrong-method", plan_hash(target, config, allowance))

    def test_actual_preview_never_reserves_or_runs_io(self):
        target = fixtures.AuthorizationTests().target()
        allowance = dict(present=True, scope_id="fixture", revision=3, run_cap=8*GIB, remaining=0)
        args = benchmark.parser().parse_args(["--disk", "/fixture", "--propose-write-bytes", str(64*GIB), "--dry-run"])
        with patch.object(benchmark, "target_info", return_value=target), \
             patch.object(benchmark, "WriteAllowance") as ledger, \
             patch.object(benchmark, "collect_hardware", return_value={}), \
             patch.object(benchmark, "load_reference_cache", return_value=({}, {})), \
             patch.object(benchmark, "apply_hardware_specs"), \
             patch.object(benchmark, "DeviceLock") as lock, \
             patch.object(benchmark, "ensure_fio") as fio, \
             patch.object(benchmark, "clean_stale_workspaces") as cleanup, \
             contextlib.redirect_stdout(io.StringIO()):
            ledger.return_value.read.return_value = allowance
            self.assertEqual(benchmark.execute(args, messages("en"), messages("en")), 0)
            ledger.return_value.reserve.assert_not_called()
            for operation in (lock, fio, cleanup):
                operation.assert_not_called()

    def test_missing_cache_goal_continues_but_initialization_or_io_failure_stops(self):
        for failure in ("cache_only", "initialization", "io_error"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                target = fixtures.AuthorizationTests().target()
                target["path"] = str(root / "target")
                Path(target["path"]).mkdir()
                ledger = WriteAllowance(root / "ledger")
                ledger.import_existing(target, 16*GIB, 0, 16*GIB, "fixture finite run")
                args = benchmark.parser().parse_args(["--disk", target["path"], "--output", str(root / "out")])
                cases = {case["name"]:case for case in fixtures.complete_result()["cases"]}
                def produced(fio, job, *unused):
                    case = copy.deepcopy(cases[job["name"]])
                    if job["name"] == "sustained":
                        case.update(status="partial", reason="incomplete_measurement",
                                    metrics_complete=False, missing_core=["sustained.post_cache_stability"])
                        if failure == "initialization":
                            case["missing_core"].append("sustained.initialization")
                        elif failure == "io_error":
                            case.update(status="failed", reason="run_failed")
                    return case
                with contextlib.ExitStack() as stack:
                    replacements = {
                        "WriteAllowance":dict(return_value=ledger), "target_info":dict(return_value=target),
                        "check_target":dict(return_value=target), "collect_hardware":dict(return_value={}),
                        "load_reference_cache":dict(return_value=({}, {})), "apply_hardware_specs":{},
                        "DeviceLock":dict(return_value=contextlib.nullcontext()),
                        "ensure_fio":dict(return_value={"path":"never-launched", "engines":"posixaio"}),
                        "select_engine":dict(return_value="posixaio"),
                        "observe_quality":dict(return_value={"status":"quiet_observed"}),
                        "confirm_write":dict(side_effect=lambda t,c,a,text:plan_hash(t,c,a)),
                        "trash_owned":{}, "clean_stale_workspaces":dict(return_value=[]),
                        "run_case":dict(side_effect=produced),
                    }
                    mocks = {name:stack.enter_context(patch.object(benchmark,name,**options))
                             for name,options in replacements.items()}
                    stack.enter_context(patch.object(benchmark.os,"ftruncate"))
                    stack.enter_context(patch("report.render_report", return_value={"html":"fixture.html","pdf":None}))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    code = benchmark.execute(args, messages("en"), messages("en"))
                result = json.loads((root / "out/results.json").read_text())
                self.assertEqual(code, 5 if failure == "io_error" else 6)
                self.assertEqual(mocks["run_case"].call_count, 11 if failure == "cache_only" else 1)
                self.assertEqual(result["outcome"], "partial")
                self.assertFalse(result["completeness"]["complete"])
