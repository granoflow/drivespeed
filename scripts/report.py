#!/usr/bin/env python3
"""Render trusted measurement numbers into an escaped, offline HTML/PDF report."""

from workloads import AUTO_METHOD, AUTO_METHODS, PEAK_METHOD, QUICK_METHODS
import argparse
import html
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from string import Template
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from progress import ASSETS, language_default, messages
from workloads import sustained_summary, cache_assessment, job_label, collection_status, core_completeness, peak_completeness
from performance_rating import link_reference, component_summary, upgrade_parameters
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



def controller_identity(hardware, text):
    """Preserve identified series separately from unbound discovery clues."""
    controller = hardware.get('controller', {})
    model = controller.get('model')
    if model:
        return model + (' · ' + text['controller_series'] if controller.get('precision') == 'series' else '')
    candidates = controller.get('candidates') or hardware.get('enclosure', {}).get('bridge_candidates') or []
    if len(candidates) == 1:
        return str(candidates[0]) + ' · ' + text['controller_candidate']
    return text['device_unknown']


def external_connection(hardware):
    """Use only the existing accepted access-interface provenance and parser."""
    from interface_types import confirmed_interface
    access = hardware.get('access_interface')
    if access is None:
        return None
    if not isinstance(access, dict) or access.get('source') not in ('user', 'ioreg:target-ancestor'):
        raise ValueError('Invalid access interface')
    return confirmed_interface(access.get('name'))['name']

def device_identity_rows(result, text, refs, show_computer_model, product_profile):
    """Device names and their provenance, retaining private-model display choice."""
    hardware = result.get('hardware', {})
    rows = []
    components = ('computer', 'drive', 'enclosure', 'controller') if show_computer_model else ('drive', 'enclosure', 'controller')
    for component in components:
        item = hardware.get(component, {})
        raw_model = item.get('model')
        model = raw_model or text['device_unknown']
        origin = 'user' if item.get('source') == 'user' else 'reference' if item.get('source') == 'reference' else 'system' if raw_model else 'unavailable'
        origin_label = text['source_unknown'] if origin == 'unavailable' else text['source_' + origin]
        if component == 'controller':
            model = controller_identity(hardware, text)
        if component == 'drive' and product_profile is not None:
            model = product_profile['display_name']
            origin = 'reference'
            origin_label = text['product_resources']
        specs = [spec['interface'] for spec in refs.get('hardware_specs', [])
                 if spec['component'] == component and spec['model'] == model
                 and refs.get('for_drive_model') == hardware.get('drive', {}).get('model')]
        parameters = f'<bdi dir="ltr">{esc(model)}</bdi>'
        if specs:
            parameters += ' · ' + esc(text['device_supported']) + ': ' + esc(' / '.join(specs))
        if origin != 'unavailable':
            parameters += ' · ' + esc(origin_label)
        rows.append(f'<tr><th>{esc(text[component + "_model"])}</th><td data-device-origin="{origin}">{parameters}</td></tr>')
        if component == 'drive' and product_profile is not None:
            original_origin = 'user' if item.get('source') == 'user' else 'system'
            rows.append(f'<tr><th>{esc(text["original_model"])}</th><td data-device-origin="{original_origin}"><bdi dir="ltr">{esc(raw_model)}</bdi> · {esc(text["source_" + original_origin])}</td></tr>')
    return rows


