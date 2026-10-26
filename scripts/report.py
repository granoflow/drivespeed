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
from pathlib import Path

from progress import ASSETS, language_default, messages
from workloads import sustained_summary


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:,.2f}"


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
        f'<text x="0" y="25" font-size="12">{max_y:.0f}</text><text x="35" y="215">0</text>'
        f'<text x="900" y="238" font-size="12">{max_x:.0f} s</text></svg>'
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


def render_report(result, output, language, text, requested_format="html"):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = result.get("cases", [])
    completed = [c for c in cases if c.get("status") == "completed"]
    target = result.get("target", {})

    def card(label, value, unit=""):
        return f'<div class="card">{esc(label)}<strong>{esc(value)}</strong><small>{esc(unit)}</small></div>'

    project = json.loads((ASSETS / "project.json").read_text(encoding="utf-8"))
    repository_url = project["repository_url"]
    if not repository_url.startswith("https://github.com/"):
        raise ValueError("Project repository URL must use HTTPS on github.com")
    project_html = (
        f'<div class="project"><p>{esc(text["skill_intro"])}</p>'
        f'<a href="{esc(repository_url)}" target="_blank" rel="noopener noreferrer">'
        f"{esc(text['skill_link'])} · {esc(repository_url)}</a></div>"
    )
    hardware = result.get("hardware", {})
    hardware_rows = []
    for key, label in (
        ("computer", "computer_model"),
        ("drive", "drive_model"),
        ("enclosure", "enclosure_model"),
    ):
        item = hardware.get(key, {})
        source = item.get("source", "unavailable")
        source_text = (
            text["source_user"]
            if source == "user"
            else text["source_unknown"]
            if source == "unavailable"
            else text["source_system"]
        )
        hardware_rows.append(
            f"<tr><th>{esc(text[label])}</th><td>{esc(item.get('model') or text['source_unknown'])}</td><td>{esc(source_text)}</td></tr>"
        )
    hardware_html = (
        "<section><h2>"
        + esc(text["hardware"])
        + "</h2><table><tbody>"
        + "".join(hardware_rows)
        + "</tbody></table>"
    )
    if not hardware.get("enclosure", {}).get("model"):
        hardware_html += '<p class="muted">' + esc(text["enclosure_unknown"]) + "</p>"
        candidates = hardware.get("enclosure", {}).get("bridge_candidates", [])
        if candidates:
            hardware_html += (
                '<p class="muted">'
                + esc(text["bridge_candidates"])
                + ": "
                + esc(" / ".join(candidates))
                + "</p>"
            )
    hardware_html += "</section>"
    cards = ""
    for name, direction in [("seq_read", "read"), ("seq_write", "write")]:
        case = next((c for c in completed if c["name"] == name), {})
        cards += card(
            text[name],
            number(case.get("metrics", {}).get(direction, {}).get("mb_s")),
            "MB/s",
        )
    cards += card(text["completed"], f"{len(completed)} / {len(cases)}")
    summary = result.get("sustained_summary") or sustained_summary([])
    summary_labels = [
        ("initial_mb_s", "initial"),
        ("tail_mean_mb_s", "tail_mean"),
        ("tail_median_mb_s", "tail_median"),
        ("minimum_5s_mb_s", "minimum_5s"),
        ("after_drop_mean_mb_s", "after_drop"),
        ("drop_percent", "drop"),
        ("change_seconds", "change_time"),
        ("bytes_at_change", "change_bytes"),
    ]
    cards += card(text["tail_mean"], number(summary.get("tail_mean_mb_s")), "MB/s")
    cards += card(
        text["after_drop"], number(summary.get("after_drop_mean_mb_s")), "MB/s"
    )
    cards += card(text["cache_status"], text["unconfirmed"])
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
                text.get(case.get("status"), case.get("status")),
                text[direction],
                number(m.get("mb_s")),
                number(m.get("iops")),
                " / ".join(number(lat.get(p)) for p in ("50", "95", "99")),
                text.get(case.get("reason"), "") if case.get("reason") else "",
            ]
            rows.append("<tr>" + "".join(f"<td>{esc(x)}</td>" for x in cells) + "</tr>")
    heads = [
        text["workload"],
        text["status"],
        text["read"] + " / " + text["write"],
        "MB/s",
        "IOPS",
        text["latency"],
        text["failure_reason"],
    ]
    table = (
        "<table><thead><tr>"
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
        lines.append(
            f'<div class="metric-line"><span>{esc(text[label])}</span><span>{number(summary.get(key))} {unit}</span></div>'
        )
    metadata = []
    for key, label in [
        ("total_bytes", "capacity"),
        ("free_bytes", "free_space"),
        ("temperature_c", "initial_temp"),
    ]:
        metadata.append(
            f'<div class="metric-line"><span>{esc(text[label])}</span><span>{number(target.get(key))}</span></div>'
        )
    temperatures = [
        point["celsius"]
        for case in cases
        for point in case.get("temperatures", [])
        if point.get("celsius") is not None
    ]
    metadata.append(
        f'<div class="metric-line"><span>{esc(text["peak_temp"])}</span><span>{number(max(temperatures) if temperatures else None)}</span></div>'
    )
    metadata.append(
        f'<div class="metric-line"><span>{esc(text["smart"])}</span><span>{esc(target.get("smart") or text["unknown"])}</span></div>'
    )
    for key, label in [
        ("device", "device"),
        ("filesystem", "filesystem"),
        ("protocol", "protocol"),
    ]:
        metadata.append(
            f'<div class="metric-line"><span>{esc(text[label])}</span><span>{esc(target.get(key) or text["unknown"])}</span></div>'
        )
    config = result.get("plan", {})
    for label, value in [
        ("file_size", config.get("file_bytes")),
        ("reserve", config.get("reserve_bytes")),
        ("max_duration", config.get("max_runtime_seconds")),
        ("write_budget", config.get("max_write_bytes")),
        ("actual_write", result.get("actual_write_bytes")),
        ("duration", result.get("elapsed_seconds")),
    ]:
        metadata.append(
            f'<div class="metric-line"><span>{esc(text[label])}</span><span>{number(value)}</span></div>'
        )
    # Explicit allowlist: never dump arbitrary raw environment, error strings, commands or paths into public HTML.
    env = result.get("environment", {})
    public_env = {k: env.get(k) for k in ("os", "architecture", "engine")}
    public_env["fio"] = env.get("fio", {}).get("version") if env.get("fio") else None
    depth_rows = "".join(
        "<tr><td>"
        + esc(text.get(c["name"], c["name"]))
        + "</td><td>"
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
        for k in ("workload", "block_size", "requested_qd", "io_limit", "time_limit")
    )
    config_rows = "".join(
        "<tr><td>"
        + esc(text.get(c["name"], c["name"]))
        + "</td>"
        + "".join(
            "<td>" + esc(number(c.get(k))) + "</td>"
            for k in ("bs", "qd", "io_bytes", "runtime")
        )
        + "</tr>"
        for c in cases
    )
    config_table = (
        "<table><thead><tr>"
        + config_head
        + "</tr></thead><tbody>"
        + config_rows
        + "</tbody></table>"
    )
    notes = [text.get(key, text["unknown"]) for key in result.get("notes", [])]
    if result.get("outcome") != "complete":
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
    document = f'''<!doctype html>
<html lang="{esc(language)}" dir="{direction}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(text["title"])}</title><style>{css}</style></head><body><main>
<div class="toolbar"><p dir="auto">{esc(text["print_hint"])}</p><button type="button" onclick="window.print()">{esc(text["download"])}</button></div>
<header><p class="muted">{esc(result.get("created", ""))}</p><h1>{esc(text["title"])}</h1><p class="status">{esc(status)}</p></header>{sample}{project_html}{hardware_html}
<div class="cards">{cards}</div><p class="notice">{esc(text["cache_limit"])}</p>
<section><h2>{esc(text["sustained_numbers"])}</h2>{"".join(lines)}<p class="muted">{esc(text["window_method"])}</p></section>
<section><h2>{esc(text["series"])}</h2>{chart(series, text)}</section>
<section><h2>{esc(text["results"])}</h2><div class="table-wrap">{table}</div></section>
<section><h2>{esc(text["environment"])}</h2>{"".join(metadata)}<pre>{esc(json.dumps(public_env, ensure_ascii=False, indent=2))}</pre>{depth_table}</section>
<section><h2>{esc(text["method"])}</h2><p>{esc(text["method_text"])}</p><p>{esc(text["sensors_limit"])}</p>{config_table}</section>
<section><h2>{esc(text["recommendations"])}</h2><p>{esc(text["analysis_hint"])}</p>{"".join(f"<p>{esc(n)}</p>" for n in notes)}</section>
<footer><p>{esc(text["privacy"])}</p><p>{esc(text["raw"])}</p><p>{esc(text["footer_method"])}</p></footer>
</main></body></html>'''
    source = output / "report.html"
    if source.is_symlink() or (output / "report.pdf").is_symlink():
        raise ValueError("Report files must not be symlinks")
    source.write_text(document, encoding="utf-8")
    pdf, error = None, None
    if requested_format == "pdf":
        pdf, error = export_pdf(source, output / "report.pdf")
        if not pdf:
            print(text["pdf_failed"], file=sys.stderr)
    return dict(
        skill_repository=repository_url,
        language=language,
        requested_format=requested_format,
        html=str(source),
        pdf=pdf,
        pdf_error=error,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--language", default=language_default())
    p.add_argument("--messages")
    p.add_argument("--format", choices=["html", "pdf"], default="html")
    args = p.parse_args()
    try:
        result = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if result.get("schema_version") != 1 or not isinstance(
            result.get("cases"), list
        ):
            raise ValueError("Unsupported result schema")
        output = Path(args.output).expanduser().absolute()
        if any(p.is_symlink() for p in (output, *output.parents)):
            raise ValueError("Output path must not contain symlinks")
        text = messages(args.language, args.messages)
        artifacts = render_report(result, output, args.language, text, args.format)
        print(json.dumps(artifacts, ensure_ascii=False))
        return 0 if args.format == "html" or artifacts["pdf"] else 5
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
