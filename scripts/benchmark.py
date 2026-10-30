#!/usr/bin/env python3
"""Run bounded file-based fio tests. Standard library only, Python 3.10+."""

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from progress import Progress, language_default, messages
from hardware import collect_hardware
from reference_data import (
    load_reference_data,
    load_reference_cache,
    cache_reference_data,
    apply_hardware_specs,
)
from system_io import (
    BenchError,
    DeviceLock,
    check_target,
    ensure_fio,
    target_info,
    trash_owned,
    stop_process,
    unsafe_path_component,
    observe_quality,
)
from workloads import (
    GIB,
    MIB,
    bandwidth_series,
    fio_arguments,
    json_objects,
    metrics,
    sustained_summary,
    cache_assessment,
    job_label,
    auto_plan,
    collection_status,
    core_completeness,
    case_core_missing,
    active_runtime_ms,
)
from write_allowance import WriteAllowance, plan_hash


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target",
        required=True,
        help="Explicit readable/writable directory on local mounted storage (internal or external); never a raw device",
    )
    p.add_argument(
        "--output",
        default="diskbench-results",
        help="New output directory, preferably on another volume",
    )
    p.add_argument(
        "--language",
        default=language_default(),
        help="Report BCP-47 language; agent passes conversation language",
    )
    p.add_argument(
        "--ui-language",
        default=language_default(),
        help="Progress/error language, independent of report language",
    )
    p.add_argument(
        "--messages",
        help="Complete translated report language JSON; see assets/locales/en.json",
    )
    p.add_argument("--ui-messages", help="Complete translated progress language JSON")
    p.add_argument("--format", choices=["html", "pdf"], default="html")
    p.add_argument(
        "--reference-file", help="Agent-researched, localized reference JSON; optional"
    )
    p.add_argument(
        "--refresh-references",
        action="store_true",
        help="Ignore cached reference facts; agent must research again",
    )
    p.add_argument(
        "--computer-model",
        help="User-confirmed test computer model; otherwise auto-detect",
    )
    p.add_argument(
        "--drive-model", help="User-confirmed drive model; otherwise auto-detect"
    )
    p.add_argument(
        "--enclosure-model",
        help="User-confirmed retail enclosure model; bridge IDs are not retail models",
    )
    p.add_argument(
        "--controller-model", help="User-confirmed enclosure controller model"
    )
    p.add_argument("--interface-name", help="User-confirmed host access-interface name; preserves system protocol and physical-device safety checks")
    p.add_argument(
        "--keep-test-files",
        action="store_true",
        help="Retain owned test files instead of moving to OS Trash",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect only: no installation, network, test files or output writes",
    )
    return p


def confirm_write(target, config, allowance, text=None):
    digest = plan_hash(target, config, allowance)
    print(json.dumps(dict(target=target["path"], physical_devices=target["physical_devices"],
                          file_bytes=config["file_bytes"], max_write_bytes=config["max_write_bytes"],
                          reserve_bytes=config["reserve_bytes"], max_runtime_seconds=config["max_runtime_seconds"],
                          remaining_authorized_bytes=allowance["remaining"] if allowance["present"] else None,
                          new_finite_allowance=not allowance["present"], plan_sha256=digest)), file=sys.stderr)
    if sys.stdin.isatty():
        answer = input((text or messages("en"))["confirm_write"])
        confirmed = answer == "RUN"
    else:
        raw = sys.stdin.readline(1025)
        try:
            answer = json.loads(raw) if len(raw) <= 1024 else None
        except ValueError:
            answer = None
        confirmed = answer == {"confirm_plan_sha256": digest}
    if not confirmed:
        raise BenchError("unsafe_target", "Write plan was not explicitly confirmed", 2)
    return digest


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def select_engine(fio):
    preferred = {"Darwin": "posixaio", "Linux": "libaio", "Windows": "windowsaio"}.get(platform.system())
    if preferred is None:
        raise BenchError("unsupported", "Unsupported operating system", 3)
    if preferred not in fio["engines"].split():
        raise BenchError(
            "unsupported", f"Required asynchronous engine unavailable: {preferred}", 3
        )
    return preferred


def check_owned(work, identity, file_identity=None):
    stat = work.lstat()
    if unsafe_path_component(work) or (stat.st_dev, stat.st_ino) != identity:
        raise BenchError("unsafe_target", "Owned workspace changed")
    payload = work / "payload.bin"
    item = payload.lstat()
    if platform.system() == "Windows" and (not stat.st_ino or not item.st_ino):
        raise BenchError("unsafe_target", "Stable owned-file identity unavailable")
    if unsafe_path_component(payload) or not payload.is_file() or item.st_nlink != 1:
        raise BenchError("unsafe_target", "Test payload is not an owned regular file")
    if file_identity and (item.st_dev, item.st_ino) != file_identity:
        raise BenchError("unsafe_target", "Test file identity changed")


