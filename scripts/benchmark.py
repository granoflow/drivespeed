#!/usr/bin/env python3
"""Run bounded file-based fio tests. Standard library only, Python 3.10+."""

from workloads import AUTO_METHODS, PEAK_METHOD, PEAK_SUPPLEMENT_METHOD, QUICK_METHODS, quick_plan
import argparse
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import threading
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
    mark_workspace,
    clean_stale_workspaces,
    trashed_workspace_identity,
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
    peak_plan,
    peak_supplement_plan,
    collection_status,
    core_completeness,
    peak_completeness,
    case_core_missing,
    active_runtime_ms,
    write_counter_samples,
    cache_observation,
)
from write_allowance import RunWriteProtection as WriteAllowance, plan_hash
from run_deadline import RunDeadline


class ReuseSpaceGuard:
    """Stop this fio process group if reused-file writes consume safety space."""
    def __init__(self, proc, path, minimum):
        self.proc, self.path, self.minimum = proc, path, minimum
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def start(self):
        self.thread.start()

    def _watch(self):
        while not self.stop.is_set():
            try:
                fs = os.statvfs(self.path)
                if fs.f_bavail * fs.f_frsize >= self.minimum:
                    if self.stop.wait(.1):
                        return
                    continue
            except OSError:
                pass
            self.failed.set()
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            return

    def close(self):
        self.stop.set()
        self.thread.join()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--target", "--disk",
        required=True,
        help="An internal or external disk root; never a raw device. Writable mounted directories remain compatible.",
    )
    p.add_argument(
        "--output",
        default="diskbench-results",
        help="New output directory, preferably on another volume",
    )
    p.add_argument("--propose-write-bytes", type=int,
                   help="Propose an exact finite normal-test write plan; does not authorize writes without plan confirmation")
    p.add_argument("--working-file-bytes", type=int,
                   help="MiB-aligned full-test working file size within the available space and write budget")
    p.add_argument("--case-write-bytes", type=int,
                   help="MiB-aligned per-case write limit for full measurements")
    p.add_argument("--peak-io-bytes", type=int,
                   help="MiB-aligned byte limit for each queue-depth scan")
    p.add_argument("--reuse-trashed-workspace", help="Explicitly reuse an owned, fully allocated macOS Trash workspace on this target")
    p.add_argument("--stage", choices=("quick", "full", "peak", "peak-supplement"), default="full",
                   help="Quick actual sequential measurement, or deeper full test; legacy CLI default is full")
    p.add_argument("--supplement-workload", action="append", choices=("seq_read", "seq_write", "rand_read", "rand_write"),
                   help="Peak supplement workload; repeat for each required workload")
    p.add_argument("--supplement-depths", choices=("16,32", "32,64"), metavar="PAIR",
                   help="Adjacent queue-depth pair for a peak supplement")
    p.add_argument("--peak-source", help="Original peak results to compare with this separate supplement")
    p.add_argument("--quick-from", help="Preserve a same-target quick result alongside the deeper test; never rerun it")
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
    p.add_argument("--capacity-from", help="Optional capacity/read-back check after this completed speed result; preview and explicit write confirmation required")
    return p


