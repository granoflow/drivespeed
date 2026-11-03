"""Qualification of repeated fio queue-depth measurements."""

import math

from workloads import MIB, active_runtime_ms, collection_status


def qualified_depth(distribution, depth, minimum_depth_percent=25,
                    minimum_half_depth_percent=None):
    """Apply the plan's I/O-count histogram policy, not a time-depth claim."""
    key = '>=64' if depth == 64 else str(depth)
    value = distribution.get(key)
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum_depth_percent <= value <= 100):
        return False
    if minimum_half_depth_percent is None:
        return True
    if depth not in (16, 32, 64):
        return False
    keys = [str(depth // 2), key]
    # A bucket covers its lower bound up to the next power of two.
    keys += [str(d) for d in (2, 4, 8, 16, 32) if d > depth]
    if depth < 64:
        keys.append('>=64')
    values = [distribution.get(k) for k in keys]
    return (all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 100 for v in values)
            and minimum_half_depth_percent <= sum(values) <= 100.5)


def assess_peak_pair(cases, minimum_seconds=5, tolerance=0.05,
                     minimum_depth_percent=25, minimum_half_depth_percent=None):
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
        if (case.get("status") != "completed"
                or collection_status(case) not in ("collected", "collected_irregular")):
            return result
        metrics = case.get("metrics", {})
        if not isinstance(metrics, dict):
            return result
        item = metrics.get(direction, {})
        depth_distribution = metrics.get("achieved_depth", {})
        if not isinstance(item, dict) or not isinstance(depth_distribution, dict):
            return result
        rate = item.get("mb_s")
        if (active_runtime_ms(metrics, signature[0]) < minimum_seconds * 1000
                or type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
                or not qualified_depth(depth_distribution, case['qd'], minimum_depth_percent,
                                       minimum_half_depth_percent)):
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

        lower_pair = pair(16, 32)
        upper_pair = pair(32, 64)
        result = upper_pair if lower_pair["status"] == "increase" else lower_pair
        best = []
        for depth in (16, 32, 64):
            candidates = []
            for repeat in (1, 2):
                case = by_name.get(f"peak_{workload}_{depth}_{repeat}")
                if not isinstance(case, dict) or case.get("status") != "completed":
                    continue
                expected_rw = {"seq_read": "read", "seq_write": "write",
                               "rand_read": "randread", "rand_write": "randwrite"}[workload]
                expected_bs = MIB if workload.startswith("seq_") else 4096
                if (case.get("rw"), case.get("bs"), case.get("qd")) != (expected_rw, expected_bs, depth):
                    continue
                data = case.get("metrics")
                if not isinstance(data, dict):
                    continue
                direction = "read" if workload.endswith("read") else "write"
                measurement = data.get(direction)
                depths = data.get("achieved_depth")
                if not isinstance(measurement, dict) or not isinstance(depths, dict):
                    continue
                rate = measurement.get("mb_s")
                if (collection_status(case) not in ("collected", "collected_irregular")
                        or active_runtime_ms(data, case.get("rw")) < criteria.get("minimum_seconds", 5) * 1000
                        or type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
                        or not qualified_depth(depths, depth, criteria.get('minimum_depth_percent', 25),
                                               criteria.get('minimum_half_depth_percent'))):
                    continue
                candidates.append({"mb_s": rate, "name": case["name"],
                                   "collection_status": collection_status(case)})
            if candidates:
                best.append({"qd": depth, **max(candidates, key=lambda item: item["mb_s"])})
        # These pair diagnostics inform a later plan. They do not replace the
        # original versioned scan rule or make an incomplete scan complete.
        results[workload] = {**result, "highest_single_by_qd": best,
                             "adjacent_pairs": {"16_32": lower_pair, "32_64": upper_pair}}
    status = "complete" if all(item["status"] == "plateau"
                               for item in results.values()) else "incomplete"
    return {"status": status, "workloads": results}


def qd16_criteria(result):
    """Select only an explicitly frozen, recognized new depth policy."""
    criteria = result.get('plan', {}).get('peak_criteria')
    if criteria == {'minimum_seconds': 5, 'tolerance': 0.05, 'minimum_depth_percent': 20,
                    'minimum_half_depth_percent': 90}:
        return {'minimum_depth_percent': 20, 'minimum_half_depth_percent': 90}
    return {}


def select_qd16_case(cases, workload, minimum_seconds=5, minimum_depth_percent=25,
                     minimum_half_depth_percent=None):
    """Return the highest qualified QD16 case from the same run and workload."""
    specs = {"seq_read": ("read", MIB), "seq_write": ("write", MIB),
             "rand_read": ("randread", 4096), "rand_write": ("randwrite", 4096)}
    if workload not in specs:
        raise ValueError("Unknown workload")
    rw, block = specs[workload]
    direction = "read" if rw in ("read", "randread") else "write"
    names = {workload + "_q", f"peak_{workload}_16_1", f"peak_{workload}_16_2"}
    candidates = []
    for case in cases:
        if (case.get("name") not in names or case.get("status") != "completed"
                or (case.get("rw"), case.get("bs"), case.get("qd")) != (rw, block, 16)):
            continue
        data = case.get("metrics")
        if not isinstance(data, dict):
            continue
        measure = data.get(direction)
        depths = data.get("achieved_depth")
        if not isinstance(measure, dict) or not isinstance(depths, dict):
            continue
        rate = measure.get("mb_s")
        if (collection_status(case) not in ("collected", "collected_irregular")
                or active_runtime_ms(data, rw) < minimum_seconds * 1000
                or type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0
                or not qualified_depth(depths, 16, minimum_depth_percent, minimum_half_depth_percent)):
            continue
        candidates.append(case)
    return max(candidates, key=lambda case: case["metrics"][direction]["mb_s"], default=None)
