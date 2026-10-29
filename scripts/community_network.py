"""Bounded community HTTP protocol. Credentials and JSON bodies stay in memory."""

import hashlib
import http.client
import json
import re
import ssl
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from community_data import CommunityError, POLICY, parse_json, validate_payload, transport, require_full_report
from agent_usage import agent_usage

CONTACT = "https://x.com/slime5409"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
RESPONSE_LIMIT = 8 * 1024 * 1024
REASONS = {None, "withdrawn", "account_restricted", "confirmed_false", "x_profile_changed", "pending_review" , "awaiting_review", "incomplete_report"}
DIFFERENCES = {"scope", "capacity_bytes", "connection", "negotiated_bps", "filesystem", "free_percent", "os", "os_version",
               "ioengine", "direct", "profile", "file_bytes", "write_budget_bytes", "unknown_write_budget_bytes",
               "seconds", "sustained_seconds", "workload", "unknown_condition"}
REMOTE_CODES = {"not_found", "forbidden", "invalid_token", "missing_token", "content_conflict", "withdrawn", "consent_required",
                "validation_failed", "rate_limited", "quota_exceeded", "flow_budget", "unavailable", "internal_error",
                "auth_expired", "auth_incomplete", "auth_declined", "account_mismatch", "invalid_ticket", "intake_paused",
                "payload_too_large", "unsupported_media_type", "bad_request", "capacity_exceeded", "account_quota", "intake_closed",
                "x_scope_mismatch", "x_unavailable", "preview_mismatch", "integrity_mismatch", "no_measurements", "flow_already_started", "method_not_allowed"}


class NetworkError(CommunityError):
    def __init__(self, code, status=None, retry_after=None, contact=None, quota=None, request_id=None):
        super().__init__(code)
        self.status, self.retry_after, self.contact = status, retry_after, contact
        self.quota, self.request_id = quota, request_id
        self.remote = "check_receipt"


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).version != 4:
            raise ValueError()
    except (ValueError, AttributeError):
        raise CommunityError("invalid_arguments", ["submission_id"]) from None
    return value


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise NetworkError("invalid_response")
    return value


def _integer(value, low=0, high=9007199254740991):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise NetworkError("invalid_response")
    return value


def _date(value):
    from datetime import datetime
    try:
        if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
            raise ValueError()
        datetime.fromisoformat(value[:-1]+"+00:00")
    except (ValueError, OverflowError):
        raise NetworkError("invalid_response") from None
    return value


def _quota(value):
    q = _object(value, ("window_seconds", "max", "remaining", "next_available_at"))
    if type(q["window_seconds"]) is not int or q["window_seconds"] != 604800 or type(q["max"]) is not int or q["max"] != 5:
        raise NetworkError("invalid_response")
    _integer(q["remaining"], 0, 5)
    if q["next_available_at"] is not None:
        _date(q["next_available_at"])
    return q


def check_receipt(value, submission_id, checksum, origin):
    _object(value, ("schema_version", "receipt", "quota", "account"), ("similar",))
    r = _object(value["receipt"], ("id", "accepted_at", "content_sha256", "replayed", "visibility", "reason_code", "public_url"))
    if value["schema_version"] != 1 or r["id"] != submission_id or r["content_sha256"] != checksum or type(r["replayed"]) is not bool:
        raise NetworkError("invalid_response")
    _date(r["accepted_at"])
    if r["visibility"] not in ("pending", "hidden", "public", "withdrawn") or (r["reason_code"] is not None and (not isinstance(r["reason_code"], str) or r["reason_code"] not in REASONS)):
        raise NetworkError("invalid_response")
    expected = origin + "/benchmarks/" + submission_id if r["visibility"] == "public" else None
    if r["public_url"] != expected:
        raise NetworkError("invalid_response")
    _quota(value["quota"])
    a = _object(value["account"], ("state", "contact_url"))
    if a["state"] not in ("active", "restricted") or a["contact_url"] != (CONTACT if a["state"] == "restricted" else None):
        raise NetworkError("invalid_response")
    if "similar" in value:
        s = _object(value["similar"], ("status", "items", "next_cursor", "as_of", "dataset_revision"))
        if s["status"] not in ("ok", "empty", "unavailable") or s["next_cursor"] is not None or not isinstance(s["items"], list) or len(s["items"]) > 20:
            raise NetworkError("invalid_response")
        if (s["status"] == "ok") != bool(s["items"]):
            raise NetworkError("invalid_response")
        _date(s["as_of"]); _integer(s["dataset_revision"])
        seen = set()
        for item in s["items"]:
            _object(item, ("record", "matched_on", "differences"))
            record = _object(item["record"], ("id", "url", "created_at", "verification", "submission"), ("agent_usage",))
            try:
                identifier(record["id"]); validate_payload(record["submission"])
            except CommunityError:
                raise NetworkError("invalid_response") from None
            if record["id"] in seen or record["id"] == submission_id or record["id"] != record["submission"]["submission_id"] or record["verification"] != "unverified" or record["url"] != origin+"/benchmarks/"+record["id"]:
                raise NetworkError("invalid_response")
            seen.add(record["id"]); _date(record["created_at"])
            # Accept older servers without metadata; never treat arbitrary
            # upstream text as trusted Agent instructions.
            if "agent_usage" in record and record["agent_usage"] != agent_usage(record["url"]):
                raise NetworkError("invalid_response")
            for field, allowed, minimum in (("matched_on", {"computer", "controller", "drive"}, 1), ("differences", DIFFERENCES, 0)):
                items = item[field]
                if not isinstance(items, list) or len(items) < minimum or any(type(v) is not str or v not in allowed for v in items) or len(items) != len(set(items)):
                    raise NetworkError("invalid_response")
    return value