def confirm_write(target, config, allowance, text=None):
    digest = plan_hash(target, config, allowance)
    if config.get("total_timeout_seconds") and config.get("profile") == "full":
        print((text or messages("en"))["test_timeout_notice"].format(
            minutes=f'{config["total_timeout_seconds"] / 60:g}'), file=sys.stderr)
    if config.get("method_version") == "drivespeed-capacity-v1":
        print((text or messages("en"))["capacity_warning"], file=sys.stderr)
    notice = dict(target=target["path"], physical_devices=target["physical_devices"],
                          file_bytes=config["file_bytes"], max_write_bytes=config["max_write_bytes"],
                          reserve_bytes=config["reserve_bytes"], max_runtime_seconds=config["max_runtime_seconds"],
                          remaining_authorized_bytes=allowance["remaining"] if allowance["present"] else None,
                          new_finite_allowance=not allowance["present"],
                          additional_authorized_bytes=config.get("additional_authorized_bytes", 0),
                          capacity_run_cap_bytes=config.get("capacity_run_cap_bytes"),
                          plan_sha256=digest)
    if allowance.get("mode") == "single_run":
        for key in ("remaining_authorized_bytes", "new_finite_allowance", "additional_authorized_bytes", "capacity_run_cap_bytes"):
            notice.pop(key)
        notice["write_protection"] = "single_run"
    print(json.dumps(notice), file=sys.stderr)
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
    deadline = getattr(progress, "deadline", None)
    if deadline is not None:
        deadline.check()
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
            if deadline is not None:
                deadline.check()
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
    reused = config.get('reuse_workspace')
    if reused and (identity != (reused['target_device'], reused['workspace_inode'])
                   or file_identity != (reused['target_device'], reused['payload_inode'])):
        raise BenchError('unsafe_target', 'Reused test file identity changed')
    check_target(target, config["reserve_bytes"])
    check_owned(work, identity, file_identity)
    if deadline is not None:
        deadline.check()
    with (output / f"{job['name']}.stderr.log").open("wb") as stderr:
        proc = subprocess.Popen(
            args,
            cwd=work,
            stdin=subprocess.DEVNULL,
            stdout=stderr,
            stderr=stderr,
            start_new_session=platform.system() != "Windows",
        )
        guard = ReuseSpaceGuard(proc, target['path'], config['reserve_bytes'] + GIB) if reused else None
        try:
            if guard:
                guard.start()
            if deadline is not None:
                deadline.attach(proc)
            while proc.poll() is None:
                if deadline is not None:
                    deadline.check()
                if reused:
                    space = os.statvfs(target['path'])
                    if space.f_bavail * space.f_frsize < config['reserve_bytes'] + GIB:
                        raise BenchError('space_failed', 'Reuse safety headroom reached')
                if time.monotonic() - last_sensor >= 5:
                    sensor = check_target(target, config["reserve_bytes"] + (GIB if reused else 0))
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
                        timing = collection_status(dict(rw="write", metrics=observed, series=cache_points)) if config.get("method_version") in (*AUTO_METHODS, PEAK_METHOD) else None
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
                time.sleep(.1 if config.get("reuse_workspace") else 1)
            if deadline is not None:
                deadline.check()
            if guard and guard.failed.is_set():
                raise BenchError('space_failed', 'Reuse safety headroom reached')
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
            if job.get('cache_test') and config.get('method_version') == PEAK_METHOD:
                case['cache_write_counters_v1'] = write_counter_samples(objects)
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
            tolerance = 0 if config.get("method_version") in (*AUTO_METHODS, PEAK_METHOD, PEAK_SUPPLEMENT_METHOD, "drivespeed-capacity-v1") else job["bs"] * job["qd"]
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
                status="partial" if getattr(exc, "key", None) == "total_timeout" else "failed",
                reason=getattr(exc, "key", "run_failed"),
                detail=str(exc),
            )
        finally:
            if guard and guard.thread.ident is not None:
                guard.close()
                if guard.failed.is_set() and case['status'] == 'completed':
                    case.update(status='failed', reason='space_failed',
                                detail='Reuse safety headroom reached')
            stop_process(proc)
            if deadline is not None:
                deadline.detach(proc)
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
                    case["series"].extend(bandwidth_series(path, strict=config.get("method_version") in (*AUTO_METHODS, PEAK_METHOD, PEAK_SUPPLEMENT_METHOD)))
                except (OSError, ValueError) as exc:
                    case["collection_error"] = str(exc)
            if config.get("method_version") in (*AUTO_METHODS, *QUICK_METHODS, PEAK_METHOD, PEAK_SUPPLEMENT_METHOD):
                case["collection_status"] = collection_status(case)
                missing = case_core_missing(case, job, config)
                if missing and case["status"] == "completed":
                    case.update(status="partial", reason="incomplete_measurement", metrics_complete=False)
                    case["missing_core"] = missing
            save(output / f"{job['name']}.case.json", case)
    return case


