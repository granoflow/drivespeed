#!/usr/bin/env python3
"""Prepare a readable community report and keep its typed state privately on disk.

JSON is an internal protocol for agents. Normal user conversations show the
preview fields, not this command's machine-readable envelope. No network in the
offline commands implemented here.
"""

import argparse
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import traceback

from community_data import CommunityError, parse_json, preview, project_result, notice_details
from community_store import CommunityStore
from community_network import CommunityClient, CommunityHTTP, NetworkError
from progress import language_default


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # Unknown argv can include secrets or private values; do not echo it.
        raise CommunityError("invalid_arguments", ["arguments"])


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="Private typed SQLite state directory; no request JSON is saved")
    common.add_argument("--ui-language", default=argparse.SUPPRESS, help="UI language: en, zh, ar or a complete custom message file")
    common.add_argument("--ui-messages", default=argparse.SUPPRESS, help="Complete translation based on assets/community/locales/en.json")
    root = Parser(description=__doc__, parents=[common])
    commands = root.add_subparsers(dest="operation", required=True, parser_class=Parser)
    prepare = commands.add_parser("prepare", parents=[common], help="Offline public-field projection and readable preview")
    prepare.add_argument("--input", required=True, help="Existing local results.json, read only")
    prepare.add_argument("--x-profile", required=True, help="Your X profile link; public reports must include it")
    prepare.add_argument("--intro", default="", help="Optional plain one-line introduction")
    showing = commands.add_parser("preview", parents=[common], help="Reopen the frozen readable preview offline; no original input needed")
    showing.add_argument("--submission-id", required=True)
    consent = commands.add_parser("consent", parents=[common], help="Record one explicit user decision after the preview")
    consent.add_argument("--submission-id", required=True)
    consent.add_argument("--decision", required=True, choices=["publish", "declined"])
    consent.add_argument("--notice-version", help="Required for publish; use the version returned by prepare/preview")
    listing = commands.add_parser("list", parents=[common], help="Read local report state; no network")
    listing.add_argument("--state", choices=["draft", "pending", "accepted", "withdrawn", "declined"])
    remember = commands.add_parser("remember", parents=[common], help="Remember refusal and retain the existing result pointer locally")
    remember.add_argument("--input", required=True)
    remember.add_argument("--decision", required=True, choices=["declined"])
    compare = commands.add_parser("compare", parents=[common], help="Offline comparison facts from typed reference snapshots; no network or rankings")
    compare.add_argument("--input", required=True, help="Preserved original result for this exact submission")
    compare.add_argument("--submission-id", required=True)
    for name in ("publish", "retry", "receipt", "withdraw"):
        network = commands.add_parser(name, parents=[common], help={
            "publish": "Share this consented snapshot; shows a browser URL and waits up to 10 minutes",
            "retry": "Recover the original receipt first; authorize again only if no report was received",
            "receipt": "Fetch the current receipt without X sign-in; optionally refresh reference samples",
            "withdraw": "Offline withdrawal preview; --confirm explicitly sends DELETE"}[name])
        network.add_argument("--submission-id", required=True)
        network.add_argument("--test-origin", help="Developer fixtures only: explicit http://127.0.0.1:PORT or http://[::1]:PORT; never an external service")
        if name == "receipt":
            network.add_argument("--refresh-similar", action="store_true", help="Explicitly fetch a new reference snapshot, replacing the old cache")
        if name == "withdraw":
            network.add_argument("--confirm", action="store_true", help="After explicit user authorization, withdraw the identified report remotely; quota is not refunded")
    return root


