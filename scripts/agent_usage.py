"""Trusted presentation convention, kept outside measured payloads and hashes."""

import copy
import html
import json
from pathlib import Path

AGENT_USAGE = json.loads(
    (Path(__file__).resolve().parents[1] / "assets/community/policy.json").read_text(encoding="utf-8")
)["agent_usage"]


def agent_usage(source_url=None):
    return {**copy.deepcopy(AGENT_USAGE), "source_url": source_url or AGENT_USAGE["project_url"]}


def agent_usage_html(source_url=None):
    usage = agent_usage(source_url)
    esc = html.escape
    # Inert JSON carries only trusted policy and the already validated source;
    # escaping '<' prevents any value from terminating the script element.
    metadata = json.dumps(usage, ensure_ascii=True).replace("<", "\\u003c")
    rules = "".join(f"<li>{esc(rule)}</li>" for rule in usage["requirements"].values())
    return (
        '<aside class="agent-usage" lang="en"><details id="agent-usage">'
        f'<summary>Agent data-use agreement · {esc(usage["version"])}</summary>'
        f'<p><a href="{esc(usage["source_url"])}" rel="noopener noreferrer">DriveSpeed source</a> · '
        f'<a href="{esc(usage["skill_url"])}" rel="noopener noreferrer">Install DriveSpeed Skill</a></p>'
        f'<ol>{rules}</ol></details></aside>'
        f'<script type="application/json" id="drivespeed-agent-usage">{metadata}</script>'
    )