def normal_plan(target, allowance, proposed_bytes=None, file_bytes=None, case_write_bytes=None):
    def make_plan(run_cap, remaining_cap):
        if file_bytes is None and case_write_bytes is None:
            return auto_plan(target["total_bytes"], target["free_bytes"], run_cap, remaining_cap)
        return auto_plan(target["total_bytes"], target["free_bytes"], run_cap, remaining_cap,
                         file_bytes, case_write_bytes)

    if allowance.get("mode") == "single_run":
        amount = 1024 * GIB if proposed_bytes is None else proposed_bytes
        if type(amount) is not int or not 0 < amount <= 2**63 - 1:
            raise ValueError("Invalid single-run write limit")
        config = make_plan(amount, amount)
        config.update(total_timeout_seconds=2580, max_runtime_seconds=2580)
        return config
    if proposed_bytes is None:
        return make_plan(allowance["run_cap"], allowance["remaining"])
    if type(proposed_bytes) is not int or not 0 < proposed_bytes <= 2**63 - 1:
        raise ValueError("Invalid finite write proposal")
    config = make_plan(proposed_bytes, proposed_bytes)
    config.update(additional_authorized_bytes=max(0, config["max_write_bytes"] - allowance["remaining"]) if allowance["present"] else 0,
                  normal_run_cap_bytes=max(allowance["run_cap"], config["max_write_bytes"]) if allowance["present"] else config["max_write_bytes"])
    return config


