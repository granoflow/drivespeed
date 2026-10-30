"""Typed, private SQLite state for community sharing; never caches request JSON."""

import hmac
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import uuid
import sys
from datetime import datetime, timezone

if sys.platform != "win32":
    import fcntl

from community_data import CommunityError, POLICY, digest, normalise_payload, validate_payload

# Fixed fields, not user-controlled paths or a generic key/value document store.
FIELDS = [
    ("tested_on", "tested_on", "TEXT"), ("measured_at_device", "measured_at_device", "TEXT"),
    ("tool_version", "tool.version", "TEXT"), ("fio_version", "tool.fio_version", "TEXT"),
    ("method_version", "tool.method_version", "TEXT"),
    ("scope", "storage.scope", "TEXT"), ("capacity_bytes", "storage.capacity_bytes", "INTEGER"),
    ("connection", "storage.connection", "TEXT"), ("negotiated_bps", "storage.negotiated_bps", "INTEGER"),
    ("filesystem", "storage.filesystem", "TEXT"), ("free_percent", "storage.free_percent", "REAL"),
    ("os_family", "environment.os", "TEXT"), ("os_version", "environment.os_version", "TEXT"),
    ("architecture", "environment.architecture", "TEXT"), ("ioengine", "environment.ioengine", "TEXT"),
    ("profile", "plan.profile", "TEXT"), ("file_bytes", "plan.file_bytes", "INTEGER"),
    ("write_budget_bytes", "plan.write_budget_bytes", "INTEGER"), ("seconds", "plan.seconds", "INTEGER"),
    ("sustained_seconds", "plan.sustained_seconds", "INTEGER"), ("outcome", "outcome", "TEXT"),
    ("profile_url", "attribution.profile_url", "TEXT"), ("intro", "attribution.intro", "TEXT"),
]
CASE_FIELDS = ("name", "rw", "bs_bytes", "qd", "numjobs", "runtime_limit_seconds", "rwmixread", "status", "measurement_seconds")
CASE_TYPES = ("TEXT", "TEXT", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "INTEGER", "TEXT", "REAL")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _value(data, path):
    for part in path.split("."):
        data = data[part]
    return data


