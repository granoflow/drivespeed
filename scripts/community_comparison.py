"""Offline comparison facts; no ranking, recommendations, network or JSON cache."""

import math
import re
import statistics
import unicodedata
import uuid

from community_data import CommunityError, digest, normalise_payload, project_result
from agent_usage import agent_usage
from community_store import _now, _value

METRICS = ("mb_s", "iops", "mean", "p50", "p95", "p99")
LIMITATIONS = ("User submitted; not independently verified.",
               "Counts are public records, not independent devices or users.",
               "Temperature, firmware, background load and previous writes are not controlled.",
               "This is a historical reference snapshot; current public eligibility may change.",
               "A performance difference alone cannot establish product quality or authenticity.")
CONDITIONS = (("scope", "storage.scope", "TEXT"), ("capacity_bytes", "storage.capacity_bytes", "INTEGER"),
    ("connection", "storage.connection", "TEXT"), ("negotiated_bps", "storage.negotiated_bps", "INTEGER"),
    ("filesystem", "storage.filesystem", "TEXT"), ("free_percent", "storage.free_percent", "REAL"),
    ("os_family", "environment.os", "TEXT"), ("os_version", "environment.os_version", "TEXT"),
    ("architecture", "environment.architecture", "TEXT"), ("ioengine", "environment.ioengine", "TEXT"),
    ("profile", "plan.profile", "TEXT"), ("file_bytes", "plan.file_bytes", "INTEGER"),
    ("write_budget_bytes", "plan.write_budget_bytes", "INTEGER"),
    ("seconds", "plan.seconds", "INTEGER"), ("sustained_seconds", "plan.sustained_seconds", "INTEGER"),
    ("method_version", "tool.method_version", "TEXT"), ("x_profile_url", "attribution.profile_url", "TEXT"))
WORKLOAD = (("name", "TEXT"), ("rw", "TEXT"), ("bs_bytes", "INTEGER"), ("qd", "INTEGER"), ("numjobs", "INTEGER"),
    ("runtime_limit_seconds", "INTEGER"), ("rwmixread", "INTEGER"), ("status", "TEXT"), ("measurement_seconds", "REAL"))


