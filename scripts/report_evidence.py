"""Render saved, run-bound device health and researched reviews; no device or network I/O."""

from datetime import date
from html import escape
import json
import math
from urllib.parse import urlsplit


def _text(item, key):
    value = item.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 5000:
        raise ValueError(f"Evidence needs nonempty {key}")
    return escape(value, quote=True)


def evidence_labels(result, language, assets):
    """Use en/zh semantic labels or the Agent's complete report-language label set."""
    normalized = language.replace('_', '-').lower()
    base = 'zh' if normalized in ('zh', 'zh-cn') else 'en'
    labels = json.loads((assets / f'report-templates/{base}.json').read_text(encoding='utf-8'))['evidence']
    evaluation = result.get('evaluation')
    evidence = evaluation.get('evidence') if isinstance(evaluation, dict) else None
    if not isinstance(evidence, dict):
        return labels
    localized = evidence.get('labels')
    if localized is None:
        if base != 'zh' and normalized.split('-')[0] != 'en':
            raise ValueError('Evidence requires a complete label set in the report language')
        return labels

    def check(values, expected):
        if not isinstance(values, dict) or values.keys() != expected.keys():
            raise ValueError('Evidence label keys must match the semantic template')
        for key, reference in expected.items():
            if isinstance(reference, dict):
                check(values[key], reference)
            elif not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > 300:
                raise ValueError('Evidence labels must be nonempty localized text')
    check(localized, labels)
    return localized


