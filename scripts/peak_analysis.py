"""Qualification of repeated fio queue-depth measurements."""

import math

from workloads import active_runtime_ms, collection_status


def assess_peak_pair(cases, minimum_seconds=5, tolerance=0.05,
                     minimum_depth_percent=25):
    """Compare two repeats at each of two depths under the same workload.

    fio depth buckets cover ranges of powers of two. A result is qualified
    only when at least the specified percentage is in the requested bucket.
    The thresholds are policy choices, not claims about device saturation.
    """
    result = {"status": "incomplete"}
    if len(cases) != 4 or any(not isinstance(case, dict) for case in cases):
        return result
    first = cases[0]
    signature = (first.get("rw"), first.get("bs"))
    if signature[0] not in ("read", "write", "randread", "randwrite"):
        return result
    if type(signature[1]) is not int or signature[1] <= 0:
        return result
    depths = [case.get("qd") for case in cases]
    if (any(type(depth) is not int for depth in depths)
            or depths[0] < 1 or depths[1] != depths[0]
            or depths[2] <= depths[0] or depths[3] != depths[2]
            or depths[2] > 64
            or any(depth & (depth - 1) for depth in (depths[0], depths[2]))):
        return result
    if any((case.get("rw"), case.get("bs")) != signature for case in cases):
        return result

    direction = "read" if signature[0] in ("read", "randread") else "write"
    rates = []
    for case in cases:
        if case.get("status") != "completed" or collection_status(case) != "collected":
            return result
        metrics = case.get("metrics", {})
        if not isinstance(metrics, dict):
            return result
        item = metrics.get(direction, {})
        depth_distribution = metrics.get("achieved_depth", {})
        if not isinstance(item, dict) or not isinstance(depth_distribution, dict):
            return result
        rate = item.get("mb_s")
        depth_key = ">=64" if case["qd"] == 64 else str(case["qd"])
        achieved = depth_distribution.get(depth_key)
        if (active_runtime_ms(metrics, signature[0]) < minimum_seconds * 1000
                or type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
                or type(achieved) not in (int, float) or not math.isfinite(achieved)
                or not minimum_depth_percent <= achieved <= 100):
            return result
        rates.append(rate)
    means = [(rates[0] + rates[1]) / 2, (rates[2] + rates[3]) / 2]
    if any(abs(a - b) / max(a, b) > tolerance
           for a, b in ((rates[0], rates[1]), (rates[2], rates[3]))):
        status = "unstable"
    elif means[1] > means[0] * (1 + tolerance):
        status = "increase"
    elif means[1] < means[0] * (1 - tolerance):
        status = "decrease"
    else:
        status = "plateau"
    return {"status": status, "depths": [depths[0], depths[2]],
            "mean_mb_s": means, "rates_mb_s": rates}


def assess_peak_scan(cases, **criteria):
    """Assess the four workloads from the frozen QD16/32/64 scan."""
    by_name = {}
    for case in cases:
        name = case.get("name")
        if name in by_name:
            return {"status": "incomplete", "workloads": {}}
        by_name[name] = case
    results = {}
    for workload in ("seq_read", "seq_write", "rand_read", "rand_write"):
        def pair(lower, upper):
            names = [f"peak_{workload}_{depth}_{repeat}"
                     for depth in (lower, upper) for repeat in (1, 2)]
            if any(name not in by_name for name in names):
                return {"status": "incomplete"}
            return assess_peak_pair([by_name[name] for name in names], **criteria)

        result = pair(16, 32)
        if result["status"] == "increase":
            result = pair(32, 64)
        results[workload] = result
    status = "complete" if all(item["status"] == "plateau"
                               for item in results.values()) else "incomplete"
    return {"status": status, "workloads": results}
