"""Behavior checks; writes are confined to unique temporary test directories."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark
import report
import system_io
import hardware
from reference_data import (
    connection_limits,
    validate_reference_data,
    cache_reference_data,
    load_reference_cache,
    reference_cache_path,
)
from progress import Progress, messages
from workloads import (
    MIB,
    PROFILES,
    fio_arguments,
    json_objects,
    metrics,
    plan,
    sustained_summary,
    cache_assessment,
    cache_write_plan,
)


def fixture_snapshot(count=1_048_576):
    return {
        "jobs": [
            {
                "error": 0,
                "eta": 0,
                "job_runtime": 1000,
                "read": {
                    "io_bytes": count,
                    "bw_bytes": 1_000_000,
                    "iops": 12,
                    "clat_ns": {
                        "percentile": {
                            "50.000000": 1000,
                            "95.000000": 2000,
                            "99.000000": 3000,
                        }
                    },
                },
                "write": {"io_bytes": 0},
                "iodepth_level": {"1": 100},
            }
        ]
    }


def new_dir():
    return Path(tempfile.mkdtemp(prefix="diskbench-test-")).resolve()


class WorkloadTests(unittest.TestCase):
    def test_total_bound_and_finite_commands(self):
        for name, profile in PROFILES.items():
            config = plan(name)
            self.assertLessEqual(config["max_write_bytes"], profile["write_budget"])
            for job in config["jobs"]:
                self.assertGreater(job["io_bytes"], 0)
                args = fio_arguments(
                    "fio", job, config["file_bytes"], "posixaio", "/tmp/out", "/tmp/log"
                )
                self.assertNotIn("--time_based", args)
                self.assertIn(f"--io_size={job['io_bytes']}", args)
                self.assertIn("--filename=payload.bin", args)

    def test_controlled_fio_signal_notice_keeps_final_snapshot_but_not_truncation(self):
        snapshot = json.dumps(fixture_snapshot())
        stream = snapshot + "\nfio: terminating on signal 2\n" + snapshot
        with self.assertRaises(ValueError):
            json_objects(stream, strict=True)
        self.assertEqual(
            len(json_objects(stream, strict=True, allow_signal_notice=True)), 2
        )
        with self.assertRaises(ValueError):
            json_objects(stream + '{"jobs":', strict=True, allow_signal_notice=True)

    def test_malformed_json_and_final_snapshot(self):
        a, b = fixture_snapshot(), fixture_snapshot(2 * MIB)
        stream = json.dumps(a) + "\n" + json.dumps(b) + '\n{"jobs":'
        snapshots = json_objects(stream)
        self.assertEqual(len(snapshots), 2)
        actual = metrics(snapshots[-1])
        self.assertEqual(actual["read"]["bytes"], 2 * MIB)
        self.assertEqual(actual["read"]["mb_s"], 1)
        self.assertEqual(actual["read"]["latency_us"]["99"], 3)
        with self.assertRaises(ValueError):
            metrics({"jobs": [{"error": 5}]})

    def test_drop_numbers_never_prove_cache_exhaustion(self):
        points = [
            dict(seconds=i, mb_s=1000 if i <= 10 else 200, direction="write")
            for i in range(1, 41)
        ]
        summary = sustained_summary(points)
        self.assertEqual(summary["tail_mean_mb_s"], 200)
        self.assertEqual(summary["after_drop_mean_mb_s"], 200)
        self.assertEqual(summary["minimum_5s_mb_s"], 200)
        self.assertEqual(summary["change_seconds"], 11)
        self.assertEqual(summary["cache_exhaustion"], "unconfirmed")
        self.assertEqual(summary["drop_percent"], 80)
        self.assertIsNone(sustained_summary(points[:3])["minimum_5s_mb_s"])

    def test_gap_prevents_change_point_claim(self):
        points = [
            dict(seconds=i, mb_s=100 if i < 10 else 10, direction="write")
            for i in range(1, 40)
            if i != 12
        ]
        points[11]["seconds"] += 2
        self.assertIsNone(sustained_summary(points)["change_seconds"])


class CacheMeasurementTests(unittest.TestCase):
    def points(self, end=100):
        return [
            dict(seconds=i, mb_s=1000 if i <= 10 else 200, direction="write")
            for i in range(1, end + 1)
        ]

    def test_waits_for_settling_and_full_post_drop_window(self):
        self.assertEqual(
            cache_assessment(self.points(99))["status"], "post_drop_incomplete"
        )
        assessment = cache_assessment(self.points())
        self.assertEqual(assessment["status"], "observed_drop_stable")
        self.assertEqual(assessment["cache_capacity_estimate_bytes"], 10_000_000_000)
        self.assertEqual(assessment["post_cache_mb_s"], 200)
        self.assertFalse(assessment["cause_confirmed"])

    def test_transient_recovery_and_missing_samples_cannot_finish_cache_test(self):
        points = self.points()
        for point in points[20:]:
            point["mb_s"] = 1000
        self.assertEqual(cache_assessment(points)["status"], "post_drop_unstable")
        self.assertIsNone(cache_assessment(points)["cache_capacity_estimate_bytes"])
        points = self.points()
        del points[30:33]
        self.assertEqual(cache_assessment(points)["status"], "sampling_gaps")

    def test_research_and_knowledge_plan_add_observation_margin_before_fallback(self):
        estimate = dict(
            basis="web",
            drive_model="Example 2TB",
            cache_bytes=700_000_000_000,
            post_cache_mb_s=3900,
            host_write_mb_s=3374,
            sources=["https://example.com/review"],
        )
        result = cache_write_plan(estimate)
        self.assertGreaterEqual(
            result["requested_bytes"], 700_000_000_000 * 1.25 + 3374_000_000 * 90
        )
        self.assertTrue(result["interface_may_mask_cache"])
        estimate.update(basis="model_knowledge", sources=[])
        with self.assertRaises(ValueError):
            cache_write_plan(estimate)
        estimate["knowledge_basis"] = (
            "Test-only recall evidence record; exact model, capacity and write-cache type checked"
        )
        self.assertEqual(cache_write_plan(estimate)["basis"], "model_knowledge")
        self.assertEqual(cache_write_plan()["requested_bytes"], 1024**4)
        estimate["basis"] = "web"
        with self.assertRaises(ValueError):
            cache_write_plan(estimate)

    def test_single_pass_plan_has_no_cache_warming_initialization(self):
        config = plan("cache", file_bytes=1024 * MIB, write_budget=2048 * MIB)
        self.assertEqual([j["name"] for j in config["jobs"]], ["sustained"])
        self.assertEqual(config["max_write_bytes"], config["file_bytes"])
        self.assertTrue(config["jobs"][0]["cache_test"])

    def test_fallback_plan_shrinks_for_smaller_drives_without_installing_or_writing(
        self,
    ):
        root = new_dir()
        args = benchmark.parser().parse_args(
            ["--target", str(root), "--profile", "cache", "--dry-run"]
        )
        captured = io.StringIO()
        with (
            patch.object(
                benchmark,
                "target_info",
                return_value={
                    "free_bytes": 400 * 1024**3,
                    "total_bytes": 500 * 1024**3,
                },
            ),
            patch.object(benchmark, "find_fio", return_value=None),
            patch.object(
                benchmark,
                "collect_hardware",
                return_value={"drive": {"model": "Test drive"}},
            ),
            patch.object(benchmark, "ensure_fio") as install,
            contextlib.redirect_stdout(captured),
        ):
            self.assertEqual(benchmark.execute(args, messages("en"), messages("en")), 0)
        config = json.loads(captured.getvalue())["plan"]
        self.assertLessEqual(
            config["file_bytes"] + config["reserve_bytes"], 400 * 1024**3
        )
        self.assertGreaterEqual(config["reserve_bytes"], 50 * 1024**3)
        self.assertTrue(config["cache_planning"]["limited_by_space_or_budget"])
        install.assert_not_called()

    def test_cache_research_for_another_drive_is_rejected_before_execution(self):
        root = new_dir()
        estimate = root / "estimate.json"
        estimate.write_text(
            json.dumps(
                dict(
                    basis="model_knowledge",
                    drive_model="Drive A 2TB",
                    cache_bytes=100_000_000_000,
                    knowledge_basis="Test-only exact-model and write-cache recall record",
                    sources=[],
                )
            )
        )
        args = benchmark.parser().parse_args(
            [
                "--target",
                str(root),
                "--profile",
                "cache",
                "--cache-estimate-file",
                str(estimate),
                "--dry-run",
            ]
        )
        with (
            patch.object(
                benchmark, "target_info", return_value={"free_bytes": 1024**4}
            ),
            patch.object(
                benchmark,
                "collect_hardware",
                return_value={"drive": {"model": "Drive B 2TB"}},
            ),
            patch.object(benchmark, "ensure_fio") as install,
        ):
            with self.assertRaises(ValueError):
                benchmark.execute(args, messages("en"), messages("en"))
        install.assert_not_called()

    def test_stable_cache_report_displays_numeric_estimates(self):
        points = self.points()
        result = {
            "cases": [{"name": "sustained", "status": "completed", "series": points}],
            "sustained_summary": sustained_summary(points),
        }
        output = new_dir()
        report.render_report(result, output, "zh-CN", messages("zh-CN"))
        source = (output / "report.html").read_text()
        self.assertIn('data-metric="cache_capacity_estimate_bytes"', source)
        self.assertIn("9.31 GiB", source)
        self.assertIn('data-metric="post_cache_mb_s"', source)
        self.assertIn("200.00 MB/s", source)
        self.assertIn(messages("zh-CN")["cache_stable"], source)

    def test_completed_io_with_unconfirmed_cache_keeps_exact_write_accounting(self):
        result = {
            "outcome": "partial",
            "cases": [{"name": "sustained", "status": "completed",
                       "metrics_complete": True, "metrics": {"runtime_ms": 2872}}],
            "sustained_summary": {"tail_mean_mb_s": 1659.54},
            "actual_write_bytes": 1000,
            "accounted_write_bytes": 1000,
        }
        output = new_dir()
        report.render_report(result, output, "zh-CN", messages("zh-CN"))
        source = (output / "report.html").read_text()
        self.assertNotIn(messages("zh-CN")["partial_write"], source)
        self.assertIn('data-metric="tail_mean_mb_s"', source)
        self.assertIn("2.87", source)
        result["cases"][0]["metrics_complete"] = False
        report.render_report(result, output, "zh-CN", messages("zh-CN"))
        self.assertIn(messages("zh-CN")["partial_write"], (output / "report.html").read_text())

    def test_short_report_explains_missing_cache_measurement(self):
        points = self.points(8)
        result = {
            "cases": [{"name": "sustained", "status": "completed", "series": points}],
            "sustained_summary": sustained_summary(points),
        }
        output = new_dir()
        report.render_report(result, output, "zh-CN", messages("zh-CN"))
        source = (output / "report.html").read_text()
        self.assertIn(messages("zh-CN")["cache_no_drop"], source)
        self.assertNotIn('data-metric="post_cache_mb_s"', source)
        self.assertNotIn('data-metric="after_drop_mean_mb_s"', source)


class SafetyTests(unittest.TestCase):
    def test_symlink_and_raw_device_are_rejected_before_device_query(self):
        root = new_dir()
        (root / "link").symlink_to(root, target_is_directory=True)
        for path in (root / "link", "/dev/null"):
            with self.assertRaises(system_io.BenchError):
                system_io.target_info(path)

    def test_device_identity_change_and_low_space_stop(self):
        original = dict(
            path="/target",
            st_dev=1,
            inode=2,
            mount="/target",
            device="disk1",
            physical_devices=["disk1"],
            free_bytes=100,
        )
        with patch.object(
            system_io, "target_info", return_value={**original, "device": "disk2"}
        ):
            with self.assertRaises(system_io.BenchError):
                system_io.check_target(original)
        with patch.object(system_io, "target_info", return_value=original):
            with self.assertRaises(system_io.BenchError):
                system_io.check_target(original, reserve=101)

    def test_lock_aliases_physical_device(self):
        lock = system_io.DeviceLock(
            {"physical_devices": ["unique-test-" + str(os.getpid())]}
        )
        with lock:
            with self.assertRaises(system_io.BenchError):
                with system_io.DeviceLock({"physical_devices": lock.devices}):
                    self.fail("Second lock acquired")

    def test_owned_file_replacement_is_rejected(self):
        work = new_dir()
        (work / "payload.bin").write_bytes(b"x")
        identity = work.stat().st_dev, work.stat().st_ino
        payload = work / "payload.bin"
        original = payload.stat().st_dev, payload.stat().st_ino
        payload.rename(work / "old")
        payload.write_bytes(b"x")
        with self.assertRaises(system_io.BenchError):
            benchmark.check_owned(work, identity, original)

    def test_same_named_fiona_is_not_fio(self):
        with patch.object(
            system_io,
            "capture",
            side_effect=[b"fio, version 1.10.1", b"Fiona command line"],
        ):
            self.assertIsNone(system_io.fio_identity("fio"))

    def test_checksum_failure_never_runs_download(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "4"}

        with patch.object(
            system_io.urllib.request, "urlopen", return_value=Response(b"bad!")
        ):
            with self.assertRaises(system_io.BenchError):
                system_io.download(
                    "https://example.invalid",
                    new_dir() / "package",
                    "0" * 64,
                    type("P", (), {"emit": lambda *a, **kw: None})(),
                )


class ProgressAndReportTests(unittest.TestCase):
    def test_unknown_eta_and_failure_do_not_complete_steps(self):
        for language in ("zh-CN", "en", "ar"):
            output = new_dir() / "progress.jsonl"
            with contextlib.redirect_stderr(io.StringIO()):
                p = Progress(["inspect", "report"], messages(language), output)
                p.emit("inspect", "completed", 100, force=True)
                p.emit("report", "failed", force=True)
            last = json.loads(output.read_text().splitlines()[-1])
            self.assertEqual(last["overall_percent"], 50)
            self.assertIsNone(last["step_remaining_seconds"])
            self.assertIsNone(last["step_percent"])

    def test_language_override_requires_complete_pack(self):
        path = new_dir() / "language.json"
        path.write_text("{}")
        with self.assertRaises(ValueError):
            messages("fr", path)
        with self.assertRaises(ValueError):
            messages("fr")

    def test_html_escape_privacy_offline_and_rtl(self):
        result = dict(
            schema_version=1,
            outcome="partial",
            synthetic=True,
            target={
                "device": "PRIVATE-DEVICE-ID",
                "path": "/Users/secret/drive",
            },
            hardware={"drive": {"model": "<script>alert(1)</script>", "source": "user"}},
            environment={
                "fio": {"version": "fio-3.43", "path": "/Users/secret/bin/fio"}
            },
            cases=[
                dict(
                    name="seq_read",
                    status="completed",
                    metrics=metrics(fixture_snapshot()),
                )
            ],
        )
        for language in ("zh-CN", "en", "ar"):
            output = new_dir()
            paths = report.render_report(result, output, language, messages(language))
            source = Path(paths["html"]).read_text()
            self.assertNotIn("<script>alert(1)</script>", source)
            self.assertNotIn("PRIVATE-DEVICE-ID", source)
            self.assertIn("&lt;script&gt;", source)
            self.assertNotIn("/Users/secret", source)
            self.assertIn('href="https://github.com/granoflow/drivespeed"', source)
            # A user-clicked project link is allowed; automatic remote assets are not.
            self.assertNotIn('src="https://', source)
            self.assertNotIn("@import", source)
            self.assertNotIn("url(https://", source)
            self.assertIn("window.print()", source)
            self.assertIn('dir="rtl"' if language == "ar" else 'dir="ltr"', source)

    def test_compact_results_restore_status_and_reason_for_incomplete_runs(self):
        result = {
            "outcome": "complete",
            "cases": [
                {
                    "name": "seq_read",
                    "status": "completed",
                    "metrics": metrics(fixture_snapshot()),
                }
            ],
        }
        output = new_dir()
        report.render_report(result, output, "en", messages("en"))
        source = (output / "report.html").read_text()
        table = source.split('<table id="measurements">')[1].split("</table>")[0]
        self.assertNotIn("<th>Status</th>", table)
        self.assertIn('data-metric="seq_read"', source)
        result["outcome"] = "partial"
        result["cases"].append(
            {"name": "seq_write", "status": "failed", "reason": "space_failed"}
        )
        report.render_report(result, output, "en", messages("en"))
        source = (output / "report.html").read_text()
        table = source.split('<table id="measurements">')[1].split("</table>")[0]
        self.assertIn("<th>Status</th>", table)
        self.assertIn(messages("en")["space_failed"], table)
        self.assertIn(messages("en")["failed"], table)

    def test_pdf_unavailable_does_not_claim_success(self):
        with patch.object(report, "export_pdf", return_value=(None, "missing")):
            with contextlib.redirect_stderr(io.StringIO()):
                paths = report.render_report(
                    {"cases": []}, new_dir(), "en", messages("en"), "pdf"
                )
        self.assertIsNone(paths["pdf"])
        self.assertTrue(Path(paths["html"]).is_file())

    def test_dry_run_does_not_install_or_write(self):
        root = new_dir()
        args = benchmark.parser().parse_args(
            [
                "--target",
                str(root),
                "--output",
                str(root / "must-not-exist"),
                "--dry-run",
                "--language",
                "en",
                "--ui-language",
                "en",
            ]
        )
        with (
            patch.object(
                benchmark, "target_info", return_value={"free_bytes": 1000 * 1024**3}
            ),
            patch.object(benchmark, "find_fio", return_value=None),
            patch.object(
                benchmark,
                "collect_hardware",
                return_value={"drive": {"model": "Test drive"}},
            ),
            patch.object(benchmark, "ensure_fio") as install,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    benchmark.execute(args, messages("en"), messages("en")), 0
                )
        install.assert_not_called()
        self.assertFalse((root / "must-not-exist").exists())


class ReadableResultsTests(unittest.TestCase):
    def case(self, name, direction, speed, block=4096, qd=1, status="completed"):
        return dict(
            name=name,
            status=status,
            bs=block,
            qd=qd,
            metrics={direction: {"mb_s": speed, "latency_us": {"95": 123.45}}},
        )

    def test_examples_and_matched_comparison_use_this_runs_measurements(self):
        result = {
            "cases": [
                self.case("seq_read", "read", 1000, block=MIB),
                self.case("seq_write", "write", 500, block=MIB),
                self.case("rand_read", "read", 10),
                self.case("rand_read_q", "read", 160, qd=16),
            ]
        }
        source = report.render_readable_results(result, messages("en"))
        self.assertIn("10.00 s", source)
        self.assertIn("20.00 s", source)
        self.assertIn("0.13 ms", source)
        self.assertIn('data-comparison="rand_read"', source)
        self.assertIn("16.00×", source)
        self.assertIn(messages("en")["scenario_limits"], source)

    def test_different_block_sizes_are_not_presented_as_a_matched_control(self):
        result = {
            "cases": [
                self.case("seq_read", "read", 1000, block=MIB),
                self.case("seq_read_q", "read", 2000, block=4096, qd=16),
            ]
        }
        source = report.render_readable_results(result, messages("en"))
        self.assertNotIn("data-comparison=", source)
        self.assertNotIn("2.00×", source)

    def test_missing_or_failed_measurements_do_not_create_copy_time_predictions(self):
        result = {
            "cases": [
                self.case("seq_read", "read", 5000, status="failed"),
                self.case("seq_write", "write", 500, block=MIB),
            ]
        }
        source = report.render_readable_results(result, messages("en"))
        self.assertNotIn("5,000.00", source)
        self.assertNotIn("10 GB", source)
        self.assertIn(messages("en")["reference_not_measured"], source)
        self.assertEqual(
            report.render_readable_results({"cases": []}, messages("en")), ""
        )


class ReferenceAndSummaryTests(unittest.TestCase):
    def reference(self, **updates):
        item = dict(
            component="drive",
            model="Drive A 2TB",
            metric="seq_read",
            value=5000,
            unit="MB/s",
            source_kind="official",
            source_url="https://example.com/spec",
            source_label="Manufacturer",
            conditions="2TB model; rated peak",
            relationship="device",
        )
        item.update(updates)
        return item

    def test_chain_minimum_normalizes_units_and_keeps_directions(self):
        result = {
            "hardware": {
                key: {"model": key}
                for key in ("drive", "computer", "enclosure", "controller")
            }
        }
        refs = {
            "for_drive_model": "drive",
            "items": [
                self.reference(
                    component="computer",
                    model="computer",
                    metric="interface_gbps",
                    unit="Gb/s",
                    value=8,
                ),
                self.reference(
                    component="enclosure",
                    model="enclosure",
                    metric="interface_gbps",
                    unit="Gb/s",
                    value=6,
                ),
                self.reference(model="drive", value=2000),
                self.reference(model="drive", metric="seq_write", value=600),
            ],
        }
        limits = connection_limits(result, refs)
        self.assertEqual(limits["read_mb_s"], 750)
        self.assertEqual(limits["write_mb_s"], 600)
        self.assertEqual(limits["missing_read"], [])
        self.assertFalse(limits["enclosure_uses_controller"])

    def test_confirmed_controller_replaces_unknown_enclosure_reference_only(self):
        result = {
            "hardware": {
                key: {"model": key} for key in ("drive", "computer", "controller")
            },
            "cases": [],
        }
        refs = {
            "for_drive_model": "drive",
            "items": [
                self.reference(
                    component="computer",
                    model="computer",
                    metric="interface_gbps",
                    unit="Gb/s",
                    value=40,
                ),
                self.reference(
                    component="controller",
                    model="controller",
                    metric="interface_gbps",
                    unit="Gb/s",
                    value=10,
                ),
                self.reference(model="drive", value=7000),
                self.reference(model="drive", metric="seq_write", value=6000),
            ],
        }
        limits = connection_limits(result, refs)
        self.assertEqual(limits["read_mb_s"], 1250)
        self.assertTrue(limits["enclosure_uses_controller"])
        self.assertIn("enclosure", limits["missing_read"])
        html, _ = report.render_connection_reference(result, refs, messages("en"))
        self.assertIn('data-complete="false"', html)
        self.assertIn(messages("en")["controller_fallback"], html)
        self.assertIn('data-limit-mb-s="1250"', html)

    def test_candidates_reviews_and_other_models_cannot_set_ceiling(self):
        result = {
            "hardware": {
                "drive": {"model": "drive"},
                "controller": {"model": "controller"},
            }
        }
        for update in (
            {"relationship": "candidate"},
            {"source_kind": "review"},
            {"model": "other"},
        ):
            item = dict(
                component="controller",
                model="controller",
                metric="interface_gbps",
                unit="Gb/s",
                value=5,
            )
            item.update(update)
            refs = {"for_drive_model": "drive", "items": [self.reference(**item)]}
            self.assertIsNone(connection_limits(result, refs))

    def test_unsafe_sources_and_incompatible_units_are_rejected(self):
        for update in (
            {"source_url": "javascript:alert(1)"},
            {"unit": "Gb/s"},
            {"source_kind": "model_guess"},
            {"value": float("nan")},
        ):
            with self.assertRaises(ValueError):
                validate_reference_data(
                    {
                        "for_drive_model": "Drive A 2TB",
                        "items": [self.reference(**update)],
                    }
                )
        self.assertEqual(validate_reference_data({"items": []})["items"], [])

    def test_partial_references_distinguish_other_devices_and_candidates(self):
        result = {
            "hardware": {"drive": {"model": "Drive B 2TB"}},
            "cases": [],
            "references": {
                "for_drive_model": "Drive B 2TB",
                "items": [
                    self.reference(),
                    self.reference(
                        relationship="peer", conditions="Other drive <test>"
                    ),
                    self.reference(
                        component="controller",
                        model="Candidate chip",
                        relationship="candidate",
                        metric="interface_gbps",
                        unit="Gb/s",
                        value=40,
                    ),
                ],
            },
        }
        source = report.render_references(result, messages("en"), {})
        self.assertNotIn('data-relationship="device"', source)
        self.assertIn('data-relationship="peer"', source)
        self.assertIn('data-relationship="candidate"', source)
        self.assertIn("Other drive &lt;test&gt;", source)
        self.assertIn("Not measured", source)
        self.assertIn("40.00 Gb/s", source)
        self.assertEqual(
            report.render_references({"references": {"items": []}}, messages("en"), {}),
            "",
        )

    def test_cache_reference_keeps_its_label_and_converts_only_measured_bytes(self):
        result = {
            "hardware": {"drive": {"model": "Drive A 2TB"}},
            "references": {
                "for_drive_model": "Drive A 2TB",
                "items": [
                    self.reference(
                        metric="cache_capacity_estimate_bytes", unit="GiB", value=20
                    )
                ],
            },
        }
        source = report.render_references(
            result, messages("en"), {"cache_capacity_estimate_bytes": 10 * 1024**3}
        )
        self.assertIn(messages("en")["cache_capacity"], source)
        self.assertNotIn(messages("en")["interface_limit"], source)
        self.assertIn("10.00 GiB", source)
        self.assertIn("20.00 GiB", source)

    def test_cache_card_reference_requires_same_device_and_measured_difference(self):
        result = {
            "hardware": {"drive": {"model": "Drive A 2TB"}},
            "references": {
                "for_drive_model": "Drive A 2TB",
                "items": [
                    self.reference(
                        metric="cache_capacity_estimate_bytes", unit="GiB", value=100
                    )
                ],
            },
        }
        source = report.render_cache_reference(
            result, {"status": "no_drop_observed"}, messages("en")
        )
        self.assertIn("100 GiB", source)
        self.assertNotIn("data-cache-reference-warning", source)
        for measured, warn in ((100, False), (70, False), (50, True), (150, True)):
            source = report.render_cache_reference(
                result,
                {
                    "status": "observed_drop_stable",
                    "cache_capacity_estimate_bytes": measured * 1024**3,
                },
                messages("en"),
            )
            self.assertEqual("data-cache-reference-warning" in source, warn)
        result["references"]["items"][0]["relationship"] = "peer"
        self.assertEqual(report.render_cache_reference(result, {}, messages("en")), "")
        result["references"]["items"][0]["relationship"] = "device"
        result["references"]["for_drive_model"] = "Other drive"
        self.assertEqual(report.render_cache_reference(result, {}, messages("en")), "")

    def test_local_reference_cache_reuses_only_device_context_without_measurements(
        self,
    ):
        from datetime import date

        hardware_models = {
            "drive": {"model": "Drive A 2TB"},
            "computer": {"model": "Computer A"},
        }
        data = {
            "for_drive_model": "Drive A 2TB",
            "checked_at": date.today().isoformat(),
            "items": [self.reference(private_path="PRIVATE-PATH")],
            "fio_results": "PRIVATE-RESULTS",
        }
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(new_dir())}):
            cache_reference_data(data, hardware_models)
            cached, info = load_reference_cache(hardware_models)
            self.assertEqual(info["status"], "fresh")
            self.assertTrue(info["from_cache"])
            self.assertEqual(cached["items"][0]["value"], 5000)
            path, _ = reference_cache_path(hardware_models)
            self.assertNotIn("PRIVATE-", path.read_text())

    def test_reference_cache_languages_are_separate_but_discoverable(self):
        from datetime import date

        device = {"drive": {"model": "Drive A 2TB"}}
        data = {
            "for_drive_model": "Drive A 2TB",
            "checked_at": date.today().isoformat(),
            "items": [self.reference()],
        }
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(new_dir())}):
            cache_reference_data(data, device, language="en")
            cached, info = load_reference_cache(device, language="zh-CN")
            self.assertIsNone(cached)
            self.assertIn("en", info["available_languages"])
            self.assertIsNotNone(load_reference_cache(device, language="en")[0])

    def test_reference_cache_expiry_refresh_and_changed_device_trigger_research(self):
        from datetime import date, timedelta

        device = {"drive": {"model": "Drive A 2TB"}}
        data = {
            "for_drive_model": "Drive A 2TB",
            "checked_at": date.today().isoformat(),
            "items": [self.reference()],
        }
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(new_dir())}):
            cache_reference_data(data, device)
            cached, info = load_reference_cache(
                device, today=date.today() + timedelta(days=31)
            )
            self.assertIsNotNone(
                cached
            )  # Still available offline, with an overdue label.
            self.assertEqual(info["status"], "stale")
            self.assertIsNone(load_reference_cache(device, refresh=True)[0])
            self.assertEqual(
                load_reference_cache({"drive": {"model": "Drive B 2TB"}})[1]["status"],
                "miss",
            )
            path, _ = reference_cache_path(device)
            path.write_text("[]")
            self.assertEqual(load_reference_cache(device)[1]["status"], "invalid")

    def test_reference_packet_for_another_drive_does_not_supply_peer_or_candidate_data(
        self,
    ):
        result = {
            "hardware": {"drive": {"model": "Drive B 2TB"}},
            "references": {
                "for_drive_model": "Drive A 2TB",
                "items": [self.reference(relationship="peer")],
            },
        }
        self.assertEqual(report.render_references(result, messages("en"), {}), "")

    def test_top_summary_includes_random_speeds_and_unmeasured_cache(self):
        cases = []
        for name, direction, value in [
            ("seq_read", "read", 2939.74),
            ("seq_write", "write", 2054.86),
            ("rand_read", "read", 72.89),
            ("rand_write", "write", 54.49),
        ]:
            cases.append(
                dict(
                    name=name,
                    status="completed",
                    metrics={
                        direction: {"mb_s": value, "bytes": 1024, "latency_us": {}}
                    },
                )
            )
        result = dict(cases=cases, sustained_summary={"tail_mean_mb_s": 3366.62})
        output = new_dir()
        report.render_report(result, output, "zh-CN", messages("zh-CN"))
        source = (output / "report.html").read_text()
        short = source.split('<dl class="cards short-cards">')[1].split("</dl>")[0]
        sustained = source.split('<dl class="cards long-cards">')[1].split("</dl>")[0]
        self.assertEqual(short.count('class="card"'), 4)
        self.assertEqual(sustained.count('class="card"'), 2)
        self.assertNotIn('data-metric="tail_mean_mb_s"', short)
        self.assertIn('data-metric="tail_mean_mb_s"', sustained)
        cards = short + sustained
        self.assertEqual(cards.count('class="card"'), 6)
        self.assertIn("72.89", cards)
        self.assertIn("54.49", cards)
        self.assertIn(messages("zh-CN")["tail_mean"] + "*", cards)
        self.assertIn(messages("zh-CN")["tail_explanation"], source)
        self.assertIn('data-state="not_measured"', cards)
        self.assertIn("尚未测出", cards)
        self.assertNotIn("末段加权平均", cards)


class ExecutionTests(unittest.TestCase):
    def setup_case(self, sleep=False, short=False):
        root = new_dir()
        work = root / "owned"
        work.mkdir()
        with (work / "payload.bin").open("wb") as file:
            file.truncate(MIB)
        output = root / "output"
        output.mkdir()
        script = root / "fake-fio"
        body = (
            "#!" + sys.executable + "\nimport sys,json,time\n"
            "a=dict(x[2:].split('=',1) for x in sys.argv[1:] if '=' in x)\n"
            "time.sleep(30)\n"
            if sleep
            else "#!" + sys.executable + "\nimport sys,json\n"
            "a=dict(x[2:].split('=',1) for x in sys.argv[1:] if '=' in x)\n"
            "from pathlib import Path\n"
            "count=int(a['io_size'])\n"
            + ("count=4096\n" if short else "")
            + "d={'jobs':[{'error':0,'eta':0,'job_runtime':1000,'write':{'io_bytes':count,'bw_bytes':count,'runtime':1000},'read':{'io_bytes':0}}]}\n"
            "Path(a['output']).write_text(json.dumps(d))\n"
        )
        script.write_text(body)
        script.chmod(0o700)
        target = dict(
            path=str(work), st_dev=work.stat().st_dev, inode=work.stat().st_ino
        )
        config = dict(file_bytes=MIB, reserve_bytes=0)
        job = dict(name="prepare", rw="write", bs=MIB, qd=1, io_bytes=MIB, runtime=10)
        return work, output, script, target, config, job

    def test_real_subprocess_measurement_and_incomplete_initialization(self):
        for short in (False, True):
            work, output, script, target, config, job = self.setup_case(short=short)
            with (
                contextlib.redirect_stderr(io.StringIO()),
                patch.object(benchmark, "check_target", return_value={}),
            ):
                progress = Progress(["prepare"], messages("en"))
                case = benchmark.run_case(
                    {"path": str(script)},
                    job,
                    config,
                    "posixaio",
                    output,
                    work,
                    target,
                    progress,
                )
            self.assertEqual(case["status"], "failed" if short else "completed")
            self.assertEqual(case["metrics_complete"], not short)
            self.assertTrue((output / "prepare.case.json").exists())

    def test_cache_runner_stops_after_stable_drop_and_keeps_final_fio_output(self):
        work, output, script, target, config, job = self.setup_case()
        job.update(
            name="sustained", cache_test=True, io_bytes=128 * 1024**3, runtime=1800
        )
        script.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """
import json,sys,signal,time
from pathlib import Path
args = dict(x[2:].split('=',1) for x in sys.argv[1:] if '=' in x)
records, log, cumulative = [], [], 0
for second in range(1, 101):
    speed = 1000 if second <= 10 else 200
    cumulative += speed * 1000000
    records.append({'jobs':[{'error':0,'eta':1,'job_runtime':second*1000,
        'write':{'io_bytes':cumulative,'bw_bytes':speed*1000000},'read':{'io_bytes':0}}]})
    log.append(f'{second*1000},{speed*1000000/1024},1')
def finish(signum, frame):
    records[-1]['jobs'][0]['eta'] = 0
    with Path(args['output']).open('a') as stream:
        stream.write(json.dumps(records[-1]))
    Path(args['write_bw_log'] + '_bw.1.log').write_text('\\n'.join(log))
    sys.exit(0)
signal.signal(signal.SIGINT, finish)
Path(args['output']).write_text('\\n'.join(json.dumps(record) for record in records) + '\\n')
time.sleep(5)
sys.exit(7)
"""
        )
        with (
            patch.object(benchmark, "check_target", return_value={}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            case = benchmark.run_case(
                {"path": str(script)},
                job,
                config,
                "posixaio",
                output,
                work,
                target,
                Progress(["sustained"], messages("en")),
            )
        self.assertEqual(case["status"], "completed", case.get("detail"))
        self.assertEqual(case["stop_reason"], "post_drop_stable")
        self.assertEqual(
            cache_assessment(case["series"])["status"], "observed_drop_stable"
        )
        self.assertTrue(case["metrics_complete"])

    def test_cancel_terminates_owned_child_and_preserves_case(self):
        work, output, script, target, config, job = self.setup_case(sleep=True)
        processes = []
        real_popen = benchmark.subprocess.Popen

        def track(*a, **kw):
            proc = real_popen(*a, **kw)
            processes.append(proc)
            return proc

        real_sleep = benchmark.time.sleep
        interrupted = False

        def interrupt_once(seconds):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            real_sleep(seconds)

        with (
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(benchmark, "check_target", return_value={}),
            patch.object(benchmark.subprocess, "Popen", side_effect=track),
            patch.object(benchmark.time, "sleep", side_effect=interrupt_once),
        ):
            progress = Progress(["prepare"], messages("en"))
            with self.assertRaises(KeyboardInterrupt):
                benchmark.run_case(
                    {"path": str(script)},
                    job,
                    config,
                    "posixaio",
                    output,
                    work,
                    target,
                    progress,
                )
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(
            json.loads((output / "prepare.case.json").read_text())["status"],
            "cancelled",
        )

    def test_fio_installed_by_package_manager_and_verified(self):
        progress = type("P", (), {"emit": lambda *a, **kw: None})()
        with (
            patch.object(
                system_io,
                "find_fio",
                side_effect=[None, {"path": "/fio", "version": "fio-3.43"}],
            ),
            patch.object(system_io.platform, "system", return_value="Darwin"),
            patch.object(system_io.shutil, "which", return_value="/brew"),
            patch.object(system_io, "logged_command") as install,
        ):
            actual = system_io.ensure_fio(new_dir(), progress)
        self.assertEqual(actual["version"], "fio-3.43")
        self.assertEqual(install.call_args.args[0], ["/brew", "install", "fio"])


class ReviewRegressionTests(unittest.TestCase):
    def test_truncated_final_snapshot_rejected(self):
        stream = json.dumps(fixture_snapshot()) + '\n{"jobs":'
        self.assertEqual(len(json_objects(stream)), 1)
        with self.assertRaises(ValueError):
            json_objects(stream, strict=True)

    def test_existing_pdf_cannot_be_reused_as_new_export(self):
        root = new_dir()
        source, destination, browser = (
            root / "report.html",
            root / "report.pdf",
            root / "fake-browser",
        )
        source.write_text("<h1>New data</h1>")
        destination.write_bytes(b"%PDF-" + b"old" * 100)
        browser.write_text("#!" + sys.executable + "\n")
        browser.chmod(0o700)
        with patch.dict(os.environ, {"DISKBENCH_BROWSER": str(browser)}):
            actual, error = report.export_pdf(source, destination)
        self.assertIsNone(actual)
        self.assertEqual(destination.read_bytes(), b"%PDF-" + b"old" * 100)

    def test_dependency_cancel_stops_child_process_group(self):
        root = new_dir()
        ready, stopped = root / "ready", root / "stopped"
        child = root / "child.py"
        child.write_text(
            "import signal,time\nfrom pathlib import Path\ndef stop(*args):\n Path("
            + repr(str(stopped))
            + ").write_text('stopped')\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nPath("
            + repr(str(ready))
            + ").write_text('ready')\ntime.sleep(30)\n"
        )
        parent = root / "parent.py"
        parent.write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable,"
            + repr(str(child))
            + "])\ntime.sleep(30)\n"
        )

        class CancelWhenReady:
            def emit(self, *args, **kwargs):
                if ready.exists():
                    raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            system_io.logged_command(
                [sys.executable, str(parent)],
                root / "install.log",
                CancelWhenReady(),
                timeout=5,
            )
        # Child signal handler is proof it received termination, not only its parent.
        import time

        for _ in range(20):
            if stopped.exists():
                break
            time.sleep(0.05)
        self.assertTrue(stopped.exists())


class ProcessTreeRegressionTests(unittest.TestCase):
    def test_child_ignoring_term_is_killed_after_leader_exits(self):
        root = new_dir()
        ready = root / "child-pid"
        child = root / "ignore-term.py"
        child.write_text(
            "import os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath("
            + repr(str(ready))
            + ").write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        parent = root / "parent.py"
        parent.write_text(
            "import subprocess,sys,time\nsubprocess.Popen([sys.executable,"
            + repr(str(child))
            + "])\ntime.sleep(30)\n"
        )

        class CancelWhenReady:
            def emit(self, *args, **kwargs):
                if ready.exists():
                    raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            system_io.logged_command(
                [sys.executable, str(parent)],
                root / "install.log",
                CancelWhenReady(),
                timeout=5,
            )
        pid = ready.read_text()
        state = system_io.subprocess.run(
            ["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True
        ).stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), state)


class HardwareTests(unittest.TestCase):
    def test_target_matching_changes_between_devices_without_retaining_serials(self):
        profile = {
            "SPHardwareDataType": [
                {
                    "machine_name": "Computer A",
                    "chip_type": "Chip A",
                    "machine_model": "Model A",
                    "serial_number": "PRIVATE-HOST-SERIAL",
                }
            ],
            "SPNVMeDataType": [
                {
                    "_items": [
                        {
                            "bsd_name": "disk0",
                            "device_model": "Internal drive",
                            "serial_num": "PRIVATE-INTERNAL",
                        },
                        {
                            "bsd_name": "disk6",
                            "device_model": "Drive A",
                            "serial_num": "PRIVATE-DRIVE-A",
                        },
                        {
                            "bsd_name": "disk8",
                            "device_model": "Drive B",
                            "serial_num": "PRIVATE-DRIVE-B",
                        },
                    ]
                }
            ],
            "SPThunderboltDataType": [
                {
                    "route_string_key": "0",
                    "device_name_key": "Computer A",
                    "_items": [
                        {
                            "route_string_key": "1",
                            "vendor_name_key": "Bridge vendor",
                            "device_name_key": "Bridge ID",
                            "serial_number": "PRIVATE-BRIDGE",
                        }
                    ],
                }
            ],
        }
        a = hardware.mac_hardware(profile, ["disk6"])
        self.assertEqual(a["drive"]["model"], "Drive A")
        profile["SPHardwareDataType"][0].update(
            machine_name="Computer B", machine_model="Model B"
        )
        b = hardware.mac_hardware(profile, ["disk8"])
        self.assertEqual(b["drive"]["model"], "Drive B")
        self.assertIn("Computer B", b["computer"]["model"])
        self.assertNotIn("Computer A", b["computer"]["model"])
        self.assertIsNone(b["enclosure"]["model"])
        self.assertEqual(
            b["enclosure"]["bridge_candidates"], ["Bridge vendor Bridge ID"]
        )
        self.assertNotIn("PRIVATE", json.dumps([a, b]))

    def test_unmatched_disk_remains_unknown_and_override_is_labeled(self):
        profile = {
            "SPNVMeDataType": [{"bsd_name": "disk0", "device_model": "Internal only"}]
        }
        self.assertIsNone(hardware.mac_hardware(profile, ["disk9"])["drive"]["model"])
        with patch.object(hardware.platform, "system", return_value="Other"):
            result = hardware.collect_hardware(
                {"model": None},
                computer="My computer",
                drive="My drive",
                enclosure="My enclosure",
            )
        self.assertEqual(
            result["enclosure"],
            {"model": "My enclosure", "source": "user", "bridge_candidates": []},
        )

    def test_hardware_html_escapes_names_and_explains_unknown_enclosure(self):
        result = {
            "cases": [],
            "hardware": {
                "computer": {"model": "Computer <A>", "source": "user"},
                "drive": {"model": "Drive & B", "source": "system_profiler"},
                "enclosure": {
                    "model": None,
                    "source": "unavailable",
                    "bridge_candidates": ["Bridge <C>"],
                },
            },
        }
        output = new_dir()
        paths = report.render_report(result, output, "zh-CN", messages("zh-CN"))
        source = Path(paths["html"]).read_text()
        self.assertIn("Computer &lt;A&gt;", source)
        self.assertIn("Drive &amp; B", source)
        self.assertIn("Bridge &lt;C&gt;", source)
        self.assertIn(messages("zh-CN")["enclosure_model"], source)
        self.assertIn(messages("zh-CN")["controller_model"], source)
        self.assertIn(messages("zh-CN")["controller_candidate"], source)


if __name__ == "__main__":
    unittest.main()
