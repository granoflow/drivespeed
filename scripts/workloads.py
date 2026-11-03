"""Finite fio plans, JSON stream parsing and sustained-write measurements."""

import json
import math
import statistics

PEAK_METHOD = "drivespeed-peak-v1"
PEAK_SUPPLEMENT_METHOD = "drivespeed-peak-supplement-v1"
AUTO_METHOD = "drivespeed-auto-full-v2"
AUTO_METHODS = ("drivespeed-auto-full-v1", AUTO_METHOD)
QUICK_METHOD = "drivespeed-quick-v2"
QUICK_METHODS = ("drivespeed-quick-v1", QUICK_METHOD)

GIB = 1_073_741_824
MIB = 1_048_576
PROFILES = {
    "quick": dict(
        file_bytes=GIB, write_budget=8 * GIB, seconds=10, sustained=30, prepare=300
    ),
    "cache": dict(
        file_bytes=1024 * GIB,
        write_budget=1024 * GIB,
        seconds=30,
        sustained=1800,
        prepare=0,
    ),
    "full": dict(
        file_bytes=8 * GIB,
        write_budget=64 * GIB,
        seconds=30,
        sustained=180,
        prepare=900,
    ),
}


def byte_size(text):
    import re

    match = re.fullmatch(r"(\d+)(KiB|MiB|GiB|TiB|B)?", text)
    if not match:
        raise ValueError("Use positive integer bytes or KiB/MiB/GiB/TiB")
    value = (
        int(match[1])
        * {"B": 1, None: 1, "KiB": 1024, "MiB": MIB, "GiB": GIB, "TiB": 1024 * GIB}[
            match[2]
        ]
    )
    if value <= 0 or value % 4096:
        raise ValueError("Size must be positive and aligned to 4 KiB")
    return value


def cache_write_plan(estimate=None):
    """Size the single pass from sourced expectations; never turn expectations into measurements."""
    if estimate is None:
        return dict(
            basis="fallback",
            requested_bytes=1024 * GIB,
            cache_bytes=None,
            sources=[],
            interface_may_mask_cache=False,
        )
    if not isinstance(estimate, dict) or estimate.get("basis") not in (
        "web",
        "model_knowledge",
    ):
        raise ValueError("Cache estimate basis must be web or model_knowledge")
    if (
        not isinstance(estimate.get("drive_model"), str)
        or not estimate["drive_model"].strip()
    ):
        raise ValueError(
            "Cache estimate must identify the exact drive model and capacity"
        )
    cache = estimate.get("cache_bytes")
    if isinstance(cache, bool) or not isinstance(cache, int) or cache <= 0:
        raise ValueError(
            "Estimated write cache must be positive integer bytes, not DRAM capacity"
        )
    sources = estimate.get("sources", [])
    if not isinstance(sources, list) or not all(
        isinstance(s, str) and s.startswith("https://") for s in sources
    ):
        raise ValueError("Cache sources must be HTTPS URLs")
    if estimate["basis"] == "web" and not sources:
        raise ValueError("Web estimates require actual source URLs")
    if estimate["basis"] == "model_knowledge":
        basis = estimate.get("knowledge_basis")
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError(
                "Model knowledge requires recorded recall evidence and model/capacity/cache checks; use the 1 TiB fallback when uncertain"
            )
    rates = []
    for key in ("post_cache_mb_s", "host_write_mb_s"):
        rate = estimate.get(key)
        if rate is not None:
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(rate)
                or rate <= 0
            ):
                raise ValueError("Expected speeds must be finite positive MB/s")
            rates.append(rate)
    # 90 seconds covers settling and observation; 25% covers reference/state uncertainty.
    post_bytes = min(rates) * 1_000_000 * 90 if rates else cache * 0.5
    requested = cache + max(32 * GIB, cache * 0.25) + max(64 * GIB, post_bytes)
    result = dict(estimate)
    result.update(
        requested_bytes=math.ceil(requested / MIB) * MIB,
        interface_may_mask_cache=(
            estimate.get("post_cache_mb_s") is not None
            and estimate.get("host_write_mb_s") is not None
            and estimate["post_cache_mb_s"] >= estimate["host_write_mb_s"]
        ),
    )
    return result


