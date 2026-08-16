#!/usr/bin/env python3
"""Fill the fields of papers.bib that the ORCID sync deliberately leaves alone.

Three subcommands, meant to be driven by the `curate-publications` skill:

  report           What is missing per entry, plus the preview images on disk,
                   as JSON. Read this first to know what to ask about.
  fetch-abstracts  Fill missing `abstract` fields from bioRxiv or Crossref.
                   No human judgement involved, so it just does it.
  apply            Write decisions collected from the user back into the file.

`abstract` is machine-derivable and so is never asked about. `preview`,
`selected`, and co-first-author asterisks are not present in any bibliographic
source and can only come from the author.

Run from the repository root.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.request

from bibfile import parse_entries, replace_entries

BIB_PATH = "_bibliography/papers.bib"
PREVIEW_DIR = "assets/img/publication_preview"
USER_AGENT = "jlotthammer.github.io curation (mailto:j.lotthammer@wustl.edu)"


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def clean_abstract(text: str) -> str:
    """Strip the JATS markup Crossref wraps abstracts in, and normalize space.

    Braces would corrupt the surrounding BibTeX field, so they are removed
    rather than escaped.
    """
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"^\s*Abstract\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def fetch_abstract(doi: str) -> str | None:
    """Try bioRxiv first for preprints, then Crossref. None if neither has one."""
    if doi.lower().startswith("10.1101/"):
        try:
            data = http_json(f"https://api.biorxiv.org/details/biorxiv/{doi}")
            collection = data.get("collection") or []
            if collection and collection[-1].get("abstract"):
                return clean_abstract(collection[-1]["abstract"])
        except Exception as exc:  # noqa: BLE001 - fall through to Crossref
            print(f"  bioRxiv lookup failed for {doi}: {exc}", file=sys.stderr)
    try:
        message = http_json(f"https://api.crossref.org/works/{doi}")["message"]
    except Exception as exc:  # noqa: BLE001 - reported by caller
        print(f"  Crossref lookup failed for {doi}: {exc}", file=sys.stderr)
        return None
    abstract = message.get("abstract")
    return clean_abstract(abstract) if abstract else None


def preview_images() -> list[str]:
    if not os.path.isdir(PREVIEW_DIR):
        return []
    return sorted(f for f in os.listdir(PREVIEW_DIR) if not f.startswith("."))


def load_entries():
    with open(BIB_PATH, encoding="utf-8") as f:
        text = f.read()
    return text, [e for e in parse_entries(text) if e.title]


def cmd_report(_args) -> int:
    _, entries = load_entries()
    report = {
        "available_preview_images": preview_images(),
        "entries": [
            {
                "key": e.key,
                "title": e.title,
                "journal": e.journal,
                "year": e.field("year"),
                "doi": e.doi,
                "authors": e.field("author"),
                "has_abstract": bool(e.field("abstract")),
                "preview": e.field("preview"),
                "selected": bool(e.field("selected")),
                "has_cofirst_marks": "*," in (e.field("author") or ""),
                "needs": [
                    name
                    for name, missing in (
                        ("abstract", not e.field("abstract")),
                        ("preview", not e.field("preview")),
                    )
                    if missing
                ],
            }
            for e in entries
        ],
    }
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def cmd_fetch_abstracts(_args) -> int:
    text, entries = load_entries()
    rewrites: dict[str, str] = {}
    for entry in entries:
        if entry.field("abstract"):
            continue
        if not entry.doi:
            print(f"SKIP {entry.key}: no DOI to look up", file=sys.stderr)
            continue
        abstract = fetch_abstract(entry.doi)
        if abstract:
            rewrites[entry.key] = entry.with_fields({"abstract": abstract})
            print(f"ADDED abstract for {entry.key} ({len(abstract)} chars)")
        else:
            print(f"NOT FOUND {entry.key}: no abstract at bioRxiv or Crossref", file=sys.stderr)
    if rewrites:
        with open(BIB_PATH, "w", encoding="utf-8") as f:
            f.write(replace_entries(text, rewrites))
    print(f"{len(rewrites)} abstracts added")
    return 0


def cmd_apply(args) -> int:
    """Apply {entry_key: {preview, selected, author}} from a JSON file.

    Any key omitted for an entry is left as it is, so a user's "skip" is
    expressed by leaving the field out rather than sending an empty value.
    """
    with open(args.decisions, encoding="utf-8") as f:
        decisions = json.load(f)

    text, entries = load_entries()
    by_key = {e.key: e for e in entries}
    unknown = sorted(set(decisions) - set(by_key))
    if unknown:
        print(f"ERROR: no such entries: {', '.join(unknown)}", file=sys.stderr)
        return 1

    rewrites: dict[str, str] = {}
    for key, fields in decisions.items():
        updates = {k: v for k, v in fields.items() if k in ("preview", "author")}
        remove: list[str] = []
        if "selected" in fields:
            # jekyll-scholar reads presence as truth, so unsetting means deleting.
            if fields["selected"]:
                updates["selected"] = "true"
            else:
                remove.append("selected")
        if not updates and not remove:
            continue
        rewrites[key] = by_key[key].with_fields(updates, remove=remove)
        changed = list(updates) + [f"-{name}" for name in remove]
        print(f"UPDATED {key}: {', '.join(changed)}")

    if rewrites:
        with open(BIB_PATH, "w", encoding="utf-8") as f:
            f.write(replace_entries(text, rewrites))
    print(f"{len(rewrites)} entries updated")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report").set_defaults(func=cmd_report)
    sub.add_parser("fetch-abstracts").set_defaults(func=cmd_fetch_abstracts)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--decisions", required=True, help="path to a JSON decisions file")
    apply_parser.set_defaults(func=cmd_apply)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