class CommunityHTTP:
    def __init__(self, test_origin=None):
        origin = test_origin or POLICY["destination"]
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError:
            raise CommunityError("invalid_arguments", ["test_origin"]) from None
        if test_origin is not None and (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or not port
                                        or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            raise CommunityError("invalid_arguments", ["test_origin"])
        self.origin, self.parsed = origin, parsed

    def request(self, method, path, body=None, headers=None, timeout=20):
        if not path.startswith("/api/v1/") or any(c in path for c in ("\r", "\n", "#")):
            raise CommunityError("invalid_arguments")
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if body is not None and len(body) > POLICY["limits"]["body_bytes"]:
            raise CommunityError("payload_too_large")
        # http.client ignores environment proxies and never follows redirects.
        timeout = min(20, timeout)
        if timeout <= 0:
            raise NetworkError("network_unavailable")
        deadline = time.monotonic()+timeout
        connection = (http.client.HTTPSConnection(self.parsed.hostname, self.parsed.port, timeout=timeout, context=ssl.create_default_context())
                      if self.parsed.scheme == "https" else http.client.HTTPConnection(self.parsed.hostname, self.parsed.port, timeout=timeout))
        live_socket = [None]
        def expire():
            # Also interrupts a slow header/body read that keeps receiving a
            # few bytes before each individual socket timeout.
            active = live_socket[0] or getattr(connection, "sock", None)
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        watchdog = threading.Timer(timeout, expire); watchdog.daemon = True; watchdog.start()
        def remaining():
            left = deadline-time.monotonic()
            if left <= 0:
                raise NetworkError("network_unavailable")
            if live_socket[0] is not None:
                live_socket[0].settimeout(left)
        try:
            connection.request(method, path, body, {"Accept": "application/json", "Content-Type": "application/json", **(headers or {})})
            live_socket[0] = getattr(connection, "sock", None)
            remaining()
            response = connection.getresponse()
            remaining()
            if 300 <= response.status < 400:
                raise NetworkError("redirect_refused", response.status)
            if response.status == 204:
                return None
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > RESPONSE_LIMIT):
                raise NetworkError("invalid_response")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise NetworkError("invalid_response")
            if response.getheader("Content-Type", "").split(";", 1)[0].strip() not in ("application/json", "application/problem+json"):
                raise NetworkError("invalid_response")
            chunks, total = [], 0
            while True:
                remaining()
                chunk = response.read1(min(65536, RESPONSE_LIMIT+1-total))
                if not chunk:
                    break
                total += len(chunk)
                if total > RESPONSE_LIMIT:
                    raise NetworkError("invalid_response")
                chunks.append(chunk)
            remaining()
            raw = b"".join(chunks)
            try:
                result = parse_json(raw, limit=RESPONSE_LIMIT, max_depth=16)
            except CommunityError:
                raise NetworkError("invalid_response") from None
            if response.status >= 400:
                code = result.get("error", {}).get("code") if isinstance(result, dict) and isinstance(result.get("error"), dict) else None
                code = code if isinstance(code, str) and code in REMOTE_CODES else "http_rejected"
                retry = response.getheader("Retry-After", "")
                retry = int(retry) if retry.isdecimal() and len(retry) < 10 else None
                account = result.get("account") if isinstance(result, dict) else None
                contact = CONTACT if account == {"state": "restricted", "contact_url": CONTACT} else None
                quota = _quota(result["quota"]) if isinstance(result, dict) and "quota" in result else None
                request_id = result.get("request_id") if isinstance(result, dict) else None
                if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", request_id):
                    request_id = None
                raise NetworkError(code, response.status, retry, contact, quota, request_id)
            if not 200 <= response.status < 300:
                raise NetworkError("invalid_response")
            if method == "DELETE":
                raise NetworkError("invalid_response")
            return result
        except (OSError, http.client.HTTPException):
            raise NetworkError("network_unavailable") from None
        finally:
            watchdog.cancel()
            connection.close()


