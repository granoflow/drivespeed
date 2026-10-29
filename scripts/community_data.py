"""Public report projection and in-memory transport; no network or cache files."""

import hashlib
import json
import math
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

CONTRACTS = Path(__file__).resolve().parents[1] / "assets/community"
POLICY = json.loads((CONTRACTS / "policy.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((CONTRACTS / "public-submission.schema.json").read_text(encoding="utf-8"))
ENVELOPE_SCHEMA = json.loads((CONTRACTS / "submission-envelope.schema.json").read_text(encoding="utf-8"))
MAX_BYTES = POLICY["limits"]["body_bytes"]


class CommunityError(ValueError):
    def __init__(self, code, fields=()):
        self.code, self.fields = code, list(fields)
        super().__init__(code)


def _fail(field, code="validation_failed"):
    raise CommunityError(code, [field])


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CommunityError("duplicate_key")
        value[key] = item
    return value


def _depth(value, level=0, max_depth=8):
    if level > max_depth:
        raise CommunityError("too_deep")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1, max_depth)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1, max_depth)
    elif isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise CommunityError("invalid_unicode") from None
    elif isinstance(value, float) and not math.isfinite(value):
        raise CommunityError("invalid_number")


def parse_json(raw, limit=MAX_BYTES, max_depth=8):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise CommunityError("payload_too_large")
    def invalid_constant(_):
        raise CommunityError("invalid_number")
    try:
        text = raw.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            raise CommunityError("invalid_json")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid_constant)
        _depth(value, max_depth=max_depth)
        return value
    except CommunityError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise CommunityError("invalid_json") from None


def _matches_type(value, kind):
    return {
        "null": lambda: value is None,
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool) and (isinstance(value, int) or math.isfinite(value)),
        "boolean": lambda: isinstance(value, bool),
    }[kind]()


def _schema(value, spec, field="$"):
    """Interpret only our bundled, ref-free schema subset using the standard library."""
    if "anyOf" in spec:
        for branch in spec["anyOf"]:
            try:
                _schema(value, branch, field)
                return
            except CommunityError:
                pass
        _fail(field)
    if "type" in spec:
        kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        if not any(_matches_type(value, kind) for kind in kinds):
            _fail(field)
    if "const" in spec and (value != spec["const"] or isinstance(value, bool) != isinstance(spec["const"], bool)):
        _fail(field)
    if "enum" in spec and value not in spec["enum"]:
        _fail(field)
    if isinstance(value, dict):
        properties = spec.get("properties", {})
        if spec.get("additionalProperties") is False and set(value) - set(properties):
            _fail(field + ".<unknown>")
        for key in spec.get("required", []):
            if key not in value:
                _fail(field + "." + key)
        for key, item in value.items():
            if key in properties:
                _schema(item, properties[key], field + "." + key)
    elif isinstance(value, list):
        if len(value) < spec.get("minItems", 0) or len(value) > spec.get("maxItems", MAX_BYTES):
            _fail(field)
        for index, item in enumerate(value):
            _schema(item, spec.get("items", {}), f"{field}[{index}]")
    elif isinstance(value, str):
        if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", MAX_BYTES):
            _fail(field)
        if "pattern" in spec and not re.search(spec["pattern"], value):
            _fail(field)
        fmt = spec.get("format")
        try:
            if fmt == "date":
                if date.fromisoformat(value).isoformat() != value:
                    _fail(field)
            elif fmt == "date-time":
                _timestamp(value, field)
            elif fmt == "uuid":
                uuid.UUID(value)
            elif fmt == "uri" and not urlsplit(value).scheme:
                _fail(field)
        except (ValueError, OverflowError):
            _fail(field)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < spec.get("minimum", -math.inf) or value > spec.get("maximum", math.inf) or (isinstance(value, float) and not math.isfinite(value)):
            _fail(field)


def _timestamp(value, field="measured_at_device"):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)", value):
            _fail(field)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            _fail(field)
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _fail(field)


