#!/usr/bin/env python3
"""Render trusted measurement numbers into an escaped, offline HTML/PDF report."""

import argparse
import html
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from progress import ASSETS, language_default, messages
from workloads import sustained_summary, cache_assessment, job_label
from performance_rating import interface_catalog, link_reference, component_summary
from reference_data import (
    load_reference_data,
    validate_reference_data,
    load_reference_cache,
    cache_reference_data,
    connection_limits,
    apply_hardware_specs,
)


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:,.2f}"


def render_hardware_assessment(result, text):
    """Show detected segments, rated device specifications and evidence-bound grades."""
    hardware = result.get("hardware", {})
    references = validate_reference_data(result.get("references") or {"items": []})
    body = []
    known = [link_reference(link) for link in hardware.get('links', [])]
    minimum = min((fact['mb_s'] for fact in known if fact), default=None)
    for link in hardware.get("links", []):
        fact = link_reference(link)
        if not fact:
            continue
        state = text["link_negotiated"] if link.get("state") == "negotiated" else text["link_reported"]
        maximum = link_reference(dict(kind=link.get('kind'), gt_s=link.get('max_gt_s'), lanes=link.get('max_lanes')))
        if maximum:
            state += ' · ' + text['link_max'] + ': ' + maximum['label']
        if minimum == fact['mb_s']:
            state += ' · ' + text['link_minimum']
        body.append(f'<tr><th>{esc(fact["label"])}</th><td>{esc(state)}</td><td>{number(fact["mb_s"])} MB/s · {esc(text["link_" + fact["basis"]])}</td></tr>')
    links = '<table><tbody>' + ''.join(body) + '</tbody></table>' if body else '<p>' + esc(text['link_unknown']) + '</p>'
    specs = []
    if references.get('for_drive_model') == hardware.get('drive', {}).get('model'):
        for spec in references.get('hardware_specs', []):
            if spec['model'] != hardware.get(spec['component'], {}).get('model'):
                continue
            specs.append(f'<tr><th>{esc(text[spec["component"] + "_model"])}</th><td>{esc(spec["model"])} · {esc(spec["interface"])}<br>{esc(spec["conditions"])}</td><td><a href="{esc(spec["source_url"])}" rel="noreferrer">{esc(spec["source_label"])}</a> · {esc(references.get("checked_at", ""))}</td></tr>')
    catalog = interface_catalog()
    for chip in catalog.get('controllers', []):
        if chip['model'] == hardware.get('controller', {}).get('model'):
            specs.append(f'<tr><th>{esc(text["controller_model"])}</th><td>{esc(chip["model"])} · {esc(chip["interface"])}</td><td><a href="{esc(chip["source_url"])}" rel="noreferrer">ASMedia</a> · {esc(catalog["checked_at"])}</td></tr>')
    spec_html = '<table><tbody>' + ''.join(specs) + '</tbody></table>' if specs else ''
    summary = component_summary(result)
    quality_limited = summary['status'] == 'measurement_quality'
    rows = []
    for row in summary['rows']:
        label = text['util_' + row['component']]
        grade = ('<span class="component-rating-stars" role="img" aria-label="' + str(row['level']) + '/5">' + '&#127775;' * row['level'] + '</span>') if row['level'] else esc(text['util_needed'])
        evidence = []
        for d in row['directions']:
            evidence.append(f'{esc(text["util_" + d["direction"]])} · QD16: {number(d["measured"])} / {number(d["bound"])} MB/s = {number(d["ratio"] * 100)}%')
        for ref in row['references']:
            if ref.get('source_url'):
                if quality_limited:
                    evidence.extend(f'{esc(text["util_" + direction])}: {number(ref[direction + "_mb_s"])} MB/s · {esc(text["util_" + ref["basis"]])}'
                                    for direction in ('read', 'write') if ref.get(direction + '_mb_s') is not None)
                evidence.append(f'<a href="{esc(ref["source_url"])}" rel="noreferrer">{esc(ref["source_label"])}</a> · {esc(ref["checked_on"])} · {esc(text["util_" + ref["basis"]])}<br>{esc(ref["conditions"])}')
        if quality_limited and not evidence:
            continue
        grade_cell = '' if quality_limited else f'<td>{grade}</td>'
        rows.append(f'<tr><th>{esc(label)}</th>{grade_cell}<td>{"<br>".join(evidence)}</td></tr>')
    comment = text['util_comment_' + summary['status']]
    if summary['bottleneck']:
        comment = comment.replace('{component}', text['util_name_' + summary['bottleneck']])
    rating_table = f'<table class="performance-ratings"><tbody>{"".join(rows)}</tbody></table>' if rows else ''
    catalog_rows = []
    for item in catalog['items']:
        link = dict(item, lanes=4) if item['kind'] == 'pcie' else item
        fact = link_reference(link)
        catalog_rows.append(f'<tr><th>{esc(fact["label"])}</th><td>{number(fact["mb_s"])} MB/s · {esc(text["link_" + fact["basis"]])}</td><td><a href="{esc(fact["source_url"])}" rel="noreferrer">{esc(text["rating_source"])}</a></td></tr>')
    return (f'<section id="hardware-assessment"><h2>{esc(text["hardware_assessment"])}</h2><p>{esc(text["link_scope"])}</p>{links}{spec_html}'
            f'<h3>{esc(text["util_title"])}</h3>{rating_table}<p class="component-comment">{esc(comment)}</p><p>{esc(text["util_scope"])}</p>'
            f'<details><summary>{esc(text["interface_catalog"])}</summary><p>{esc(text["link_scope"])} · {esc(catalog["checked_at"])}</p><table><tbody>{"".join(catalog_rows)}</tbody></table></details></section>')


def render_user_input(result, language, text):
    """Keep optional conversational additions separate from measured evidence."""
    data = result.get("user_input")
    if data is None:
        return ""
    if not isinstance(data, dict) or not result.get("run_id") or (
        data.get("run_id") != result["run_id"] or data.get("language") != language
    ):
        raise ValueError("User input must match this run_id and report language")
    models = data.get("device_models", {})
    if not isinstance(models, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in models.items()
    ):
        raise ValueError("User device models must map nonempty labels to model names")
    parts = []
    if models:
        rows = "".join(f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in models.items())
        parts.append(f'<h3>{esc(text["user_models"])}</h3><table><tbody>{rows}</tbody></table>')
    for key in ("test_notes", "self_analysis"):
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ValueError("User notes and analysis must be text")
        if value.strip():
            parts.append(f'<h3>{esc(text["user_" + key])}</h3><p class="user-text">{esc(value)}</p>')
    if not parts:
        return ""
    return '<section id="user-input"><h2>' + esc(text["user_input"]) + '</h2>' + "".join(parts) + '</section>'


