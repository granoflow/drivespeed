"""Deterministic access-interface names; never infer a type from throughput."""
import argparse
import json
from pathlib import Path
import sys
import unicodedata


def interface_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).lower().split())


def confirmed_interface(name):
    if not isinstance(name,str) or not name.strip() or len(name) > 80 or any(unicodedata.category(c) in ("Cc","Cs","Zl","Zp") for c in name):
        raise ValueError("User-confirmed interface must contain 1 to 80 plain characters")
    return {"name":name,"source":"user"}


def normalise_interface(name, language="en", catalog=None):
    if not isinstance(name, str) or len(name) > 80:
        raise ValueError("Interface name must be text of at most 80 characters")
    if catalog is None:
        catalog = json.loads((Path(__file__).resolve().parents[1]/"assets/interface-types.json").read_text())
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1 or not isinstance(catalog.get("items"), list):
        raise ValueError("Unsupported interface catalog")
    key = interface_key(name)
    for item in catalog["items"]:
        aliases = [item["id"], *item["label"].values(), *item["aliases"]]
        if key and key in {interface_key(alias) for alias in aliases}:
            return {"status": "known", "original_name": name, "type_id": item["id"],
                    "name": item["label"]["zh" if language.startswith("zh") else "en"], "revision": catalog["revision"]}
    return {"status": "user" if key else "unknown", "original_name": name, "type_id": None,
            "name": name, "revision": catalog["revision"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--catalog-stdin", action="store_true", help="Read a current public interface dictionary in memory; do not cache JSON")
    args = parser.parse_args()
    try:
        catalog = None
        if args.catalog_stdin:
            raw = sys.stdin.buffer.read(262_145)
            if len(raw) > 262_144:
                raise ValueError("Interface catalog exceeds its size limit")
            catalog = json.loads(raw)
        print(json.dumps(normalise_interface(args.name,args.language,catalog),ensure_ascii=False))
        return 0
    except (KeyError, TypeError, ValueError, OSError):
        print("Invalid interface name or catalog",file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