def x_profile(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://x\.com/[A-Za-z0-9_]{1,15}", value):
        _fail("attribution.profile_url")
    if value.rsplit("/", 1)[1].lower() in {
        "home", "login", "settings", "share", "i", "intent", "search", "compose", "messages",
        "notifications", "explore", "privacy", "tos", "premium", "communities", "hashtag",
    }:
        _fail("attribution.profile_url")
    return value


def validate_payload(payload):
    _depth(payload)
    _schema(payload, SCHEMA)
    if 'assessment' in payload:
        from community_assessment import validate_assessment
        validate_assessment(payload['assessment'],payload['hardware'])
    x_profile(payload["attribution"]["profile_url"])
    intro = payload["attribution"]["intro"]
    if any(unicodedata.category(c) in ("Cc", "Cs", "Zl", "Zp") for c in intro) or re.search(r"https?://|www\.|<\s*/?\s*[A-Za-z!]", intro, re.I):
        _fail("attribution.intro")
    instant = _timestamp(payload["measured_at_device"])
    if payload["tested_on"] != instant.date().isoformat():
        _fail("tested_on")
    for kind, model in payload["hardware"].items():
        if (model["model"] is None) != (model["source"] == "unknown") or isinstance(model["model"], str) and not model["model"].strip():
            _fail("hardware." + kind)
    completed = False
    for index, case in enumerate(payload["cases"]):
        for direction in ("read", "write"):
            value = case[direction]
            if not value["bytes"] and (value["mb_s"] is not None or value["iops"] is not None or any(x is not None for x in value["latency_us"].values())):
                _fail(f"cases[{index}].{direction}")
        if case["read"]["bytes"] + case["write"]["bytes"] > 0 and case["measurement_seconds"] <= 0:
            _fail(f"cases[{index}].measurement_seconds")
        if case["status"] == "completed" and case["read"]["bytes"] + case["write"]["bytes"] > 0:
            completed = True
        cache = case["cache"]
        if cache and cache["status"] != "observed" and (cache["capacity_estimate_bytes"] is not None or cache["post_cache_mb_s"] is not None):
            _fail(f"cases[{index}].cache")
    if not completed:
        raise CommunityError("no_measurements")
    return payload


def payload_bytes(payload):
    validate_payload(payload)
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise CommunityError("invalid_json") from None
    if len(raw) > MAX_BYTES:
        raise CommunityError("payload_too_large")
    return raw


def digest(payload):
    return hashlib.sha256(payload_bytes(payload)).hexdigest()


def transport(payload, expected_digest=None):
    raw = payload_bytes(payload)
    checksum = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and checksum != expected_digest:
        raise CommunityError("integrity_mismatch")
    envelope = {"schema_version": 1, "payload_json": raw.decode("utf-8"), "integrity": {"algorithm": "SHA-256", "sha256": checksum}}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise CommunityError("payload_too_large")
    return encoded


def read_transport(raw):
    envelope = parse_json(raw)
    _schema(envelope, ENVELOPE_SCHEMA)
    try:
        encoded = envelope["payload_json"].encode("utf-8", errors="strict")
    except UnicodeError:
        raise CommunityError("invalid_unicode") from None
    if hashlib.sha256(encoded).hexdigest() != envelope["integrity"]["sha256"]:
        raise CommunityError("integrity_mismatch")
    return validate_payload(parse_json(encoded))


def _command(case, name, field):
    command = case.get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        _fail(field + ".command", "missing_parameters")
    values = [item.split("=", 1)[1] for item in command if item.startswith("--" + name + "=")]
    if len(values) != 1:
        _fail(field + "." + name, "missing_parameters")
    return values[0]


def _required(source, key, field):
    if not isinstance(source, dict) or key not in source:
        _fail(field + "." + key, "missing_parameters")
    return source[key]


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 9_007_199_254_740_991 or isinstance(value, float) and not math.isfinite(value):
        _fail(field)
    return 0.0 if value == 0 else float(value)


def _direction(metrics, direction):
    source = metrics.get(direction, {})
    if not isinstance(source, dict):
        _fail("cases.metrics." + direction)
    count = source.get("bytes", 0)
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 9_007_199_254_740_991:
        _fail("cases.metrics." + direction + ".bytes")
    latency = source.get("latency_us", {})
    if not isinstance(latency, dict):
        _fail("cases.metrics.latency_us")
    if count == 0:
        for value in [source.get("mb_s"), source.get("iops"), *[latency.get(k) for k in ("50", "95", "99")]]:
            if value is not None and _number(value, "cases.metrics." + direction) != 0:
                _fail("cases.metrics." + direction)
        return {"bytes": 0, "mb_s": None, "iops": None, "latency_us": dict.fromkeys(("mean", "p50", "p95", "p99"))}
    return {"bytes": count, "mb_s": _number(source["mb_s"], "cases.metrics.mb_s") if source.get("mb_s") is not None else None,
            "iops": _number(source["iops"], "cases.metrics.iops") if source.get("iops") is not None else None,
            "latency_us": {"mean": None, **{"p" + k: _number(latency[k], "cases.metrics.latency_us") if latency.get(k) is not None else None for k in ("50", "95", "99")}}}


FULL_CASES = {
    "seq_write": ("write", 1048576, 1), "seq_read": ("read", 1048576, 1),
    "seq_write_q": ("write", 1048576, 16), "seq_read_q": ("read", 1048576, 16),
    "rand_write": ("randwrite", 4096, 1), "rand_read": ("randread", 4096, 1),
    "rand_write_q": ("randwrite", 4096, 16), "rand_read_q": ("randread", 4096, 16),
    "mixed": ("randrw", 4096, 16), "sustained": ("write", 1048576, 16),
    "sustained_read": ("read", 1048576, 16),
}


def require_full_report(payload):
    if payload["plan"]["profile"] != "full" or payload["outcome"] != "completed":
        raise CommunityError("validation_failed", ["plan.profile", "outcome"])
    cases = payload["cases"]
    if len(cases) != len(FULL_CASES) or {case["name"] for case in cases} != set(FULL_CASES):
        raise CommunityError("validation_failed", ["cases"])
    for case in cases:
        spec = FULL_CASES[case["name"]]
        measured = (case["read"]["bytes"] if spec[0] in ("read", "randread") else
                    case["write"]["bytes"] if spec[0] in ("write", "randwrite") else
                    case["read"]["bytes"] + case["write"]["bytes"])
        if ((case["rw"], case["bs_bytes"], case["qd"]) != spec or case["status"] != "completed"
                or measured <= 0):
            raise CommunityError("validation_failed", ["cases"])
    return payload


def project_result(result, profile_url, intro="", submission_id=None):
    """Never re-probe hardware or infer missing historical method parameters."""
    if not isinstance(result, dict):
        _fail("result")
    for key in ("environment", "plan", "target", "hardware", "tool"):
        if not isinstance(result.get(key), dict):
            _fail(key, "missing_parameters")
    if not isinstance(result.get("cases"), list) or not all(isinstance(item, dict) for item in result["cases"]):
        _fail("cases")
    env, plan = result.get("environment", {}), result.get("plan", {})
    target = result.get("target", {})
    instant = _timestamp(result.get("created"), "created")
    if result.get("tool", {}).get("method_version") != "drivespeed-file-v1":
        _fail("tool.method_version", "missing_parameters")
    profile = _required(plan, "profile", "plan")
    family = _required(env, "os_family", "environment")
    os_version = _required(env, "os_version", "environment")
    if not isinstance(env.get("fio"), dict):
        _fail("environment.fio", "missing_parameters")
    models = {}
    for kind in ("computer", "controller", "drive", "enclosure"):
        source = result.get("hardware", {}).get(kind, {})
        if not isinstance(source, dict):
            _fail("hardware." + kind)
        value = source.get("model") if source.get("source") == "user" or kind in ('controller','enclosure') else source.get("matching_model")
        if value is None:
            models[kind] = {"model": None, "source": "unknown"}
        else:
            models[kind] = {"model": value, "source": "user" if source.get("source") == "user" else "detected"}
    cases, engines = [], set()
    for local in result.get("cases", []):
        if local.get("name") == "prepare" or local.get("status") == "skipped":
            continue
        field = f"cases[{len(cases)}]"
        if _command(local, "direct", field) != "1":
            _fail(field + ".direct")
        engine = _command(local, "ioengine", field)
        engines.add(engine)
        try:
            numeric = {key: int(_command(local, flag, field)) for key, flag in (("bs", "bs"), ("qd", "iodepth"), ("runtime", "runtime"))}
            if any(not isinstance(_required(local, key, field), int) or isinstance(local[key], bool) or numeric[key] != local[key] for key in numeric) or _command(local, "rw", field) != local.get("rw"):
                _fail(field + ".command")
            numjobs = int(_command(local, "numjobs", field))
            mix = int(_command(local, "rwmixread", field)) if local.get("rw") == "randrw" else None
        except CommunityError:
            raise
        except (ValueError, TypeError):
            _fail(field + ".command")
        measurements = local.get("metrics", {})
        if not isinstance(measurements, dict):
            _fail(field + ".metrics")
        raw_depth = measurements.get("achieved_depth", {})
        if not isinstance(raw_depth, dict):
            _fail(field + ".achieved_depth")
        depth = None
        if raw_depth and all(isinstance(k, str) and k.isdecimal() and int(k) > 0 for k in raw_depth):
            depth = [{"depth": int(k), "percent": _number(v, field + ".achieved_depth")} for k, v in sorted(raw_depth.items(), key=lambda item: int(item[0]))]
        cache = None
        if local.get("cache_test") or local.get('name') == 'sustained' and 'links' in result.get('hardware', {}):
            assessed = result.get("cache_assessment", {})
            if not isinstance(assessed, dict):
                _fail("cache_assessment")
            observed = assessed.get("status") == "observed_drop_stable" and local.get("status") == "completed"
            cache = {"status": "observed" if observed else "not_observed" if assessed.get("status") == "no_drop_observed" else "incomplete",
                     "capacity_estimate_bytes": assessed.get("cache_capacity_estimate_bytes") if observed else None,
                     "post_cache_mb_s": _number(assessed.get("post_cache_mb_s"), field + ".cache") if observed else None, "cause_confirmed": False}
        cases.append({"name": local.get("name"), "rw": local.get("rw"), "bs_bytes": numeric["bs"], "qd": numeric["qd"],
            "numjobs": numjobs, "runtime_limit_seconds": numeric["runtime"], "rwmixread": mix, "status": local.get("status"),
            "measurement_seconds": _number(measurements.get("runtime_ms", 0), field + ".runtime_ms") / 1000, "read": _direction(measurements, "read"),
            "write": _direction(measurements, "write"), "achieved_depth": depth, "cache": cache})
    if not cases:
        raise CommunityError("no_measurements")
    if len(engines) != 1 or env.get("engine") not in engines:
        _fail("environment.ioengine", "missing_parameters")
    capacity, free = target.get("total_bytes"), target.get("free_bytes")
    _schema(capacity, SCHEMA["properties"]["storage"]["properties"]["capacity_bytes"], "target.total_bytes")
    if free is not None and (isinstance(free, bool) or not isinstance(free, int) or not 0 <= free <= capacity):
        _fail("target.free_bytes")
    free_percent = float(free / capacity * 100) if free is not None else None
    payload = {"schema_version": 1, "submission_id": submission_id or str(uuid.uuid4()), "tested_on": instant.date().isoformat(),
        "measured_at_device": instant.isoformat().replace("+00:00", "Z"),
        "tool": {"version": result["tool"].get("version", "unknown"), "fio_version": env.get("fio", {}).get("version", "unknown"), "method_version": "drivespeed-file-v1"},
        "hardware": models, "storage": {"scope": _required(target, "scope", "target"), "capacity_bytes": capacity,
            "connection": target.get("protocol"), "negotiated_bps": target.get("negotiated_bps"), "filesystem": target.get("filesystem"), "free_percent": free_percent},
        "environment": {"os": family, "os_version": os_version, "architecture": env.get("architecture"), "ioengine": next(iter(engines)), "direct": True},
        "plan": {"profile": profile, "file_bytes": plan.get("file_bytes"), "write_budget_bytes": plan.get("write_budget"), "seconds": plan.get("seconds"), "sustained_seconds": plan.get("sustained")},
        "outcome": "completed" if result.get("outcome") == "complete" else result.get("outcome"), "cases": cases,
        "attribution": {"platform": "x", "profile_url": x_profile(profile_url), "intro": intro}, "integrity_notice": dict(POLICY["integrity_notice"])}
    from community_assessment import project_assessment
    assessment = project_assessment(result, payload)
    if assessment is not None:
        payload['assessment'] = assessment
    return require_full_report(validate_payload(payload))


def preview(payload):
    """Readable fields only; deliberately omit hashes and transport text."""
    validate_payload(payload)
    return {"destination": POLICY["destination"], "tested_on": payload["tested_on"], "tested_at_device": payload["measured_at_device"], "method": payload["tool"], "devices": payload["hardware"],
            **({'assessment': payload['assessment']} if 'assessment' in payload else {}),
            "conditions": {"storage": payload["storage"], "environment": payload["environment"], "plan": payload["plan"]},
            "measurements": payload["cases"], "contributor": payload["attribution"],
            "notice": "The report and your X link will be public only after your explicit consent and review. Account control and checksums do not verify the measurements."}


def normalise_payload(payload):
    """Fix numeric representation before freezing a new local structured snapshot."""
    validate_payload(payload)
    def visit(value, spec):
        if "anyOf" in spec:
            for branch in spec["anyOf"]:
                try:
                    _schema(value, branch)
                    return visit(value, branch)
                except CommunityError:
                    continue
        if isinstance(value, dict):
            return {key: visit(item, spec.get("properties", {}).get(key, {})) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, spec.get("items", {})) for item in value]
        kind = spec.get("type", [])
        if value is not None and (kind == "number" or isinstance(kind, list) and "number" in kind):
            return 0.0 if value == 0 else float(value)
        return value
    return visit(payload, SCHEMA)


def notice_details():
    return {"notice_version": POLICY["notice_version"], "privacy_version": POLICY["privacy_version"],
            "privacy_url": POLICY["destination"] + "/privacy"}
