"""Validate agent-researched, public reference facts. This module never fetches URLs."""

import json
import math
from pathlib import Path
from urllib.parse import urlsplit


def load_reference_data(path):
    return validate_reference_data(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_reference_data(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Reference JSON must contain an items list")
    if data["items"] and (
        not isinstance(data.get("for_drive_model"), str)
        or not data["for_drive_model"].strip()
    ):
        raise ValueError(
            "Reference packet must identify the drive model researched for this run"
        )
    for item in data["items"]:
        if not isinstance(item, dict):
            raise ValueError("Each reference must be an object")
        if item.get("component") not in (
            "drive",
            "computer",
            "enclosure",
            "controller",
        ):
            raise ValueError("Unknown reference component")
        if item.get("relationship") not in ("device", "peer", "candidate"):
            raise ValueError(
                "References must distinguish this device, peers and candidates"
            )
        if item.get("source_kind") not in ("official", "review"):
            raise ValueError("Reference facts require official or reviewed sources")
        for key in ("model", "metric", "unit", "source_label", "conditions"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Reference {key} must be nonempty text")
        value = item.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("Reference values must be positive finite numbers")
        units = {
            "seq_read": "MB/s",
            "seq_write": "MB/s",
            "seq_read_q": "MB/s",
            "seq_write_q": "MB/s",
            "rand_read": "MB/s",
            "rand_write": "MB/s",
            "post_cache_mb_s": "MB/s",
            "cache_capacity_estimate_bytes": "GiB",
            "interface_gbps": "Gb/s",
        }
        if item["metric"] not in units or item["unit"] != units[item["metric"]]:
            raise ValueError("Reference metric/unit mismatch; Gb/s is not MB/s")
        if not isinstance(item.get("source_url"), str):
            raise ValueError("Reference source URL must be text")
        url = urlsplit(item["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(
                "References require public HTTPS source URLs without credentials"
            )
    missing = data.get("missing", [])
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise ValueError("Missing-reference notes must be a text list")
    return data


def reference_cache_path(hardware, language=None):
    """Model-scoped per-user cache; never part of the distributed skill."""
    import hashlib
    import os

    models = {
        key: hardware.get(key, {}).get("model")
        for key in ("drive", "computer", "enclosure", "controller")
    }
    key = hashlib.sha256(json.dumps(models, sort_keys=True).encode("utf-8")).hexdigest()
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    import re

    language = language or "und"
    if not re.fullmatch(r"[A-Za-z0-9-]+", language):
        raise ValueError("Reference cache language must be a language tag")
    return root / "drivespeed" / "references" / key / (language + ".json"), models


def load_reference_cache(hardware, refresh=False, today=None, language=None):
    from datetime import date

    path, models = reference_cache_path(hardware, language)
    info = dict(
        status="refresh_requested" if refresh else "miss",
        path=str(path),
        from_cache=False,
        max_age_days=30,
    )
    if refresh or not models["drive"]:
        return None, info
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or saved.get("hardware_models") != models:
            raise ValueError("Reference cache hardware mismatch")
        data = validate_reference_data(saved["references"])
        if data.get("for_drive_model") != models["drive"]:
            raise ValueError("Reference cache drive mismatch")
        checked = date.fromisoformat(data.get("checked_at", ""))
        age = ((today or date.today()) - checked).days
        info.update(
            status="fresh" if 0 <= age <= 30 else "stale",
            checked_at=checked.isoformat(),
            age_days=age,
            from_cache=True,
        )
        return data, info
    except FileNotFoundError:
        info["available_languages"] = [p.stem for p in path.parent.glob("*.json")]
        return None, info
    except (OSError, ValueError, KeyError, TypeError):
        info["status"] = "invalid"
        return None, info


def cache_reference_data(data, hardware, language=None):
    """Save public facts and dates only; no fio results, serials or private paths."""
    import uuid
    from datetime import date

    data = validate_reference_data(data)
    path, models = reference_cache_path(hardware, language)
    if (
        not data["items"]
        or not models["drive"]
        or data.get("for_drive_model") != models["drive"]
    ):
        return dict(status="not_cached", from_cache=False)
    try:
        date.fromisoformat(data.get("checked_at", ""))
    except (ValueError, TypeError):
        return dict(status="date_missing", from_cache=False)
    public = {
        key: data[key]
        for key in ("for_drive_model", "checked_at", "items", "missing")
        if key in data
    }
    fields = {
        "component",
        "model",
        "metric",
        "value",
        "unit",
        "source_kind",
        "source_url",
        "source_label",
        "conditions",
        "relationship",
        "approximate",
    }
    public["items"] = [
        {key: value for key, value in item.items() if key in fields}
        for item in data["items"]
    ]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = path.with_name(path.stem + "." + uuid.uuid4().hex + ".tmp")
    if language:
        public["language"] = language
    staged.write_text(
        json.dumps(
            {"hardware_models": models, "references": public},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    staged.replace(path)
    _, info = load_reference_cache(hardware, language=language)
    info["from_cache"] = False
    return info


def connection_limits(result, references):
    """Known rated bounds in decimal MB/s; candidates and reviews cannot set a path ceiling."""
    normalize = lambda value: "".join(c.lower() for c in (value or "") if c.isalnum())
    hardware = result.get("hardware", {})
    drive = hardware.get("drive", {}).get("model")
    if not drive or normalize(drive) != normalize(references.get("for_drive_model")):
        return None
    parts = []
    for component in ("computer", "enclosure", "controller", "drive"):
        model = hardware.get(component, {}).get("model")
        values = {"read": [], "write": []}
        items = []
        for item in references.get("items", []):
            if (
                item["component"] != component
                or item["relationship"] != "device"
                or item["source_kind"] != "official"
                or not model
                or normalize(item["model"]) != normalize(model)
            ):
                continue
            metric = item["metric"]
            if metric == "interface_gbps" and component != "drive":
                # Decimal Gb/s to decimal MB/s, before transport and protocol overhead.
                values["read"].append(item["value"] * 125)
                values["write"].append(item["value"] * 125)
            elif component != "computer" and metric in ("seq_read", "seq_read_q"):
                values["read"].append(item["value"])
            elif component != "computer" and metric in ("seq_write", "seq_write_q"):
                values["write"].append(item["value"])
            else:
                continue
            items.append(item)
        parts.append(
            dict(
                component=component,
                model=model,
                sources=items,
                read_mb_s=min(values["read"]) if values["read"] else None,
                write_mb_s=min(values["write"]) if values["write"] else None,
            )
        )
    if not any(part["sources"] for part in parts):
        return None
    enclosure = next(p for p in parts if p["component"] == "enclosure")
    controller = next(p for p in parts if p["component"] == "controller")
    # A bridge chip supplies a useful bound, but does not establish the enclosure's
    # port, firmware, cooling or negotiated link. Keep enclosure completeness unknown.
    out = dict(
        parts=parts,
        enclosure_uses_controller=not enclosure["sources"]
        and bool(controller["sources"]),
    )
    for direction in ("read", "write"):
        field = direction + "_mb_s"
        known = [p[field] for p in parts if p[field] is not None]
        out[field] = min(known) if known else None
        out["missing_" + direction] = [
            p["component"]
            for p in parts
            if p["component"] in ("computer", "enclosure", "drive") and p[field] is None
        ]
    return out
