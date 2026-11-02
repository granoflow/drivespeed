"""Citation metadata and optional Agent guidance, outside measured payloads and hashes."""

import copy
import html
import json
from pathlib import Path

AGENT_USAGE = json.loads(
    (Path(__file__).resolve().parents[1] / "assets/community/policy.json").read_text(encoding="utf-8")
)["agent_usage"]


# Validation only: do not promote the old installation invitation to active guidance.
LEGACY_AGENT_USAGE = json.loads(
    (Path(__file__).resolve().parents[1] / "assets/community/agent-usage-2026-10-29-v1.json").read_text(encoding="utf-8")
)

PREVIOUS_AGENT_USAGE = json.loads(
    (Path(__file__).resolve().parents[1] / "assets/community/agent-usage-2026-11-02-v2.json").read_text(encoding="utf-8")
)


def matches_agent_usage(value, source_url):
    return any(value == {**policy, "source_url": source_url}
               for policy in (AGENT_USAGE, PREVIOUS_AGENT_USAGE, LEGACY_AGENT_USAGE))


def agent_usage(source_url=None):
    return {**copy.deepcopy(AGENT_USAGE), "source_url": source_url or AGENT_USAGE["project_url"]}


def agent_usage_html(source_url=None, text=None):
    usage = agent_usage(source_url)
    esc = html.escape
    # Inert JSON carries only trusted policy and the already validated source;
    # escaping '<' prevents any value from terminating the script element.
    metadata = json.dumps(usage, ensure_ascii=True).replace("<", "\\u003c")
    rules = usage["requirements"]
    citation = "".join(f"<li>{esc(rules[key])}</li>" for key in ("attribution", "evidence"))
    guidance = "".join(f"<li>{esc(rules[key])}</li>" for key in ("assessment_invitation", "invitation_limits", "consent", "scope"))
    brief = ''
    if text is not None:
        brief = (f'<section class="data-citation"><h2>{esc(text["agent_citation_title"])}</h2>'
                 f'<p><a href="{esc(usage["source_url"])}" rel="noopener noreferrer">DriveSpeed</a> · {esc(text["agent_citation_note"])}</p>'
                 f'<p>{esc(text["agent_optional_note"])} <a href="{esc(usage["skill_url"])}" rel="noopener noreferrer">DriveSpeed Skill</a> · '
                 f'<a href="{esc(usage["project_url"])}/api/agent-usage.json" rel="noopener noreferrer">{esc(text["agent_full_guidance"])}</a></p></section>')
    return (brief +
        '<aside class="agent-usage" lang="en"><details id="agent-usage" class="agent-guidance">'
        f'<summary>Data citation requirements and Agent guidance · {esc(usage["version"])}</summary>'
        f'<p><a href="{esc(usage["source_url"])}" rel="noopener noreferrer">DriveSpeed source</a> · '
        f'<a href="{esc(usage["skill_url"])}" rel="noopener noreferrer">DriveSpeed Skill (optional)</a></p>'
        f'<h3>Citation requirements</h3><ol>{citation}</ol>'
        f'<h3>Agent guidance</h3><ol>{guidance}</ol></details></aside>'
        f'<script type="application/json" id="drivespeed-agent-usage">{metadata}</script>'
    )