def run_case(fio, job, config, engine, output, work, target, progress):
    raw = output / f"{job['name']}.fio.json-stream"
    log = output / job["name"]
    args = fio_arguments(fio["path"], job, config["file_bytes"], engine, raw, log)
    case = dict(
        **job,
        status="running",
        command=args,
        raw=raw.name,
        series=[],
        temperatures=[],
        reason=None,
    )
    if job.get("idle_seconds"):
        idle_start = time.monotonic()
        while time.monotonic() - idle_start < job["idle_seconds"]:
            remaining = job["idle_seconds"] - (time.monotonic() - idle_start)
            check_target(target, config["reserve_bytes"])
            progress.emit(job["name"], percent=0, remaining=remaining + job["runtime"])
            time.sleep(max(0, min(1, remaining)))
    start = time.monotonic()
    cache_points = []
    last_write_bytes, last_elapsed = 0, 0.0
    cache_stop_requested = None
    last_sensor = start - 5
    identity = (work.stat().st_dev, work.stat().st_ino)
    file_identity = (
        (work / "payload.bin").stat().st_dev,
        (work / "payload.bin").stat().st_ino,
    )
    check_target(target, config["reserve_bytes"])
    check_owned(work, identity, file_identity)
    with (output / f"{job['name']}.stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(
            args,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=stderr,
            stderr=stderr,
            start_new_session=platform.system() != "Windows",
        )
        try:
            while proc.poll() is None:
                if time.monotonic() - last_sensor >= 5:
                    sensor = check_target(target, config["reserve_bytes"])
                    case["temperatures"].append(
                        dict(
                            seconds=round(time.monotonic() - start, 2),
                            celsius=sensor.get("temperature_c"),
                        )
                    )
                    last_sensor = time.monotonic()
                check_owned(work, identity, file_identity)
                stat = Path(target["path"]).stat()
                if (stat.st_dev, stat.st_ino) != (target["st_dev"], target["inode"]):
                    raise BenchError("unsafe_target", "Target disconnected or replaced")
                if time.monotonic() - start > job["runtime"] + 45:
                    raise BenchError("run_failed", f"Timeout: {job['name']}", 5)
                objects = (
                    json_objects(raw.read_text(encoding="utf-8"))
                    if raw.exists()
                    else []
                )
                if objects:
                    snapshot = metrics(objects[-1])
                    done = sum(snapshot[d]["bytes"] for d in ("read", "write"))
                    elapsed = (snapshot.get("runtime_ms") or 0) / 1000
                    if job.get("cache_test"):
                        for record in objects:
                            observed = metrics(record)
                            observed_seconds = (observed.get("runtime_ms") or 0) / 1000
                            if observed_seconds <= last_elapsed:
                                continue
                            written = observed["write"]["bytes"]
                            if written >= last_write_bytes:
                                cache_points.append(
                                    dict(
                                        seconds=observed_seconds,
                                        mb_s=(written - last_write_bytes)
                                        / (observed_seconds - last_elapsed)
                                        / 1_000_000,
                                        direction="write",
                                    )
                                )
                            last_elapsed, last_write_bytes = observed_seconds, written
                        timing = collection_status(dict(rw="write", metrics=observed, series=cache_points)) if config.get("method_version") == "drivespeed-auto-full-v1" else None
                        assessment = cache_assessment(cache_points, sampling_status=timing)
                        if (
                            assessment["status"] == "observed_drop_stable"
                            and timing in (None, "collected")
                            and written >= job.get("initialize_bytes", 0)
                            and cache_stop_requested is None
                            and platform.system() != "Windows"
                        ):
                            # SIGINT lets fio flush its final snapshot and bandwidth log.
                            cache_stop_requested = time.monotonic()
                            os.killpg(proc.pid, signal.SIGINT)
                    if (
                        cache_stop_requested is not None
                        and time.monotonic() - cache_stop_requested > 30
                    ):
                        raise BenchError(
                            "run_failed",
                            "fio did not finish after the cache observation",
                            5,
                        )
                    fraction = max(done / job["io_bytes"], elapsed / job["runtime"])
                    rate = done / elapsed if elapsed > 0 else None
                    eta = (
                        min(
                            max(0, job["runtime"] - elapsed),
                            max(0, job["io_bytes"] - done) / rate,
                        )
                        if rate
                        else None
                    )
                    # Cache exhaustion time is unknown; budget consumption is not completion.
                    progress.emit(
                        job["name"],
                        percent=None
                        if job.get("cache_test")
                        else min(99, fraction * 100),
                        remaining=None if job.get("cache_test") else eta,
                    )
                else:
                    progress.emit(job["name"])
                time.sleep(1)
            if proc.returncode and not (
                cache_stop_requested is not None
                and proc.returncode in (-signal.SIGINT, 128 + signal.SIGINT)
            ):
                raise BenchError(
                    "run_failed", f"fio exit {proc.returncode}; {raw.name}", 5
                )
            objects = json_objects(
                raw.read_text(encoding="utf-8"),
                strict=True,
                allow_signal_notice=cache_stop_requested is not None,
            )
            if not objects:
                raise ValueError("fio produced no JSON measurements")
            case["metrics"] = metrics(objects[-1])
            completed = sum(case["metrics"][d]["bytes"] for d in ("read", "write"))
            if not completed:
                raise ValueError("fio completed zero I/O")
            # fio's ETA is an estimate (sometimes INT_MAX even in its final
            # successful JSON). Require the measured workload endpoint instead
            # of treating ETA as a completion flag, including for early exits.
            if (cache_stop_requested is None and completed < job["io_bytes"]
                    and active_runtime_ms(case["metrics"], job["rw"]) < job["runtime"] * 1000):
                raise ValueError("fio final snapshot has not reached the byte or runtime limit")
            if (job["name"] == "prepare" or job.get("require_complete")) and completed < job["io_bytes"]:
                raise ValueError(
                    "Incomplete diagnostic coverage; refusing to claim a complete write/read verification"
                )
            tolerance = 0 if config.get("method_version") == "drivespeed-auto-full-v1" else job["bs"] * job["qd"]
            if completed > job["io_bytes"] + tolerance:
                raise ValueError("Reported I/O exceeds the planned bound")
            case["status"] = "completed"
            if job.get("cache_test"):
                case["stop_reason"] = (
                    "post_drop_stable"
                    if cache_stop_requested is not None
                    else "byte_limit"
                    if completed >= job["io_bytes"]
                    else "time_limit"
                )
        except KeyboardInterrupt:
            case.update(status="cancelled", reason="cancelled")
            raise
        except (OSError, ValueError, BenchError) as exc:
            case.update(
                status="failed",
                reason=getattr(exc, "key", "run_failed"),
                detail=str(exc),
            )
        finally:
            stop_process(proc)
            case["elapsed_seconds"] = round(time.monotonic() - start, 3)
            if "metrics" not in case and raw.exists():
                try:
                    snapshots = json_objects(raw.read_text(encoding="utf-8"))
                    if snapshots:
                        case["metrics"] = metrics(snapshots[-1])
                except (ValueError, OSError):
                    pass
            case["metrics_complete"] = case["status"] == "completed"
            for path in output.glob(f"{job['name']}_bw.*.log"):
                try:
                    case["series"].extend(bandwidth_series(path, strict=config.get("method_version") == "drivespeed-auto-full-v1"))
                except (OSError, ValueError) as exc:
                    case["collection_error"] = str(exc)
            if config.get("method_version") == "drivespeed-auto-full-v1":
                case["collection_status"] = collection_status(case)
                missing = case_core_missing(case, job, config)
                if missing and case["status"] == "completed":
                    case.update(status="partial", reason="incomplete_measurement", metrics_complete=False)
                    case["missing_core"] = missing
            save(output / f"{job['name']}.case.json", case)
    return case