def render_evaluation(result, language):
    """Render run-bound agent judgments, never infer product quality from speed."""
    evaluation = result.get("evaluation")
    if evaluation is None:
        return ""
    if not isinstance(evaluation, dict) or not result.get("run_id") or (
        evaluation.get("run_id") != result["run_id"]
        or evaluation.get("language") != language
    ):
        raise ValueError("Evaluation must match this run_id and report language")
    sections = evaluation.get("sections")
    if not isinstance(sections, list) or len(sections) != 3:
        raise ValueError("Evaluation requires bottleneck, upgrades and anomalies sections")
    rendered = []
    for section, kind in zip(sections, ("bottleneck", "upgrades", "anomalies")):
        if not isinstance(section, dict) or section.get("kind") != kind:
            raise ValueError("Invalid evaluation section order")
        title, paragraphs = section.get("title"), section.get("paragraphs")
        if not isinstance(title, str) or not title.strip() or not isinstance(paragraphs, list) or not paragraphs or not all(isinstance(p, str) and p.strip() for p in paragraphs):
            raise ValueError("Evaluation needs a title and nonempty text paragraphs")
        links = []
        sources = section.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("Evaluation sources must be a list")
        for source in sources:
            if not isinstance(source, dict) or not all(isinstance(source.get(k), str) and source[k].strip() for k in ("url", "label", "checked_at")):
                raise ValueError("Evaluation source needs URL, label and checked_at")
            url = urlsplit(source["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Evaluation sources must use public HTTPS URLs")
            links.append(f'<a href="{esc(source["url"])}" rel="noreferrer">{esc(source["label"])}</a> ({esc(source["checked_at"])})')
        body = "".join(f"<p>{esc(p)}</p>" for p in paragraphs)
        citations = '<p class="muted">' + " · ".join(links) + "</p>" if links else ""
        rendered.append(f'<section id="evaluation-{kind}"><h2>{esc(title)}</h2>{body}{citations}</section>')
    return "".join(rendered)


def chart(points, text):
    samples = [
        p
        for p in points
        if isinstance(p.get("seconds"), (int, float))
        and isinstance(p.get("mb_s"), (int, float))
        and math.isfinite(p["seconds"])
        and math.isfinite(p["mb_s"])
        and p["seconds"] >= 0
        and p["mb_s"] >= 0
    ]
    if not samples:
        return f"<p>{esc(text['empty'])}</p>"
    max_x = max(1, max(p["seconds"] for p in samples))
    max_y = max(1, max(p["mb_s"] for p in samples))
    dots = " ".join(
        f"{55 + p['seconds'] / max_x * 895:.1f},{210 - p['mb_s'] / max_y * 180:.1f}"
        for p in samples
    )
    return (
        f'<svg viewBox="0 0 980 260" role="img" aria-label="{esc(text["series"])}">'
        '<path d="M55 25V210H950" fill="none" stroke="#8296ab"/>'
        f'<polyline points="{dots}" fill="none" stroke="#1d65a6" stroke-width="2.5"/>'
        f'<text x="0" y="25" font-size="22">{max_y:.0f}</text><text x="35" y="215" font-size="22">0</text>'
        f'<text x="900" y="238" font-size="22">{max_x:.0f} s</text></svg>'
        f'<p class="legend">{esc(text["seconds"])} → · MB/s ↑</p>'
    )


def export_pdf(source, destination):
    candidates = [
        os.environ.get("DISKBENCH_BROWSER"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
            / "Google/Chrome/Application/chrome.exe"
        ),
    ]
    browser = next((x for x in candidates if x and Path(x).is_file()), None)
    if not browser:
        return None, "No existing Chromium browser found"
    profile = tempfile.mkdtemp(prefix="diskbench-print-")
    staged = destination.with_name(f".report-{uuid.uuid4().hex}.pdf")
    args = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile}",
        f"--print-to-pdf={staged}",
        source.as_uri(),
    ]
    try:
        completed = subprocess.run(args, capture_output=True, timeout=60)
        if completed.returncode or not staged.is_file() or staged.stat().st_size < 100:
            return None, completed.stderr.decode(errors="replace")[-1000:]
        if not staged.read_bytes().startswith(b"%PDF-"):
            return None, "Browser output is not PDF"
        staged.replace(destination)
        return str(destination), None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def render_peak_comparison(result, text):
    refs = validate_reference_data(result.get("references") or {"items": []})
    limits = connection_limits(result, refs)
    host = next((p for p in limits["parts"] if p["component"] == "computer"), {}) if limits else {}
    rows = []
    for case in result.get("cases", []):
        if case.get("status") != "completed" or case.get("name") not in (
            "seq_read", "seq_write", "seq_read_q", "seq_write_q", "sustained"
        ):
            continue
        direction = "read" if case["name"] in ("seq_read", "seq_read_q") else "write"
        samples = [p["mb_s"] for p in case.get("series", [])
                   if p.get("direction") == direction and isinstance(p.get("mb_s"), (int, float))
                   and math.isfinite(p["mb_s"]) and p["mb_s"] >= 0]
        peak = max(samples) if samples else None
        average = case.get("metrics", {}).get(direction, {}).get("mb_s")
        cap = host.get(direction + "_mb_s")
        relation = text["reference_not_measured"]
        if peak is not None and cap:
            label = "peak_below" if peak < cap else "peak_review"
            relation = text[label].format(percent=number(peak / cap * 100))
        cells = [text.get(case["name"], case["name"]), number(peak), number(average),
                 str(len(samples)), number(cap), relation]
        rows.append('<tr>' + ''.join('<td>' + esc(c) + '</td>' for c in cells) + '</tr>')
    if not rows:
        return ""
    heads = ''.join('<th>' + esc(text[k]) + '</th>' for k in (
        "workload", "peak_sample", "peak_average", "peak_count", "peak_host", "peak_relation"))
    return (f'<section id="peak-comparison"><h2>{esc(text["peak_title"])}</h2>'
            f'<p>{esc(text["peak_note"])}</p><div class="table-wrap"><table><thead><tr>{heads}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


def render_connection_reference(result, references, text):
    limits = connection_limits(result, references)
    if limits is None:
        return "", []
    comparisons = []
    for direction in ("read", "write"):
        cap = limits[direction + "_mb_s"]
        if cap is None:
            continue
        names = (
            ("seq_read_q", "seq_read")
            if direction == "read"
            else ("seq_write_q", "seq_write")
        )
        case = next(
            (
                c
                for name in names
                for c in result.get("cases", [])
                if c["name"] == name and c.get("status") == "completed"
            ),
            {},
        )
        measured = case.get("metrics", {}).get(direction, {}).get("mb_s")
        actual = (
            number(measured) + " MB/s"
            if measured is not None
            else text["reference_not_measured"]
        )
        gap = (
            number(cap - measured) + " MB/s"
            if measured is not None
            else text["reference_not_measured"]
        )
        comparisons.append(
            f'<tr data-path-direction="{direction}" data-limit-mb-s="{cap}">'
            f'<th scope="row">{esc(text[direction])}</th><td dir="ltr">{number(cap)} MB/s</td>'
            f'<td>{esc(actual)}<small>{esc(text.get(case.get("name"), ""))}</small></td><td dir="ltr">{esc(gap)}</td></tr>'
        )
    component_rows, used = [], []
    for part in limits["parts"]:
        controller_fallback = (
            part["component"] == "enclosure" and limits["enclosure_uses_controller"]
        )
        rate_part = (
            next(p for p in limits["parts"] if p["component"] == "controller")
            if controller_fallback
            else part
        )
        rates = []
        for direction in ("read", "write"):
            value = rate_part[direction + "_mb_s"]
            rates.append(
                esc(text[direction])
                + " "
                + (
                    f"<bdi>{number(value)} MB/s</bdi>"
                    if value is not None
                    else esc(text["reference_missing"])
                )
            )
        roles = []
        for direction in ("read", "write"):
            value = part[direction + "_mb_s"]
            limit = limits[direction + "_mb_s"]
            if value is not None and limit is not None and math.isclose(value, limit):
                roles.append(text[direction])
        role = (
            text["lowest_known_limit"].format(direction=" / ".join(roles))
            if roles
            else text["reference_headroom"]
            if part["sources"]
            else text["reference_unknown_constraint"]
        )
        if controller_fallback:
            role = text["controller_fallback"]
        source_links = []
        for item in part["sources"]:
            used.append(item)
            original = (
                (number(item["value"]) + " Gb/s · ") if item["unit"] == "Gb/s" else ""
            )
            link = (
                f'<a href="{esc(item["source_url"])}" target="_blank" rel="noopener noreferrer">{esc(item["source_label"])}</a>'
                f"<small>{esc(original + item['conditions'])}</small>"
            )
            if link not in source_links:
                source_links.append(link)
        component_rows.append(
            f'<tr data-path-component="{part["component"]}"><th scope="row">'
            f"{esc(text['path_' + part['component']])}<small>{esc(part['model'] or text['source_unknown'])}</small></th>"
            f"<td>{'<br>'.join(rates)}</td><td>{esc(role)}</td><td>{' · '.join(source_links) if source_links else esc(text['controller_source_below'] if controller_fallback else text['reference_missing'])}</td></tr>"
        )
    headers = "".join(
        f"<th>{esc(text[key])}</th>"
        for key in ("reference_item", "path_ceiling", "reference_actual", "path_gap")
    )
    part_headers = "".join(
        f"<th>{esc(text[key])}</th>"
        for key in (
            "path_component",
            "normalized_limit",
            "path_role",
            "reference_source",
        )
    )
    partial = bool(limits["missing_read"] or limits["missing_write"])
    html = (
        f'<div id="connection-reference" data-complete="{str(not partial).lower()}">'
        f'<p>{esc(text["path_minimum_rule"])}</p><div class="table-wrap"><table class="reference-table"><thead><tr>{headers}</tr></thead>'
        f"<tbody>{''.join(comparisons)}</tbody></table></div>"
        f'<p class="notice">{esc(text["path_partial"] if partial else text["path_rated_only"])}</p>'
        f'<h3>{esc(text["path_breakdown"])}</h3><div class="table-wrap"><table class="reference-table"><thead><tr>{part_headers}</tr></thead>'
        f'<tbody>{"".join(component_rows)}</tbody></table></div><p class="muted">{esc(text["path_gap_explanation"])}</p></div>'
    )
    return html, used


def render_references(result, text, assessment):
    references = validate_reference_data(result.get("references") or {"items": []})
    current_drive = result.get("hardware", {}).get("drive", {}).get("model")
    normalize = lambda value: "".join(c.lower() for c in (value or "") if c.isalnum())
    if references["items"] and (
        not current_drive
        or normalize(references["for_drive_model"]) != normalize(current_drive)
    ):
        return ""  # The entire packet belongs to another user's selected drive.
    chain_html, used_items = render_connection_reference(result, references, text)
    rows = []
    for item in references.get("items", []):
        if item in used_items:
            continue
        relation = item["relationship"]
        current = result.get("hardware", {}).get(item["component"], {}).get("model")
        if relation == "device" and (
            not current or normalize(current) != normalize(item["model"])
        ):
            continue  # A report for another device must not reuse these reference facts.
        measured = None
        metric = item["metric"]
        if relation != "candidate":
            if metric in ("cache_capacity_estimate_bytes", "post_cache_mb_s"):
                measured = assessment.get(metric)
                if measured is not None and metric == "cache_capacity_estimate_bytes":
                    measured /= 1_073_741_824
            else:
                direction = "read" if "read" in metric else "write"
                case = next(
                    (
                        c
                        for c in result.get("cases", [])
                        if c["name"] == metric and c.get("status") == "completed"
                    ),
                    {},
                )
                measured = case.get("metrics", {}).get(direction, {}).get("mb_s")
        label_key = {
            "cache_capacity_estimate_bytes": "cache_capacity",
            "post_cache_mb_s": "post_cache_speed",
            "interface_gbps": "interface_limit",
        }.get(metric, metric)
        label = text[label_key]
        actual = (
            number(measured) + " " + item["unit"]
            if measured is not None
            else text["reference_not_measured"]
        )
        value = (
            ("≈ " if item.get("approximate") else "")
            + number(item["value"])
            + " "
            + item["unit"]
        )
        rows.append(
            f'<tr data-reference-metric="{esc(metric)}" data-relationship="{esc(relation)}">'
            f'<th scope="row">{esc(label)}<small>{esc(item["model"])}</small>'
            f"<small>{esc(text['reference_' + relation])}</small></th>"
            f'<td dir="ltr">{esc(actual)}</td><td dir="ltr">{esc(value)}</td>'
            f'<td><a href="{esc(item["source_url"])}" target="_blank" rel="noopener noreferrer">'
            f"{esc(item['source_label'])}</a> · {esc(text['reference_' + item['source_kind']])}"
            f"<small>{esc(item['conditions'])}</small></td></tr>"
        )
    if not rows and not chain_html:
        return ""
    checked = references.get("checked_at")
    cache_info = result.get("reference_cache", {})
    freshness = (
        (
            f'<p class="muted">{esc(text["reference_checked"])} {esc(checked)}'
            + (
                f" · {esc(text['reference_stale'])}"
                if cache_info.get("status") == "stale"
                else ""
            )
            + "</p>"
        )
        if checked
        else ""
    )
    heads = "".join(
        f"<th>{esc(text[k])}</th>"
        for k in (
            "reference_item",
            "reference_actual",
            "reference_value",
            "reference_source",
        )
    )
    missing = "".join(
        f'<p class="muted">{esc(note)}</p>' for note in references.get("missing", [])
    )
    return (
        f'<section id="references"><h2>{esc(text["references"])}</h2>'
        f"{freshness}{chain_html}"
        + (
            (
                f'<details class="additional-references"><summary>{esc(text["other_references"])}</summary>'
                f'<p class="muted">{esc(text["reference_conditions"])}</p>'
                f'<div class="table-wrap"><table class="reference-table"><thead><tr>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>'
            )
            if rows
            else ""
        )
        + f"{missing}</section>"
    )


def render_cache_reference(result, assessment, text):
    """Expose same-drive source facts beside the measured cache without replacing it."""
    refs = validate_reference_data(result.get("references") or {"items": []})
    normalize = lambda value: "".join(c.lower() for c in (value or "") if c.isalnum())
    model = result.get("hardware", {}).get("drive", {}).get("model")
    if not model or normalize(model) != normalize(refs.get("for_drive_model")):
        return ""
    items = [
        item
        for item in refs["items"]
        if item["component"] == "drive"
        and item["relationship"] == "device"
        and normalize(item["model"]) == normalize(model)
        and item["metric"] == "cache_capacity_estimate_bytes"
    ]
    if not items:
        return ""
    values = [item["value"] for item in items]
    low, high = min(values), max(values)
    value = f"{low:,.0f}" if low == high else f"{low:,.0f}–{high:,.0f}"
    sources = " · ".join(
        dict.fromkeys(
            f'<a href="{esc(item["source_url"])}" target="_blank" rel="noopener noreferrer">{esc(item["source_label"])}</a>'
            for item in items
        )
    )
    note = esc(text["cache_online_reference"]).format(value=value) + " · " + sources
    note += f" <span>{esc(text['reference_checked'])} {esc(refs.get('checked_at') or text['source_unknown'])}</span>"
    if result.get("reference_cache", {}).get("status") == "stale":
        note += " · " + esc(text["reference_stale"])
    measured = assessment.get("cache_capacity_estimate_bytes")
    warning = ""
    if (
        assessment.get("status") == "observed_drop_stable"
        and isinstance(measured, (int, float))
        and not isinstance(measured, bool)
        and math.isfinite(measured)
        and measured > 0
    ):
        actual = measured / 1_073_741_824
        # A triage threshold, not an SSD specification or a device-failure verdict.
        if actual < low * 0.7 or actual > high * 1.3:
            nearest = low if actual < low else high
            difference = abs(actual / nearest - 1) * 100
            warning = f'<p class="notice" data-cache-reference-warning="review">{esc(text["cache_difference_review"]).format(percent=number(difference))}</p>'
    return f'<p class="cache-reference">{note}</p>' + warning


def render_readable_results(result, text):
    cases = {
        c["name"]: c for c in result.get("cases", []) if c.get("status") == "completed"
    }

    def measured(name, direction, field="mb_s"):
        value = cases.get(name, {}).get("metrics", {}).get(direction, {}).get(field)
        return (
            value
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            else None
        )

    def quantity(value, unit):
        return (
            f"<bdi>{number(value)} {esc(unit)}</bdi>"
            if value is not None
            else esc(text["reference_not_measured"])
        )

    sections = []
    read, write = measured("seq_read", "read"), measured("seq_write", "write")
    if read is not None or write is not None:
        detail = esc(text["large_file_result"]).format(
            read=quantity(read, "MB/s"), write=quantity(write, "MB/s")
        )
        if read is not None and write is not None:
            detail += " " + esc(text["file_time_example"]).format(
                read_time=quantity(10_000 / read, "s"),
                write_time=quantity(10_000 / write, "s"),
            )
        sections.append(
            f"<div><h3>{esc(text['large_file_use'])}</h3><p>{detail}</p></div>"
        )
    random_read, random_write = (
        measured("rand_read", "read"),
        measured("rand_write", "write"),
    )
    if random_read is not None or random_write is not None:
        detail = esc(text["small_file_result"]).format(
            read=quantity(random_read, "MB/s"), write=quantity(random_write, "MB/s")
        )
        latency = (
            cases.get("rand_read", {})
            .get("metrics", {})
            .get("read", {})
            .get("latency_us", {})
            .get("95")
        )
        if isinstance(latency, (int, float)) and math.isfinite(latency) and latency > 0:
            detail += " " + esc(text["latency_example"]).format(
                latency=quantity(math.ceil(latency / 10) / 100, "ms")
            )
        sections.append(
            f"<div><h3>{esc(text['small_file_use'])}</h3><p>{detail}</p></div>"
        )
    rows = []
    for name, direction in [
        ("seq_read", "read"),
        ("seq_write", "write"),
        ("rand_read", "read"),
        ("rand_write", "write"),
    ]:
        single, queued = cases.get(name, {}), cases.get(name + "_q", {})
        a, b = measured(name, direction), measured(name + "_q", direction)
        if (
            a is None
            or b is None
            or not single.get("bs")
            or single.get("bs") != queued.get("bs")
            or single.get("qd") != 1
            or queued.get("qd") != 16
        ):
            continue
        label = text[name].split(" · ")[0]
        rows.append(
            f'<tr data-comparison="{esc(name)}"><th scope="row">{esc(label)}</th>'
            f"<td>{quantity(a, 'MB/s')}</td><td>{quantity(b, 'MB/s')}</td>"
            f"<td><bdi>{number(b / a)}×</bdi></td></tr>"
        )
    comparison = ""
    if rows:
        heads = "".join(
            f"<th>{esc(text[key])}</th>"
            for key in (
                "comparison_workload",
                "single_request",
                "queued_requests",
                "throughput_ratio",
            )
        )
        comparison = (
            f"<h3>{esc(text['same_run_comparison'])}</h3>"
            f'<div class="table-wrap"><table class="comparison-table"><thead><tr>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<p class="muted">{esc(text["comparison_limits"])}</p>'
        )
    if not sections and not comparison:
        return ""
    return (
        f'<section id="reading-results"><h2>{esc(text["reading_results"])}</h2>'
        f'<div class="usage-explanation">{"".join(sections)}</div>'
        f'<p class="muted">{esc(text["scenario_limits"])}</p>{comparison}</section>'
    )


def render_diagnostics(result, text):
    cases = result.get("cases", [])
    mapping = [c for c in cases if c.get("label_key") == "mapping_read"]
    integrity = {c["name"]: c for c in cases if c["name"].startswith("integrity_")}
    recovery = next((c for c in cases if c["name"] == "recovery_write"), None)
    if not (mapping or integrity or recovery):
        return ""
    parts = []
    if mapping:
        parts.append(f'<p>{esc(text["mapping_note"])}</p>')
        rows = []
        for case in mapping:
            m = case.get("metrics", {}).get("read", {})
            rows.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in (
                job_label(case, text), text.get(case["status"], case["status"]),
                number(m.get("mb_s")), number(m.get("iops")),
                number(m.get("latency_us", {}).get("99")))) + '</tr>')
        headers = (text["workload"], text["status"], "MB/s", "IOPS", "p99 (µs)")
        parts.append('<table><thead><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in headers)
                     + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')
    if integrity:
        write = integrity.get("integrity_write", {})
        read = integrity.get("integrity_read", {})
        written = write.get("metrics", {}).get("write", {}).get("bytes", 0)
        verified = read.get("metrics", {}).get("read", {}).get("bytes", 0)
        planned = result.get("plan", {}).get("file_bytes", 0)
        passed = all(c.get("status") == "completed" for c in (write, read)) and planned > 0 and min(written, verified) >= planned
        # On a failed read, processed byte count is not proof of verified coverage.
        verified_text = number(verified / 1073741824) if passed else "—"
        parts.append(f'<p>{esc(text["integrity_coverage"])}: {number(written / 1073741824)} / {verified_text} / {number(planned / 1073741824)} GiB</p>')
        parts.append(f'<p>{esc(text["integrity_pass"] if passed else text["integrity_incomplete"])}</p>')
        parts.append(chart(write.get('series', []), text))
    if recovery:
        summary = result.get("sustained_summary", {})
        speed = recovery.get("metrics", {}).get("write", {}).get("mb_s") if recovery.get("status") == "completed" else None
        values = ' / '.join(number(x) for x in (summary.get("initial_mb_s"), summary.get("tail_mean_mb_s"), speed))
        parts.append(f'<p>{esc(text["recovery_comparison"])}: {values}</p><p>{esc(text["idle_seconds"])}: {esc(recovery.get("idle_seconds"))}</p><p>{esc(text["recovery_note"])}</p>')
    temperatures = [t["celsius"] for c in cases for t in c.get("temperatures", [])
                    if isinstance(t.get("celsius"), (int, float))]
    if temperatures:
        parts.append(f'<p>{esc(text["temperature_range"])}: {number(min(temperatures))}–{number(max(temperatures))}</p>')
    return '<section id="diagnostics"><h2>' + esc(text["diagnostics"]) + '</h2>' + ''.join(parts) + '</section>'


def render_report(result, output, language, text, requested_format="html", comparison=None, community_messages=None, show_computer_model=False):
    community_html = ""
    if comparison is not None:
        from community_report import validate_context, report_messages, summary_html
        validate_context(comparison, result)
        comparison_text = report_messages(language, community_messages)
        community_html = '<section id="community-comparison"><h2>'+esc(comparison_text["title"])+"</h2>"+summary_html(comparison,comparison_text)+f'<p><a href="comparison.html">{esc(comparison_text["title"])} →</a></p><p>{esc(comparison_text["unverified"])}</p></section>'
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = result.get("cases", [])
    text = dict(text)
    text.update({c["name"]: job_label(c, text) for c in cases})
    completed = [c for c in cases if c.get("status") == "completed"]
    target = result.get("target", {})

    def card(label, value, metric, unit="MB/s", state="measured", note=""):
        value_class = ' class="value-long"' if state == "measured" and len(str(value)) > 14 else ""
        return (
            f'<div class="card" data-metric="{esc(metric)}" data-state="{esc(state)}"><dt>{esc(label)}</dt>'
            f'<dd{value_class}>{esc(value)}<span class="unit">{esc(unit)}</span>{note}</dd></div>'
        )

    project = json.loads((ASSETS / "project.json").read_text(encoding="utf-8"))
    from agent_usage import agent_usage_html
    repository_url = project["repository_url"]
    if not repository_url.startswith("https://github.com/"):
        raise ValueError("Project repository URL must use HTTPS on github.com")
    for key in ("website_url", "translation_url"):
        if not project[key].startswith("https://"):
            raise ValueError("Project website links must use HTTPS")
    project_html = (
        f'<div class="project-name"><bdi>{esc(project["name"])}</bdi></div>'
        f'<p>{esc(text["skill_intro"])}</p>'
        f'<nav class="project-links"><a href="{esc(repository_url)}" target="_blank" rel="noopener noreferrer">GitHub <span aria-hidden="true">↗</span></a>'
        f'<a href="{esc(project["website_url"])}" target="_blank" rel="noopener noreferrer">'
        f'{esc(text["website_label"])} · <bdi>anz.tv</bdi> <span aria-hidden="true">↗</span></a></nav>'
    )
    promotion_html = (
        f'<div class="promotion"><a class="promotion-link" href="{esc(project["translation_url"])}" target="_blank" rel="noopener noreferrer">'
        f'<bdi>translate.anz.tv</bdi><span aria-hidden="true">↗</span></a>'
        f'<p>{esc(text["translation_promo"])}</p></div>'
    )
    hardware = result.get("hardware", {})
    hardware_rows = []
    for key, label in (
        ("computer", "computer_model"),
        ("drive", "drive_model"),
        ("enclosure", "enclosure_model"),
        ("controller", "controller_model"),
    ):
        if key == "computer" and not show_computer_model:
            continue
        item = hardware.get(key, {})
        model = (item.get("matching_model") or item.get("model")) if key in ("computer", "drive") else item.get("model")
        if key == "controller" and not model:
            candidates = item.get("candidates") or hardware.get("enclosure", {}).get(
                "bridge_candidates", []
            )
            if candidates:
                model = " / ".join(candidates) + " · " + text["controller_candidate"]
        source = item.get("source", "unavailable")
        source_text = (
            text["source_user"]
            if source == "user"
            else text["source_unknown"]
            if source == "unavailable"
            else text["source_reference"] if source == "reference"
            else text["source_system"]
        )
        hardware_rows.append(
            f'<tr><th scope="row">{esc(text[label])}</th><td>{esc(model or text["source_unknown"])}</td><td class="muted">{esc(source_text) if item.get("model") else ""}</td></tr>'
        )
    if hardware.get("access_interface") is not None:
        from interface_types import confirmed_interface
        interface = hardware["access_interface"]
        if not isinstance(interface, dict) or interface.get("source") != "user":
            raise ValueError("Invalid user-confirmed interface")
        interface = confirmed_interface(interface.get("name"))
        hardware_rows.append(f'<tr><th scope="row">{esc(text["protocol"])}</th><td>{esc(interface["name"])}</td><td>{esc(text["source_user"])}</td></tr>')
    hardware_html = (
        '<section id="hardware"><h2>'
        + esc(text["hardware"])
        + "</h2><table><tbody>"
        + "".join(hardware_rows)
        + "</tbody></table>"
    )
    hardware_html += "</section>"
    sustained_case = next((c for c in cases if c["name"] == "sustained"), {})
    assessment = result.get("cache_assessment") or cache_assessment(
        sustained_case.get("series", [])
    )
    cards = []
    for name, direction in [
        ("seq_read", "read"),
        ("seq_write", "write"),
        ("rand_read", "read"),
        ("rand_write", "write"),
    ]:
        case = next((c for c in completed if c["name"] == name), {})
        value = case.get("metrics", {}).get(direction, {}).get("mb_s")
        if value is not None:
            cards.append(card(text[name], number(value), name))
    short_cards, cards = cards, []
    summary = result.get("sustained_summary") or sustained_summary([])
    summary_labels = [
        ("initial_mb_s", "initial"),
        ("tail_mean_mb_s", "tail_mean"),
        ("tail_median_mb_s", "tail_median"),
        ("minimum_5s_mb_s", "minimum_5s"),
        ("drop_percent", "drop"),
        ("change_seconds", "change_time"),
    ]
    if summary.get("tail_mean_mb_s") is not None:
        cards.append(
            card(
                text["tail_mean"] + "*",
                number(summary["tail_mean_mb_s"]),
                "tail_mean_mb_s",
            )
        )
    cache_bytes = assessment.get("cache_capacity_estimate_bytes")
    if cache_bytes is not None or result.get("tool", {}).get("method_version") != "drivespeed-auto-full-v1":
        cards.insert(
            min(2, len(cards)),
            card(
                text["cache_capacity"] + "*",
                number(cache_bytes / 1_073_741_824)
                if cache_bytes is not None
                else text["cache_pending"],
                "cache_capacity_estimate_bytes",
                "GiB" if cache_bytes is not None else "",
                "measured_estimate" if cache_bytes is not None else "not_measured",
                note=render_cache_reference(result, assessment, text),
            ),
        )
    show_status = any(case.get("status") != "completed" for case in cases)
    show_reason = any(case.get("reason") for case in cases)
    rows = []
    for case in cases:
        metric = case.get("metrics", {})
        for direction in ("read", "write"):
            m = metric.get(direction, {})
            if (
                not m.get("bytes")
                and direction == "write"
                and metric.get("read", {}).get("bytes")
            ):
                continue
            if (
                not m.get("bytes")
                and direction == "read"
                and metric.get("write", {}).get("bytes")
            ):
                continue
            lat = m.get("latency_us", {})
            cells = [
                text.get(case["name"], case["name"]),
                text[direction],
                number(m.get("mb_s")),
                number(m.get("iops")),
                number(lat.get("mean")),
                " / ".join(number(lat.get(p)) for p in ("50", "95", "99")),
            ]
            if show_status:
                cells.insert(1, text.get(case.get("status"), case.get("status")))
            if show_reason:
                cells.append(
                    text.get(case["reason"], text["unknown"])
                    if case.get("reason")
                    else ""
                )
            numeric_start = 3 if show_status else 2
            rows.append(
                "<tr>"
                + "".join(
                    f'<td dir="ltr">{esc(value)}</td>'
                    if numeric_start <= index < numeric_start + 4
                    else f"<td>{esc(value)}</td>"
                    for index, value in enumerate(cells)
                )
                + "</tr>"
            )
    heads = [
        text["workload"],
        text["read"] + " / " + text["write"],
        "MB/s",
        "IOPS",
        text["mean_latency"],
        text["latency"],
    ]
    if show_status:
        heads.insert(1, text["status"])
    if show_reason:
        heads.append(text["failure_reason"])
    table = (
        '<table id="measurements"><thead><tr>'
        + "".join(f"<th>{esc(x)}</th>" for x in heads)
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    lines = []
    for key, label in summary_labels:
        unit = (
            "%"
            if key == "drop_percent"
            else (
                "s"
                if key == "change_seconds"
                else ("bytes" if key == "bytes_at_change" else "MB/s")
            )
        )
        if summary.get(key) is None:
            continue
        lines.append(
            f'<div class="metric-line" data-metric="{esc(key)}"><dt>{esc(text[label])}</dt><dd>{number(summary.get(key))} {unit}</dd></div>'
        )
    reference_html = render_references(result, text, assessment)
    readable_html = render_readable_results(result, text)
    metric_guide = "".join(
        f"<div><dt>{esc(text[label])}</dt><dd>{esc(text[explanation])}</dd></div>"
        for label, explanation in [
            ("guide_speed", "guide_speed_text"),
            ("guide_iops", "guide_iops_text"),
            ("guide_latency", "guide_latency_text"),
            ("guide_queue", "guide_queue_text"),
        ]
    )
    cache_state = assessment.get("status")
    cache_label = (
        "cache_stable"
        if cache_state == "observed_drop_stable"
        else "cache_no_drop"
        if cache_state == "no_drop_observed"
        else "cache_waiting"
        if cache_state in ("post_drop_incomplete", "post_drop_unstable")
        else "cache_sampling"
    )
    cache_metrics = []
    for key, label, unit, divisor in [
        ("cache_capacity_estimate_bytes", "cache_capacity", "GiB", 1_073_741_824),
        ("post_cache_mb_s", "post_cache_speed", "MB/s", 1),
    ]:
        value = assessment.get(key)
        if value is not None:
            cache_metrics.append(
                f'<div class="metric-line" data-metric="{key}"><dt>{esc(text[label])}</dt><dd>{number(value / divisor)} {unit}</dd></div>'
            )
    written = sustained_case.get("metrics", {}).get("write", {}).get("bytes")
    runtime_ms = sustained_case.get("metrics", {}).get("runtime_ms")
    duration = (
        runtime_ms / 1000 if runtime_ms is not None else summary.get("covered_seconds")
    )
    if written is not None:
        cache_metrics.append(
            f'<div class="metric-line"><dt>{esc(text["cache_write"])}</dt><dd>{number(written / 1_073_741_824)} GiB</dd></div>'
        )
    if duration is not None:
        cache_metrics.append(
            f'<div class="metric-line"><dt>{esc(text["cache_time"])}</dt><dd>{number(duration)} s</dd></div>'
        )
    cache_html = f'<div id="cache-assessment" data-status="{esc(cache_state)}"><p class="notice">{esc(text[cache_label])}</p><dl>{"".join(cache_metrics)}</dl></div>'
    metadata = []
    quality = result.get("measurement_quality", {}).get("status", "unknown")
    metadata.append(f'<div class="metric-line metadata-quality"><dt>{esc(text["measurement_quality"])}</dt><dd>{esc(text.get("quality_" + quality, text["quality_unknown"]))}</dd></div>')
    for key, label in [
        ("total_bytes", "capacity"),
        ("free_bytes", "free_space"),
        ("temperature_c", "initial_temp"),
    ]:
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text[label])}</dt><dd>{number(target.get(key))}</dd></div>'
        )
    temperatures = [
        point["celsius"]
        for case in cases
        for point in case.get("temperatures", [])
        if point.get("celsius") is not None
    ]
    metadata.append(
        f'<div class="metric-line"><dt>{esc(text["peak_temp"])}</dt><dd>{number(max(temperatures) if temperatures else None)}</dd></div>'
    )
    metadata.append(
        f'<div class="metric-line"><dt>{esc(text["smart"])}</dt><dd>{esc(target.get("smart") or text["unknown"])}</dd></div>'
    )
    for key, label in [
        ("filesystem", "filesystem"),
        ("protocol", "protocol"),
    ]:
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text[label])}</dt><dd>{esc(target.get(key) or text["unknown"])}</dd></div>'
        )
    config = result.get("plan", {})
    for label, value in [
        ("file_size", config.get("file_bytes")),
        ("reserve", config.get("reserve_bytes")),
        ("max_duration", config.get("max_runtime_seconds")),
        ("write_budget", config.get("max_write_bytes", config.get("write_budget_bytes", config.get("write_budget")))),
        ("actual_write", result.get("actual_write_bytes")),
        ("duration", result.get("elapsed_seconds")),
    ]:
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text[label])}</dt><dd>{number(value)}</dd></div>'
        )
    # Explicit allowlist: never dump arbitrary raw environment, error strings, commands or paths into public HTML.
    env = result.get("environment", {})
    public_env = {"os_family": env.get("os_family"), "os_version": env.get("os_version"),
                  "architecture": env.get("architecture"), "ioengine": env.get("engine")}
    public_env["fio"] = env.get("fio", {}).get("version") if env.get("fio") else None
    environment_html = "".join(f'<div class="metric-line"><dt>{esc(text[key if key != "fio" else "fio_version"])}</dt><dd>{esc(value or text["unknown"])}</dd></div>'
        for key,value in public_env.items())
    depth_rows = "".join(
        "<tr><td>"
        + esc(text.get(c["name"], c["name"]))
        + '</td><td dir="ltr">'
        + esc(
            ", ".join(
                f"{k}: {v:g}%"
                for k, v in c.get("metrics", {}).get("achieved_depth", {}).items()
                if isinstance(v, (int, float)) and v
            )
        )
        + "</td></tr>"
        for c in cases
    )
    depth_table = (
        "<table><thead><tr><th>"
        + esc(text["workload"])
        + "</th><th>"
        + esc(text["depth"])
        + "</th></tr></thead><tbody>"
        + depth_rows
        + "</tbody></table>"
    )
    config_head = "".join(
        "<th>" + esc(text[k]) + "</th>"
        for k in ("workload", "block_size", "requested_qd", "io_limit", "time_limit", "duration")
    )
    config_rows = "".join(
        "<tr><td>"
        + esc(text.get(c["name"], c["name"]))
        + "</td>"
        + "".join(
            "<td>" + esc(number(c.get(k))) + "</td>"
            for k in ("bs", "qd", "io_bytes", "runtime")
        )
        + "<td>"
        + esc(number(
            c["metrics"]["runtime_ms"] / 1000
            if c.get("metrics", {}).get("runtime_ms") is not None else None
        ))
        + "</td></tr>"
        for c in cases
    )
    config_table = (
        "<table><thead><tr>"
        + config_head
        + "</tr></thead><tbody>"
        + config_rows
        + "</tbody></table>"
    )
    # File lifecycle and execution notices belong in the delivery conversation.
    delivery_only = {"trash_note", "retain_note", "cleanup_failed", "cache_incomplete"}
    notes = [
        text.get(key, text["unknown"])
        for key in result.get("notes", []) if key not in delivery_only
    ]
    if result.get("outcome") != "complete" and not (
        cases and all(c.get("status") == "completed" and c.get("metrics_complete") is True for c in cases)
    ):
        notes.append(
            text["partial_write"]
            + ": "
            + number(result.get("accounted_write_bytes"))
            + " bytes"
        )
    if result.get("error"):
        notes.append(text.get(result["error"].get("key"), text["run_failed"]))
    status = text.get(result.get("outcome"), result.get("outcome", ""))
    sample = (
        f'<p class="notice">{esc(text["sample"])}</p>'
        if result.get("synthetic")
        else ""
    )
    series = next((c.get("series", []) for c in cases if c["name"] == "sustained"), [])
    direction = "rtl" if language.split("-")[0] in ("ar", "he", "fa", "ur") else "ltr"
    css = (ASSETS / "report.css").read_text(encoding="utf-8")
    created = result.get("created", "")
    try:
        created_display = (
            datetime.fromisoformat(created).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        )
    except (ValueError, TypeError):
        created_display = created
    cache_method = (
        f"<p>{esc(text['cache_method'])}</p>"
        if any(case.get("cache_test") for case in cases)
        else ""
    )
    long_summary_html = f'''<h2 class="group-title">{esc(text["long_group"])}</h2><p class="group-caption">QD {esc(sustained_case.get("qd", "—"))} · {number(sustained_case.get("metrics", {}).get("runtime_ms", 0) / 1000)} s</p>
{('<dl class="cards long-cards">' + "".join(cards) + '</dl><div class="summary-notes"><p>* ' + esc(text["cache_summary_note"]) + '</p></div>') if cards else '<p class="notice">' + esc(text[cache_label]) + '</p>'}''' if result.get('plan', {}).get('profile') not in ('mapping', 'integrity') else ''
    sustained_html = f'''<section id="sustained"><h2>{esc(text["sustained"])}</h2><p class="muted" id="tail-speed-note">{esc(text["tail_explanation"])}</p><div class="sustained-grid"><div>{chart(series, text)}</div><dl>{"".join(lines)}</dl></div>{cache_html}</section>''' if result.get('plan', {}).get('profile') not in ('mapping', 'integrity') else ''
    drive = hardware.get("drive", {})
    object_line = str(drive.get("matching_model") or drive.get("model") or text["unknown"])+" · "+text.get("scope_"+str(target.get("scope")),text["unknown"])
    document = f'''<!doctype html>
<html lang="{esc(language)}" dir="{direction}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(text["title"])}</title><style>{css}</style></head><body><main>
<header><div><h1>{esc(text["title"])}</h1><p>{esc(object_line)}</p><p class="run-meta"><time datetime="{esc(created)}">{esc(created_display)}</time><span>{esc(status)} · {len(completed)}/{len(cases)}</span></p></div><button type="button" title="{esc(text["print_hint"])}" onclick="window.print()">{esc(text["download"])}</button></header>
{sample}
{('<p class="notice">' + esc(text['core_incomplete']) + '</p>') if result.get('completeness', {}).get('complete') is False else ''}
{('<h2 class="group-title">' + esc(text["short_group"]) + '</h2><p class="group-caption">' + esc(text["short_group_note"]) + '</p><dl class="cards short-cards">' + "".join(short_cards) + '</dl>') if short_cards else ''}
{long_summary_html}
{render_diagnostics(result, text)}
{render_peak_comparison(result, text)}
{hardware_html}
{render_hardware_assessment(result, text)}
{render_evaluation(result, language)}
{render_user_input(result, language, text)}
{community_html}
{readable_html}
{reference_html}
{sustained_html}
<details id="results"><summary>{esc(text["full_results"])}</summary><dl class="metric-guide">{metric_guide}</dl><div class="table-wrap">{table}</div></details>
<details id="environment"><summary>{esc(text["environment"])}</summary><dl class="metadata">{"".join(metadata)}{environment_html}</dl>{depth_table}</details>
<div class="report-ending"><details id="method"><summary>{esc(text["method"])}</summary><p>{esc(text["method_text"])}</p><p>{esc(text["window_method"])}</p><p>{esc(text["sensors_limit"])}</p>{cache_method}<div class="table-wrap">{config_table}</div></details>
{('<aside class="run-notes">' + "".join(f"<p>{esc(n)}</p>" for n in notes) + "</aside>") if notes else ""}
<div class="report-closing">{agent_usage_html()}
<footer><div class="footer-content"><div class="project">{project_html}</div>{promotion_html}</div><p class="privacy">{esc(text["privacy"])} {esc(text["raw"])}</p></footer></div></div>
</main><script>
const collapsedForPrint = [];
window.addEventListener('beforeprint', () => {{
  document.querySelectorAll('details:not([open])').forEach(item => {{
    collapsedForPrint.push(item);
    item.open = true;
  }});
}});
window.addEventListener('afterprint', () => {{
  collapsedForPrint.splice(0).forEach(item => {{ item.open = false; }});
}});
</script></body></html>'''
    source = output / "report.html"
    if source.is_symlink() or (output / "report.pdf").is_symlink():
        raise ValueError("Report files must not be symlinks")
    if not show_computer_model:
        from report_privacy import computer_models, hide_computer_html
        document = hide_computer_html(document, computer_models(result, (text["computer_model"],)))
    source.write_text(document, encoding="utf-8")
    pdf, error = None, None
    if requested_format == "pdf":
        pdf, error = export_pdf(source, output / "report.pdf")
        if not pdf:
            print(text["pdf_failed"], file=sys.stderr)
    artifacts = dict(
        skill_repository=repository_url,
        language=language,
        requested_format=requested_format,
        html=str(source),
        pdf=pdf,
        pdf_error=error,
    )
    if comparison is not None:
        from community_report import render_comparison
        artifacts.update(render_comparison(comparison, result, output, language, text, requested_format, community_messages, show_computer_model))
    return artifacts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--language", default=language_default())
    p.add_argument("--messages")
    p.add_argument("--user-input-file", help="Optional run-bound conversational additions JSON")
    evaluation_input = p.add_mutually_exclusive_group()
    evaluation_input.add_argument("--evaluation-file", help="Run-bound, localized AI evaluation JSON (legacy compatible)")
    evaluation_input.add_argument("--evaluation-stdin", action="store_true", help="Read bounded run-bound evaluation JSON from stdin, without creating a JSON cache file")
    p.add_argument("--comparison-id", help="Offline typed comparison ID; also produce comparison.html/PDF without changing original results")
    p.add_argument("--community-state-dir", help="Private community SQLite directory containing that comparison")
    p.add_argument("--community-messages", help="Complete comparison report translation based on assets/community/report-locales/en.json")
    p.add_argument(
        "--refresh-references", action="store_true", help="Ignore local reference cache"
    )
    p.add_argument(
        "--reference-file",
        help="Agent-researched reference JSON with public source links",
    )
    p.add_argument("--format", choices=["html", "pdf"], default="html")
    p.add_argument("--show-computer-model", action="store_true", help="Display this computer model in local HTML/PDF only; never changes the submission or its hash")
    args = p.parse_args()
    try:
        result = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if result.get("schema_version") != 1 or not isinstance(
            result.get("cases"), list
        ):
            raise ValueError("Unsupported result schema")
        if args.user_input_file:
            result["user_input"] = json.loads(Path(args.user_input_file).read_text(encoding="utf-8"))
        if args.evaluation_file:
            result["evaluation"] = json.loads(Path(args.evaluation_file).read_text(encoding="utf-8"))
        elif args.evaluation_stdin:
            from community_data import parse_json, MAX_BYTES
            if sys.stdin.isatty():
                raise ValueError("Evaluation stdin requires non-interactive input")
            result["evaluation"] = parse_json(sys.stdin.buffer.read(MAX_BYTES+1))
        output = Path(args.output).expanduser().absolute()
        if any(p.is_symlink() for p in (output, *output.parents)):
            raise ValueError("Output path must not contain symlinks")
        if args.reference_file:
            result["references"] = load_reference_data(args.reference_file)
            result["references"]["language"] = args.language
            try:
                result["reference_cache"] = cache_reference_data(
                    result["references"],
                    result.get("hardware", {}),
                    language=args.language,
                )
            except OSError:
                result["reference_cache"] = dict(
                    status="write_failed", from_cache=False
                )
        elif (
            args.refresh_references
            or not result.get("references")
            or result["references"].get("language") not in (None, args.language)
        ):
            cached, info = load_reference_cache(
                result.get("hardware", {}),
                args.refresh_references,
                language=args.language,
            )
            result["references"] = cached or {}
            result["reference_cache"] = info
        apply_hardware_specs(result.setdefault("hardware", {}), result.get("references") or {})
        text = messages(args.language, args.messages)
        comparison = None
        if args.comparison_id:
            from community_store import CommunityStore
            from community_comparison import read_comparison
            with CommunityStore(args.community_state_dir) as store:
                comparison = read_comparison(store,args.comparison_id)
        elif args.community_state_dir or args.community_messages:
            raise ValueError("Community report options require --comparison-id")
        artifacts = render_report(result, output, args.language, text, args.format, comparison, args.community_messages, args.show_computer_model)
        print(json.dumps(artifacts, ensure_ascii=False))
        return 0 if args.format == "html" or (artifacts["pdf"] and (comparison is None or artifacts.get("comparison_pdf"))) else 5
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