class CommunityClient:
    def __init__(self, store, http, progress=None, sleep=time.sleep, clock=time.monotonic):
        self.store, self.http, self.progress, self.sleep, self.clock = store, http, progress, sleep, clock
        self.remote = "none"

    def _call(self, method, path, body=None, headers=None, deadline=None):
        stage = "check_receipt" if method in ("POST", "DELETE") and "/auth/" not in path else ("authorization_only" if "/auth/" in path else "receipt_checked")
        stages = ("none", "receipt_checked", "authorization_only", "check_receipt")
        self.remote = stages[max(stages.index(stage), stages.index(self.remote))]
        safe_retry = method == "GET" or path == "/api/v1/auth/poll"
        limited_retry = False
        for attempt in range(3):
            try:
                timeout = min(20, deadline-self.clock()) if deadline is not None else 20
                if timeout <= 0:
                    raise NetworkError("auth_expired", 410)
                return self.http.request(method, path, body, headers, timeout=timeout)
            except NetworkError as error:
                error.remote = self.remote
                if not safe_retry or attempt == 2:
                    raise
                if error.status == 429 and not limited_retry and error.retry_after is not None and error.retry_after <= 30:
                    limited_retry = True; wait = error.retry_after
                elif error.status is None or error.status >= 500:
                    wait = (2, 5)[attempt]
                else:
                    raise
                if deadline is not None and self.clock()+wait >= deadline:
                    raise NetworkError("auth_expired", 410)
                self.sleep(wait)

    def _receipt(self, identifier, token, checksum, refresh=False):
        result = self._call("GET", "/api/v1/benchmarks/"+identifier+"/receipt"+("?include=similar" if refresh else ""), headers={"Authorization": "Bearer "+token})
        result = check_receipt(result, identifier, checksum, self.http.origin)
        self.store.save_receipt(identifier, result)
        return self.store.receipt_summary(identifier)

    def receipt(self, submission_id, refresh=False):
        with self.store.network_lock(identifier(submission_id)):
            token, checksum = self.store.credentials(submission_id, self.http.origin)
            return self._receipt(submission_id, token, checksum, refresh)

    def withdraw(self, submission_id, confirm=False):
        identifier(submission_id)
        if not confirm:
            self.store._report(submission_id)
            return {"submission_id": submission_id, "status": "preview", "remote_unchanged": True, "quota_refunded": False}
        with self.store.network_lock(submission_id):
            token, _ = self.store.credentials(submission_id, self.http.origin)
            result = self._call("DELETE", "/api/v1/benchmarks/"+submission_id, headers={"Authorization": "Bearer "+token})
            if result is not None:
                raise NetworkError("invalid_response")
            self.store.mark_withdrawn(submission_id)
            return {"submission_id": submission_id, "status": "withdrawn", "quota_refunded": False}

    def publish(self, submission_id):
        identifier(submission_id)
        with self.store.network_lock(submission_id):
            token, checksum = self.store.credentials(submission_id, self.http.origin, create=True)
            try:
                return self._receipt(submission_id, token, checksum, refresh=True)
            except NetworkError as error:
                if error.status != 404:
                    raise
                if self.store._report(submission_id)["state"] in ("accepted", "withdrawn"):
                    # A deleted server record must not be recreated from an old
                    # consent merely because its private receipt disappeared.
                    raise
            payload, consent = self.store.require_consent(submission_id)
            require_full_report(payload)
            consent_id = self.store._report(submission_id)["consent_id"]
            flow = self._call("POST", "/api/v1/auth/flows", {"submission_id": submission_id, "content_sha256": checksum,
                "management_token_sha256": hashlib.sha256(token.encode()).hexdigest(), "profile_url": payload["attribution"]["profile_url"],
                "notice_version": consent["notice_version"], "privacy_version": consent["privacy_version"], "consent_at": consent["confirmed_at"],
                "preview": {"tested_on": payload["tested_on"], "models": {kind: item["model"] for kind,item in payload["hardware"].items()}}})
            _object(flow, ("schema_version", "flow_id", "verification_url", "user_code", "poll_token", "expires_at", "poll_interval_seconds"))
            try:
                identifier(flow["flow_id"])
            except CommunityError:
                raise NetworkError("invalid_response") from None
            if flow["schema_version"] != 1 or flow["verification_url"] != self.http.origin+"/auth/start?flow="+flow["flow_id"] or not isinstance(flow["user_code"], str) or not re.fullmatch(r"[A-F0-9]{4}-[A-F0-9]{4}", flow["user_code"]) or not isinstance(flow["poll_token"], str) or not TOKEN.fullmatch(flow["poll_token"]) or flow["poll_interval_seconds"] != 3:
                raise NetworkError("invalid_response")
            _date(flow["expires_at"])
            if self.progress:
                self.progress({"status": "authorization_required", "verification_url": flow["verification_url"], "user_code": flow["user_code"]})
            deadline = self.clock()+600
            while self.clock() < deadline:
                polled = self._call("POST", "/api/v1/auth/poll", {"flow_id": flow["flow_id"], "poll_token": flow["poll_token"]}, deadline=deadline)
                _object(polled, ("schema_version", "status"), ("reason_code", "upload_ticket", "expires_at", "poll_interval_seconds"))
                if polled["schema_version"] != 1:
                    raise NetworkError("invalid_response")
                if polled["status"] == "authorized":
                    if set(polled) != {"schema_version", "status", "upload_ticket", "expires_at"} or not isinstance(polled["upload_ticket"], str) or not TOKEN.fullmatch(polled["upload_ticket"]):
                        raise NetworkError("invalid_response")
                    _date(polled["expires_at"])
                    ticket = polled["upload_ticket"]
                    break
                if polled["status"] in ("declined", "failed", "expired"):
                    if polled["status"] == "declined":
                        self.store.decline(submission_id)
                    raise NetworkError("auth_"+polled["status"], 409)
                if polled != {"schema_version": 1, "status": "pending", "poll_interval_seconds": 3}:
                    raise NetworkError("invalid_response")
                self.sleep(3)
            else:
                raise NetworkError("auth_expired", 410)
            limited_retry = False
            for attempt in range(3):
                # Another process may revoke or replace consent while OAuth waits.
                current_payload, current = self.store.require_consent(submission_id)
                if current != consent or self.store._report(submission_id)["consent_id"] != consent_id:
                    raise CommunityError("consent_required")
                try:
                    value = self._call("POST", "/api/v1/benchmarks", transport(current_payload, checksum), {"Authorization": "Bearer "+token,
                        "X-DriveSpeed-Upload": ticket, "X-DriveSpeed-Public": "true", "X-DriveSpeed-Consent-Version": consent["notice_version"],
                        "X-DriveSpeed-Privacy-Version": consent["privacy_version"], "X-DriveSpeed-Consent-At": consent["confirmed_at"]})
                    self.store.save_receipt(submission_id, check_receipt(value, submission_id, checksum, self.http.origin))
                    return self.store.receipt_summary(submission_id)
                except NetworkError as error:
                    transient = error.status is None or error.status >= 500
                    if error.status == 429 and not limited_retry and error.retry_after is not None and error.retry_after <= 30:
                        limited_retry = True; wait = error.retry_after
                    elif transient:
                        wait = (2, 5, 5)[attempt]
                    else:
                        raise
                    if attempt == 2:
                        raise
                    self.sleep(wait)
                    try:
                        return self._receipt(submission_id, token, checksum, refresh=True)
                    except NetworkError as recovery:
                        if recovery.status != 404:
                            raise
            raise NetworkError("network_unavailable")
