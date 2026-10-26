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
from progress import Progress, messages
from workloads import (
    MIB,
    PROFILES,
    fio_arguments,
    json_objects,
    metrics,
    plan,
    sustained_summary,
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
                "device": "<script>alert(1)</script>",
                "path": "/Users/secret/drive",
            },
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
            self.assertIn("&lt;script&gt;", source)
            self.assertNotIn("/Users/secret", source)
            self.assertIn(
                'href="https://github.com/granoflow/external-disk-benchmark"', source
            )
            # A user-clicked project link is allowed; automatic remote assets are not.
            self.assertNotIn('src="https://', source)
            self.assertNotIn("@import", source)
            self.assertNotIn("url(https://", source)
            self.assertIn("window.print()", source)
            self.assertIn('dir="rtl"' if language == "ar" else 'dir="ltr"', source)

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
            patch.object(benchmark, "ensure_fio") as install,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    benchmark.execute(args, messages("en"), messages("en")), 0
                )
        install.assert_not_called()
        self.assertFalse((root / "must-not-exist").exists())


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
        self.assertIn(messages("zh-CN")["enclosure_unknown"], source)


if __name__ == "__main__":
    unittest.main()
