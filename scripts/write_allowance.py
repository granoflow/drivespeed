"""Local finite write authorization. Reserve before I/O; unknown tails stay charged."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import uuid
from contextlib import contextmanager

from system_io import BenchError, CACHE, unsafe_path_component
from workloads import GIB

MAX_BYTES = 2**63 - 1  # SQLite's exact signed integer range.
APPLICATION_ID = 1146310755


def _bytes(value):
    if type(value) is not int or not 0 <= value <= MAX_BYTES:
        raise ValueError("Invalid authorized byte count")
    return value


def device_key(target):
    devices = target.get("physical_devices")
    if not devices or any(not isinstance(d, str) or not d for d in devices):
        raise BenchError("unsafe_target", "Physical identity required for write authorization")
    return hashlib.sha256(json.dumps(sorted(set(devices))).encode()).hexdigest()


def plan_hash(target, config, allowance):
    bound = dict(target={k: target[k] for k in ("path", "st_dev", "inode", "device", "physical_devices")},
                 plan=config, allowance=allowance)
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class WriteAllowance:
    """One active finite session; device renaming never creates a new allowance."""

    def __init__(self, directory=None):
        self.directory = (Path(directory) if directory is not None else CACHE / "write-allowance").expanduser().absolute()
        self.path = self.directory / "write-allowance.sqlite3"

    def _check_existing(self):
        if unsafe_path_component(self.path):
            raise BenchError("unsafe_target", "Unsafe write-allowance path")
        if not self.path.exists():
            if self.directory.exists():
                raise BenchError("unsafe_target", "Existing write-allowance state is incomplete; restore its authorization")
            return False
        if sys.platform == "win32":
            from community_storage import read_windows_input
            read_windows_input(self.path, 16 * 1024 * 1024)
        else:
            for path in (self.directory, self.path):
                info = path.lstat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise BenchError("unsafe_target", "Write-allowance state must be private")
            if not self.path.is_file() or self.path.stat().st_nlink != 1:
                raise BenchError("unsafe_target", "Invalid write-allowance database")
        return True

    def read(self, target):
        """No mkdir, database initialization, migration, or file locks."""
        if not self._check_existing():
            return dict(present=False, scope_id=None, revision=0, run_cap=64 * GIB, remaining=64 * GIB)
        device_key(target)  # Validate current identity; it does not key persistent accounting.
        with self._connection(create=False) as db:
            row = db.execute("SELECT scope_id,cap,charged,revision,run_cap FROM scopes WHERE active=1").fetchone()
            if row is None:
                raise BenchError("unsafe_target", "An existing write authorization must be selected")
        scope_id, cap, charged, revision, run_cap = row
        for value in (cap, charged, revision, run_cap):
            _bytes(value)
        if charged > cap:
            raise BenchError("unsafe_target", "Invalid write-allowance accounting")
        return dict(present=True, scope_id=scope_id, revision=revision, run_cap=run_cap, remaining=cap - charged)

    @contextmanager
    def _connection(self, create=True):
        db = windows = None
        try:
            if create:
                db, windows = self._open()
            else:
                db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise BenchError("unsafe_target", "Unrecognized write-allowance state")
            yield db
        except sqlite3.Error:
            raise BenchError("unsafe_target", "Write-allowance database unavailable") from None
        finally:
            if db is not None:
                db.close()
            if windows:
                windows.close()

    def _open(self):
        if unsafe_path_component(self.path):
            raise BenchError("unsafe_target", "Unsafe write-allowance path")
        windows = None
        if sys.platform == "win32":
            from community_storage import WindowsPrivateState
            windows = WindowsPrivateState(self.directory)
            try:
                with windows.initialise("write-allowance.sqlite3") as created:
                    db = sqlite3.connect(self.path, timeout=5)
            except BaseException:
                windows.close()
                raise
        else:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise BenchError("unsafe_target", "Write-allowance directory must be private")
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                self._check_existing()
                created = False
            else:
                os.close(fd)
                created = True
            db = sqlite3.connect(self.path, timeout=5)
        try:
            if created:
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            elif db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise BenchError("unsafe_target", "Unrecognized write-allowance state")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scopes(scope_id TEXT PRIMARY KEY,cap INTEGER NOT NULL,
                charged INTEGER NOT NULL,revision INTEGER NOT NULL,run_cap INTEGER NOT NULL,source TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_scope ON scopes(active) WHERE active=1;
            CREATE TABLE IF NOT EXISTS reservations(run_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id),
                plan_hash TEXT NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activations(scope_id TEXT NOT NULL,source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(source TEXT PRIMARY KEY,scope_id TEXT NOT NULL UNIQUE,
                cap INTEGER NOT NULL,charged INTEGER NOT NULL,run_cap INTEGER NOT NULL);
            PRAGMA user_version=1;
            """)
        except BaseException:
            db.close()
            if windows:
                windows.close()
            raise
        return db, windows

    def import_existing(self, target, cap, charged, run_cap, source):
        """Idempotent migration keyed by the stable verified authorization record ID."""
        for value in (cap, charged, run_cap):
            _bytes(value)
        if charged > cap or run_cap > cap or not isinstance(source, str) or not source.strip():
            raise ValueError("Invalid existing write authorization")
        source = source.strip()
        device_key(target)
        scope_id = str(uuid.uuid4())
        with self._connection() as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                original = db.execute("SELECT scope_id,cap,charged,run_cap FROM imports WHERE source=?", (source,)).fetchone()
                if original is not None:
                    if original[1:] != (cap, charged, run_cap):
                        raise BenchError("unsafe_target", "Existing authorization import conflicts with its original record")
                    return original[0]  # Preserve current charges and active selection.
                active = int(not db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0])
                db.execute("INSERT INTO scopes VALUES(?,?,?,?,?,?,?)", (scope_id, cap, charged, 1, run_cap, source, active))
                db.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (source, scope_id, cap, charged, run_cap))
        return scope_id

    def select_existing(self, scope_id, source):
        """Internal selection of verified permission records, not workload tuning."""
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Authorization selection requires its source")
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM scopes WHERE scope_id=?", (scope_id,)).fetchone():
                raise BenchError("unsafe_target", "Unknown existing authorization")
            if db.execute("SELECT 1 FROM reservations WHERE status='reserved'").fetchone():
                raise BenchError("device_busy", "An unsettled write reservation exists")
            db.execute("UPDATE scopes SET active=0,revision=revision+1 WHERE active=1")
            db.execute("UPDATE scopes SET active=1,revision=revision+1 WHERE scope_id=?", (scope_id,))
            db.execute("INSERT INTO activations VALUES(?,?)", (scope_id, source))

    def reserve(self, target, config, expected, run_id, digest):
        amount = _bytes(config["max_write_bytes"])
        if amount <= 0 or digest != plan_hash(target, config, expected):
            raise BenchError("unsafe_target", "Write plan changed after confirmation")
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope_id,cap,charged,revision,run_cap FROM scopes WHERE active=1").fetchone()
            if row is None:
                if expected["present"] or expected["revision"] != 0 or db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0]:
                    raise BenchError("unsafe_target", "Write authorization changed")
                row = (str(uuid.uuid4()), config["write_budget"], 0, 0, config["write_budget"])
                db.execute("INSERT INTO scopes VALUES(?,?,?,?,?,?,1)", (*row, "confirmed automatic plan"))
            scope_id, cap, charged, revision, run_cap = row
            actual = dict(present=expected["present"], scope_id=scope_id if expected["present"] else None,
                          revision=revision, run_cap=run_cap, remaining=cap - charged)
            if actual != expected or amount > min(run_cap, cap - charged):
                raise BenchError("unsafe_target", "Write authorization changed or exhausted")
            db.execute("UPDATE scopes SET charged=charged+?,revision=revision+1 WHERE scope_id=?", (amount, scope_id))
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,NULL,'reserved')", (run_id, scope_id, digest, amount))
            db.commit()

    def settle(self, run_id, actual):
        if actual is not None:
            _bytes(actual)
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope_id,reserved,status FROM reservations WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise BenchError("unsafe_target", "Missing write reservation")
            scope_id, reserved, status = row
            if status != "reserved":
                raise BenchError("unsafe_target", "Write reservation already settled")
            if actual is not None and actual > reserved:
                raise BenchError("unsafe_target", "Measured writes exceeded authorization")
            charged = reserved if actual is None else actual
            db.execute("UPDATE scopes SET charged=charged-?,revision=revision+1 WHERE scope_id=?", (reserved - charged, scope_id))
            db.execute("UPDATE reservations SET actual=?,status=? WHERE run_id=?", (actual, "unknown" if actual is None else "settled", run_id))
            db.commit()
