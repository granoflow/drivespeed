"""Finite fio plans, JSON stream parsing and sustained-write measurements."""

import json
import math
import statistics

GIB = 1_073_741_824
MIB = 1_048_576
PROFILES = {
    "quick": dict(
        file_bytes=GIB, write_budget=8 * GIB, seconds=10, sustained=30, prepare=300
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
        f"--size={file_bytes}",
        f"--io_size={job['io_bytes']}",
        f"--runtime={job['runtime']}",
        "--direct=1",
        "--fallocate=none",
        "--refill_buffers=1",
        "--randrepeat=0",
        "--end_fsync=1",
        "--group_reporting=1",
        "--output-format=json",
        "--status-interval=1",
        f"--output={raw}",
        f"--write_bw_log={log}",
        "--log_avg_msec=1000",
        "--log_unix_epoch=0",
        "--log_offset=0",
        "--eta=never",
        "--exitall_on_error=1",
    ]
    if job["rw"] == "randrw":
        args.append("--rwmixread=70")
    if job["rw"] in ("read", "randread"):
        args.extend(["--readonly", "--allow_file_create=0"])
    return args


def json_objects(text, strict=False):
    """fio --status-interval emits concatenated JSON objects, including a final snapshot."""
    decoder, position, objects = json.JSONDecoder(), 0, []
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
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
    for direction in ("read", "write"):
        item = job.get(direction, {})
        latency = item.get("clat_ns", {})
        percentiles = latency.get("percentile", {})
        result[direction] = dict(
            bytes=item.get("io_bytes", item.get("io_kbytes", 0) * 1024),
            mb_s=item.get("bw_bytes", item.get("bw", 0) * 1024) / 1_000_000,
            iops=item.get("iops", 0),
            latency_us={
                p: percentiles.get(f"{float(p):.6f}", None) / 1000
                if percentiles.get(f"{float(p):.6f}") is not None
                else None
                for p in ("50", "95", "99")
            },
        )
    result["achieved_depth"] = job.get("iodepth_level", {})
    result["runtime_ms"] = job.get("job_runtime") or max(
        job.get("read", {}).get("runtime", 0), job.get("write", {}).get("runtime", 0)
    )
    return result


def bandwidth_series(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(",")
        if len(parts) >= 3:
            try:
                t, kib, direction = float(parts[0]), float(parts[1]), int(parts[2])
                if all(math.isfinite(v) and v >= 0 for v in (t, kib)):
                    rows.append(
                        dict(
                            seconds=t / 1000,
                            mb_s=kib * 1024 / 1_000_000,
                            direction="read" if direction == 0 else "write",
                        )
                    )
            except ValueError:
                continue
    return rows


def sustained_summary(points):
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