def _read_json_file(filename, limit=8 * 1024 * 1024):
    path = Path(filename).expanduser().absolute()
    if sys.platform == "win32":
        from community_storage import read_windows_input
        return path, parse_json(read_windows_input(path, limit), limit=limit)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise CommunityError("unsafe_input_path")
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise CommunityError("invalid_input_file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or info.st_size > limit:
                raise CommunityError("invalid_input_file")
            result = parse_json(source.read(limit + 1), limit=limit)
    except (OSError, ValueError) as error:
        if isinstance(error, CommunityError):
            raise
        raise CommunityError("input_unavailable") from None
    return path, result


def read_result(filename):
    path, result = _read_json_file(filename)
    if not isinstance(result, dict) or not isinstance(result.get("run_id"), str) or not 1 <= len(result["run_id"]) <= 128:
        raise CommunityError("missing_parameters", ["run_id"])
    return path, result



def _messages(language="en", filename=None):
    directory = Path(__file__).resolve().parents[1] / "assets/community/locales"
    template = json.loads((directory / "en.json").read_text(encoding="utf-8"))
    if filename is not None:
        try:
            _, result = _read_json_file(filename, limit=64 * 1024)
            if not isinstance(result, dict) or set(result) != set(template):
                raise CommunityError("invalid_messages", ["ui_messages"])
            for key, value in template.items():
                if isinstance(value, str):
                    valid = isinstance(result[key], str) and 0 < len(result[key]) <= 2000
                else:
                    valid = isinstance(result[key], dict) and set(result[key]) == set(value) and all(
                        isinstance(text, str) and 0 < len(text) <= 2000 for text in result[key].values())
                if not valid:
                    raise CommunityError("invalid_messages", ["ui_messages"])
            return result
        except (CommunityError, ValueError, OSError):
            raise CommunityError("invalid_messages", ["ui_messages"]) from None
    base = language.lower().replace("_", "-").split("-", 1)[0].split(".", 1)[0]
    if base not in ("en", "zh", "ar"):
        raise CommunityError("unsupported_language", ["ui_language"])
    return json.loads((directory / (base + ".json")).read_text(encoding="utf-8"))


def _error_group(code):
    if code == "management_token_missing":
        return "credentials", code
    if code == "operation_busy":
        return "busy", code
    if code == "samples_unavailable":
        return "samples", code
    if code in ("invalid_arguments",):
        return "arguments", "invalid_arguments"
    if code in ("input_unavailable", "invalid_input_file", "unsafe_input_path"):
        return "input_access", "input_unavailable"
    if code == "missing_parameters":
        return "parameters", code
    if code == "consent_required":
        return "consent", code
    if code == "integrity_mismatch":
        return "integrity", code
    if code == "not_found":
        return "not_found", code
    if code.startswith("unsafe_state") or code in ("local_storage_failed", "unrecognized_state_database"):
        return "state", "local_storage_failed"
    if code in ("unsupported_snapshot_version", "unsupported_state_version"):
        return "version", "unsupported_snapshot_version"
    if code == "content_conflict":
        return "conflict", code
    if code in ("unsupported_language", "invalid_messages"):
        return "language", "unsupported_language"
    if code in ("validation_failed", "invalid_json", "duplicate_key", "invalid_number", "invalid_unicode", "payload_too_large", "no_measurements", "too_deep"):
        return "input", "validation_failed"
    return "generic", "invalid_arguments"


def _failure(operation, error, ui, state_attempted=False, cause=None, remote_state="none"):
    group, anchor = _error_group(error.code)
    if isinstance(error, NetworkError):
        group = "authorization" if error.code.startswith("auth_") or error.code == "account_mismatch" else (
            "http" if error.status is not None and error.status < 500 and error.status != 429 else "network")
        anchor = "network_recovery"
    message = ui[group]
    detail = {"code": error.code, "message": message["message"], "fields": error.fields,
              "next_action": message["next_action"], "docs": "references/community.md#" + anchor,
              "side_effects": {"remote": remote_state, "local": "check_history" if state_attempted else "none"},
              "context": {"python_version": ".".join(str(value) for value in sys.version_info[:3]), "state_schema_version": 2}}
    if isinstance(error, NetworkError):
        detail.update({"http_status": error.status, "retry_after_seconds": error.retry_after, "contact_url": error.contact,
                       "quota": error.quota, "request_id": error.request_id})
    if cause is not None:
        detail["context"]["cause_type"] = type(cause).__name__
        if isinstance(cause, sqlite3.Error):
            detail["context"]["sqlite_error"] = getattr(cause, "sqlite_errorname", "unknown")
        elif isinstance(cause, OSError):
            detail["context"]["errno"] = cause.errno
        detail["trace"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                           for frame in traceback.extract_tb(cause.__traceback__)[-8:]]
    print(json.dumps({"schema_version": 1, "operation": operation, "status": "error", "error": detail}, ensure_ascii=False))
    print(message["message"] + " " + message["next_action"], file=sys.stderr)
    if group in ("state", "busy", "network") or cause is not None:
        return 5
    if isinstance(error, NetworkError):
        return 3 if error.status == 404 else 4
    return 3 if group in ("not_found", "integrity", "version", "credentials") else 2

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    operation = None
    state_attempted = False
    client = None
    ui = _messages("en")
    try:
        options = [arg.split("=", 1)[0] for arg in argv if arg.startswith("--")]
        if len(options) != len(set(options)):
            raise CommunityError("invalid_arguments", ["arguments"])
        args = parser().parse_args(argv)
        operation = args.operation
        ui = _messages(getattr(args, "ui_language", language_default()), getattr(args, "ui_messages", None))
        # Read and validate the original result before opening/writing cache.
        if operation in ("prepare", "remember", "compare"):
            source_path, result = read_result(args.input)
            if operation == "prepare":
                payload = project_result(result, args.x_profile, args.intro)
        state_attempted = True
        with CommunityStore(getattr(args, "state_dir", None)) as store:
            if operation == "prepare":
                identifier = store.create(payload, result["run_id"], source_path)
                output = {"status": "prepared", "submission_id": identifier, "preview": preview(store.payload(identifier)), **notice_details()}
            elif operation == "preview":
                output = {"status": "previewed", "submission_id": args.submission_id,
                          "preview": preview(store.payload(args.submission_id)), **notice_details()}
            elif operation == "consent":
                if args.decision == "declined":
                    state = store.decline(args.submission_id)
                    output = {"status": "declined", "submission_id": args.submission_id,
                              "local_state": state, "remote_unchanged": True}
                else:
                    if args.notice_version is None:
                        raise CommunityError("consent_required", ["notice_version"])
                    consent_id = store.consent(args.submission_id, args.notice_version, args.decision)
                    output = {"status": "consented", "submission_id": args.submission_id, "consent_id": consent_id}
            elif operation == "list":
                output = {"status": "listed", "items": store.list_reports(args.state)}
            elif operation == "compare":
                from community_comparison import build_comparison
                output = build_comparison(store, result, args.submission_id)
            elif operation in ("publish", "retry", "receipt", "withdraw"):
                def show_authorization(event):
                    print(json.dumps({"schema_version": 1, "operation": operation, "submission_id": args.submission_id,
                        **event, "message": ui["authorization_pending"]}, ensure_ascii=False), flush=True)
                client = CommunityClient(store, CommunityHTTP(args.test_origin), progress=show_authorization)
                if operation in ("publish", "retry"):
                    output = client.publish(args.submission_id)
                elif operation == "receipt":
                    output = client.receipt(args.submission_id, args.refresh_similar)
                else:
                    output = client.withdraw(args.submission_id, args.confirm)
            else:
                store.remember(result["run_id"], source_path)
                output = {"status": "remembered", "run_id": result["run_id"], "decision": "declined"}
        if "preview" in output:
            output["preview"]["notice"] = ui["share_notice"]
        print(json.dumps({"schema_version": 1, "operation": operation, **output}, ensure_ascii=False, allow_nan=False))
        return 0
    except CommunityError as error:
        return _failure(operation, error, ui, state_attempted, remote_state=client.remote if client else "none")
    except KeyboardInterrupt:
        print("Community operation cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError, sqlite3.Error) as error:
        return _failure(operation, CommunityError("local_storage_failed"), ui, state_attempted, cause=error,
                        remote_state=client.remote if client else "none")



if __name__ == "__main__":
    sys.exit(main())
