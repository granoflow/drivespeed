"""Localizable human progress and stable machine events; no external dependencies."""

import json
import locale
import sys
import string
import time
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def language_default():
    return (locale.getlocale()[0] or "en").replace("_", "-")


def messages(language, override=None):
    base = json.loads((ASSETS / "locales/en.json").read_text(encoding="utf-8"))
    key = language.split("-")[0].lower()
    built = ASSETS / "locales" / f"{key}.json"
    if key != "en" and built.exists():
        base.update(json.loads(built.read_text(encoding="utf-8")))
    elif key != "en" and not override:
        raise ValueError(
            f"Language {language}: provide --messages / --ui-messages JSON translated from assets/locales/en.json"
        )
    if override:
        translated = json.loads(Path(override).read_text(encoding="utf-8"))
        if set(translated) != set(base) or not all(
            isinstance(v, str) for v in translated.values()
        ):
            raise ValueError(
                "Translation must contain exactly the string keys in assets/locales/en.json"
            )
        for key, value in translated.items():
            fields = lambda s: [
                name
                for _, name, _, _ in string.Formatter().parse(s)
                if name is not None
            ]
            if fields(value) != fields(base[key]):
                raise ValueError(f"Translation placeholder mismatch: {key}")
        base = translated
    return base


class Progress:
    def __init__(self, labels, text, event_path=None):
        self.labels, self.text, self.event_path = labels, text, event_path
        self.completed = 0
        self.last = 0
        self.last_stage = None
        self.started = time.monotonic()
        print(
            text["steps"] + ": " + " → ".join(text.get(x, x) for x in labels),
            file=sys.stderr,
        )

    def emit(self, stage, status="running", percent=None, remaining=None, force=False):
        now = time.monotonic()
        if (
            not force
            and stage == self.last_stage
            and now - self.last < (1 if sys.stderr.isatty() else 5)
        ):
            return
        self.last, self.last_stage = now, stage
        if status == "completed":
            self.completed += 1
            remaining = 0
        pct = min(100, max(0, percent)) if percent is not None else None
        event = dict(
            stage=stage,
            status=status,
            completed_steps=self.completed,
            total_steps=len(self.labels),
            step_percent=pct,
            overall_percent=round(self.completed / len(self.labels) * 100, 1),
            step_remaining_seconds=remaining,
            overall_remaining_seconds=0 if self.completed == len(self.labels) else None,
            elapsed_seconds=round(now - self.started, 2),
            eta_basis="observed_io_and_runtime_limit"
            if remaining is not None
            else "unavailable",
        )
        if self.event_path:
            with self.event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        current = self.text["unknown_percent"] if pct is None else f"{pct:.0f}%"
        eta = (
            self.text["unknown_eta"]
            if remaining is None
            else self.text["eta"].format(seconds=round(remaining))
        )
        line = (
            f"[{self.labels.index(stage) + 1}/{len(self.labels)}] "
            f"{self.text.get(stage, stage)} · {self.text.get(status, status)} · {current} · "
            f"{self.text['overall']} {event['overall_percent']}% · {eta}"
            + (
                f" · {self.text['overall_eta_unknown']}"
                if self.completed < len(self.labels)
                else ""
            )
        )
        print(line, file=sys.stderr, flush=True)