def _assign(data, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


def write_quality(db, owner, report, quality):
    if quality is not None:
        db.execute("INSERT INTO measurement_quality VALUES(?,?,?)", (owner, report, quality["status"]))


def read_quality(db, owner, report):
    row = db.execute("SELECT status FROM measurement_quality WHERE owner_id=? AND report_id=?", (owner, report)).fetchone()
    return {"status": row[0]} if row is not None else None


class CommunityStore:
    def __init__(self, directory=None):
        self._windows_state = None
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        self.directory = Path(directory) if directory is not None else base / "drivespeed/community"
        self.directory = self.directory.expanduser().absolute()
        self.path = self.directory / "community.sqlite3"
        if sys.platform == "win32":
            from community_storage import WindowsPrivateState
            self._windows_state = WindowsPrivateState(self.directory)
            try:
                with self._windows_state.initialise() as created:
                    self._open_database(created)
            except BaseException:
                self._windows_state.close()
                raise
            return
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise CommunityError("unsafe_state_directory")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CommunityError("unsafe_state_directory")
        lock_fd = os.open(self.directory / ".init.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) & 0o077:
                raise CommunityError("unsafe_state_lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                fd = None
            created = fd is not None
            if created:
                os.close(fd)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise CommunityError("unsafe_state_database")
            self._open_database(created)
        finally:
            os.close(lock_fd)

    def _open_database(self, created):
        self.db = sqlite3.connect(str(self.path), timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            if created:
                self.db.execute("PRAGMA application_id=1146310724")
            elif self.db.execute("PRAGMA application_id").fetchone()[0] != 1146310724:
                raise CommunityError("unrecognized_state_database")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._schema()
        except sqlite3.DatabaseError:
            self.db.close()
            raise CommunityError("unsafe_state_database") from None
        except BaseException:
            self.db.close()
            raise

    def close(self):
        try:
            self.db.close()
        finally:
            if self._windows_state is not None:
                self._windows_state.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self, write=False):
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            if owned:
                self.db.rollback()
            raise
        else:
            if owned:
                self.db.commit()

    def _schema(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise CommunityError("unsupported_state_version")
        columns = ",".join(f"{name} {kind}" for name, _, kind in FIELDS)
        cases = ",".join(f"{name} {kind}" for name, kind in zip(CASE_FIELDS, CASE_TYPES))
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS reports (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, result_path TEXT NOT NULL,
              expected_hash TEXT NOT NULL, encoding TEXT NOT NULL, integrity_version INTEGER NOT NULL,
              created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft', visibility TEXT,
              management_token TEXT, consent_id TEXT, consent_at TEXT, notice_version TEXT,
              privacy_version TEXT, destination TEXT, public_url TEXT, remaining INTEGER,
              reason_code TEXT, account_state TEXT, {columns});
            CREATE TABLE IF NOT EXISTS hardware (
              report_id TEXT REFERENCES reports(id) ON DELETE CASCADE, kind TEXT, model TEXT, source TEXT,
              PRIMARY KEY(report_id,kind));
            CREATE TABLE IF NOT EXISTS cases (
              report_id TEXT REFERENCES reports(id) ON DELETE CASCADE, ordinal INTEGER, {cases},
              has_depth INTEGER NOT NULL, cache_status TEXT, cache_capacity INTEGER, cache_speed REAL,
              PRIMARY KEY(report_id,ordinal));
            CREATE TABLE IF NOT EXISTS measurements (
              report_id TEXT, ordinal INTEGER, direction TEXT, bytes INTEGER NOT NULL,
              mb_s REAL, iops REAL, mean REAL, p50 REAL, p95 REAL, p99 REAL,
              PRIMARY KEY(report_id,ordinal,direction),
              FOREIGN KEY(report_id,ordinal) REFERENCES cases(report_id,ordinal) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS depth_samples (
              report_id TEXT, ordinal INTEGER, sample INTEGER, depth INTEGER NOT NULL, percent REAL NOT NULL,
              PRIMARY KEY(report_id,ordinal,sample),
              FOREIGN KEY(report_id,ordinal) REFERENCES cases(report_id,ordinal) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS decisions (
              run_id TEXT PRIMARY KEY, result_path TEXT NOT NULL, decision TEXT NOT NULL, recorded_at TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        from community_samples import schema
        self.db.execute("""CREATE TABLE IF NOT EXISTS measurement_quality(
            owner_id TEXT REFERENCES reports(id) ON DELETE CASCADE,report_id TEXT,status TEXT NOT NULL
            CHECK(status IN ('quiet_observed','interference_observed','visibility_limited')),
            PRIMARY KEY(owner_id,report_id))""")
        schema(self.db)
        from community_comparison import schema as comparison_schema
        comparison_schema(self.db)
        from community_assessment import schema as assessment_schema
        assessment_schema(self.db)

    @contextmanager
    def network_lock(self, identifier):
        from community_network import identifier as check_identifier
        check_identifier(identifier)
        if self._windows_state is not None:
            with self._windows_state.lock(identifier):
                yield
            return
        fd = os.open(self.directory / (".network-" + identifier + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
                raise CommunityError("unsafe_state_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommunityError("operation_busy") from None
            yield
        finally:
            os.close(fd)

    def credentials(self, identifier, origin, create=False):
        import secrets
        from community_network import TOKEN
        with self._transaction(write=create):
            row = self._report(identifier)
            binding = self.db.execute("SELECT origin FROM network_bindings WHERE report_id=?", (identifier,)).fetchone()
            token = row["management_token"]
            if token is None:
                if not create or row["state"] != "draft" or binding is not None:
                    raise CommunityError("management_token_missing")
                self.require_consent(identifier)
                token = secrets.token_urlsafe(32)
                self.db.execute("UPDATE reports SET management_token=?,state='pending' WHERE id=?", (token, identifier))
                self.db.execute("INSERT INTO network_bindings VALUES(?,?)", (identifier, origin))
            elif not TOKEN.fullmatch(token) or binding is None or binding[0] != origin:
                raise CommunityError("management_token_missing")
            return token, row["expected_hash"]

    def save_receipt(self, identifier, response):
        from community_samples import replace
        receipt, quota, account = response["receipt"], response["quota"], response["account"]
        with self._transaction(write=True):
            row = self._report(identifier)
            if row["expected_hash"] != receipt["content_sha256"] or receipt["id"] != identifier:
                raise CommunityError("content_conflict")
            self.db.execute("""UPDATE reports SET state=?,visibility=?,public_url=?,remaining=?,reason_code=?,account_state=? WHERE id=?""",
                ("withdrawn" if receipt["visibility"] == "withdrawn" else "accepted", receipt["visibility"], receipt["public_url"],
                 quota["remaining"], receipt["reason_code"], account["state"], identifier))
            self.db.execute("""INSERT INTO receipts(report_id,accepted_at,replayed,next_available_at,contact_url,fetched_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(report_id) DO UPDATE SET accepted_at=excluded.accepted_at,replayed=excluded.replayed,
                next_available_at=excluded.next_available_at,contact_url=excluded.contact_url,fetched_at=excluded.fetched_at""",
                (identifier, receipt["accepted_at"], int(receipt["replayed"]), quota["next_available_at"], account["contact_url"], _now()))
            if "similar" in response:
                s = response["similar"]
                replace(self.db, identifier, s["items"])
                self.db.execute("UPDATE receipts SET similar_status=?,as_of=?,dataset_revision=?,samples_fetched_at=? WHERE report_id=?",
                    (s["status"], s["as_of"], s["dataset_revision"], _now(), identifier))

    def receipt_summary(self, identifier):
        with self._transaction():
            row = self._report(identifier)
            r = self.db.execute("SELECT * FROM receipts WHERE report_id=?", (identifier,)).fetchone()
            if r is None:
                raise CommunityError("not_found")
            count = self.db.execute("SELECT COUNT(*) FROM samples WHERE parent_id=?", (identifier,)).fetchone()[0]
            return {"submission_id": identifier, "status": "withdrawn" if row["state"] == "withdrawn" else "accepted", "accepted": True,
                "visibility": row["visibility"], "public_url": row["public_url"], "replayed": bool(r["replayed"]),
                "similar_status": r["similar_status"], "sample_count": count, "sample_as_of": r["as_of"], "samples_fetched_at": r["samples_fetched_at"], "dataset_revision": r["dataset_revision"],
                "quota": {"max": 5, "window_seconds": 604800, "remaining": row["remaining"], "next_available_at": r["next_available_at"]},
                "account": {"state": row["account_state"], "contact_url": r["contact_url"]}, "reason_code": row["reason_code"]}

    def samples(self, identifier):
        from community_samples import read
        with self._transaction():
            self._report(identifier)
            return read(self.db, identifier)

    def mark_withdrawn(self, identifier):
        from community_samples import replace
        with self._transaction(write=True):
            self._report(identifier)
            self.db.execute("UPDATE reports SET state='withdrawn',visibility='withdrawn',public_url=NULL,reason_code='withdrawn' WHERE id=?", (identifier,))
            replace(self.db, identifier, [])
            self.db.execute("UPDATE receipts SET similar_status='unavailable',as_of=NULL,dataset_revision=NULL WHERE report_id=?", (identifier,))

    def create(self, payload, run_id, result_path):
        payload = normalise_payload(payload)
        checksum = digest(payload)
        identifier = payload["submission_id"]
        columns = ["id", "run_id", "result_path", "expected_hash", "encoding", "integrity_version", "created_at"] + [name for name, _, _ in FIELDS]
        values = [identifier, run_id, str(result_path), checksum, POLICY["payload_encoding"], payload["integrity_notice"]["version"], _now()]
        values += [_value(payload, path) for _, path, _ in FIELDS]
        try:
            with self._transaction(write=True):
                self.db.execute(f"INSERT INTO reports({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
                for kind, item in payload["hardware"].items():
                    self.db.execute("INSERT INTO hardware VALUES (?,?,?,?)", (identifier, kind, item["model"], item["source"]))
                for ordinal, case in enumerate(payload["cases"]):
                    cache = case["cache"]
                    row = [identifier, ordinal] + [case[key] for key in CASE_FIELDS]
                    row += [int(case["achieved_depth"] is not None), cache["status"] if cache else None,
                            cache["capacity_estimate_bytes"] if cache else None, cache["post_cache_mb_s"] if cache else None]
                    self.db.execute(f"INSERT INTO cases VALUES ({','.join('?' for _ in row)})", row)
                    for direction in ("read", "write"):
                        metrics = case[direction]
                        row = [identifier, ordinal, direction, metrics["bytes"], metrics["mb_s"], metrics["iops"]]
                        row += [metrics["latency_us"][key] for key in ("mean", "p50", "p95", "p99")]
                        self.db.execute("INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                    for sample, point in enumerate(case["achieved_depth"] or []):
                        self.db.execute("INSERT INTO depth_samples VALUES (?,?,?,?,?)", (identifier, ordinal, sample, point["depth"], point["percent"]))
                from community_assessment import write
                write(self.db, identifier, identifier, payload.get('assessment'))
                write_quality(self.db, identifier, identifier, payload.get("quality"))
        except sqlite3.IntegrityError:
            raise CommunityError("content_conflict") from None
        return identifier

    def _report(self, identifier):
        row = self.db.execute("SELECT * FROM reports WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise CommunityError("not_found")
        return row

    def payload(self, identifier):
        # One read transaction prevents a mixed snapshot across the child tables.
        with self._transaction():
            row = self._report(identifier)
            if row["encoding"] != POLICY["payload_encoding"] or row["integrity_version"] != POLICY["integrity_notice"]["version"]:
                raise CommunityError("unsupported_snapshot_version")
            payload = {"schema_version": 1, "submission_id": identifier, "hardware": {}, "cases": [],
                       "integrity_notice": dict(POLICY["integrity_notice"])}
            for name, path, _ in FIELDS:
                _assign(payload, path, row[name])
            payload["environment"]["direct"] = True
            payload["attribution"]["platform"] = "x"
            for model in self.db.execute("SELECT * FROM hardware WHERE report_id=? ORDER BY kind", (identifier,)):
                payload["hardware"][model["kind"]] = {"model": model["model"], "source": model["source"]}
            for item in self.db.execute("SELECT * FROM cases WHERE report_id=? ORDER BY ordinal", (identifier,)):
                case = {key: item[key] for key in CASE_FIELDS}
                case["cache"] = {"status": item["cache_status"], "capacity_estimate_bytes": item["cache_capacity"],
                    "post_cache_mb_s": item["cache_speed"], "cause_confirmed": False} if item["cache_status"] is not None else None
                case["achieved_depth"] = None
                if item["has_depth"]:
                    case["achieved_depth"] = [{"depth": point["depth"], "percent": point["percent"]} for point in self.db.execute(
                        "SELECT depth,percent FROM depth_samples WHERE report_id=? AND ordinal=? ORDER BY sample", (identifier, item["ordinal"]))]
                for metrics in self.db.execute("SELECT * FROM measurements WHERE report_id=? AND ordinal=?", (identifier, item["ordinal"])):
                    case[metrics["direction"]] = {"bytes": metrics["bytes"], "mb_s": metrics["mb_s"], "iops": metrics["iops"],
                        "latency_us": {key: metrics[key] for key in ("mean", "p50", "p95", "p99")}}
                payload["cases"].append(case)
            from community_assessment import read
            assessment = read(self.db, identifier, identifier)
            if assessment is not None:
                payload['assessment'] = assessment
            quality = read_quality(self.db, identifier, identifier)
            if quality is not None:
                payload["quality"] = quality
        validate_payload(payload)
        if not hmac.compare_digest(digest(payload), row["expected_hash"]):
            raise CommunityError("integrity_mismatch")
        return payload

    def consent(self, identifier, notice_version, decision="publish"):
        if notice_version != POLICY["notice_version"] or decision != "publish":
            raise CommunityError("consent_required")
        with self._transaction(write=True):
            self.payload(identifier)
            row = self._report(identifier)
            if row["state"] in ("accepted", "withdrawn"):
                raise CommunityError("invalid_state")
            consent_id, now = str(uuid.uuid4()), _now()
            self.db.execute("UPDATE reports SET consent_id=?,consent_at=?,notice_version=?,privacy_version=?,destination=? WHERE id=?",
                (consent_id, now, notice_version, POLICY["privacy_version"], POLICY["destination"], identifier))
            self.db.execute("INSERT INTO decisions VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET decision=excluded.decision,recorded_at=excluded.recorded_at",
                            (row["run_id"], row["result_path"], "approved", now))
        return consent_id

    def require_consent(self, identifier):
        payload = self.payload(identifier)
        row = self._report(identifier)
        decision = self.db.execute("SELECT decision FROM decisions WHERE run_id=?", (row["run_id"],)).fetchone()
        if row["state"] == "withdrawn" or decision is None or decision[0] != "approved" or not row["consent_id"] or row["notice_version"] != POLICY["notice_version"] or row["privacy_version"] != POLICY["privacy_version"] or row["destination"] != POLICY["destination"]:
            raise CommunityError("consent_required")
        return payload, {"confirmed_at": row["consent_at"], "notice_version": row["notice_version"],
                         "privacy_version": row["privacy_version"], "content_sha256": row["expected_hash"]}

    def remember(self, run_id, result_path):
        with self._transaction(write=True):
            self.db.execute("INSERT INTO decisions VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET result_path=excluded.result_path,decision=excluded.decision,recorded_at=excluded.recorded_at",
                            (run_id, str(result_path), "declined", _now()))
            self.db.execute("UPDATE reports SET consent_id=NULL,consent_at=NULL,notice_version=NULL,privacy_version=NULL,destination=NULL WHERE run_id=? AND state IN ('draft','pending')", (run_id,))

    def decline(self, identifier):
        # Refusing future upload must work even if the original file is gone or
        # a cached snapshot no longer passes integrity validation.
        with self._transaction(write=True):
            row = self._report(identifier)
            self.remember(row["run_id"], row["result_path"])
        return row["state"]

    def list_reports(self, state=None):
        if state is not None and state not in ("draft", "pending", "accepted", "withdrawn", "declined"):
            raise CommunityError("invalid_state")
        query = """SELECT r.id AS submission_id,r.run_id,r.result_path,r.state,r.visibility,r.public_url,r.created_at,
                          COALESCE(d.decision,'unasked') AS decision
                   FROM reports r LEFT JOIN decisions d ON d.run_id=r.run_id"""
        params = ()
        if state == "declined":
            query += " WHERE d.decision='declined'"
        elif state is not None:
            query += " WHERE r.state=?"
            params = (state,)
        result = [dict(row) for row in self.db.execute(query + " ORDER BY r.created_at,r.id", params)]
        if state in (None, "declined"):
            for row in self.db.execute("""SELECT d.* FROM decisions d
                WHERE d.decision='declined' AND NOT EXISTS(SELECT 1 FROM reports r WHERE r.run_id=d.run_id)
                ORDER BY d.recorded_at,d.run_id"""):
                result.append({"submission_id": None, "run_id": row["run_id"], "result_path": row["result_path"],
                    "state": None, "visibility": None, "public_url": None, "created_at": row["recorded_at"], "decision": "declined"})
        return result
