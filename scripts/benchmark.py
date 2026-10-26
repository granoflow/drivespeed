#!/usr/bin/env python3
"""Run bounded file-based fio tests. Standard library only, Python 3.10+."""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from progress import Progress, language_default, messages
from hardware import collect_hardware
from system_io import (
    BenchError,
    DeviceLock,
    check_target,
    ensure_fio,
    find_fio,
    target_info,
    trash_owned,
    stop_process,
)
from workloads import (
    bandwidth_series,
    byte_size,
    fio_arguments,
    json_objects,
    metrics,
    plan,
    sustained_summary,
)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target",
        required=True,
        help="Existing directory on a mounted external disk; never a raw device",
    )
    p.add_argument(
        "--output",
        default="diskbench-results",
        help="New output directory, preferably on another volume",
    )
    p.add_argument("--profile", choices=["quick", "full"], default="full")
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
    p.add_argument("--file-size", type=byte_size)
    p.add_argument("--write-budget", type=byte_size)
    p.add_argument("--seconds", type=int, help="Maximum seconds per ordinary test")
    p.add_argument(
        "--sustained-seconds",
        type=int,
        help="Maximum sustained write seconds; byte budget still applies",
    )
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


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def select_engine(fio):
    preferred = "posixaio" if platform.system() == "Darwin" else "libaio"
    if preferred not in fio["engines"].split():
        raise BenchError(
            "unsupported", f"Required asynchronous engine unavailable: {preferred}", 3
        )
    return preferred


def check_owned(work, identity, file_identity=None):
    stat = work.lstat()
    if work.is_symlink() or (stat.st_dev, stat.st_ino) != identity:
        raise BenchError("unsafe_target", "Owned workspace changed")
    payload = work / "payload.bin"
    item = payload.lstat()
    if payload.is_symlink() or not payload.is_file() or item.st_nlink != 1:
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
    start = time.monotonic()
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
            start_new_session=True,
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
                    progress.emit(
                        job["name"], percent=min(99, fraction * 100), remaining=eta
                    )
                else:
                    progress.emit(job["name"])
                time.sleep(1)
            if proc.returncode:
                raise BenchError(
                    "run_failed", f"fio exit {proc.returncode}; {raw.name}", 5
                )
            objects = json_objects(raw.read_text(encoding="utf-8"), strict=True)
            if not objects:
                raise ValueError("fio produced no JSON measurements")
            if objects[-1]["jobs"][0].get("eta") != 0:
                raise ValueError("fio final snapshot does not indicate completion")
            case["metrics"] = metrics(objects[-1])
            completed = sum(case["metrics"][d]["bytes"] for d in ("read", "write"))
            if not completed:
                raise ValueError("fio completed zero I/O")
            if job["name"] == "prepare" and completed < config["file_bytes"]:
                raise ValueError(
                    "Initialization did not write the full file; refusing read benchmarks"
                )
            if completed > job["io_bytes"] + job["bs"] * job["qd"]:
                raise ValueError("Reported I/O exceeds the planned bound")
            case["status"] = "completed"
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
                case["series"].extend(bandwidth_series(path))
            save(output / f"{job['name']}.case.json", case)
    return case


def execute(args, text, report_text):
    config = plan(
        args.profile,
        args.file_size,
        args.write_budget,
        args.seconds,
        args.sustained_seconds,
    )
    target = target_info(args.target)
    if target["free_bytes"] < config["file_bytes"] + config["reserve_bytes"]:
        raise BenchError(
            "space_failed", str(config["file_bytes"] + config["reserve_bytes"])
        )
    if args.dry_run:
        print(
            json.dumps(
                dict(
                    target=target,
                    plan=config,
                    fio=find_fio(),
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
    if output.exists() or any(x.is_symlink() for x in (output, *output.parents)):
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
            os=platform.platform(), architecture=platform.machine(), fio=None
        ),
        plan=config,
        cases=[],
        series=[],
        outcome="failed",
        report={},
        notes=[],
    )
    labels = (
        ["inspect", "dependency"]
        + [j["name"] for j in config["jobs"]]
        + ["cleanup", "report"]
    )
    progress = Progress(labels, text, output / "progress.jsonl")
    progress.emit("inspect", force=True)
    result["hardware"] = collect_hardware(
        target,
        computer=args.computer_model,
        drive=args.drive_model,
        enclosure=args.enclosure_model,
    )
    progress.emit("inspect", "completed", 100, force=True)
    work = None
    error_code = 0
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
            check_target(target, config["file_bytes"] + config["reserve_bytes"])
            work = Path(target["path"]) / f".diskbench-{run_id}"
            work.mkdir(mode=0o700)
            fd = os.open(
                work / "payload.bin",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            os.ftruncate(fd, config["file_bytes"])
            os.close(fd)
            result["test_directory"] = str(work)
            for job in config["jobs"]:
                progress.emit(job["name"], percent=0, force=True)
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
                    error_code = 5
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
            error_code = getattr(exc, "code", 5)
            print(
                text.get(getattr(exc, "key", "run_failed"), text["run_failed"])
                + ": "
                + str(exc),
                file=sys.stderr,
            )
        finally:
            if work:
                try:
                    if args.keep_test_files:
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
                    c.get("status") in ("completed", "skipped") for c in result["cases"]
                )
                and result["outcome"] != "cancelled"
                else None
            )
            sustained = next(
                (c for c in result["cases"] if c["name"] == "sustained"), {}
            )
            result["sustained_summary"] = sustained_summary(sustained.get("series", []))
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
        return getattr(exc, "code", 2 if isinstance(exc, ValueError) else 5)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