def render_evidence(result, language, labels):
    """Return SMART table, review appendix and further-test advice from saved evidence."""
    evaluation = result.get("evaluation")
    if evaluation is None:
        return "", "", ""
    if not isinstance(evaluation, dict):
        raise ValueError("Evaluation must be an object")
    evidence = evaluation.get("evidence")
    if evidence is None:
        return "", "", ""
    if evaluation.get("run_id") != result.get("run_id") or not result.get("run_id") or evaluation.get("language") != language:
        raise ValueError("Evidence must match this run and language")
    if not isinstance(evidence, dict):
        raise ValueError("Evidence must be an object")
    verified_only = evaluation.get("report_policy") == "verified_only"
    recommendation = ""
    advice = evidence.get("further_testing")
    if advice is not None:
        if not isinstance(advice, dict) or advice.get("decision") not in labels["deep_decisions"]:
            raise ValueError("Invalid further-testing recommendation")
        recommendation = (f'<section id="further-testing"><h2>{escape(labels["deep_decisions"][advice["decision"]])}</h2>'
                          + ''.join(f'<p>{_text(advice, key)}</p>' for key in ("reason", "remaining_unknowns", "when_to_continue")) + '</section>')
    if verified_only:
        recommendation = ""
    health, reviews = evidence.get("device_health"), evidence.get("online_reviews", [])
    top = ""
    if health is not None:
        if not isinstance(health, dict) or health.get("status") not in ("collected", "unavailable"):
            raise ValueError("Invalid device health status")
        if health.get("model") != result.get("hardware", {}).get("drive", {}).get("model"):
            raise ValueError("Device health must match the measured drive model")
        rows = health.get("rows", [])
        if not isinstance(rows, list) or len(rows) > len(labels["fields"]):
            raise ValueError("Invalid SMART rows")
        seen, cells = set(), []
        for row in rows:
            if not isinstance(row, dict) or row.get("field") not in labels["fields"] or row["field"] in seen:
                raise ValueError("Unknown or duplicate SMART field (identifiers are not accepted)")
            seen.add(row["field"])
            cells.append(f'<tr><th scope="row">{escape(labels["fields"][row["field"]])}</th><td>{_text(row, "value")}</td></tr>')
        if health["status"] == "collected" and not rows:
            raise ValueError("Collected SMART evidence requires data")
        if health["status"] == "unavailable" and rows:
            raise ValueError("Unavailable SMART evidence cannot contain data rows")
        meta = " · ".join(_text(health, key) for key in ("tool", "captured_at"))
        _text(health, "scope")  # Delivery context is retained in the input, not printed.
        body = '<table class="smart-table"><tbody>' + ''.join(cells) + '</tbody></table>' if cells else ""
        top = (f'<section id="device-health"><h2>{escape(labels["health_title"])}</h2>'
               f'<p class="muted">{meta}</p>{body}</section>')
        if health["status"] == "unavailable":
            top = ""
    if not isinstance(reviews, list) or len(reviews) > 10:
        raise ValueError("At most 10 online reviews are accepted; select before rendering")
    selection = evidence.get("review_selection")
    if not reviews and selection is None:
        return top, "", recommendation
    selection_text = _text(evidence, "review_selection") if selection is not None else ""
    items, urls = [], set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Invalid review")
        url = urlsplit(review.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password or review["url"] in urls:
            raise ValueError("Reviews require unique public HTTPS source URLs")
        urls.add(review["url"])
        date.fromisoformat(review.get("checked_at", ""))
        measurements = review.get("measurements", [])
        if not isinstance(measurements, list) or len(measurements) > 32:
            raise ValueError("Invalid review measurements")
        metrics = []
        for measurement in measurements:
            if not isinstance(measurement, dict):
                raise ValueError("Invalid review measurement")
            value, unit, qualifier = measurement.get("value"), measurement.get("unit", ""), measurement.get("qualifier", "exact")
            valid_value = ((type(value) in (int, float) and math.isfinite(value) and value >= 0)
                           or (isinstance(value, str) and bool(value.strip()) and len(value) <= 200))
            if (not valid_value or not isinstance(unit, str) or len(unit) > 40
                    or qualifier not in ("exact", "approximate", "greater_than", "less_than")):
                raise ValueError("Review measurements need a measured value, range or text")
            prefix = {"exact": "", "approximate": "≈ ", "greater_than": "> ", "less_than": "< "}[qualifier]
            metrics.append(f'<div><span>{_text(measurement, "label")}</span><strong><bdi>{escape((prefix + str(value) + " " + unit).strip())}</bdi></strong></div>')
        metric_html = '<div class="result-performance">' + ''.join(metrics) + '</div>' if metrics else ''
        context = review.get("context", [])
        if not isinstance(context, list) or len(context) > 16 or not all(isinstance(item, dict) for item in context):
            raise ValueError("Review context must contain label/value facts")
        path = ''.join(f'<div><dt>{_text(item, "label")}</dt><dd>{_text(item, "value")}</dd></div>' for item in context)
        path += ''.join(f'<div><dt>{escape(labels[key])}</dt><dd>{_text(review, key)}</dd></div>' for key in ("interface", "controller", "tool") if key in review)
        path_html = '<dl class="device-path">' + path + '</dl>' if path else ''
        details = ''.join(f'<dt>{escape(labels[key])}</dt><dd>{_text(review, key)}</dd>' for key in ("conditions", "results", "comparison") if key in review and (key != "comparison" or not verified_only or review.get("comparison_status") == "supported"))
        sections = review.get("sections", [])
        if not isinstance(sections, list) or len(sections) > 12 or not all(isinstance(section, dict) for section in sections):
            raise ValueError("Invalid custom review sections")
        for section in sections:
            if section.get("evidence_status") not in (None, "supported", "needs_followup"):
                raise ValueError("Invalid review section evidence status")
            paragraphs = section.get("paragraphs")
            if not isinstance(paragraphs, list) or not paragraphs or len(paragraphs) > 10:
                raise ValueError("Review sections need text paragraphs")
            title = _text(section, "title")
            body = ''.join(f'<p>{_text({"paragraph": paragraph}, "paragraph")}</p>' for paragraph in paragraphs)
            if not verified_only or section.get("evidence_status") == "supported":
                details += f'<dt>{title}</dt><dd>{body}</dd>'
        detail_html = '<dl class="review-details">' + details + '</dl>' if details else ''
        heading = _text(review, "title" if "title" in review else "drive_model" if "drive_model" in review else "label")
        capacity = f' <span class="result-capacity">{_text(review, "capacity")}</span>' if "capacity" in review else ''
        url = escape(review["url"], quote=True)
        items.append(f'<article class="online-review result"><div class="result-device"><h3><a href="{url}" rel="noreferrer">{heading}</a>{capacity}</h3>{path_html}</div>{metric_html}{detail_html}'
                     + f'<div class="result-contributor"><div><a href="{url}" rel="noreferrer">{_text(review, "label")} ↗</a><span>{escape(labels["checked_at"])} <time datetime="{escape(review["checked_at"])}">{escape(review["checked_at"])}</time></span></div><a href="{url}" rel="noreferrer">{escape(labels["view_review"])} →</a></div><p class="review-source-url">{url}</p></article>')
    appendix = (f'<section id="online-reviews"><h2>{escape(labels["reviews_title"])}</h2>{("<p>" + selection_text + "</p>") if not verified_only else ""}'
                + ''.join(items) + '</section>')
    return top, appendix, recommendation
