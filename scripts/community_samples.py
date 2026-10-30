"""Typed reference snapshots, isolated from locally owned submissions."""

from community_data import POLICY, digest, normalise_payload, validate_payload
from agent_usage import agent_usage
from community_store import FIELDS, CASE_FIELDS, CASE_TYPES, _value, _assign, write_quality, read_quality


def schema(db):
    columns = ",".join(f"{name} {kind}" for name, _, kind in FIELDS)
    cases = ",".join(f"{name} {kind}" for name, kind in zip(CASE_FIELDS, CASE_TYPES))
    db.executescript(f"""
      CREATE TABLE IF NOT EXISTS receipts(
        report_id TEXT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
        accepted_at TEXT NOT NULL,replayed INTEGER NOT NULL,next_available_at TEXT,contact_url TEXT,
        fetched_at TEXT NOT NULL,similar_status TEXT,as_of TEXT,dataset_revision INTEGER,samples_fetched_at TEXT);
      CREATE TABLE IF NOT EXISTS network_bindings(
        report_id TEXT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,origin TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS samples(
        parent_id TEXT REFERENCES reports(id) ON DELETE CASCADE,id TEXT,url TEXT NOT NULL,created_at TEXT NOT NULL,
        expected_hash TEXT NOT NULL,ordinal INTEGER NOT NULL,{columns},PRIMARY KEY(parent_id,id));
      CREATE TABLE IF NOT EXISTS sample_matches(
        parent_id TEXT,id TEXT,kind TEXT,ordinal INTEGER,
        PRIMARY KEY(parent_id,id,kind),FOREIGN KEY(parent_id,id) REFERENCES samples(parent_id,id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS sample_differences(
        parent_id TEXT,id TEXT,field TEXT,ordinal INTEGER,
        PRIMARY KEY(parent_id,id,field),FOREIGN KEY(parent_id,id) REFERENCES samples(parent_id,id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS sample_hardware(
        parent_id TEXT,id TEXT,kind TEXT,model TEXT,source TEXT,
        PRIMARY KEY(parent_id,id,kind),FOREIGN KEY(parent_id,id) REFERENCES samples(parent_id,id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS sample_cases(
        parent_id TEXT,id TEXT,ordinal INTEGER,{cases},has_depth INTEGER NOT NULL,
        cache_status TEXT,cache_capacity INTEGER,cache_speed REAL,PRIMARY KEY(parent_id,id,ordinal),
        FOREIGN KEY(parent_id,id) REFERENCES samples(parent_id,id) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS sample_measurements(
        parent_id TEXT,id TEXT,ordinal INTEGER,direction TEXT,bytes INTEGER NOT NULL,
        mb_s REAL,iops REAL,mean REAL,p50 REAL,p95 REAL,p99 REAL,PRIMARY KEY(parent_id,id,ordinal,direction),
        FOREIGN KEY(parent_id,id,ordinal) REFERENCES sample_cases(parent_id,id,ordinal) ON DELETE CASCADE);
      CREATE TABLE IF NOT EXISTS sample_depth(
        parent_id TEXT,id TEXT,ordinal INTEGER,sample INTEGER,depth INTEGER NOT NULL,percent REAL NOT NULL,
        PRIMARY KEY(parent_id,id,ordinal,sample),FOREIGN KEY(parent_id,id,ordinal) REFERENCES sample_cases(parent_id,id,ordinal) ON DELETE CASCADE);
      PRAGMA user_version=2;
    """)
    # The local v2 development cache may predate this explicit timestamp.
    if "samples_fetched_at" not in {r[1] for r in db.execute("PRAGMA table_info(receipts)")}:
        db.execute("ALTER TABLE receipts ADD COLUMN samples_fetched_at TEXT")