def render_device_parameters(result, text, summary, show_computer_model=False, product_profile=None):
    """Only this device's identities, observed connection and exact official specifications."""
    hardware = result.get('hardware', {})
    refs = validate_reference_data(result.get('references') or {'items': []})
    rows = device_identity_rows(result, text, refs, show_computer_model, product_profile)
    access = external_connection(hardware)
    if access:
        provenance = text['source_user'] if hardware['access_interface']['source'] == 'user' else text['source_system']
        rows.append(f'<tr><th>{esc(text["device_external"])}</th><td><bdi dir="ltr">{esc(access)}</bdi> · {esc(provenance)}</td></tr>')
    for link in hardware.get('links', []):
        if access and link.get('kind') in ('usb', 'thunderbolt'):
            continue
        fact = link_reference(link)
        if fact:
            state = text['device_negotiated'] if link.get('state') == 'negotiated' else text['device_reported']
            label = text['device_internal_segment'] if access and link.get('kind') == 'pcie' else text['device_connection']
            rows.append(f'<tr><th>{esc(label)}</th><td><bdi dir="ltr">{esc(fact["label"])}</bdi> · {esc(state)}</td></tr>')
    if not access and not any(link_reference(link) for link in hardware.get('links', [])):
        rows.append(f'<tr><th>{esc(text["device_connection"])}</th><td>{esc(text["device_unknown"])}</td></tr>')
    limits = connection_limits(result, refs)
    if limits:
        for part in limits['parts']:
            for item in part['sources']:
                label = text['device_supported'] if item['metric'] == 'interface_gbps' else text['device_rated_read' if 'read' in item['metric'] else 'device_rated_write']
                value = f'<bdi dir="ltr">{number(item["value"])} {esc(item["unit"])}</bdi>'
                component = 'device_host' if part['component'] == 'computer' else 'util_name_' + part['component']
                name = text.get(component, text['enclosure_model'])
                rows.append(f'<tr><th>{esc(name)} · {esc(label)}</th>'
                            f'<td>{value} · <a href="{esc(item["source_url"])}" rel="noreferrer">{esc(text["rating_source"])}</a> · <bdi dir="ltr">{esc(refs.get("checked_at", ""))}</bdi></td></tr>')
    advice = upgrade_parameters(result, summary)
    recommendation = ''
    if advice:
        target = (f'<a href="{esc(advice["interface"]["source_url"])}" rel="noreferrer"><bdi dir="ltr">{esc(advice["interface"]["label"])}</bdi></a>') if 'interface' in advice else esc(text['device_target_rates']).format(
            read=f'<bdi dir="ltr">{number(advice["read_mb_s"])} MB/s</bdi>',
            write=f'<bdi dir="ltr">{number(advice["write_mb_s"])} MB/s</bdi>')
        recommendation = (f'<h3>{esc(text["device_upgrade"])}</h3><p>{esc(text["util_name_" + advice["component"]])}: {target}</p>'
                          f'<p>{esc(text["device_compatibility"])}</p>')
    return (f'<table class="device-parameters"><thead><tr>'
            f'<th>{esc(text["device_part"])}</th><th>{esc(text["device_parameter"])}</th></tr></thead><tbody>{"".join(rows)}</tbody></table>{recommendation}')