def execute(args, text, report_text):
    reference_data = (
        load_reference_data(args.reference_file) if args.reference_file else {}
    )
    target = target_info(args.target)
    ledger = WriteAllowance()
    allowance = ledger.read(target)
    config = auto_plan(target["total_bytes"], target["free_bytes"], allowance["run_cap"], allowance["remaining"])

    current_hardware = collect_hardware(
        target,
        computer=args.computer_model,
        drive=args.drive_model,
        enclosure=args.enclosure_model,
        controller=args.controller_model,
        access_interface=args.interface_name,
    )
    cached, reference_cache = load_reference_cache(
        current_hardware, args.refresh_references, language=args.language
    )
    if args.reference_file:
        reference_data["language"] = args.language
        reference_cache = dict(
            status="provided",
            checked_at=reference_data.get("checked_at"),
            from_cache=False,
        )
    elif cached:
        reference_data = cached
    apply_hardware_specs(current_hardware, reference_data)
    text = dict(text)
    for job in config["jobs"]:
        text[job["name"]] = job_label(job, text)
    if target["free_bytes"] < config["file_bytes"] + config["reserve_bytes"]:
        raise BenchError(
            "space_failed", str(config["file_bytes"] + config["reserve_bytes"])
        )
    if args.dry_run:
        print(
            json.dumps(
                dict(
                    target=target,
                    hardware=current_hardware,
                    reference_cache=reference_cache,
                    plan=config,
                    plan_sha256=plan_hash(target, config, allowance),
                    write_authorization=allowance,
                    cleanup_policy="retain_owned_files" if args.keep_test_files or platform.system() == "Windows" else "system_trash",
                    language=args.language,
                    ui_language=args.ui_language,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    output = Path(args.output).expanduser().absolute()
    # Must not follow existing output symlinks or overwrite previous evidence.
    if output.exists() or unsafe_path_component(output):
        raise BenchError(
            "unsafe_target", "Output must be a new path without symlink components"
        )
    output.mkdir(parents=True, mode=0o700)
    output = output.resolve()
    run_id = uuid.uuid4().hex
    result = dict(
        schema_version=1,
        run_id=run_id,
        created=datetime.now(timezone.utc).isoformat(),
        target=target,
        environment=dict(
            os=platform.platform(), architecture=platform.machine(), fio=None,
            os_family={"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "unknown"),
            os_version=platform.mac_ver()[0] if platform.system() == "Darwin" else platform.release()
        ),
        plan=config,
        tool=dict(version="unknown", method_version="drivespeed-auto-full-v1"),
        cases=[],
        series=[],
        outcome="failed",
        report={},
        notes=[],
        references=reference_data,
    )
    labels = (
        ["inspect", "dependency"]
        + [j["name"] for j in config["jobs"]]
        + ["cleanup", "report"]
    )
    progress = Progress(labels, text, output / "progress.jsonl")
    progress.emit("inspect", force=True)
    result["hardware"] = current_hardware
    if args.reference_file:
        try:
            reference_cache = cache_reference_data(
                reference_data, current_hardware, language=args.language
            )
        except OSError:
            reference_cache = dict(status="write_failed", from_cache=False)
    result["reference_cache"] = reference_cache
    progress.emit("inspect", "completed", 100, force=True)
    work = None
    error_code = 0
    reserved = False
    attempted = set()
    with DeviceLock(target):
        try:
            progress.emit("dependency", force=True)
            fio = ensure_fio(output, progress)
            engine = select_engine(fio)
            result["environment"].update(
                fio=fio,
                engine=engine,
                load_average=list(os.getloadavg())
                if hasattr(os, "getloadavg")
                else None,
            )
            progress.emit("dependency", "completed", 100, force=True)
            result["measurement_quality"] = observe_quality(target)
            fresh = check_target(target, config["reserve_bytes"])
            target.update(fresh)
            config = auto_plan(fresh["total_bytes"], fresh["free_bytes"], allowance["run_cap"], allowance["remaining"])
            result["plan"] = config
            confirmed = confirm_write(target, config, allowance, text)
            ledger.reserve(target, config, allowance, run_id, confirmed)
            reserved = True
            result["write_authorization"] = dict(plan_sha256=confirmed, reserved_bytes=config["max_write_bytes"])
            check_target(target, config["file_bytes"] + config["reserve_bytes"] + config["metadata_headroom_bytes"])
            candidate = Path(target["path"]) / f".diskbench-{run_id}"
            candidate.mkdir(mode=0o700)
            work = candidate
            result["test_directory"] = str(work)
            fd = os.open(
                work / "payload.bin",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                os.ftruncate(fd, config["file_bytes"])
            finally:
                os.close(fd)
            for job in config["jobs"]:
                progress.emit(job["name"], percent=0, force=True)
                attempted.add(job["name"])
                try:
                    case = run_case(
                        fio, job, config, engine, output, work, target, progress
                    )
                except KeyboardInterrupt:
                    case_path = output / f"{job['name']}.case.json"
                    if case_path.exists():
                        result["cases"].append(
                            json.loads(case_path.read_text(encoding="utf-8"))
                        )
                    progress.emit(job["name"], "cancelled", force=True)
                    raise
                result["cases"].append(case)
                result["series"].extend(
                    dict(case=case["name"], **point) for point in case["series"]
                )
                progress.emit(
                    job["name"],
                    case["status"],
                    100 if case["status"] == "completed" else None,
                    force=True,
                )
                save(output / "results.json", result)
                if case["status"] != "completed":
                    error_code = 6 if case["status"] == "partial" else 5
                    break
            result["outcome"] = (
                "complete"
                if len(result["cases"]) == len(config["jobs"]) and not error_code
                else "partial"
            )
            check_target(target)
        except KeyboardInterrupt:
            result["outcome"], error_code = "cancelled", 130
        except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
            result["error"] = dict(
                key=getattr(exc, "key", "run_failed"), detail=str(exc)
            )
            result["outcome"] = "partial" if result["cases"] else "failed"
            error_code = exc.code if isinstance(exc, BenchError) else 5
            print(
                text.get(getattr(exc, "key", "run_failed"), text["run_failed"])
                + ": "
                + str(exc),
                file=sys.stderr,
            )
        finally:
            if work:
                try:
                    if args.keep_test_files or platform.system() == "Windows":
                        result["notes"].append("retain_note")
                    else:
                        trash_owned(work, target)
                        result["notes"].append("trash_note")
                    progress.emit("cleanup", "completed", 100, force=True)
                except (OSError, BenchError, subprocess.SubprocessError) as exc:
                    result["notes"].append("cleanup_failed")
                    result["cleanup_error"] = str(exc)
                    progress.emit("cleanup", "failed", force=True)
                    error_code = error_code or 5
                    if result["outcome"] == "complete":
                        result["outcome"] = "partial"
            else:
                progress.emit("cleanup", "skipped", force=True)
            done = {c["name"] for c in result["cases"]}
            unreturned_attempt = bool(attempted - done)
            for job in config["jobs"]:
                if job["name"] not in done:
                    result["cases"].append(
                        dict(**job, status="skipped", reason=result["outcome"])
                    )
            result["elapsed_seconds"] = round(time.monotonic() - progress.started, 3)
            result["accounted_write_bytes"] = sum(
                c.get("metrics", {}).get("write", {}).get("bytes", 0)
                for c in result["cases"]
            )
            result["actual_write_bytes"] = (
                result["accounted_write_bytes"]
                if all(
                    c.get("status") in ("completed", "skipped", "partial") for c in result["cases"]
                )
                and result["outcome"] != "cancelled"
                and not unreturned_attempt
                else None
            )
            sustained = next(
                (c for c in result["cases"] if c["name"] == "sustained"), {}
            )
            if any(c.get("collection_status") == "collected_irregular" for c in result["cases"]):
                result["notes"].append("sampling_irregular")
            timing = collection_status(sustained) if config.get("method_version") == "drivespeed-auto-full-v1" else None
            result["sustained_summary"] = sustained_summary(sustained.get("series", []), sampling_status=timing)
            result["cache_assessment"] = cache_assessment(sustained.get("series", []), sampling_status=timing)
            result["cache_assessment"]["stop_reason"] = sustained.get(
                "stop_reason", "bounded_test"
            )
            result["completeness"] = core_completeness(result)
            if not result["completeness"]["complete"]:
                if result["outcome"] == "complete":
                    result["outcome"] = "partial"
                error_code = error_code or 6
            if reserved:
                ledger.settle(run_id, result["actual_write_bytes"])
            save(output / "results.json", result)
    from report import render_report

    progress.emit("report", force=True)
    paths = render_report(result, output, args.language, report_text, args.format)
    result["report"] = paths
    save(output / "results.json", result)
    progress.emit(
        "report",
        "completed" if paths["pdf"] or args.format == "html" else "failed",
        100 if paths["pdf"] or args.format == "html" else None,
        force=True,
    )
    print(
        json.dumps(
            dict(
                outcome=result["outcome"],
                results=str(output / "results.json"),
                report=paths,
            ),
            ensure_ascii=False,
        )
    )
    return error_code or (5 if args.format == "pdf" and not paths["pdf"] else 0)


def main():
    args = parser().parse_args()
    try:
        text = messages(args.ui_language, args.ui_messages)
        report_text = messages(args.language, args.messages)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return execute(args, text, report_text)
    except (BenchError, OSError, ValueError, subprocess.SubprocessError) as exc:
        key = getattr(exc, "key", "run_failed")
        print(
            f"{text.get(key, text['run_failed'])}: {exc}\n{text['recovery']}",
            file=sys.stderr,
        )
        return exc.code if isinstance(exc, BenchError) else (2 if isinstance(exc, ValueError) else 5)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