def schema(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS comparisons(
        id TEXT PRIMARY KEY,report_id TEXT REFERENCES reports(id),run_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,as_of TEXT NOT NULL,fetched_at TEXT NOT NULL,dataset_revision INTEGER NOT NULL,sample_count INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS comparison_metrics(
        comparison_id TEXT REFERENCES comparisons(id) ON DELETE CASCADE,case_index INTEGER,case_name TEXT,direction TEXT,metric TEXT,
        n INTEGER NOT NULL,local_value REAL,median REAL,minimum REAL,maximum REAL,delta_percent REAL,
        PRIMARY KEY(comparison_id,case_index,direction,metric));
      CREATE TABLE IF NOT EXISTS comparison_sources(
        comparison_id TEXT REFERENCES comparisons(id) ON DELETE CASCADE,case_index INTEGER,direction TEXT,sample_id TEXT,
        sample_case INTEGER,url TEXT NOT NULL,tested_on TEXT NOT NULL,computer_model TEXT,controller_model TEXT,drive_model TEXT,
        comparable INTEGER NOT NULL,PRIMARY KEY(comparison_id,case_index,direction,sample_id));
      CREATE TABLE IF NOT EXISTS comparison_differences(
        comparison_id TEXT,case_index INTEGER,direction TEXT,sample_id TEXT,field TEXT,
        PRIMARY KEY(comparison_id,case_index,direction,sample_id,field),
        FOREIGN KEY(comparison_id,case_index,direction,sample_id)
          REFERENCES comparison_sources(comparison_id,case_index,direction,sample_id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS comparison_values(
        comparison_id TEXT,case_index INTEGER,direction TEXT,sample_id TEXT,metric TEXT,value REAL,
        PRIMARY KEY(comparison_id,case_index,direction,sample_id,metric),
        FOREIGN KEY(comparison_id,case_index,direction,sample_id)
          REFERENCES comparison_sources(comparison_id,case_index,direction,sample_id) ON DELETE CASCADE);
    """)
    columns = ",".join(f"{name} {kind}" for name, _, kind in CONDITIONS)
    workload = ",".join(f"case_{name} {kind}" for name, kind in WORKLOAD)
    db.executescript(f"""CREATE TABLE IF NOT EXISTS comparison_source_context(
        comparison_id TEXT,case_index INTEGER,direction TEXT,sample_id TEXT,{columns},{workload},
        PRIMARY KEY(comparison_id,case_index,direction,sample_id),
        FOREIGN KEY(comparison_id,case_index,direction,sample_id)
          REFERENCES comparison_sources(comparison_id,case_index,direction,sample_id) ON DELETE CASCADE);""")
    # Historical snapshots did not record this condition. Preserve NULL rather
    # than rebuilding them from reference samples that may since have changed.
    if "write_budget_bytes" not in {row[1] for row in db.execute("PRAGMA table_info(comparison_source_context)")}:
        db.execute("BEGIN IMMEDIATE")
        try:
            if "write_budget_bytes" not in {row[1] for row in db.execute("PRAGMA table_info(comparison_source_context)")}:
                db.execute("ALTER TABLE comparison_source_context ADD COLUMN write_budget_bytes INTEGER")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _write_budget(value):
    return value if type(value) is int and 1 <= value <= 9_007_199_254_740_991 else None


def _statistics(measured, values):
    median = statistics.median(values) if len(values) >= 3 else None
    delta = (measured/median-1)*100 if measured is not None and median is not None and median > 0 else None
    if delta is not None and not math.isfinite(delta):
        delta = None
    return dict(n=len(values), median=median, minimum=min(values) if len(values) >= 3 else None,
                maximum=max(values) if len(values) >= 3 else None, delta_percent=delta)


def _model(value):
    if value is None:
        return None
    result = " ".join(unicodedata.normalize("NFKC", value).strip().lower().split())
    return None if result in ("", "unknown", "未知", "unavailable", "n/a") else result


def _major(value):
    match = re.match(r"^(\d+)(?:\.|$)", value)
    return match[1] if match else None


def _conditions(local, sample):
    differences = []
    if local["storage"]["scope"] == "unresolved" or sample["storage"]["scope"] == "unresolved":
        differences.append("unresolved_storage")
    for section, fields in (("storage", ("scope", "capacity_bytes", "connection", "negotiated_bps", "filesystem", "free_percent")),
                            ("environment", ("os", "os_version", "ioengine", "direct")),
                            ("plan", ("profile", "file_bytes", "write_budget_bytes", "seconds", "sustained_seconds"))):
        for field in fields:
            a, b = local[section].get(field), sample[section].get(field)
            if field == "write_budget_bytes":
                a, b = _write_budget(a), _write_budget(b)
            if field == "os_version":
                a, b = _major(a), _major(b)
            if a is None or b is None or isinstance(a, str) and _model(a) is None or isinstance(b, str) and _model(b) is None:
                differences.append("unknown_"+field)
            elif (int(a//10) != int(b//10) if field == "free_percent" else a != b):
                differences.append(field)
    if local["tool"]["method_version"] != sample["tool"]["method_version"]:
        differences.append("method_version")
    if local["plan"]["profile"] in ("mapping", "integrity"):
        differences.append("diagnostic_profile_background_only")
    if not any(_model(local["hardware"][kind]["model"]) is not None and
               _model(local["hardware"][kind]["model"]) == _model(sample["hardware"][kind]["model"]) for kind in ("computer", "controller", "drive")):
        differences.append("no_matching_model")
    return differences


def _phase(case):
    return "recovery" if case["name"].startswith("recovery") else ("sustained" if case["name"].startswith("sustained") else "initial")


def _workload(case):
    # Recovery and sustained phases are not interchangeable with an initial
    # sequential case, even when their fio flags happen to match.
    return (_phase(case), *tuple(case[field] for field in ("rw", "bs_bytes", "qd", "numjobs", "rwmixread", "runtime_limit_seconds")))


def _metric(case, direction, metric):
    value = case[direction]
    return value[metric] if metric in ("mb_s", "iops") else value["latency_us"][metric]


def build_comparison(store, result, submission_id):
    comparison_id = str(uuid.uuid4())
    with store._transaction(write=True):
        row = store._report(submission_id)
        local = store.payload(submission_id)
        projected = normalise_payload(project_result(result, local["attribution"]["profile_url"], local["attribution"]["intro"], submission_id=submission_id))
        if result["run_id"] != row["run_id"] or digest(projected) != row["expected_hash"]:
            raise CommunityError("integrity_mismatch")
        receipt = store.db.execute("SELECT * FROM receipts WHERE report_id=?", (submission_id,)).fetchone()
        if receipt is None or receipt["similar_status"] not in ("ok", "empty") or receipt["as_of"] is None:
            raise CommunityError("samples_unavailable")
        samples = store.samples(submission_id)
        store.db.execute("INSERT INTO comparisons VALUES(?,?,?,?,?,?,?,?,?)", (comparison_id, submission_id, result["run_id"], row["expected_hash"],
            _now(), receipt["as_of"], receipt["samples_fetched_at"] or receipt["fetched_at"], receipt["dataset_revision"], len(samples)))
        for case_index, case in enumerate(local["cases"]):
            for direction in ("read", "write"):
                if case[direction]["bytes"] <= 0:
                    continue
                metrics = {metric: [] for metric in METRICS}
                for item in samples:
                    record = item["record"]; sample = record["submission"]
                    differences = _conditions(local, sample)
                    if case["name"].startswith("recovery"):
                        differences.append("unknown_recovery_idle")
                    matching = [(index, candidate) for index, candidate in enumerate(sample["cases"])
                                if _workload(candidate) == _workload(case) and candidate[direction]["bytes"] > 0 and candidate["status"] == "completed"]
                    if not matching:
                        differences.append("workload_or_measurement")
                    if case["status"] != "completed":
                        differences.append("local_measurement_incomplete")
                    background = [(index, candidate) for index,candidate in enumerate(sample["cases"])
                                  if _phase(candidate) == _phase(case) and candidate[direction]["bytes"] > 0]
                    selected_index, selected = matching[0] if matching else (background[0] if background else (None, None))
                    keys = (comparison_id, case_index, direction, record["id"])
                    store.db.execute("INSERT INTO comparison_sources VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*keys, selected_index, record["url"], sample["tested_on"],
                        *(sample["hardware"][kind]["model"] for kind in ("computer", "controller", "drive")), int(not differences)))
                    context = [*keys, *(_value(sample, field) for _,field,_ in CONDITIONS),
                               *(selected[field] if selected else None for field,_ in WORKLOAD)]
                    names = ["comparison_id", "case_index", "direction", "sample_id", *[name for name,_,_ in CONDITIONS],
                             *["case_"+name for name,_ in WORKLOAD]]
                    store.db.execute("INSERT INTO comparison_source_context("+",".join(names)+") VALUES("+",".join("?" for _ in context)+")", context)
                    for field in differences:
                        store.db.execute("INSERT INTO comparison_differences VALUES(?,?,?,?,?)", (*keys, field))
                    if selected is not None:
                        for metric in METRICS:
                            value = _metric(selected, direction, metric)
                            store.db.execute("INSERT INTO comparison_values VALUES(?,?,?,?,?,?)", (*keys, metric, value))
                            if not differences and value is not None:
                                metrics[metric].append(value)
                for metric, values in metrics.items():
                    measured = _metric(case, direction, metric)
                    stats = _statistics(measured, values)
                    store.db.execute("INSERT INTO comparison_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)", (comparison_id, case_index, case["name"], direction,
                        metric, stats["n"], measured, stats["median"], stats["minimum"], stats["maximum"], stats["delta_percent"]))
    return summary(store, comparison_id)


def read_comparison(store, comparison_id):
    with store._transaction():
        row = store.db.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
        if row is None:
            raise CommunityError("not_found")
        local = store.payload(row["report_id"])
        if digest(local) != row["content_sha256"]:
            raise CommunityError("integrity_mismatch")
        context_complete = True
        budget_recheck = set()
        groups = {}
        for metric in store.db.execute("SELECT * FROM comparison_metrics WHERE comparison_id=? ORDER BY case_index,direction,metric", (comparison_id,)):
            key = metric["case_index"], metric["direction"]
            group = groups.setdefault(key, {"case_index": key[0], "case_name": metric["case_name"], "direction": key[1], "metrics": [], "references": []})
            group["metrics"].append({field: metric[field] for field in ("metric", "n", "local_value", "median", "minimum", "maximum", "delta_percent")})
        for source in store.db.execute("SELECT * FROM comparison_sources WHERE comparison_id=? ORDER BY case_index,direction,sample_id", (comparison_id,)):
            keys = (comparison_id, source["case_index"], source["direction"], source["sample_id"])
            item = {key: source[key] for key in ("sample_id", "sample_case", "url", "tested_on", "computer_model", "controller_model", "drive_model")}
            item["comparable"] = bool(source["comparable"]); item["verification"] = "unverified"
            item["agent_usage"] = agent_usage(source["url"])
            item["differences"] = [r[0] for r in store.db.execute("SELECT field FROM comparison_differences WHERE comparison_id=? AND case_index=? AND direction=? AND sample_id=? ORDER BY field", keys)]
            item["values"] = {r[0]: r[1] for r in store.db.execute("SELECT metric,value FROM comparison_values WHERE comparison_id=? AND case_index=? AND direction=? AND sample_id=?", keys)}
            context = store.db.execute("SELECT * FROM comparison_source_context WHERE comparison_id=? AND case_index=? AND direction=? AND sample_id=?", keys).fetchone()
            item["conditions"] = {name: context[name] for name,_,_ in CONDITIONS} if context else None
            item["workload"] = {name: context["case_"+name] for name,_ in WORKLOAD} if context else None
            item["matched_on"] = [kind for kind in ("computer", "controller", "drive")
                if _model(local["hardware"][kind]["model"]) is not None and
                _model(local["hardware"][kind]["model"]) == _model(source[kind+"_model"])]
            if context is None:
                context_complete = False
            else:
                local_budget = _write_budget(local["plan"].get("write_budget_bytes"))
                source_budget = _write_budget(context["write_budget_bytes"])
                reason = ("unknown_write_budget_bytes" if local_budget is None or source_budget is None
                          else "write_budget_bytes" if local_budget != source_budget else None)
                if reason:
                    item["comparable"] = False
                    if reason not in item["differences"]:
                        item["differences"].append(reason)
                    item["conditions"]["write_budget_bytes"] = source_budget
                    budget_recheck.add((source["case_index"], source["direction"]))
            groups[(source["case_index"], source["direction"])]["references"].append(item)
        # Recompute only the presentation of affected historical groups, using
        # their own frozen values. Never rewrite old facts or borrow new samples.
        for key in budget_recheck:
            group = groups[key]
            for metric in group["metrics"]:
                values = [ref["values"][metric["metric"]] for ref in group["references"]
                          if ref["comparable"] and ref["values"].get(metric["metric"]) is not None]
                metric.update(_statistics(metric["local_value"], values))
        for group in groups.values():
            group["metrics"].sort(key=lambda item: METRICS.index(item["metric"]))
        return {"comparison_id": comparison_id, "submission_id": row["report_id"], "run_id": row["run_id"], "content_sha256": row["content_sha256"],
            "sample_count": row["sample_count"], "sample_as_of": row["as_of"], "samples_fetched_at": row["fetched_at"], "dataset_revision": row["dataset_revision"],
            "created_at": row["created_at"], "local": local, "context_complete": context_complete,
            "groups": list(groups.values()), "limitations": list(LIMITATIONS)}


def summary(store, comparison_id):
    facts = read_comparison(store, comparison_id)
    comparable = sum(any(metric["n"] >= 3 for metric in group["metrics"]) for group in facts["groups"])
    return {"status": "compared", "comparison_id": comparison_id, "submission_id": facts["submission_id"], "sample_count": facts["sample_count"],
        "comparable_group_count": comparable, "sample_as_of": facts["sample_as_of"], "samples_fetched_at": facts["samples_fetched_at"],
        "dataset_revision": facts["dataset_revision"], "statistical_comparison": "available" if comparable else "insufficient_comparable_records",
        "limitations": list(LIMITATIONS)}