def render_controller_research(result, text):
    observation = result.get('hardware_observation', {})
    research = observation.get('research', {}) if isinstance(observation, dict) else {}
    sources = research.get('candidates', [])
    if not sources:
        return ''
    links = []
    for source in sources:
        url = urlsplit(source['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Controller research requires public HTTPS sources')
        links.append(f'<li><a href="{esc(source["url"])}" rel="noreferrer">{esc(source["model"])}</a></li>')
    return f'<h3>{esc(text["controller_research_title"])}</h3><p>{esc(text["controller_research_note"])}</p><ul>{"".join(links)}</ul>'

def render_hardware_assessment(result, text, show_computer_model=False, product_profile=None):
    """Keep device-specific parameters primary and rating calculations optional."""
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
    parameters = render_device_parameters(result, text, summary, show_computer_model, product_profile)
    message = text['device_no_upgrade'] if quality_limited else comment
    observation = result.get('hardware_observation', {})
    stamp = observation.get('observed_at') if isinstance(observation, dict) else None
    observed = '<p class="muted">' + esc(text['device_observed_note'].format(date=stamp)) + '</p>' if stamp else ''
    return (f'<section id="hardware-assessment"><h2>{esc(text["hardware_assessment"])}</h2>{observed}{parameters}'
            f'<p class="component-comment">{esc(message)}</p>'
            f'<details class="device-rating-evidence"><summary>{esc(text["device_evidence"])}</summary>'
            f'{render_controller_research(result, text)}<h3>{esc(text["util_title"])}</h3>{rating_table}'
            f'{("<p>" + esc(text["util_scope"]) + "</p>") if not quality_limited else ""}</details></section>')


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
    if evaluation.get("report_policy") == "verified_only":
        followup = evaluation.get("followup", [])
        if not isinstance(followup, list) or not all(isinstance(issue, dict) and all(isinstance(issue.get(key), str) and issue[key].strip() for key in ("issue", "reason", "solution", "retest_plan")) for issue in followup):
            raise ValueError("Follow-up needs issue, reason, solution and a bounded retest plan")
    sections = evaluation.get("sections")
    if not isinstance(sections, list) or len(sections) != 3:
        raise ValueError("Evaluation requires bottleneck, upgrades and anomalies sections")
    rendered = []
    for section, kind in zip(sections, ("bottleneck", "upgrades", "anomalies")):
        if not isinstance(section, dict) or section.get("kind") != kind:
            raise ValueError("Invalid evaluation section order")
        if section.get("evidence_status") not in (None, "supported", "needs_followup"):
            raise ValueError("Invalid evaluation evidence status")
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
        if evaluation.get("report_policy") != "verified_only" or section.get("evidence_status") == "supported":
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
        timing_ok = result.get("tool", {}).get("method_version") not in (*AUTO_METHODS, PEAK_METHOD) or collection_status(case) == "collected"
        peak = max(samples) if samples and timing_ok else None
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
    hardware = result.get("hardware", {})
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
        display_model = part['model'] or text['source_unknown']
        if part['component'] == 'controller':
            display_model = controller_identity(hardware, text)
            if hardware.get('controller', {}).get('model') and not part['sources']:
                access = external_connection(hardware)
                rates = [esc(text['controller_ratings_pending'])]
                if access:
                    rates.insert(0, '<bdi dir="ltr">' + esc(access) + '</bdi>')
                role = text['controller_series'] if hardware['controller'].get('precision') == 'series' else text['source_system']
                source_links = [esc(text['source_system'])]
        component_rows.append(
            f'<tr data-path-component="{part["component"]}"><th scope="row">'
            f"{esc(text['path_' + part['component']])}<small>{esc(display_model)}</small></th>"
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
        if relation != "device":
            continue
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


def render_cache_reference(result, assessment, text, include_followup=None):
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
    if include_followup is None:
        include_followup = (result.get("evaluation") or {}).get("report_policy") != "verified_only"
    if include_followup and result.get("reference_cache", {}).get("status") == "stale":
        note += " · " + esc(text["reference_stale"])
    measured = assessment.get("cache_capacity_estimate_bytes")
    warning = ""
    if (
        include_followup
        and assessment.get("status") == "observed_drop_stable"
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

    verified_only = result.get("evaluation", {}).get("report_policy") == "verified_only" if isinstance(result.get("evaluation"), dict) else False
    sections = []
    read, write = measured("seq_read", "read"), measured("seq_write", "write")
    if read is not None or write is not None:
        detail = (esc(text["large_file_result"]).format(
            read=quantity(read, "MB/s"), write=quantity(write, "MB/s")
        ) if not verified_only or read is not None and write is not None else
            " · ".join(esc(text[name]) + ": " + quantity(value, "MB/s")
                        for name, value in (("seq_read", read), ("seq_write", write))
                        if value is not None))
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
        detail = (esc(text["small_file_result"]).format(
            read=quantity(random_read, "MB/s"), write=quantity(random_write, "MB/s")
        ) if not verified_only or random_read is not None and random_write is not None else
            " · ".join(esc(text[name]) + ": " + quantity(value, "MB/s")
                        for name, value in (("rand_read", random_read), ("rand_write", random_write))
                        if value is not None))
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
            f'<td data-label="{esc(text["single_request"])}">{quantity(a, "MB/s")}</td>'
            f'<td data-label="{esc(text["queued_requests"])}">{quantity(b, "MB/s")}</td>'
            f'<td data-label="{esc(text["throughput_ratio"])}"><bdi>{number(b / a)}×</bdi></td></tr>'
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
    scenario_note = '<p class="muted">' + esc(text["scenario_limits"]) + "</p>" if not verified_only else ""
    return (
        f'<section id="reading-results"><h2>{esc(text["reading_results"])}</h2>'
        f'<div class="usage-explanation">{"".join(sections)}</div>'
        f'{scenario_note}{comparison}</section>'
    )


def render_capacity_check(result, text):
    check = result.get("capacity_check")
    if not isinstance(check, dict):
        return ""
    from capacity_check import completeness
    passed = check.get("outcome") == "complete" and completeness(check)["complete"]
    cases = {case.get("name"): case for case in check.get("cases", [])}
    written = cases.get("integrity_write", {}).get("metrics", {}).get("write", {}).get("bytes")
    planned = check.get("plan", {}).get("file_bytes")
    verified_only = (result.get("evaluation") or {}).get("report_policy") == "verified_only"
    verified = number(planned / 1073741824) if passed else "—"
    if verified_only and not passed:
        coverage = " · ".join(text[label] + ": " + number(value) for label, value in (
            ("file_size", planned), ("actual_write", written)) if value is not None)
    else:
        coverage = text["capacity_coverage"].format(planned=number((planned or 0) / 1073741824),
                                                  written=number((written or 0) / 1073741824), verified=verified)
    limits = ('<p class="muted">' + esc(text["capacity_limits"]) + '</p>'
              if not verified_only else '')
    return ('<section id="capacity-check"><h2>' + esc(text["capacity_heading"]) + '</h2><p><strong>'
            + esc(text["capacity_ok"] if passed else text["capacity_not_completed"])
            + '</strong></p><p>' + esc(coverage) + '</p>' + limits + '</section>')


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


def render_product_resources(profile, text):
    """Render already validated derived evidence without network or measurement changes."""
    if profile is None:
        return ""
    groups = [(text["official_resources"], profile["official_sources"]),
              (text["independent_reviews"], profile["review_sources"])]
    sources = [source for _, items in groups for source in items if source["availability"] == "verified"]
    if not sources:
        return ""
    dates = {source["checked_on"] for source in sources}
    sections = []
    for title, items in groups:
        rows = []
        for source in items:
            if source["availability"] != "verified":
                continue
            date = (f' · {esc(text["sources_checked"])} <time datetime="{esc(source["checked_on"])}">'
                    f'{esc(source["checked_on"])}</time>') if len(dates) > 1 else ""
            rows.append(f'<li><a href="{esc(source["canonical_url"])}" target="_blank" rel="noopener noreferrer">'
                        f'{esc(source["display_name"])} ↗</a><p>{esc(source["publisher"])} · {esc(source["scope"])}{date}</p></li>')
        if rows:
            sections.append(f'<div class="product-source-group"><h3>{esc(title)}</h3><ul>{"".join(rows)}</ul></div>')
    checked = (f'<p class="source-checked">{esc(text["sources_checked"])}: <time datetime="{esc(next(iter(dates)))}">'
               f'{esc(next(iter(dates)))}</time></p>') if len(dates) == 1 else ""
    return f'<section id="product-resources"><h2>{esc(text["product_resources"])}</h2>{"".join(sections)}{checked}</section>'


def render_report(result, output, language, text, requested_format="html", comparison=None, community_messages=None, show_computer_model=False, product_profile=None):
    if product_profile is not None:
        from product_profile import validate_profile, matches_profile
        product_profile = validate_profile(product_profile)
        if not matches_profile(product_profile, result):
            raise ValueError('identity_conflict')
        sidecar = Path(output) / 'product-profile.json'
        if any(p.is_symlink() for p in (sidecar, *sidecar.parents)):
            raise ValueError('output_failed')
    verified_only = result.get("evaluation", {}).get("report_policy") == "verified_only" if isinstance(result.get("evaluation"), dict) else False
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
    sustained_case = next((c for c in cases if c["name"] == "sustained"), {})
    automatic = result.get("tool", {}).get("method_version") in (*AUTO_METHODS, PEAK_METHOD)
    mandatory_cache = result.get("tool", {}).get("method_version") in (AUTO_METHOD, PEAK_METHOD)
    completeness = (peak_completeness(result) if result.get("tool", {}).get("method_version") == PEAK_METHOD
                    else core_completeness(result) if mandatory_cache else result.get("completeness", {}))
    timing = collection_status(sustained_case) if automatic else None
    limited_windows = timing not in (None, "collected")
    assessment = (cache_assessment(sustained_case.get("series", []), sampling_status=timing)
                  if limited_windows or mandatory_cache else result.get("cache_assessment") or cache_assessment(sustained_case.get("series", [])))
    cards = []
    for name, direction in [
        ("seq_read", "read"),
        ("seq_write", "write"),
        ("rand_read", "read"),
        ("rand_write", "write"),
    ]:
        for case_name in (name, name + "_q"):
            case = next((c for c in completed if c["name"] == case_name), {})
            metrics = case.get("metrics", {}).get(direction, {})
            value = metrics.get("mb_s")
            if value is not None:
                cards.append(card(text[case_name], number(value), case_name))
    short_cards, cards = cards, []
    summary = (sustained_summary(sustained_case.get("series", []), sampling_status=timing)
               if limited_windows else result.get("sustained_summary") or sustained_summary([]))
    if automatic and not limited_windows:
        qualified = sustained_summary(sustained_case.get("series", []), sampling_status=timing)
        summary = dict(summary, initial_mb_s=qualified["initial_mb_s"], minimum_5s_mb_s=qualified["minimum_5s_mb_s"])
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
    if cache_bytes is not None or result.get("tool", {}).get("method_version") not in (*AUTO_METHODS, PEAK_METHOD):
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
    show_status = not verified_only and any(case.get("status") != "completed" for case in cases)
    show_reason = not verified_only and any(case.get("reason") for case in cases)
    rows = []
    for case in (completed if verified_only else cases):
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
        if cache_state == "no_drop_observed" or assessment.get("curve_diagnostic") == "no_drop_observed"
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
    cache_notice = '<p class="notice">' + esc(text[cache_label]) + '</p>' if not verified_only else ''
    cache_html = f'<div id="cache-assessment" data-status="{esc(cache_state)}">{cache_notice}<dl>{"".join(cache_metrics)}</dl></div>'
    metadata = []
    for key, label in [
        ("total_bytes", "capacity"),
        ("free_bytes", "free_space"),
        ("temperature_c", "initial_temp"),
    ]:
        if verified_only and target.get(key) is None:
            continue
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text[label])}</dt><dd>{number(target.get(key))}</dd></div>'
        )
    temperatures = [
        point["celsius"]
        for case in cases
        for point in case.get("temperatures", [])
        if point.get("celsius") is not None
    ]
    if temperatures or not verified_only:
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text["peak_temp"])}</dt><dd>{number(max(temperatures) if temperatures else None)}</dd></div>'
        )
    if target.get("smart") or not verified_only:
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text["smart"])}</dt><dd>{esc(target.get("smart") or text["unknown"])}</dd></div>'
        )
    for key, label in [
        ("filesystem", "filesystem"),
        ("protocol", "protocol"),
    ]:
        if verified_only and not target.get(key):
            continue
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
        if verified_only and value is None:
            continue
        metadata.append(
            f'<div class="metric-line"><dt>{esc(text[label])}</dt><dd>{number(value)}</dd></div>'
        )
    # Explicit allowlist: never dump arbitrary raw environment, error strings, commands or paths into public HTML.
    env = result.get("environment", {})
    public_env = {"os_family": env.get("os_family"), "os_version": env.get("os_version"),
                  "architecture": env.get("architecture"), "ioengine": env.get("engine")}
    public_env["fio"] = env.get("fio", {}).get("version") if env.get("fio") else None
    environment_html = "".join(f'<div class="metric-line"><dt>{esc(text[key if key != "fio" else "fio_version"])}</dt><dd>{esc(value or text["unknown"])}</dd></div>'
        for key,value in public_env.items() if value or not verified_only)
    depth_values = [
        ", ".join(
            f"{k}: {v:g}%"
            for k, v in c.get("metrics", {}).get("achieved_depth", {}).items()
            if type(v) in (int, float) and math.isfinite(v) and v > 0
        )
        for c in cases
    ]
    depth_rows = "".join(
        "<tr><td>"
        + esc(text.get(c["name"], c["name"]))
        + '</td><td dir="ltr">'
        + esc(value or text["unknown"])
        + "</td></tr>"
        for c, value in zip(cases, depth_values) if value or not verified_only
    )
    depth_table = (
        "<table><thead><tr><th>"
        + esc(text["workload"])
        + "</th><th>"
        + esc(text["depth_raw"] if automatic else text["depth"])
        + "</th></tr></thead><tbody>"
        + depth_rows
        + "</tbody></table>"
        if any(depth_values) else '' if verified_only else f'<p>{esc(text["depth"])}: {esc(text["unknown"])}</p>'
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
    delivery_only = {"trash_note", "retain_note", "cleanup_failed", "cache_incomplete", "sampling_irregular"}
    notes = [
        text.get(key, text["unknown"])
        for key in result.get("notes", []) if key not in delivery_only
    ]
    if result.get("outcome") != "complete" and result.get("actual_write_bytes") is None and not (
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
    report_title = text["title"]
    if result.get("tool", {}).get("method_version") in QUICK_METHODS:
        report_title = text["quick_title"]
    if mandatory_cache and not completeness["complete"]:
        report_title = text["incomplete_test_title"]
        status = text["partial"]
    sample = (
        f'<p class="notice">{esc(text["sample"])}</p>'
        if result.get("synthetic")
        else ""
    )
    irregular_cases = [text.get(c.get("name"), c.get("name", "")) for c in cases
                       if automatic and collection_status(c) == "collected_irregular"]
    sampling_notice = ('<p class="notice" data-sampling="collected_irregular">'
                       + esc(text["sampling_irregular"]) + " · " + esc(" / ".join(irregular_cases)) + "</p>") if irregular_cases else ""
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
    if automatic and assessment.get('status') != 'observed_drop_stable':
        observed_ms = sustained_case.get('metrics', {}).get('runtime_ms')
        observed_seconds = observed_ms / 1000 if isinstance(observed_ms, (int, float)) else None
        long_summary_html = '<p class="notice">' + esc(text['write_observation_brief'].format(seconds=number(observed_seconds))) + '</p>'
        if mandatory_cache:
            long_summary_html = '<p class="notice" data-required-cache="incomplete">' + esc(text["required_cache_incomplete"]) + '</p>'
    sustained_html = f'''<section id="sustained"><h2>{esc(text["sustained"])}</h2><p class="muted" id="tail-speed-note">{esc(text["tail_explanation"])}</p><div class="sustained-grid"><div>{chart(series, text)}</div><dl>{"".join(lines)}</dl></div>{cache_html}</section>''' if result.get('plan', {}).get('profile') not in ('mapping', 'integrity') else ''
    if result.get('plan', {}).get('profile') == 'quick':
        long_summary_html = sustained_html = ''
    quick_cards = []
    for case in result.get('quick_measurement', {}).get('cases', []):
        quick_direction = 'read' if case.get('name') in ('seq_read', 'rand_read') else 'write'
        value = case.get('metrics', {}).get(quick_direction, {}).get('mb_s')
        if value is not None:
            quick_cards.append(card(text.get(case['name'], case['name']), number(value), 'quick_' + case['name']))
    quick_history_html = ('<section id="quick-measurement"><h2>' + esc(text['quick_title'])
                          + '</h2>' + ('' if verified_only else '<p>' + esc(text['quick_notice']) + '</p>') + '<dl class="cards">'
                          + ''.join(quick_cards) + '</dl></section>') if quick_cards else ''
    drive = hardware.get("drive", {})
    display_model = product_profile["display_name"] if product_profile is not None else drive.get("matching_model") or drive.get("model") or text["unknown"]
    object_line = str(display_model)+" · "+text.get("scope_"+str(target.get("scope")),text["unknown"])
    document_title = str(display_model)+" · "+report_title if product_profile is not None else report_title
    from report_evidence import render_evidence, evidence_labels
    evidence_text = evidence_labels(result, language, ASSETS)
    device_health_html, online_reviews_html, further_testing_html = render_evidence(result, language, evidence_text)
    connection_facts = []
    hardware = result.get('hardware', {})
    access = external_connection(hardware)
    if access:
        connection_facts.append(f'<div><dt>{esc(text["device_external"])}</dt><dd><bdi>{esc(access)}</bdi></dd></div>')
    for link in hardware.get('links', []):
        if access and link.get('kind') in ('usb', 'thunderbolt'):
            continue
        fact = link_reference(link)
        if fact and (link.get('state') == 'negotiated' or link.get('kind') == 'pcie' and link.get('state') == 'reported'):
            label = text['device_internal_segment'] if link.get('kind') == 'pcie' else text['device_connection']
            connection_facts.append(f'<div><dt>{esc(label)}</dt><dd><bdi>{esc(fact["label"])}</bdi></dd></div>')
    connection_html = '<dl class="connection-facts">' + ''.join(connection_facts) + '</dl>' if connection_facts else ''
    reading_links = []
    for identifier, label, present in (
        ('short-results', text['short_group'], short_cards),
        ('reading-results', text['reading_results'], readable_html),
        ('online-reviews', evidence_text['reviews_title'], online_reviews_html),
    ):
        if present:
            reading_links.append(f'<a href="#{identifier}">{esc(label)}</a>')
    document_fields = {
        'language': esc(language),
        'direction': direction,
        'document_title': esc(document_title),
        'css': css,
        'report_title': esc(report_title),
        'object_line': esc(object_line),
        'created': esc(created),
        'created_display': esc(created_display),
        'status': esc(status),
        'completed_count': len(completed),
        'case_count': len(cases),
        'print_hint': esc(text['print_hint']),
        'download': esc(text['download']),
        'reading_navigation': '<nav class="reading-navigation">' + ''.join(reading_links) + '</nav>' if reading_links else '',
        'device_health': device_health_html,
        'online_reviews': online_reviews_html,
        'further_testing': further_testing_html,
        'sampling_notice': sampling_notice,
        'sample': sample,
        'quick_notice': '<p class="notice">' + esc(text['quick_notice']) + '</p>' if result.get('tool', {}).get('method_version') in QUICK_METHODS else '',
        'completeness_notice': '<p class="notice">' + esc(text['core_incomplete']) + '</p>' if completeness.get('complete') is False else '',
        'short_summary': '<div id="short-results"><h2 class="group-title">' + esc(text['short_group']) + '</h2>' + connection_html + '<p class="group-caption">' + esc(text['short_group_note']) + '</p><dl class="cards short-cards">' + ''.join(short_cards) + '</dl></div>' if short_cards else '',
        'long_summary': long_summary_html,
        'quick_history': quick_history_html,
        'diagnostics': render_diagnostics(result, text),
        'capacity_check': render_capacity_check(result, text),
        'evaluation': render_evaluation(result, language),
        'hardware_assessment': render_hardware_assessment(result, text, show_computer_model, product_profile),
        'product_resources': render_product_resources(product_profile, text),
        'user_input': render_user_input(result, language, text),
        'community': community_html,
        'readable_results': readable_html,
        'technical_appendix': esc(text['technical_appendix']),
        'peak_comparison': render_peak_comparison(result, text),
        'references': reference_html,
        'sustained': sustained_html,
        'full_results': esc(text['full_results']),
        'metric_guide': metric_guide,
        'results_table': table,
        'environment': esc(text['environment']),
        'metadata': ''.join(metadata),
        'environment_html': environment_html,
        'depth_table': depth_table,
        'method': esc(text['method']),
        'method_text': esc(text['method_text']),
        'window_method': '<p>' + esc(text['window_method']) + '</p>',
        'sensors_limit': '<p>' + esc(text['sensors_limit']) + '</p>',
        'cache_method': cache_method,
        'configuration_table': config_table,
        'technical_appendix_note': esc(text['technical_appendix_note']),
        'run_notes': '<aside class="run-notes">' + ''.join((f'<p>{esc(n)}</p>' for n in notes)) + '</aside>' if notes else '',
        'agent_usage': agent_usage_html(text=text),
        'project': project_html,
        'promotion': promotion_html,
        'privacy': esc(text['privacy']),
        'raw_notice': esc(text['raw']),
    }
    document_fields['status_line'] = esc(status) + ' · ' + str(len(completed)) + '/' + str(len(cases))
    if verified_only:
        document_fields.update(
            document_title=esc(text['quick_title'] if result.get('tool', {}).get('method_version') in QUICK_METHODS else text['title']),
            report_title=esc(text['quick_title'] if result.get('tool', {}).get('method_version') in QUICK_METHODS else text['title']),
            status_line=esc(text['workload']) + ' · ' + str(len(completed)),
            sampling_notice='', quick_notice='', completeness_notice='', run_notes='',
            further_testing='', hardware_assessment='', references='',
            sensors_limit='', window_method='', cache_method='', peak_comparison='', privacy='', raw_notice='',
        )
        if assessment.get('status') != 'observed_drop_stable':
            document_fields.update(long_summary='', sustained='')
    document = Template((ASSETS / 'report-templates/document.html').read_text(encoding='utf-8')).substitute(document_fields)
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
    if verified_only:
        artifacts["delivery_followup"] = dict(
            outcome=result.get("outcome"), completeness=completeness,
            issues=result.get("evaluation", {}).get("followup", []),
            withheld_sections=[section for section in result.get("evaluation", {}).get("sections", []) if section.get("evidence_status") != "supported"],
            collection_notes=[text.get(key, key) for key in result.get("notes", [])],
            device_context=result.get("evaluation", {}).get("evidence", {}).get("device_health"),
            reading_context=dict(scenario_limits=text["scenario_limits"], privacy=text["privacy"],
                                 window_method=text["window_method"], cache_method=text["cache_method"],
                                 cache_observation=text[cache_label],
                                 reference_cache=result.get("reference_cache", {}),
                                 cache_reference_html=render_cache_reference(result, assessment, text, include_followup=True),
                                 capacity_limits=text["capacity_limits"] if result.get("capacity_check") else None),
            further_testing=result.get("evaluation", {}).get("evidence", {}).get("further_testing"),
        )
        # Follow-up prose can include execution context. It belongs to the
        # conversation, never to the report's machine-readable receipt.
        followup = artifacts.pop('delivery_followup')
        observation = result.get('measurement_quality', {}).get('status')
        if observation:
            print(text.get('quality_' + observation, observation), file=sys.stderr)
        for issue in followup['issues']:
            print(' · '.join(issue[key] for key in ('issue', 'reason', 'solution', 'retest_plan')), file=sys.stderr)
        for section in followup['withheld_sections']:
            print(section['title'] + ': ' + ' '.join(section['paragraphs']), file=sys.stderr)
        for note in followup['collection_notes']:
            print(note, file=sys.stderr)
        if followup['device_context']:
            print(followup['device_context'].get('scope', ''), file=sys.stderr)
        context = followup['reading_context']
        for key in ('cache_method', 'cache_observation', 'capacity_limits'):
            if context.get(key):
                print(context[key], file=sys.stderr)
        if context.get('cache_reference_html'):
            from html.parser import HTMLParser
            class ReferenceText(HTMLParser):
                def handle_data(self, value):
                    print(value, file=sys.stderr)
            ReferenceText().feed(context['cache_reference_html'])
        try:
            from community_data import project_result, require_publication_identity, CommunityError
            projected = project_result(result, 'https://x.com/report_preview')
            require_publication_identity(projected)
            artifacts['share_recommendation_allowed'] = True
        except (CommunityError, ValueError, TypeError, KeyError, AttributeError):
            artifacts['share_recommendation_allowed'] = False
    if comparison is not None:
        from community_report import render_comparison
        artifacts.update(render_comparison(comparison, result, output, language, text, requested_format, community_messages, show_computer_model))
    if product_profile is not None:
        from product_profile import save_snapshot
        sidecar = output / 'product-profile.json'
        save_snapshot(product_profile, sidecar)
        artifacts['product_profile'] = str(sidecar)
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
    p.add_argument("--product-profile", help="Optional offline, identity-bound product evidence; export a separate snapshot without changing measurements")
    args = p.parse_args()
    try:
        result = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if result.get("schema_version") != 1 or not isinstance(
            result.get("cases"), list
        ):
            raise ValueError("Unsupported result schema")
        profile = None
        if args.product_profile:
            from product_profile import load_profile, matches_profile
            profile = load_profile(args.product_profile)
            if not matches_profile(profile, result):
                raise ValueError('identity_conflict')
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
        artifacts = render_report(result, output, args.language, text, args.format, comparison, args.community_messages, args.show_computer_model, profile)
        print(json.dumps(artifacts, ensure_ascii=False))
        return 0 if args.format == "html" or (artifacts["pdf"] and (comparison is None or artifacts.get("comparison_pdf"))) else 5
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