def load_quick_source(filename, target):
    path = Path(filename)
    limit = 16*1024*1024
    try:
        if unsafe_path_component(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Quick result must be a regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Invalid quick result file")
            raw = stream.read(limit+1)
        if len(raw) > limit:
            raise ValueError("Quick result exceeds the size limit")
        source = json.loads(raw)
        if (not isinstance(source.get("run_id"), str) or not source["run_id"].strip()
                or source.get("tool", {}).get("method_version") not in QUICK_METHODS
                or source.get("plan", {}).get("method_version") != source.get("tool", {}).get("method_version")
                or source.get("outcome") != "complete" or source.get("synthetic")
                or any(source.get("target", {}).get(key) != target.get(key)
                       for key in ("path", "st_dev", "inode", "physical_devices"))
                or not core_completeness(source)["complete"]):
            raise ValueError("A completed same-target quick measurement is required")
        return source
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
        raise ValueError("Invalid or unavailable quick result") from exc


def execute(args, text, report_text):
    reuse_path = getattr(args, 'reuse_trashed_workspace', None)
    if reuse_path and (args.stage not in ('peak', 'peak-supplement') or args.working_file_bytes is None or args.keep_test_files):
        raise ValueError('Trash reuse requires a peak stage, an exact working file size, and default cleanup')
    supplement_workloads = getattr(args, 'supplement_workload', None)
    supplement_depths = getattr(args, 'supplement_depths', None)
    peak_source_path = getattr(args, 'peak_source', None)
    if args.stage == 'peak-supplement':
        if (args.capacity_from or args.quick_from or args.case_write_bytes is not None
                or args.propose_write_bytes is None or args.working_file_bytes is None
                or args.peak_io_bytes is None or not supplement_workloads or not supplement_depths
                or args.format != 'html'):
            raise ValueError('Peak supplement requires explicit write, file, I/O, workloads and depths')
    elif supplement_workloads is not None or supplement_depths is not None or peak_source_path is not None:
        raise ValueError('Supplement options require peak-supplement stage')
    if args.stage == "quick" and (args.peak_io_bytes is not None or args.capacity_from or args.propose_write_bytes is not None or args.quick_from or args.working_file_bytes is not None or args.case_write_bytes is not None):
        raise ValueError("Quick measurements do not take deeper-test or capacity options")
    if args.capacity_from and (args.propose_write_bytes is not None or args.working_file_bytes is not None or args.case_write_bytes is not None):
        raise ValueError("Capacity checks use their own exact authorization proposal")
    if args.stage == "peak":
        if (args.capacity_from or args.quick_from or args.propose_write_bytes is None
                or args.working_file_bytes is None or args.case_write_bytes is None
                or args.peak_io_bytes is None):
            raise ValueError("Peak stage requires explicit write, file, case and peak I/O bounds")
    elif args.stage != 'peak-supplement' and args.peak_io_bytes is not None:
        raise ValueError("Peak I/O limit requires peak stage")
    reference_data = (
        load_reference_data(args.reference_file) if args.reference_file else {}
    )
    target = target_info(args.target)
    reuse = trashed_workspace_identity(reuse_path, target, args.working_file_bytes) if reuse_path else None
    ledger = WriteAllowance()
    allowance = ledger.read(target)
    peak_source = None
    peak_source_hash = None
    if peak_source_path:
        from peak_comparison import load_peak_source, validate_source_target
        peak_source, peak_source_hash = load_peak_source(peak_source_path)
        validate_source_target(peak_source, target, args.working_file_bytes)
    capacity_source = None
    if args.capacity_from:
        from capacity_check import load_source, capacity_plan
        capacity_source = load_source(args.capacity_from, target)
    def build_plan():
        if args.stage == "quick":
            return quick_plan(target)
        if args.stage == 'peak-supplement':
            effective_free = min(target['total_bytes'], target['free_bytes'] + (reuse['payload_bytes'] if reuse else 0))
            result = peak_supplement_plan(target['total_bytes'], effective_free,
                args.propose_write_bytes, args.working_file_bytes, args.peak_io_bytes,
                supplement_workloads, [int(value) for value in supplement_depths.split(',')])
            if reuse:
                result['reuse_workspace'] = reuse
            if peak_source is not None:
                result['source_peak_sha256'] = peak_source_hash
                result['source_peak_run_id'] = peak_source['run_id']
            return result
        if args.stage == "peak":
            effective_free = min(target["total_bytes"], target["free_bytes"] + (reuse['payload_bytes'] if reuse else 0))
            plan = peak_plan(target["total_bytes"], effective_free,
                             args.propose_write_bytes, args.working_file_bytes,
                             args.case_write_bytes, args.peak_io_bytes)
            if reuse:
                plan['reuse_workspace'] = reuse
            return plan
        if capacity_source is not None:
            return capacity_plan(target, allowance, capacity_source)
        return normal_plan(target, allowance, args.propose_write_bytes,
                           args.working_file_bytes, args.case_write_bytes)

    config = build_plan()
    quick_source = None
    if args.quick_from:
        quick_source = load_quick_source(args.quick_from, target)

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
    required_free = config["reserve_bytes"] + (GIB if reuse else config["file_bytes"])
    if target["free_bytes"] < required_free:
        raise BenchError(
            "space_failed", str(required_free)
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
                    cleanup_policy="already_in_system_trash" if reuse else "retain_owned_files" if args.keep_test_files or platform.system() == "Windows" else "system_trash",
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
        tool=dict(version="unknown", method_version=config["method_version"]),
        cases=[],
        series=[],
        outcome="failed",
        report={},
        notes=[],
        references=reference_data,
    )
    if quick_source is not None:
        result["quick_measurement"] = dict(run_id=quick_source["run_id"],
                                         target=quick_source["target"], cases=quick_source["cases"],
                                         note="Earlier quick working set; retained separately, not substituted for deeper workloads")
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
    deadline = None
    with DeviceLock(target):
        try:
            check_target(target)
            result["startup_cleanup"] = clean_stale_workspaces(target)
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
            from device_identity import collect_device_identity
            identity = collect_device_identity(target)
            if identity is not None:
                result['device_identity'] = identity
            if peak_source is not None and (identity != peak_source['device_identity']
                    or engine != peak_source['environment']['engine']
                    or fio.get('version') != peak_source['environment']['fio']['version']):
                raise BenchError('unsafe_target', 'Peak source device, I/O engine or fio version changed')
            observation = observe_quality(target)
            # Observation is conversation context, not report/JSON data. Do not
            # persist it or rewrite a saved run using a later idle observation.
            observation_status = observation['status']
            print(text.get('quality_' + observation_status, observation_status), file=sys.stderr)
            fresh = check_target(target, config["reserve_bytes"])
            target.update(fresh)
            config = build_plan()
            result["plan"] = config
            confirmed = confirm_write(target, config, allowance, text)
            deadline = RunDeadline(config.get("total_timeout_seconds", config["max_runtime_seconds"]))
            progress.deadline = deadline
            result["time_protection"] = dict(seconds=deadline.seconds, scope="all test jobs and waits after confirmation; report generation follows shutdown")
            ledger.reserve(target, config, allowance, run_id, confirmed)
            reserved = True
            result["write_authorization"] = dict(plan_sha256=confirmed, reserved_bytes=config["max_write_bytes"])
            check_target(target, config["reserve_bytes"] + (GIB if reuse else config["file_bytes"] + config["metadata_headroom_bytes"]))
            if reuse:
                if trashed_workspace_identity(reuse_path, target, config['file_bytes']) != reuse:
                    raise BenchError('unsafe_target', 'Trashed workspace changed after preview')
                work = Path(reuse['path'])
            else:
                candidate = Path(target["path"]) / f".diskbench-{run_id}"
                candidate.mkdir(mode=0o700)
                work = candidate
                mark_workspace(work, target, run_id, retain=args.keep_test_files or platform.system() == "Windows")
                fd = os.open(work / "payload.bin", os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    os.ftruncate(fd, config["file_bytes"])
                finally:
                    os.close(fd)
            result["test_directory"] = str(work)
            for job in config["jobs"]:
                deadline.check()
                if (config.get("method_version") == PEAK_METHOD
                        and job.get("peak_workload") and job["qd"] == 64):
                    from peak_analysis import assess_peak_pair
                    workload = job["peak_workload"]
                    previous = {case["name"]: case for case in result["cases"]}
                    names = [f"peak_{workload}_{depth}_{repeat}"
                             for depth in (16, 32) for repeat in (1, 2)]
                    if all(name in previous for name in names) and assess_peak_pair(
                            [previous[name] for name in names],
                            **config["peak_criteria"])["status"] == "plateau":
                        case = dict(**job, status="skipped", reason="peak_plateau", series=[])
                        result["cases"].append(case)
                        progress.emit(job["name"], "skipped", force=True)
                        save(output / f"{job['name']}.case.json", case)
                        save(output / "results.json", result)
                        continue
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
                    if case.get("reason") == "total_timeout":
                        result["stop_reason"] = "total_timeout"
                        result["notes"].append("total_timeout")
                    # A valid initialized file still supports the remaining
                    # measurements when only the cache-stability goal is missing.
                    # Preserve partial qualification; never continue after I/O,
                    # initialization, identity or collection-integrity failure.
                    if (config.get("method_version") == "drivespeed-auto-full-v2"
                            and job["name"] == "sustained" and case["status"] == "partial"
                            and case.get("reason") == "incomplete_measurement"
                            and set(case.get("missing_core", [])) == {"sustained.post_cache_stability"}):
                        continue
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
            if getattr(exc, "key", None) == "total_timeout":
                result.update(outcome="partial", stop_reason="total_timeout")
            error_code = exc.code if isinstance(exc, BenchError) else 5
            print(
                text.get(getattr(exc, "key", "run_failed"), text["run_failed"])
                + ": "
                + str(exc),
                file=sys.stderr,
            )
        finally:
            if deadline is not None:
                deadline.close()
            if work:
                try:
                    if reuse:
                        result["notes"].append("trash_note")
                    elif args.keep_test_files or platform.system() == "Windows":
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
                and not any(c.get("reason") == "total_timeout" for c in result["cases"])
                and not unreturned_attempt
                else None
            )
            sustained = next(
                (c for c in result["cases"] if c["name"] == "sustained"), {}
            )
            if any(c.get("collection_status") == "collected_irregular" for c in result["cases"]):
                result["notes"].append("sampling_irregular")
            points, timing = cache_observation(sustained)
            if config.get("method_version") not in (*AUTO_METHODS, PEAK_METHOD):
                timing = None
            result["sustained_summary"] = sustained_summary(points, sampling_status=timing)
            result["cache_assessment"] = cache_assessment(points, sampling_status=timing)
            result["cache_assessment"]["stop_reason"] = sustained.get(
                "stop_reason", "bounded_test"
            )
            if config.get('method_version') == PEAK_SUPPLEMENT_METHOD:
                from peak_analysis import assess_peak_pair
                cases = {case['name']: case for case in result['cases']}
                pair = [int(value) for value in supplement_depths.split(',')]
                assessments = {}
                for workload in supplement_workloads:
                    names = [f'peak_{workload}_{depth}_{repeat}' for depth in pair for repeat in (1, 2)]
                    assessments[workload] = assess_peak_pair([cases[name] for name in names], **config['peak_criteria'])
                result['peak_supplement'] = assessments
                if peak_source is not None:
                    from peak_comparison import compare_peak_runs
                    result['peak_comparison'] = compare_peak_runs(peak_source, result)
                # A separate diagnostic cannot make the prior full run complete.
                result['completeness'] = {'complete': False, 'missing_core': ['full_run']}
            elif capacity_source is not None:
                from capacity_check import completeness
                result["completeness"] = completeness(result)
            else:
                result["completeness"] = core_completeness(result)
            if config.get("method_version") == PEAK_METHOD:
                from peak_analysis import assess_peak_scan
                result["peak_assessment"] = assess_peak_scan(result["cases"], **config["peak_criteria"])
                result["completeness"] = peak_completeness(result)
            if not result["completeness"]["complete"]:
                if result["outcome"] == "complete":
                    result["outcome"] = "partial"
                error_code = error_code or 6
            if reserved:
                ledger.settle(run_id, result["actual_write_bytes"])
            save(output / "results.json", result)
    if config.get('method_version') == PEAK_SUPPLEMENT_METHOD:
        progress.emit('report', 'skipped', force=True)
        print(json.dumps(dict(outcome=result['outcome'], results=str(output / 'results.json'), report=None)))
        return error_code
    from report import render_report

    progress.emit("report", force=True)
    paths = render_report(result, output, args.language, report_text, args.format)
    if capacity_source is not None:
        import copy
        combined = copy.deepcopy(capacity_source)
        combined.pop("_source_sha256", None)
        historical_observation = combined.pop('measurement_quality', None)
        combined.pop('quality', None)
        if isinstance(historical_observation, dict):
            status = historical_observation.get('status')
            if status in ('quiet_observed', 'interference_observed', 'visibility_limited'):
                print(text['quality_' + status], file=sys.stderr)
        if isinstance(combined.get('report'), dict):
            combined['report'].pop('delivery_followup', None)
        combined["capacity_check"] = {key: result[key] for key in
                                      ("run_id", "created", "tool", "plan", "cases", "outcome", "completeness")}
        combined["capacity_check"]["speed_source_sha256"] = config["source_sha256"]
        save(output / "combined-results.json", combined)
        result["capacity_report"] = paths
        paths = render_report(combined, output / "combined-report", args.language, report_text, args.format)
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