def replace(db, parent, items):
    db.execute('DELETE FROM measurement_quality WHERE owner_id=? AND report_id<>?', (parent,parent))
    db.execute('DELETE FROM assessment_presence WHERE owner_id=? AND report_id<>?', (parent,parent))
    db.execute("DELETE FROM samples WHERE parent_id=?", (parent,))
    for ordinal, item in enumerate(items):
        record = item["record"]; payload = normalise_payload(record["submission"]); remote = record["id"]
        names = ["parent_id", "id", "url", "created_at", "expected_hash", "ordinal"] + [name for name, _, _ in FIELDS]
        values = [parent, remote, record["url"], record["created_at"], digest(payload), ordinal] + [_value(payload, field) for _, field, _ in FIELDS]
        db.execute(f"INSERT INTO samples({','.join(names)}) VALUES({','.join('?' for _ in values)})", values)
        for field, table in (("matched_on", "sample_matches"), ("differences", "sample_differences")):
            for index, value in enumerate(item[field]):
                db.execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (parent, remote, value, index))
        for kind, model in payload["hardware"].items():
            db.execute("INSERT INTO sample_hardware VALUES(?,?,?,?,?)", (parent, remote, kind, model["model"], model["source"]))
        for index, case in enumerate(payload["cases"]):
            cache = case["cache"]
            row = [parent, remote, index] + [case[field] for field in CASE_FIELDS]
            row += [int(case["achieved_depth"] is not None), cache["status"] if cache else None,
                    cache["capacity_estimate_bytes"] if cache else None, cache["post_cache_mb_s"] if cache else None]
            db.execute(f"INSERT INTO sample_cases VALUES({','.join('?' for _ in row)})", row)
            for direction in ("read", "write"):
                metrics = case[direction]
                row = [parent, remote, index, direction, metrics["bytes"], metrics["mb_s"], metrics["iops"]]
                row += [metrics["latency_us"][key] for key in ("mean", "p50", "p95", "p99")]
                db.execute("INSERT INTO sample_measurements VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
            for sample, point in enumerate(case["achieved_depth"] or []):
                db.execute("INSERT INTO sample_depth VALUES(?,?,?,?,?,?)", (parent, remote, index, sample, point["depth"], point["percent"]))
        from community_assessment import write
        write(db, parent, remote, payload.get('assessment'))
        write_quality(db, parent, remote, payload.get("quality"))


def read(db, parent):
    from community_data import CommunityError
    import hmac
    result = []
    for row in db.execute("SELECT * FROM samples WHERE parent_id=? ORDER BY ordinal", (parent,)):
        remote = row["id"]; keys = (parent, remote)
        payload = {"schema_version": 1, "submission_id": remote, "hardware": {}, "cases": [], "integrity_notice": dict(POLICY["integrity_notice"])}
        for name, field, _ in FIELDS:
            _assign(payload, field, row[name])
        payload["environment"]["direct"] = True; payload["attribution"]["platform"] = "x"
        for model in db.execute("SELECT * FROM sample_hardware WHERE parent_id=? AND id=? ORDER BY kind", keys):
            payload["hardware"][model["kind"]] = {"model": model["model"], "source": model["source"]}
        for case in db.execute("SELECT * FROM sample_cases WHERE parent_id=? AND id=? ORDER BY ordinal", keys):
            item = {field: case[field] for field in CASE_FIELDS}; case_keys = (*keys, case["ordinal"])
            item["cache"] = ({"status": case["cache_status"], "capacity_estimate_bytes": case["cache_capacity"], "post_cache_mb_s": case["cache_speed"], "cause_confirmed": False}
                             if case["cache_status"] is not None else None)
            item["achieved_depth"] = [{"depth": p["depth"], "percent": p["percent"]} for p in db.execute(
                "SELECT * FROM sample_depth WHERE parent_id=? AND id=? AND ordinal=? ORDER BY sample", case_keys)] if case["has_depth"] else None
            for metrics in db.execute("SELECT * FROM sample_measurements WHERE parent_id=? AND id=? AND ordinal=?", case_keys):
                item[metrics["direction"]] = {"bytes": metrics["bytes"], "mb_s": metrics["mb_s"], "iops": metrics["iops"],
                    "latency_us": {key: metrics[key] for key in ("mean", "p50", "p95", "p99")}}
            payload["cases"].append(item)
        from community_assessment import read as read_assessment
        assessment = read_assessment(db,parent,remote)
        if assessment is not None:
            payload['assessment'] = assessment
        quality = read_quality(db, parent, remote)
        if quality is not None:
            payload["quality"] = quality
        validate_payload(payload)
        if not hmac.compare_digest(digest(payload), row["expected_hash"]):
            raise CommunityError("integrity_mismatch")
        result.append({"record": {"id": remote, "url": row["url"], "created_at": row["created_at"], "verification": "unverified", "submission": payload,
                                  "agent_usage": agent_usage(row["url"])},
            "matched_on": [r[0] for r in db.execute("SELECT kind FROM sample_matches WHERE parent_id=? AND id=? ORDER BY ordinal", keys)],
            "differences": [r[0] for r in db.execute("SELECT field FROM sample_differences WHERE parent_id=? AND id=? ORDER BY ordinal", keys)]})
    return result
