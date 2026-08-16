#!/usr/bin/env python3
"""Add missing `doi` fields to _bibliography/papers.bib from Crossref.

Entries in this file were written by hand and carry no DOI, which leaves the
Altmetric and Dimensions badges enabled in _config.yml unable to render (see
_layouts/bib.liquid: each badge needs a resolvable identifier) and gives
crawlers no canonical link to the published version.

A DOI is inserted only when the Crossref result's title matches the local
title exactly after normalization. A near-miss is reported and skipped rather
than guessed, because a wrong DOI silently attributes someone else's paper.

Intended as a one-off, but safe to re-run: entries that already have a `doi`
are left alone. Run from the repository root.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

from bibfile import normalize_title, parse_entries, replace_entries

BIB_PATH = "_bibliography/papers.bib"
# Crossref asks for a contact address to grant polite-pool rate limits.
USER_AGENT = "jlotthammer.github.io DOI backfill (mailto:j.lotthammer@wustl.edu)"


# Crossref registers supporting information, figures, and datasets as
# `component` records that carry the *same title* as their parent article, so
# an exact title match alone will happily return a DOI like
# `10.1021/acs.jcim.4c02005.s001` — the SI, not the paper. Components are
# therefore excluded outright, and a version of record is preferred over a
# preprint (`posted-content`) when Crossref holds both.
EXCLUDED_TYPES = {"component", "dataset", "peer-review"}
TYPE_PRIORITY = {"journal-article": 0, "proceedings-article": 0, "book-chapter": 1}
PREPRINT_TYPE_PRIORITY = 2


def crossref_lookup(title: str) -> tuple[str, str] | None:
    """Return (DOI, Crossref type) for the work whose title matches `title`."""
    query = urllib.parse.urlencode(
        {"query.bibliographic": title, "rows": "10", "select": "DOI,title,type"}
    )
    req = urllib.request.Request(
        f"https://api.crossref.org/works?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        items = json.load(resp)["message"]["items"]

    wanted = normalize_title(title)
    matches = []
    for item in items:
        item_type = item.get("type", "")
        if item_type in EXCLUDED_TYPES:
            continue
        if any(normalize_title(c) == wanted for c in item.get("title", [])):
            matches.append((TYPE_PRIORITY.get(item_type, PREPRINT_TYPE_PRIORITY), item["DOI"], item_type))

    if not matches:
        return None
    _, doi, item_type = min(matches, key=lambda m: m[0])
    return doi, item_type


def main() -> int:
    with open(BIB_PATH, encoding="utf-8") as f:
        text = f.read()

    replacements: dict[str, str] = {}
    unmatched: list[str] = []

    for entry in parse_entries(text):
        title = entry.title
        if not title or entry.doi:
            continue
        try:
            match = crossref_lookup(title)
        except Exception as exc:  # noqa: BLE001 - report and continue; re-runnable
            print(f"ERROR  {entry.key}: Crossref lookup failed: {exc}", file=sys.stderr)
            unmatched.append(entry.key)
            continue
        if match:
            doi, item_type = match
            replacements[entry.key] = entry.with_fields({"doi": doi})
            print(f"FOUND  {entry.key}: {doi}  [{item_type}]")
        else:
            print(f"NO EXACT TITLE MATCH  {entry.key}: {title[:70]}", file=sys.stderr)
            unmatched.append(entry.key)

    if replacements:
        with open(BIB_PATH, "w", encoding="utf-8") as f:
            f.write(replace_entries(text, replacements))

    print(f"\n{len(replacements)} DOIs added, {len(unmatched)} need manual review")
    if unmatched:
        print("Manual review: " + ", ".join(unmatched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
