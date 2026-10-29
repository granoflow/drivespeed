"""Offline HTML/PDF views of validated typed comparison facts."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from community_data import CONTRACTS, CommunityError, digest, normalise_payload, project_result, x_profile
from agent_usage import agent_usage_html


def report_messages(language, filename=None):
    from string import Formatter
    base = language.replace("_", "-").lower().split("-", 1)[0]
    directory = CONTRACTS / "report-locales"
    template = json.loads((directory / "en.json").read_text())
    if filename:
        selected = json.loads(Path(filename).read_text(encoding="utf-8"))
        if not isinstance(selected, dict) or selected.keys() != template.keys() or any(not isinstance(v, str) or not v.strip() for v in selected.values()):
            raise ValueError("Incomplete community report translation")
        for key, value in selected.items():
            wanted = {"records", "groups"} if key == "summary" else ({"value"} if key in ("latency_more", "latency_less", "rate_more", "rate_less") else set())
            try:
                fields = {field for _,field,_,_ in Formatter().parse(value) if field is not None}
                if fields != wanted:
                    raise ValueError()
            except ValueError:
                raise ValueError("Invalid community report translation placeholders") from None
        return selected
    if base not in ("en", "zh", "ar"):
        raise ValueError("A complete community report translation is required for this language")
    return json.loads((directory / (base+".json")).read_text())


def validate_context(facts, result):
    if not facts.get("context_complete"):
        raise CommunityError("unsupported_comparison_version")
    local = facts["local"]
    projected = normalise_payload(project_result(result, local["attribution"]["profile_url"], local["attribution"]["intro"], submission_id=facts["submission_id"]))
    if result.get("run_id") != facts["run_id"] or digest(projected) != facts["content_sha256"] or digest(local) != facts["content_sha256"]:
        raise CommunityError("integrity_mismatch")


def _source_url(value):
    from community_network import identifier
    parsed = urlsplit(value)
    allowed = (parsed.scheme == "https" and parsed.netloc == "drivespeed.anz.tv") or (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1") and parsed.port)
    if not allowed or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith("/benchmarks/"):
        raise ValueError("Invalid comparison source URL")
    identifier(parsed.path.removeprefix("/benchmarks/"))
    return value


def _values(local, case):
    return {"scope": local["storage"]["scope"], **local["storage"], "os_family": local["environment"]["os"],
            **{k: local["environment"][k] for k in ("os_version", "architecture", "ioengine")},
            **local["plan"], "method_version": local["tool"]["method_version"],
            **{"case_"+key: value for key,value in case.items() if key in ("name", "rw", "bs_bytes", "qd", "numjobs", "runtime_limit_seconds", "rwmixread", "status")}}


def _display(value, text):
    from report import esc, number
    if value is None:
        return esc(text["unknown"])
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value,int) else number(value)
    return esc(text.get("value_"+str(value), str(value)))


def _delta(metric, text):
    from report import esc, number
    value = metric["delta_percent"]
    if value is None:
        return esc(text["no_statistic"])
    if value == 0:
        return esc(text["same"])
    latency = metric["metric"] not in ("mb_s", "iops")
    key = ("latency_more" if value > 0 else "latency_less") if latency else ("rate_more" if value > 0 else "rate_less")
    return esc(text[key].format(value=number(abs(value))))


def summary_html(facts, text):
    from report import esc
    from datetime import datetime
    groups = sum(any(m["n"] >= 3 for m in group["metrics"]) for group in facts["groups"])
    def stamp(value):
        return datetime.fromisoformat(value.replace("Z","+00:00")).strftime("%Y-%m-%d %H:%M UTC")
    return f'<p class="comparison-lead">{esc(text["summary"].format(records=facts["sample_count"],groups=groups))}</p><p class="muted">{esc(text["as_of"])}: <time datetime="{esc(facts["sample_as_of"])}">{esc(stamp(facts["sample_as_of"]))}</time> · {esc(text["fetched"])}: <time datetime="{esc(facts["samples_fetched_at"])}">{esc(stamp(facts["samples_fetched_at"]))}</time></p><p>{esc(text["record_count_notice"])}</p>'


def _differences(source, local, case, text):
    from report import esc
    a = _values(local, case)
    b = dict(source["conditions"])
    b.update({"case_"+key: value for key,value in source["workload"].items()})
    rows = []
    for difference in source["differences"]:
        field = difference.removeprefix("unknown_")
        field = "os_family" if field == "os" else field
        if difference == "workload_or_measurement":
            workload = ["case_"+key for key in ("rw", "bs_bytes", "qd", "numjobs", "runtime_limit_seconds", "rwmixread", "status")]
            for key in workload:
                if a.get(key) != b.get(key) or b.get(key) is None:
                    rows.append((text[key], a.get(key), b.get(key)))
        elif field in a and field in b:
            rows.append((text.get(field, field), a[field], b[field]))
        else:
            rows.append((text.get(difference, text["unknown_condition"]), None, None))
    if not rows:
        return '<p>'+esc(text["comparable"] if source["comparable"] else text["background"])+"</p>"
    body = "".join(f'<tr><th>{esc(label)}</th><td>{_display(left,text)}</td><td>{_display(right,text)}</td></tr>' for label,left,right in rows)
    return '<p>'+esc(text["background"])+f'</p><div class="table-wrap"><table class="condition-differences"><thead><tr><th>{esc(text["condition"])}</th><th>{esc(text["local"])}</th><th>{esc(text["reference"])}</th></tr></thead><tbody>{body}</tbody></table></div>'


def render_comparison(facts, result, output, language, report_text, requested_format="html", message_file=None):
    from report import esc, number, render_evaluation, export_pdf
    from community_comparison import METRICS
    validate_context(facts, result)
    text = report_messages(language, message_file)
    local, output = facts["local"], Path(output)
    sections = []
    for group in facts["groups"]:
        case = local["cases"][group["case_index"]]
        rows = []
        for metric in group["metrics"]:
            stats = number(metric["median"]) if metric["median"] is not None else esc(text["no_statistic"])
            if metric["median"] is not None:
                stats += f'<span class="comparison-detail">{esc(text["range"])}: {number(metric["minimum"])}–{number(metric["maximum"])}</span>'
            stats += f'<span class="comparison-detail">n={metric["n"]} {esc(text["records"])}</span>'
            rows.append(f'<tr><th>{esc(text["metric_"+metric["metric"]])}</th><td>{number(metric["local_value"])}</td><td>{stats}</td><td>{_delta(metric,text)}</td></tr>')
        references = []
        for source in group["references"]:
            url = _source_url(source["url"])
            profile = x_profile(source["conditions"]["x_profile_url"])
            values = " · ".join(esc(text["metric_"+key])+": "+number(source["values"][key]) for key in METRICS if source["values"].get(key) is not None)
            matched = " / ".join(text[kind+"_model"] for kind in source["matched_on"])
            references.append(f'<article class="comparison-source"><p><a href="{esc(url)}" rel="noopener noreferrer"><bdi>{esc(source["sample_id"])}</bdi></a> · <a href="{esc(profile)}" rel="noopener noreferrer"><bdi>X @{esc(profile.rsplit("/",1)[1])}</bdi></a> · <time>{esc(source["tested_on"])}</time></p><p>{esc(source["drive_model"] or text["unknown"])} · {esc(source["computer_model"] or text["unknown"])} · {esc(text["unverified"])}</p><p>{esc(text["matched"])}: {esc(matched or text["unknown"])}</p><p class="comparison-example">{values or esc(text["no_statistic"])}</p>'+_differences(source,local,case,text)+"</article>")
        workload = f'{text["case_bs_bytes"]}: {case["bs_bytes"]:,} B · QD{case["qd"]} · {case["numjobs"]} {text["jobs"]} · {case["runtime_limit_seconds"]} s'
        case_label = report_text.get(group["case_name"], group["case_name"])
        state = report_text.get(case["status"],case["status"])
        sections.append(f'<section class="comparison-group"><h2>{esc(case_label)} · {esc(report_text[group["direction"]])} · {esc(state)}</h2><p>{esc(workload)}</p><div class="table-wrap"><table class="comparison-metrics"><thead><tr><th>{esc(text["metric"])}</th><th>{esc(text["local"])}</th><th>{esc(text["median"])}</th><th>{esc(text["delta"])}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div><p>{esc(text["threshold"])}</p><h3>{esc(text["sources"])}</h3>{"".join(references) or "<p>"+esc(text["no_sources"])+"</p>"}</section>')
    context = _values(local, {})
    condition_rows = "".join(f'<div class="metric-line"><dt>{esc(text[key])}</dt><dd>{_display(context.get(key),text)}</dd></div>' for key in
        ("scope", "capacity_bytes", "connection", "negotiated_bps", "filesystem", "free_percent", "os_family", "os_version", "ioengine", "profile", "file_bytes", "seconds", "sustained_seconds"))
    model_rows = "".join(f'<div class="metric-line"><dt>{esc(text[kind+"_model"])}</dt><dd>{esc(local["hardware"][kind]["model"] or text["unknown"])}</dd></div>' for kind in ("computer","controller","drive"))
    css = (Path(__file__).resolve().parents[1]/"assets/report.css").read_text()
    direction = "rtl" if language.split("-")[0] == "ar" else "ltr"
    evaluation = render_evaluation(result,language)
    document = f'''<!doctype html><html lang="{esc(language)}" dir="{direction}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(text['title'])}</title><style>{css}</style></head><body class="community-report"><main>
<header><div><h1>{esc(text['title'])}</h1><p class="run-meta">{esc(local['hardware']['drive']['model'] or text['unknown'])} · <time>{esc(local['tested_on'])}</time> · {esc(report_text.get(result.get('outcome'),result.get('outcome','')))}</p></div><button type="button" onclick="window.print()">{esc(report_text['download'])}</button></header>
{('<p class="notice">'+esc(report_text['sample'])+'</p>') if result.get('synthetic') else ''}
<section>{summary_html(facts,text)}<p>{esc(text['unverified'])}</p><p>{esc(text['threshold'])}</p><p><a href="{esc(x_profile(local['attribution']['profile_url']))}" rel="noopener noreferrer">{esc(text['author'])} · X @{esc(local['attribution']['profile_url'].rsplit('/',1)[1])}</a></p></section>
<section><h2>{esc(text['local_conditions'])}</h2><dl>{model_rows}{condition_rows}</dl></section>{''.join(sections)}
{evaluation or '<section class="comparison-interpretation"><h2>'+esc(text['interpretation'])+'</h2><p>'+esc(text['no_interpretation'])+'</p></section>'}
<section class="comparison-method"><h2>{esc(text['method'])}</h2><p>{esc(text['formula'])}</p><p>{esc(text['confounders'])}</p><p>{esc(text['historical'])}</p><p>{esc(text['authenticity'])}</p></section>
{agent_usage_html()}
<footer><a href="report.html">{esc(text['back'])}</a> · DriveSpeed · <a href="https://anz.tv/" rel="noopener noreferrer">A New Zero</a></footer></main></body></html>'''
    output.mkdir(parents=True, exist_ok=True)
    source, pdf_path = output/"comparison.html", output/"comparison.pdf"
    if source.is_symlink() or pdf_path.is_symlink():
        raise ValueError("Report files must not be symlinks")
    source.write_text(document,encoding="utf-8")
    pdf, error = export_pdf(source,pdf_path) if requested_format == "pdf" else (None,None)
    return {"comparison_id": facts["comparison_id"], "comparison_html": str(source), "comparison_pdf": pdf, "comparison_pdf_error": error}