def plan(profile, file_bytes=None, write_budget=None, seconds=None, sustained=None):
    config = dict(PROFILES[profile])
    config.update(
        {
            k: v
            for k, v in dict(
                file_bytes=file_bytes,
                write_budget=write_budget,
                seconds=seconds,
                sustained=sustained,
            ).items()
            if v is not None
        }
    )
    if profile == "cache":
        size = config["file_bytes"]
        budget = config["write_budget"]
        if size < MIB or size % MIB or budget < MIB or config["sustained"] < 95:
            raise ValueError(
                "Cache test requires MiB-aligned space, positive budget and at least 95 seconds"
            )
        # One pass over a fresh file: no initialization writes or wraparound before the cache test.
        config.update(
            jobs=[
                dict(
                    name="sustained",
                    rw="write",
                    bs=MIB,
                    qd=16,
                    io_bytes=min(size, budget) // MIB * MIB,
                    runtime=config["sustained"],
                    cache_test=True,
                )
            ],
            reserve_bytes=max(10 * GIB, size // 4),
            max_runtime_seconds=config["sustained"] + 45,
            max_write_bytes=min(size, budget) // MIB * MIB,
            cache_post_drop_seconds=60,
        )
        return config
    size, budget = config["file_bytes"], config["write_budget"]
    if size < MIB or size % MIB or budget < size + 6 * MIB:
        raise ValueError(
            "File must be MiB-aligned and budget must cover initialization plus six MiB"
        )
    if min(config["seconds"], config["sustained"]) <= 0:
        raise ValueError("Duration must be positive")
    ordinary = min(size, max(MIB, ((budget - size) // 10 // MIB) * MIB))
    jobs = [
        dict(
            name="prepare",
            rw="write",
            bs=MIB,
            qd=1,
            io_bytes=size,
            runtime=config["prepare"],
        )
    ]
    for name, rw, bs, qd in [
        ("seq_write", "write", MIB, 1),
        ("seq_read", "read", MIB, 1),
        ("seq_write_q", "write", MIB, 16),
        ("seq_read_q", "read", MIB, 16),
        ("rand_write", "randwrite", 4096, 1),
        ("rand_read", "randread", 4096, 1),
        ("rand_write_q", "randwrite", 4096, 16),
        ("rand_read_q", "randread", 4096, 16),
        ("mixed", "randrw", 4096, 16),
    ]:
        jobs.append(
            dict(
                name=name,
                rw=rw,
                bs=bs,
                qd=qd,
                io_bytes=size if rw in ("read", "randread") else ordinary,
                runtime=config["seconds"],
            )
        )
    spent = (
        size + 5 * ordinary
    )  # Mixed I/O charged entirely as writes: conservative upper bound.
    jobs.append(
        dict(
            name="sustained",
            rw="write",
            bs=MIB,
            qd=16,
            io_bytes=((budget - spent) // MIB) * MIB,
            runtime=config["sustained"],
        )
    )
    jobs.append(
        dict(
            name="sustained_read",
            rw="read",
            bs=MIB,
            qd=16,
            io_bytes=size,
            runtime=config["seconds"],
        )
    )
    config.update(
        jobs=jobs,
        reserve_bytes=max(GIB, size // 4),
        max_runtime_seconds=sum(x["runtime"] + 45 for x in jobs),
        max_write_bytes=sum(
            x["io_bytes"] for x in jobs if x["rw"] not in ("read", "randread")
        ),
    )
    return config


def auto_plan(total_bytes, free_bytes, run_cap, remaining_cap, file_bytes=None, case_write_bytes=None):
    """Freeze a bounded full plan; authorization is supplied by the write ledger."""
    for value in (total_bytes, free_bytes, run_cap, remaining_cap):
        if type(value) is not int or not 0 <= value <= 2**64 - 1:
            raise ValueError("Capacity and authorized byte limits must be uint64 integers")
    if not total_bytes or free_bytes > total_bytes:
        raise ValueError("Invalid target capacity")
    budget = min(run_cap, remaining_cap)
    reserve = max(10 * GIB, (total_bytes + 9) // 10)
    available = max(0, free_bytes - reserve - 64 * MIB)
    if file_bytes is None:
        size = min(8 * GIB, (4 * available) // 5, budget // 4) // MIB * MIB
    else:
        if (type(file_bytes) is not int or file_bytes % MIB
                or file_bytes < GIB or file_bytes > available
                or file_bytes > budget // 4):
            raise ValueError("Working file must fit available space and one quarter of the write budget")
        size = file_bytes
    if size < GIB or budget < 8 * GIB:
        raise ValueError("Insufficient free space or authorized bytes for a complete test")
    if case_write_bytes is None:
        ordinary = min(size, budget // 20) // MIB * MIB
    else:
        if (type(case_write_bytes) is not int or case_write_bytes < MIB
                or case_write_bytes % MIB or case_write_bytes > budget // 5):
            raise ValueError("Case write limit must be MiB-aligned and fit the write budget")
        ordinary = case_write_bytes
    sustained = (budget - 5 * ordinary) // MIB * MIB
    if sustained < size:
        raise ValueError("Write allowance cannot initialize the working file")
    # Reuse the established workload identities, block sizes and queue depths.
    config = plan("full", size, budget, 30, 1800)
    jobs = [job for job in config["jobs"] if job["name"] != "prepare"]
    for job in jobs:
        job["io_bytes"] = size if job["rw"] in ("read", "randread") else ordinary
    first = next(job for job in jobs if job["name"] == "sustained")
    first.update(io_bytes=sustained, cache_test=True, initialize_bytes=size)
    jobs = [first] + [job for job in jobs if job is not first]
    config.update(
        profile="full", method_version=AUTO_METHOD, jobs=jobs,
        reserve_bytes=reserve, metadata_headroom_bytes=64 * MIB,
        max_write_bytes=sustained + 5 * ordinary,
        max_runtime_seconds=sum(job["runtime"] + 45 for job in jobs),
    )
    return config


def peak_plan(total_bytes, free_bytes, write_budget, file_bytes,
              case_write_bytes, peak_io_bytes, seconds=20):
    """Freeze every possible scan job and its worst-case write reservation."""
    for value in (peak_io_bytes, seconds):
        if type(value) is not int or value <= 0:
            raise ValueError("Peak I/O and duration must be positive integers")
    if peak_io_bytes % MIB:
        raise ValueError("Peak I/O must be MiB-aligned")
    config = auto_plan(total_bytes, free_bytes, write_budget, write_budget,
                       file_bytes, case_write_bytes)
    peak_writes = 12 * peak_io_bytes
    sustained = write_budget - file_bytes - 5 * case_write_bytes - peak_writes
    sustained = sustained // MIB * MIB
    if sustained < file_bytes:
        raise ValueError("Write budget cannot cover initialization, scanning and sustained write")

    init = dict(name="prepare", rw="write", bs=MIB, qd=16,
                io_bytes=file_bytes, runtime=900, require_complete=True)
    base = config["jobs"]
    sustained_job = next(job for job in base if job["name"] == "sustained")
    sustained_job.update(io_bytes=sustained, initialize_bytes=file_bytes)
    short_jobs = [job for job in base if job["name"] != "sustained"]
    for job in short_jobs:
        if job["rw"] in ("read", "randread"):
            job["io_bytes"] = max(file_bytes, peak_io_bytes)
    scans = []
    for workload, rw, bs in (("seq_read", "read", MIB),
                             ("seq_write", "write", MIB),
                             ("rand_read", "randread", 4096),
                             ("rand_write", "randwrite", 4096)):
        for depth in (16, 32, 64):
            for repeat in (1, 2):
                scans.append(dict(name=f"peak_{workload}_{depth}_{repeat}",
                                  rw=rw, bs=bs, qd=depth, io_bytes=peak_io_bytes,
                                  runtime=seconds, peak_workload=workload))
    jobs = [init] + short_jobs + scans + [sustained_job]
    config.update(method_version=PEAK_METHOD, jobs=jobs,
                  peak_criteria={"minimum_seconds": 5, "tolerance": 0.05,
                                 "minimum_depth_percent": 20, "minimum_half_depth_percent": 90},
                  max_write_bytes=file_bytes + 5 * case_write_bytes
                  + peak_writes + sustained,
                  max_runtime_seconds=sum(job["runtime"] + 45 for job in jobs))
    return config


def peak_supplement_plan(total_bytes, free_bytes, write_budget, file_bytes,
                         peak_io_bytes, workloads, depths, seconds=20):
    """Reserve an independent, bounded pair of repeated peak measurements.

    This is not a complete full run and does not merge historical samples. The
    caller selects the workloads and adjacent depths after inspecting evidence.
    """
    values = (total_bytes, free_bytes, write_budget, file_bytes, peak_io_bytes, seconds)
    if any(type(v) is not int or not 0 < v <= 2**63 - 1 for v in values):
        raise ValueError('Peak supplement requires finite positive integer limits')
    if free_bytes > total_bytes or file_bytes < GIB or file_bytes % MIB or peak_io_bytes % MIB:
        raise ValueError('Invalid capacity or MiB-aligned peak supplement bounds')
    specs = {'seq_read': ('read', MIB), 'seq_write': ('write', MIB),
             'rand_read': ('randread', 4096), 'rand_write': ('randwrite', 4096)}
    if (not isinstance(workloads, (tuple, list)) or not workloads
            or any(not isinstance(w, str) or w not in specs for w in workloads)
            or len(set(workloads)) != len(workloads)):
        raise ValueError('Select distinct known peak workloads')
    if not isinstance(depths, (tuple, list)) or tuple(depths) not in ((16, 32), (32, 64)):
        raise ValueError('Select adjacent queue depths 16/32 or 32/64')
    if not 5 <= seconds <= 86400:
        raise ValueError('Peak sample runtime must be between five seconds and one day')
    reserve = max(10 * GIB, (total_bytes + 9) // 10)
    if free_bytes < reserve + file_bytes + 64 * MIB:
        raise ValueError('Insufficient space after reserve and metadata headroom')
    jobs = [dict(name='prepare', rw='write', bs=MIB, qd=16,
                 io_bytes=file_bytes, runtime=900, require_complete=True)]
    for workload in workloads:
        rw, bs = specs[workload]
        for depth in depths:
            for repeat in (1, 2):
                jobs.append(dict(name=f'peak_{workload}_{depth}_{repeat}', rw=rw,
                                 bs=bs, qd=depth, io_bytes=peak_io_bytes,
                                 runtime=seconds, peak_workload=workload))
    maximum = sum(job['io_bytes'] for job in jobs if job['rw'] in ('write', 'randwrite'))
    if maximum > write_budget or maximum > 2**63 - 1:
        raise ValueError('Write budget cannot cover the selected peak measurements')
    return dict(profile='peak_supplement', method_version=PEAK_SUPPLEMENT_METHOD,
                file_bytes=file_bytes, write_budget=write_budget, jobs=jobs,
                reserve_bytes=reserve, metadata_headroom_bytes=64 * MIB,
                max_write_bytes=maximum,
                max_runtime_seconds=sum(job['runtime'] + 45 for job in jobs),
                peak_criteria={'minimum_seconds': 5, 'tolerance': 0.05,
                               'minimum_depth_percent': 20, 'minimum_half_depth_percent': 90})


def quick_plan(target):
    """Initialize once, then measure bounded QD1 sequential and 4 KiB random I/O."""
    reserve = max(10 * GIB, (target["total_bytes"] + 9) // 10)
    size = GIB
    if target["free_bytes"] < reserve + size + 64*MIB:
        raise ValueError("Insufficient space for a quick measurement")
    jobs = [dict(name="seq_write", rw="write", bs=MIB, qd=1, io_bytes=size,
                 runtime=30, require_complete=True),
            dict(name="seq_read", rw="read", bs=MIB, qd=1, io_bytes=size,
                 runtime=30, require_complete=True),
            dict(name="rand_read", rw="randread", bs=4096, qd=1, io_bytes=16*MIB, runtime=5),
            dict(name="rand_write", rw="randwrite", bs=4096, qd=1, io_bytes=16*MIB, runtime=5)]
    return dict(profile="quick", method_version=QUICK_METHOD, file_bytes=size,
                write_budget=size+16*MIB, max_write_bytes=size+16*MIB, reserve_bytes=reserve,
                metadata_headroom_bytes=64*MIB, max_runtime_seconds=90,
                total_timeout_seconds=90, jobs=jobs)


def diagnostic_plan(profile, target, file_bytes=None, write_budget=None, seconds=30,
                    duration=1800):
    """Opt-in file-based diagnostics. Existing quick/full/cache defaults stay unchanged."""
    reserve = max(10 * GIB, int(target["total_bytes"] * 0.05))
    available = max(0, target["free_bytes"] - reserve - 64 * MIB) // MIB * MIB
    size = file_bytes or (256 if profile == "mapping" else 64) * GIB
    if size < MIB or size % MIB or size > available:
        raise ValueError("Diagnostic file must fit available space after the safety reserve")
    if seconds <= 0 or duration <= 0 or (write_budget is not None and write_budget < size):
        raise ValueError("Positive durations and a write budget covering the complete file are required")
    jobs = [dict(name="prepare", rw="write", bs=MIB, qd=16, io_bytes=size,
                 runtime=duration, require_complete=True)]
    if profile == "integrity":
        jobs[0].update(name="integrity_write", verify=True)
        jobs.append(dict(name="integrity_read", rw="read", bs=MIB, qd=16,
                         io_bytes=size, runtime=duration, verify=True, require_complete=True))
    elif profile == "mapping":
        ranges = sorted(set([min(size, n * GIB) for n in (8, 64, 256)] + [size]))
        for region in ranges:
            for qd in (1, 16):
                for repeat in (1, 2):
                    jobs.append(dict(name=f"mapping_{region}_{qd}_{repeat}", rw="randread",
                                     bs=4096, qd=qd, io_bytes=min(region, GIB),
                                     runtime=seconds, region_bytes=region, repeat=repeat,
                                     random_seed=2026, label_key="mapping_read"))
    else:
        raise ValueError("Unknown diagnostic profile")
    return dict(profile=profile, file_bytes=size, write_budget=size, seconds=seconds,
                sustained=duration, jobs=jobs, reserve_bytes=reserve,
                max_write_bytes=size, max_runtime_seconds=sum(j["runtime"] + 45 for j in jobs),
                available_bytes=available)


def add_recovery(config, idle_seconds):
    if idle_seconds <= 0:
        raise ValueError("Recovery idle time must be positive")
    # Reuse the existing file; this extra write is explicitly included in the preview budget.
    size = min(config["file_bytes"], 8 * GIB)
    config["jobs"].append(dict(name="recovery_write", rw="write", bs=MIB, qd=16,
                               io_bytes=size, runtime=30, idle_seconds=idle_seconds))
    config["max_write_bytes"] += size
    config["write_budget"] += size
    config["max_runtime_seconds"] += idle_seconds + 75


def job_label(job, text):
    if job.get("label_key") == "mapping_read":
        return text["mapping_read"].format(gib=round(job["region_bytes"] / GIB, 2),
                                          qd=job["qd"], repeat=job["repeat"])
    return text.get(job["name"], job["name"])


def fio_arguments(fio, job, file_bytes, engine, raw, log):
    args = [
        fio,
        f"--name={job['name']}",
        "--filename=payload.bin",
        f"--ioengine={engine}",
        "--thread=1",
        "--numjobs=1",
        f"--rw={job['rw']}",
        f"--bs={job['bs']}",
        f"--iodepth={job['qd']}",
        f"--size={job.get('region_bytes', file_bytes)}",
        f"--io_size={job['io_bytes']}",
        f"--runtime={job['runtime']}",
        "--direct=1",
        "--fallocate=none",
        "--refill_buffers=1",
        "--randrepeat=0",
        "--end_fsync=1",
        "--group_reporting=1",
        "--output-format=json",
        f"--status-interval={job.get('status_interval', 1)}",
        f"--output={raw}",
        f"--write_bw_log={log}",
        f"--log_avg_msec={job.get('status_interval', 1) * 1000}",
        "--log_unix_epoch=0",
        "--log_offset=0",
        "--eta=never",
        "--exitall_on_error=1",
    ]
    if "random_seed" in job:
        args.remove("--randrepeat=0")
        args.extend(["--randrepeat=1", f"--randseed={job['random_seed']}"])
    if job.get("verify"):
        args.extend(["--verify=crc32c", "--verify_fatal=1", "--verify_dump=0",
                     "--do_verify=0", "--verify_state_save=0"])
        if job["rw"] == "read":
            args.extend(["--verify_write_sequence=0", "--verify_header_seed=0"])
    if job["rw"] == "randrw":
        args.append("--rwmixread=70")
    if job["rw"] in ("read", "randread"):
        args.extend(["--readonly", "--allow_file_create=0"])
    return args


def json_objects(text, strict=False, allow_signal_notice=False):
    """fio --status-interval emits concatenated JSON objects, including a final snapshot."""
    decoder, position, objects = json.JSONDecoder(), 0, []
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        # fio writes this exact line between JSON snapshots on a controlled SIGINT.
        notice = "fio: terminating on signal 2\n"
        if allow_signal_notice and text.startswith(notice, position):
            position += len(notice)
            continue
        try:
            item, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            if strict:
                raise ValueError("Incomplete or malformed final fio JSON stream")
            break  # Partial trailing object will be reconsidered on the next snapshot.
        if isinstance(item, dict):
            objects.append(item)
    return objects


def metrics(snapshot):
    jobs = snapshot.get("jobs", [])
    if len(jobs) != 1 or jobs[0].get("error", 0):
        raise ValueError("Missing job or fio I/O error")
    job, result = jobs[0], {}
    runtimes = [job.get("job_runtime"), *(job.get(d, {}).get("runtime") for d in ("read", "write"))]
    if any(value is not None and (type(value) is not int or not 0 <= value <= 2**64 - 1) for value in runtimes):
        raise ValueError("fio runtime counters must be nonnegative uint64 milliseconds")
    for direction in ("read", "write"):
        item = job.get(direction, {})
        count = item.get("io_bytes") if "io_bytes" in item else item.get("io_kbytes", 0)
        if type(count) is not int or not 0 <= count <= 2**64 - 1:
            raise ValueError("fio byte counters must be nonnegative uint64 integers")
        count *= 1 if "io_bytes" in item else 1024
        if count > 2**64 - 1:
            raise ValueError("fio byte counter overflow")
        latency = item.get("clat_ns", {})
        percentiles = latency.get("percentile", {})
        numeric = [item.get("bw_bytes", item.get("bw", 0)), item.get("iops", 0),
                   latency.get("mean"), *(percentiles.get(f"{float(p):.6f}") for p in ("50", "95", "99"))]
        if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0) for value in numeric):
            raise ValueError("Invalid fio throughput or completion latency")
        result[direction] = dict(
            bytes=count,
            runtime_ms=item.get("runtime") if item.get("runtime") is not None else (runtimes[0] or 0),
            mb_s=item.get("bw_bytes", item.get("bw", 0) * 1024) / 1_000_000,
            iops=item.get("iops", 0),
            latency_us={
                p: percentiles.get(f"{float(p):.6f}", None) / 1000
                if percentiles.get(f"{float(p):.6f}") is not None
                else None
                for p in ("50", "95", "99")
            },
        )
        result[direction]["latency_us"]["mean"] = (
            latency["mean"] / 1000 if latency.get("mean") is not None else None
        )
    result["achieved_depth"] = job.get("iodepth_level", {})
    result["runtime_ms"] = runtimes[0] or max(value or 0 for value in runtimes[1:])
    return result


def bandwidth_series(path, strict=False):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(",")
        valid = False
        if len(parts) >= 3:
            try:
                t, kib, direction = float(parts[0]), float(parts[1]), int(parts[2])
                if direction in (0, 1) and all(math.isfinite(v) and v >= 0 for v in (t, kib)):
                    rows.append(
                        dict(
                            seconds=t / 1000,
                            mb_s=kib * 1024 / 1_000_000,
                            direction="read" if direction == 0 else "write",
                        )
                    )
                    valid = True
            except ValueError:
                pass
        if strict and line.strip() and not valid:
            raise ValueError("Malformed fio bandwidth sample")
    return rows


def active_runtime_ms(data, rw):
    """Elapsed time evidenced by valid active I/O, without changing raw metrics."""
    if not isinstance(data, dict):
        return 0
    total_runtime = data.get("runtime_ms")
    if type(total_runtime) is not int or not 0 < total_runtime <= 2**64 - 1:
        return 0
    if rw == "randrw":
        directions = ("read", "write")
    elif rw in ("read", "randread"):
        directions = ("read",)
    elif rw in ("write", "randwrite"):
        directions = ("write",)
    else:
        return 0
    durations = []
    for direction in directions:
        item = data.get(direction)
        if not isinstance(item, dict):
            return 0
        count = item.get("bytes")
        runtime = item.get("runtime_ms", total_runtime)
        if type(count) is not int or not 0 < count <= 2**64 - 1:
            return 0
        if type(runtime) is not int or not 0 < runtime <= 2**64 - 1:
            return 0
        durations.append(runtime)
    return max(durations)


def collection_status(case):
    """Separate broken collection from valid logs with limited time resolution."""
    data = case.get("metrics", {})
    if not isinstance(data, dict):
        return "collection_incomplete"
    runtime_ms = data.get("runtime_ms", 0)
    if type(runtime_ms) is not int or runtime_ms <= 0:
        return "collection_incomplete"
    active = ("read", "write") if case.get("rw") == "randrw" else (
        "read" if case.get("rw") in ("read", "randread") else "write",
    )
    if case.get("collection_error"):
        return "collection_incomplete"
    series = case.get("series", [])
    if not isinstance(series, list) or any(
        not isinstance(p, dict) or p.get("direction") not in active for p in series
    ):
        return "collection_incomplete"
    # Nominal cadence is a resolution description, not a fio timer deadline.
    # See fio-3.43 stat.c add_log_sample/__add_samples: actual elapsed time is
    # recorded and delayed samples can still contain accumulated I/O.
    interval_seconds, timer_tolerance = 1.0, .05
    max_interval = interval_seconds + timer_tolerance
    # Only float roundoff, far below fio's millisecond timestamp precision.
    comparison_epsilon = 1e-9
    elapsed = case.get("elapsed_seconds")
    if "elapsed_seconds" in case and (
        type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0
    ):
        return "collection_incomplete"
    irregular = False
    durations = []
    for direction in active:
        if not isinstance(data.get(direction), dict):
            return "collection_incomplete"
        active_runtime = data[direction].get("runtime_ms", runtime_ms)
        if type(active_runtime) is not int or active_runtime <= 0:
            return "collection_incomplete"
        direction_seconds = active_runtime / 1000
        durations.append(direction_seconds)
        points = [p for p in series if p.get("direction") == direction]
        times = [p.get("seconds") for p in points]
        if any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in times):
            return "collection_incomplete"
        if any(type(p.get("mb_s")) not in (int, float) or not math.isfinite(p["mb_s"]) or p["mb_s"] < 0 for p in points):
            return "collection_incomplete"
        # The wrapper starts before fio's epoch and ends after the child exits.
        # Its rounded monotonic duration provides an outer execution envelope.
        if elapsed is not None and direction_seconds > elapsed + .001 + comparison_epsilon:
            return "collection_incomplete"
        upper = elapsed + .001 if elapsed is not None else direction_seconds + timer_tolerance
        if times and times[-1] > upper + comparison_epsilon:
            return "collection_incomplete"
        if not times:
            if direction_seconds > max_interval + comparison_epsilon:
                return "collection_incomplete"
            continue
        if times[0] > max_interval + comparison_epsilon or direction_seconds - times[-1] > max_interval + comparison_epsilon:
            irregular = True
        if any(b <= a for a, b in zip(times, times[1:])):
            return "collection_incomplete"
        if any(b - a > max_interval + comparison_epsilon for a, b in zip(times, times[1:])):
            irregular = True
    if irregular:
        return "collected_irregular"
    return "short_window" if max(durations) < interval_seconds or not series else "collected"


def write_counter_samples(objects):
    """Extract cumulative fio status counters; reject resets and ambiguous times."""
    if not isinstance(objects, list) or len(objects) > 20000:
        raise ValueError('Invalid fio status snapshot count')
    result = []
    for item in objects:
        data = metrics(item)
        runtime = data['write']['runtime_ms']
        count = data['write']['bytes']
        if runtime == 0 and count == 0:
            continue
        if runtime <= 0:
            raise ValueError('Invalid write status runtime')
        sample = {'runtime_ms': runtime, 'bytes': count}
        if result and sample == result[-1]:
            continue
        if result and (runtime <= result[-1]['runtime_ms'] or count < result[-1]['bytes']):
            raise ValueError('Fio status counters reset or overlap')
        result.append(sample)
    return result


def cache_observation(case):
    """Return cache series and qualification; old results keep their old source."""
    counters = case.get('cache_write_counters_v1')
    if counters is None:
        return case.get('series', []), collection_status(case)
    data = case.get('metrics', {})
    write = data.get('write', {}) if isinstance(data, dict) else {}
    if (not isinstance(counters, list) or not counters or len(counters) > 20000
            or not isinstance(write, dict)):
        return [], 'collection_incomplete'
    rows, last_t, last_b = [], 0, 0
    for item in counters:
        if not isinstance(item, dict):
            return [], 'collection_incomplete'
        t, b = item.get('runtime_ms'), item.get('bytes')
        if (type(t) is not int or type(b) is not int or t <= last_t or b < last_b
                or b > 2**64 - 1 or t > 2**64 - 1):
            return [], 'collection_incomplete'
        rows.append({'seconds': t / 1000, 'direction': 'write',
                     'mb_s': (b - last_b) / (t - last_t) / 1000})
        last_t, last_b = t, b
    if last_t != write.get('runtime_ms') or last_b != write.get('bytes'):
        return [], 'collection_incomplete'
    candidate = dict(case, series=rows)
    return rows, collection_status(candidate)


def case_core_missing(case, job, config):
    """Qualification shared by the runner and offline public projection."""
    name, missing = job["name"], []
    if any(case.get(k) != job[k] for k in ("rw", "bs", "qd")):
        missing.append(f"{name}.workload")
    if case.get("status") != "completed":
        missing.append(f"{name}.status")
    data = case.get("metrics", {})
    if not isinstance(data, dict):
        return [f"{name}.metrics"]
    if any(not isinstance(data.get(direction, {}), dict) for direction in ("read", "write")):
        return [f"{name}.directions"]
    if type(data.get("runtime_ms")) is not int or data["runtime_ms"] <= 0:
        missing.append(f"{name}.runtime_ms")
    if collection_status(case) == "collection_incomplete":
        missing.append(f"{name}.collection")
    directions = ("read", "write") if job["rw"] == "randrw" else (
        "read" if job["rw"] in ("read", "randread") else "write",
    )
    for direction in directions:
        item = data.get(direction, {})
        if not isinstance(item, dict):
            missing.append(f"{name}.{direction}")
            continue
        if not isinstance(item.get("latency_us", {}), dict):
            missing.append(f"{name}.{direction}.latency_us")
            continue
        if type(item.get("bytes")) is not int or item["bytes"] <= 0:
            missing.append(f"{name}.{direction}.bytes")
        for field, value in [(k, item.get(k)) for k in ("mb_s", "iops")] + [
            (f"latency_us.{k}", item.get("latency_us", {}).get(k)) for k in ("mean", "50", "95", "99")
        ]:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (field in ("mb_s", "iops") and not value):
                missing.append(f"{name}.{direction}.{field}")
    if name == "sustained":
        written = data.get("write", {}).get("bytes", 0)
        if type(written) is not int:
            written = 0
        if written < config.get("file_bytes", 0):
            missing.append("sustained.initialization")
        if config.get("method_version") in (AUTO_METHOD, PEAK_METHOD):
            points, timing = cache_observation(case)
            assessed = cache_assessment(points, sampling_status=timing)
            speed = assessed.get("post_cache_mb_s")
            if (timing != "collected" or assessed["status"] != "observed_drop_stable"
                    or type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0):
                missing.append("sustained.post_cache_stability")
        if written < job["io_bytes"] and not (
            case.get("stop_reason") == "post_drop_stable"
            and cache_assessment(cache_observation(case)[0], sampling_status=cache_observation(case)[1]
                                 if config.get("method_version") in (*AUTO_METHODS, PEAK_METHOD) else None)["status"] == "observed_drop_stable"
        ):
            missing.append("sustained.endpoint")
    transferred = sum(data.get(d, {}).get("bytes", 0) for d in ("read", "write")
                      if type(data.get(d, {}).get("bytes", 0)) is int)
    if transferred > job["io_bytes"]:
        missing.append(f"{name}.io_bound")
    if name != "sustained" and transferred < job["io_bytes"] and ((job.get("require_complete") and config.get("method_version") == QUICK_METHOD) or active_runtime_ms(data, job["rw"]) < job["runtime"] * 1000):
        missing.append(f"{name}.endpoint")
    return missing


def peak_completeness(result):
    """Qualify the base workload and peak evidence against the frozen plan."""
    from peak_analysis import assess_peak_scan

    plan = result.get("plan", {})
    jobs = plan.get("jobs", [])
    cases = result.get("cases", [])
    expected = {job["name"]: job for job in jobs}
    observed = {case["name"]: case for case in cases}
    missing = []
    base_names = {"prepare", "seq_read", "seq_write", "seq_read_q", "seq_write_q",
                  "rand_read", "rand_write", "rand_read_q", "rand_write_q",
                  "mixed", "sustained_read", "sustained"}
    scan_names = {f"peak_{workload}_{depth}_{repeat}"
                  for workload in ("seq_read", "seq_write", "rand_read", "rand_write")
                  for depth in (16, 32, 64) for repeat in (1, 2)}
    if (plan.get("method_version") != PEAK_METHOD
            or result.get("tool", {}).get("method_version") != PEAK_METHOD
            or len(jobs) != len(expected) or set(expected) != base_names | scan_names
            or len(cases) != len(observed)):
        missing.append("plan.jobs")
    old_criteria = {"minimum_seconds": 5, "tolerance": 0.05, "minimum_depth_percent": 25}
    new_criteria = {"minimum_seconds": 5, "tolerance": 0.05, "minimum_depth_percent": 20,
                    "minimum_half_depth_percent": 90}
    criteria = plan.get("peak_criteria")
    if criteria not in (old_criteria, new_criteria):
        missing.append("plan.peak_criteria")
        criteria = old_criteria
    specs = {
        "prepare": ("write", MIB, 16), "seq_read": ("read", MIB, 1),
        "seq_write": ("write", MIB, 1), "seq_read_q": ("read", MIB, 16),
        "seq_write_q": ("write", MIB, 16), "rand_read": ("randread", 4096, 1),
        "rand_write": ("randwrite", 4096, 1), "rand_read_q": ("randread", 4096, 16),
        "rand_write_q": ("randwrite", 4096, 16), "mixed": ("randrw", 4096, 16),
        "sustained_read": ("read", MIB, 16), "sustained": ("write", MIB, 16),
    }
    for workload in ("seq_read", "seq_write", "rand_read", "rand_write"):
        direction, block, _ = specs[workload]
        for depth in (16, 32, 64):
            for repeat in (1, 2):
                specs[f"peak_{workload}_{depth}_{repeat}"] = (direction, block, depth)
    for name, job in expected.items():
        if name not in specs or (job.get("rw"), job.get("bs"), job.get("qd")) != specs[name]:
            missing.append(name + ".plan_workload")
    write_sizes = [job.get("io_bytes") for job in jobs
                   if job.get("rw") not in ("read", "randread")]
    if (any(type(size) is not int or size <= 0 for size in write_sizes)
            or type(plan.get("max_write_bytes")) is not int
            or type(plan.get("write_budget")) is not int
            or sum(size for size in write_sizes if type(size) is int) != plan.get("max_write_bytes")
            or plan.get("max_write_bytes", 0) > plan.get("write_budget", 0)):
        missing.append("plan.write_budget")
    for name in base_names:
        if name not in observed or name not in expected:
            missing.append(name + ".missing")
            continue
        missing.extend(case_core_missing(observed[name], expected[name], plan))
        if name in base_names - {"prepare", "mixed", "sustained_read", "sustained"}:
            case = observed[name]
            if (active_runtime_ms(case.get("metrics", {}), case.get("rw")) < 5000
                    or collection_status(case) not in ("collected", "collected_irregular")):
                missing.append(name + ".sampling")
    for name in scan_names:
        if name not in observed or name not in expected:
            missing.append(name + ".missing")
            continue
        case = observed[name]
        job = expected[name]
        if case.get("status") == "skipped" and case.get("reason") == "peak_plateau":
            from peak_analysis import assess_peak_pair
            workload = job.get("peak_workload")
            names = [f"peak_{workload}_{depth}_{repeat}"
                     for depth in (16, 32) for repeat in (1, 2)]
            if (job.get("qd") == 64 and all(key in observed for key in names)
                    and assess_peak_pair([observed[key] for key in names],
                                         **criteria)["status"] == "plateau"
                    and all(case.get(key) == job[key] for key in ("rw", "bs", "qd"))):
                continue
        missing.extend(case_core_missing(case, job, plan))
    assessment = assess_peak_scan(cases, **criteria)
    if assessment["status"] != "complete":
        missing.append("peak.plateau")
    return {"complete": not missing, "missing_core": sorted(set(missing))}


def core_completeness(result):
    """Derive the new method's core qualification from measurements, never a flag."""
    config = result.get("plan", {})
    expected = {j["name"]: j for j in config.get("jobs", [])}
    missing, seen = [], set()
    if (result.get("tool", {}).get("method_version") in (AUTO_METHOD, *QUICK_METHODS)
            and config.get("method_version") != result["tool"]["method_version"]):
        missing.append("plan.method_version")
    quick = config.get("method_version") in QUICK_METHODS
    quick_jobs = {"seq_write", "seq_read"}
    if config.get("method_version") == QUICK_METHOD:
        quick_jobs |= {"rand_read", "rand_write"}
    if quick and (set(expected) != quick_jobs or config.get("profile") != "quick"):
        missing.append("plan.jobs")
    if not quick and len(expected) != 11:
        missing.append("plan.jobs")
    for case in result.get("cases", []):
        name = case.get("name")
        if name not in expected or name in seen:
            missing.append(f"{name}.identity")
            continue
        seen.add(name)
        missing.extend(case_core_missing(case, expected[name], config))
    missing.extend(f"{name}.missing" for name in expected.keys() - seen)
    return dict(complete=not missing, missing_core=sorted(missing))


def sustained_summary(points, sampling_status=None):
    """Transparent descriptive statistics, never claim SLC exhaustion from bandwidth alone."""
    samples = sorted(
        (p for p in points if p["direction"] == "write"), key=lambda x: x["seconds"]
    )
    out = dict(
        cache_exhaustion="unconfirmed",
        samples=len(samples),
        initial_mb_s=None,
        tail_mean_mb_s=None,
        tail_median_mb_s=None,
        minimum_5s_mb_s=None,
        drop_percent=None,
        change_seconds=None,
        bytes_at_change=None,
        after_drop_mean_mb_s=None,
        covered_seconds=None,
    )
    if not samples:
        return out
    if sampling_status not in (None, "collected"):
        out["sampling_gaps"] = True
        end = samples[-1].get("seconds")
        if type(end) in (int, float) and math.isfinite(end) and end >= 0:
            out["covered_seconds"] = end
        return out
    end = samples[-1]["seconds"]
    out["covered_seconds"] = end
    # Logs are nominally 1-second averages. Weight irregular intervals and disclose gaps.
    intervals, previous, cumulative = [], 0.0, 0.0
    for p in samples:
        dt = p["seconds"] - previous
        if dt <= 0:
            continue
        cumulative += p["mb_s"] * 1_000_000 * dt
        intervals.append(dict(**p, dt=dt, cumulative=cumulative))
        previous = p["seconds"]
    if not intervals:
        return out
    out["sampling_gaps"] = any(p["dt"] > 2 for p in intervals)
    initial = [p for p in intervals if p["seconds"] <= 5]
    tail_start = max(0, end - min(30, end / 3))
    tail = [p for p in intervals if p["seconds"] > tail_start]

    def mean(rows):
        return (
            sum(p["mb_s"] * p["dt"] for p in rows) / sum(p["dt"] for p in rows)
            if rows
            else None
        )

    out["initial_mb_s"], out["tail_mean_mb_s"] = mean(initial), mean(tail)
    out["tail_median_mb_s"] = statistics.median(p["mb_s"] for p in tail)
    windows = []
    for p in intervals:
        window = [
            q for q in intervals if p["seconds"] - 5 < q["seconds"] <= p["seconds"]
        ]
        if (
            len(window) >= 5
            and sum(q["dt"] for q in window) >= 4.5
            and not any(q["dt"] > 2 for q in window)
        ):
            windows.append(mean(window))
    out["minimum_5s_mb_s"] = min(windows) if windows else None
    if sampling_status is not None and end < 5:
        # New-method fixed five-second values require five observed seconds.
        # Keep the legacy descriptive calculation unchanged by default.
        out["initial_mb_s"] = None
        out["minimum_5s_mb_s"] = None
    first = out["initial_mb_s"]
    if first and end >= 20 and not out["sampling_gaps"]:
        out["drop_percent"] = (1 - out["tail_mean_mb_s"] / first) * 100
        for i, p in enumerate(intervals):
            after = intervals[i : i + 10]
            if (
                p["seconds"] > 5
                and len(after) == 10
                and all(q["mb_s"] < first * 0.7 for q in after)
            ):
                out.update(
                    change_seconds=p["seconds"],
                    bytes_at_change=round(p["cumulative"]),
                    after_drop_mean_mb_s=mean(intervals[i:]),
                )
                break
    return out


def cache_assessment(points, sampling_status=None):
    """Infer an available write-cache boundary only after a sustained lower plateau.

    Bandwidth alone cannot prove the cause; the estimate describes this run, not
    physical SLC capacity. A 30-second settling period precedes the 60-second window.
    """
    out = dict(
        status="insufficient_samples",
        cache_capacity_estimate_bytes=None,
        post_cache_mb_s=None,
        change_seconds=None,
        bytes_before_drop=None,
        post_drop_seconds=0,
        stable_window_seconds=60,
        cause_confirmed=False,
    )
    if sampling_status not in (None, "collected"):
        if sampling_status != "short_window":
            out["status"] = "sampling_gaps"
        # Resolution qualification and curve shape answer different questions.
        # Retain only the diagnostic status: never promote its cache numbers or
        # use it to authorize early termination / a complete result.
        if sampling_status == "collected_irregular":
            out["curve_diagnostic"] = cache_assessment(points)["status"]
        return out
    samples = sorted(
        (p for p in points if p.get("direction") == "write"), key=lambda p: p["seconds"]
    )
    rows, previous, cumulative = [], 0.0, 0.0
    for p in samples:
        end, speed = p["seconds"], p["mb_s"]
        if not all(
            isinstance(x, (int, float)) and math.isfinite(x) for x in (end, speed)
        ):
            out["status"] = "sampling_gaps"
            return out
        dt = end - previous
        if dt <= 0 or dt > 2 or speed < 0:
            out["status"] = "sampling_gaps"
            return out
        rows.append(
            dict(start=previous, end=end, dt=dt, speed=speed, before=cumulative)
        )
        cumulative += speed * 1_000_000 * dt
        previous = end
    baseline = [r for r in rows if r["start"] < 5]
    baseline_weights = [min(r["end"], 5) - r["start"] for r in baseline]
    baseline_duration = sum(baseline_weights)
    if baseline_duration < 4.5:
        return out
    first = (
        sum(r["speed"] * weight for r, weight in zip(baseline, baseline_weights))
        / baseline_duration
    )
    if first <= 0 or max(abs(r["speed"] - first) for r in baseline) > first * 0.1:
        out["status"] = "unstable_baseline"
        return out
    drop = None
    for i, row in enumerate(rows):
        following = rows[i : i + 10]
        if (
            row["start"] >= 5
            and len(following) == 10
            and sum(r["dt"] for r in following) >= 9.5
            and all(r["speed"] < first * 0.7 for r in following)
        ):
            drop = row
            break
    if drop is None:
        out["status"] = "no_drop_observed"
        return out
    out.update(
        status="post_drop_incomplete",
        change_seconds=drop["start"],
        bytes_before_drop=round(drop["before"]),
        post_drop_seconds=rows[-1]["end"] - drop["start"],
    )
    window_start = rows[-1]["end"] - 60
    if window_start < drop["start"] + 30:
        return out
    window = [r for r in rows if r["end"] > window_start]
    weights = [r["end"] - max(r["start"], window_start) for r in window]
    duration = sum(weights)
    mean = sum(r["speed"] * w for r, w in zip(window, weights)) / duration
    variance = (
        sum(w * (r["speed"] - mean) ** 2 for r, w in zip(window, weights)) / duration
    )
    midpoint = [(max(r["start"], window_start) + r["end"]) / 2 for r in window]
    center = sum(x * w for x, w in zip(midpoint, weights)) / duration
    denominator = sum(w * (x - center) ** 2 for x, w in zip(midpoint, weights))
    slope = (
        sum(
            w * (x - center) * (r["speed"] - mean)
            for r, x, w in zip(window, midpoint, weights)
        )
        / denominator
        if denominator
        else float("inf")
    )
    if (
        duration < 59.5
        or mean <= 0
        or math.sqrt(variance) > mean * 0.1
        or abs(slope) * duration > mean * 0.1
        or any(r["speed"] >= first * 0.7 for r in window)
    ):
        out["status"] = "post_drop_unstable"
        return out
    out.update(
        status="observed_drop_stable",
        cache_capacity_estimate_bytes=round(drop["before"]),
        post_cache_mb_s=mean,
        stable_window_start=window_start,
        stable_window_end=rows[-1]["end"],
    )
    return out
