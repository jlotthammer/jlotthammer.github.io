#!/usr/bin/env python3
"""Sync new publications from ORCID into _bibliography/papers.bib.

papers.bib is the sole source of truth for the site's publication list, so a
merged change propagates to /publications/ and the homepage on the next deploy.

The hard part is not finding new papers, it is recognising that a "new" paper
is really an existing preprint entry that has now been published. Titles
routinely change between preprint and version of record — STARLING went from
"Accurate predictions of conformational ensembles of disordered proteins with
STARLING" to "Accurate predictions of disordered protein ensembles with
STARLING", which is only 0.76 similar — so title matching alone would append a
duplicate. Instead, each candidate is matched against existing entries by, in
order:

  1. DOI;
  2. exact normalized title;
  3. Crossref's `relation.has-preprint` / `is-preprint-of`, resolving the
     linked preprint's own DOI and title back to an entry.

Step 3 is what catches a retitled paper. Note the preprint DOI a publisher
declares need not be the server the entry cites: Nature's record for STARLING
links the Research Square posting, not the bioRxiv one, so the linked preprint
is resolved and matched on its title as well as its DOI.

A matched preprint entry is upgraded in place — only `journal`, `volume`,
`pages`, and `doi` are rewritten. `abstract`, `author` (with its co-first-author
asterisks), `preview`, and `selected` are hand-curated and left untouched.

Run from the repository root.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass

from bibfile import (
    PREPRINT_SERVERS,
    Entry,
    normalize_title,
    parse_entries,
    replace_entries,
    sort_bibliography,
)

ORCID_ID = "0000-0002-5022-7006"
BIB_PATH = "_bibliography/papers.bib"
USER_AGENT = "jlotthammer.github.io publication sync (mailto:j.lotthammer@wustl.edu)"

# Reported, never acted on: close enough to be worth a human look, not close
# enough to rewrite an entry on.
NEAR_MATCH_RATIO = 0.90

# Search fallback thresholds. Publishers do not always register a preprint
# link, so a preprint entry with no authoritative link is also searched for by
# title and author. Two guards keep that from going wrong:
#
#   * Conference abstracts are indexed as `journal-article` with the same
#     authors and a near-identical title. Biophysical Journal's BPS2026
#     abstract for STARLING scores 0.95 against the preprint title while the
#     real Nature paper scores 0.76 — ranking on title alone picks the wrong
#     one. Abstracts carry no reference list, so a minimum reference count
#     separates them from research articles.
#   * A candidate must share the first author and nearly all other authors,
#     and only an unambiguous single survivor is applied.
MIN_REFERENCES = 5
SEARCH_ROWS = 20

# Matching is scored across several independent signals rather than gated on
# one, because every individual signal is unreliable in a different way:
#
#   * Titles get reworded on acceptance. STARLING scored 0.76 preprint to
#     published, so a high title bar rejects true matches — but a conference
#     abstract scored 0.95, so a low bar accepts false ones.
#   * Author lists grow, shrink, and reorder. The finches preprint has 5
#     authors against the published paper's 9, and Research Square lists
#     Holehouse first for STARLING where bioRxiv lists Novak. So neither an
#     exact list nor a fixed first author can be required; what matters is
#     that the preprint's authors are largely retained somewhere in the
#     published list.
#   * Abstracts are the most distinctive signal when present, but Springer
#     deposits no abstract to Crossref, so it is unavailable for exactly the
#     Nature papers here. It corroborates when available and is skipped
#     otherwise.
#
# A candidate must clear the floors below, then earn enough total confidence
# from whichever signals exist. Requiring corroboration from a second signal
# stops any one of them deciding alone.
MIN_AUTHOR_RETAINED = 0.5  # half the preprint's authors must survive
MIN_TITLE_RATIO = 0.5
STRONG_TITLE_RATIO = 0.85
STRONG_ABSTRACT_RATIO = 0.75
CORROBORATING_ABSTRACT_RATIO = 0.55
CORROBORATING_TITLE_RATIO = 0.6
CORROBORATING_AUTHOR_RETAINED = 0.6


def http_get(url: str, accept: str) -> str:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def crossref_work(doi: str) -> dict | None:
    """Return the Crossref record for `doi`, or None if it has none."""
    try:
        return json.loads(http_get(f"https://api.crossref.org/works/{doi}", "application/json"))[
            "message"
        ]
    except Exception as exc:  # noqa: BLE001 - absence is normal, e.g. datacite DOIs
        print(f"  no Crossref record for {doi}: {exc}", file=sys.stderr)
        return None


def fetch_bibtex(doi: str) -> str:
    return http_get(f"https://doi.org/{doi}", "application/x-bibtex").strip()


def is_preprint_journal(journal: str) -> bool:
    return any(server in journal.lower() for server in PREPRINT_SERVERS)


def orcid_works() -> list[dict]:
    """Return [{"title", "journal", "doi"}] from the public ORCID API."""
    data = json.loads(http_get(f"https://pub.orcid.org/v3.0/{ORCID_ID}/works", "application/json"))
    works = []
    for group in data.get("group", []):
        summary = group["work-summary"][0]
        title = (summary.get("title") or {}).get("title", {}).get("value")
        if not title:
            continue
        doi = next(
            (
                ext["external-id-value"]
                for ext in group.get("external-ids", {}).get("external-id", [])
                if ext.get("external-id-type") == "doi"
            ),
            None,
        )
        works.append(
            {
                "title": title,
                "journal": (summary.get("journal-title") or {}).get("value") or "",
                "doi": doi,
            }
        )
    return works


def linked_preprint_dois(record: dict) -> list[str]:
    """DOIs of preprints the publisher links to this record."""
    relation = record.get("relation") or {}
    return [
        item["id"]
        for name in ("has-preprint", "is-preprint-of")
        for item in relation.get(name, [])
        if item.get("id-type") == "doi" and item.get("id")
    ]


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def record_fields(record: dict) -> dict[str, str | None]:
    """Bibliographic fields of a Crossref record, in BibTeX terms.

    `year` matters beyond the citation string: _config.yml groups the
    publications page by year, so an entry still carrying its preprint year
    is filed under the wrong heading. `issued` is Crossref's earliest
    publication date, which is when the paper actually appeared.
    """
    container = record.get("container-title") or []
    date_parts = ((record.get("issued") or {}).get("date-parts") or [[]])[0]
    year = str(date_parts[0]) if date_parts else None
    month = MONTHS[date_parts[1] - 1] if len(date_parts) > 1 and 1 <= date_parts[1] <= 12 else None
    return {
        "journal": container[0] if container else None,
        "volume": record.get("volume"),
        "pages": record.get("page"),
        "doi": record.get("DOI"),
        "year": year,
        "month": month,
    }


def author_families(bibtex_authors: str) -> list[str]:
    """Family names from a BibTeX author string, in order.

    Entries use `Family, Given and Family, Given`, with an asterisk marking
    co-first authorship (`Lotthammer*, Jeffrey M`) that is not part of the name.
    """
    families = []
    for name in bibtex_authors.split(" and "):
        family = name.split(",")[0].strip().replace("*", "")
        if family:
            families.append(family.lower())
    return families


def record_families(record: dict) -> list[str]:
    return [(author.get("family") or "").lower() for author in record.get("author") or []]


def author_retention(wanted: list[str], record: dict) -> float:
    """Fraction of the preprint's authors that survive into `record`.

    Measured against the preprint's list, not the union, so authors *added*
    on publication cost nothing — the finches preprint's 5 authors all appear
    among the published paper's 9 and score 1.0. Authors dropped or renamed
    do reduce it, which is the drift worth being suspicious of.
    """
    if not wanted:
        return 0.0
    found = set(record_families(record))
    return sum(1 for family in wanted if family in found) / len(wanted)


def text_similarity(left: str, right: str) -> float:
    """Word-level similarity, robust to the reordering copy-editing produces."""
    return difflib.SequenceMatcher(None, left.split(), right.split()).ratio()


def normalize_abstract(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"^\s*abstract\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", text.lower())).strip()


@dataclass
class Evidence:
    """How strongly a candidate looks like the published version of a preprint."""

    title: float
    authors: float
    abstract: float | None

    def summary(self) -> str:
        parts = [f"title {self.title:.2f}", f"authors {self.authors:.2f}"]
        parts.append("abstract n/a" if self.abstract is None else f"abstract {self.abstract:.2f}")
        return ", ".join(parts)

    @property
    def confident(self) -> bool:
        """True when the signals that exist agree it is the same paper.

        Any one strong signal needs a second to corroborate it, so a reworded
        title alone or a shared abstract alone is never enough.
        """
        if self.authors < MIN_AUTHOR_RETAINED:
            return False
        authors_agree = self.authors >= CORROBORATING_AUTHOR_RETAINED
        # A closely matching abstract outweighs any title evidence, so it is
        # allowed past the title floor: papers get retitled far more often
        # than two different papers share an abstract.
        if self.abstract is not None and self.abstract >= STRONG_ABSTRACT_RATIO:
            return authors_agree or self.title >= CORROBORATING_TITLE_RATIO
        if self.title < MIN_TITLE_RATIO:
            return False
        if self.title >= STRONG_TITLE_RATIO:
            return authors_agree
        # Neither signal is strong alone: require both to be reasonable, with
        # the abstract corroborating if it exists at all.
        if self.title >= CORROBORATING_TITLE_RATIO and authors_agree:
            return self.abstract is None or self.abstract >= CORROBORATING_ABSTRACT_RATIO
        return False


def weigh_candidate(
    title: str, families: list[str], abstract: str, candidate: dict
) -> Evidence:
    candidate_titles = candidate.get("title") or []
    title_ratio = max(
        (
            difflib.SequenceMatcher(None, normalize_title(title), normalize_title(other)).ratio()
            for other in candidate_titles
        ),
        default=0.0,
    )
    candidate_abstract = candidate.get("abstract")
    abstract_ratio = None
    if abstract and candidate_abstract:
        abstract_ratio = text_similarity(
            normalize_abstract(abstract), normalize_abstract(candidate_abstract)
        )
    return Evidence(title_ratio, author_retention(families, candidate), abstract_ratio)


def crossref_search(title: str) -> list[dict]:
    query = urllib.parse.urlencode({"query.bibliographic": title, "rows": str(SEARCH_ROWS)})
    try:
        data = json.loads(http_get(f"https://api.crossref.org/works?{query}", "application/json"))
    except Exception as exc:  # noqa: BLE001 - searched again next run
        print(f"  Crossref search failed for “{title[:50]}”: {exc}", file=sys.stderr)
        return []
    return data["message"]["items"]


def find_published_version(
    title: str, families: list[str], abstract: str = ""
) -> tuple[dict | None, list[str]]:
    """Search Crossref for the published version of a preprint.

    Takes a title and author family names rather than an Entry, because the
    same search is needed both for preprint entries already in the file and
    for preprints arriving from ORCID that would otherwise be appended as
    preprints and never revisited.

    Returns (record, rejected_descriptions). The record is returned only when
    exactly one candidate survives every guard, so an ambiguous result is
    reported rather than applied.
    """
    qualified, rejected = [], []

    for item in crossref_search(title):
        if item.get("type") not in ("journal-article", "proceedings-article"):
            continue
        container = (item.get("container-title") or [None])[0]
        if not container or is_preprint_journal(container):
            continue

        evidence = weigh_candidate(title, families, abstract, item)
        where = f"{container} (doi:{item['DOI']})"

        # A meeting abstract carries its parent paper's title and authors, so
        # it can only be told apart by having no reference list of its own.
        if (item.get("reference-count") or 0) < MIN_REFERENCES:
            if evidence.confident:
                rejected.append(
                    f"{where} — matches on {evidence.summary()} but has no reference list, "
                    "so it is a meeting abstract rather than the paper"
                )
            continue
        if not evidence.confident:
            if evidence.authors >= MIN_AUTHOR_RETAINED:
                rejected.append(f"{where} — too weak a match: {evidence.summary()}")
            continue
        qualified.append((evidence, item))

    if len(qualified) == 1:
        return qualified[0][1], rejected
    for evidence, item in qualified:
        rejected.append(
            f"{(item.get('container-title') or [''])[0]} (doi:{item['DOI']}) — one of several "
            f"plausible matches ({evidence.summary()}), so none was applied"
        )
    return None, rejected


def find_superseded(
    record: dict, by_doi: dict[str, Entry], by_title: dict[str, Entry]
) -> Entry | None:
    """Find the existing entry that `record` is the published version of."""
    for preprint_doi in linked_preprint_dois(record):
        entry = by_doi.get(preprint_doi.lower())
        if entry:
            return entry
        # The linked preprint may sit on a different server than the entry
        # cites, so fall back to matching the preprint's own title.
        preprint = crossref_work(preprint_doi)
        titles = (preprint or {}).get("title") or []
        for title in titles:
            entry = by_title.get(normalize_title(title))
            if entry:
                return entry
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--notes-path",
        help="Write the report here as Markdown. Omitted, it is only printed.",
    )
    args = parser.parse_args()

    with open(BIB_PATH, encoding="utf-8") as f:
        text = f.read()
    entries = [e for e in parse_entries(text) if e.title]
    by_title = {normalize_title(e.title): e for e in entries}
    by_doi = {e.doi.lower(): e for e in entries if e.doi}

    new_bibtex: list[str] = []
    added: list[str] = []
    upgrades: list[str] = []
    near_matches: list[str] = []
    skipped: list[str] = []
    # Entry text to substitute, keyed by entry key. Also stops one entry being
    # upgraded twice when several ORCID works resolve to it.
    rewrites: dict[str, str] = {}

    def register(entry: Entry) -> None:
        """Index an entry so later passes recognise it as already present."""
        if entry.title:
            by_title[normalize_title(entry.title)] = entry
        if entry.doi:
            by_doi[entry.doi.lower()] = entry

    # First pass: rescue preprint entries whose published version exists but
    # was never linked by a publisher. This runs before the ORCID pass so that
    # an upgraded entry is already indexed under its published DOI — otherwise
    # ORCID's copy of the same paper looks new and gets appended alongside it.
    for entry in entries:
        if not entry.is_preprint:
            continue
        record, rejected = find_published_version(
            entry.title or "",
            author_families(entry.field("author") or ""),
            entry.field("abstract") or "",
        )
        if record:
            fields = record_fields(record)
            upgraded_raw = entry.with_fields(fields)
            rewrites[entry.key] = upgraded_raw
            register(Entry(entry.key, upgraded_raw, entry.start, entry.end))
            upgrades.append(
                f"- `{entry.key}`: *{entry.journal}* → **{fields['journal']}** "
                f"{fields['volume'] or ''}:{fields['pages'] or ''} (doi:{fields['doi']}), "
                f"{fields['year']}. Found by title and author search, not by a publisher "
                f"link — confirm it is the right paper. "
                f"Titled “{(record.get('title') or [''])[0]}”."
            )
        for note in rejected:
            skipped.append(f"- `{entry.key}`: considered and rejected {note}.")

    for work in orcid_works():
        key = normalize_title(work["title"])
        existing = by_title.get(key)
        if not existing and work["doi"]:
            existing = by_doi.get(work["doi"].lower())

        # Already present under this title or DOI: nothing to do.
        if existing:
            continue

        if not work["doi"]:
            skipped.append(
                f"- “{work['title']}” has no DOI on ORCID, so it could not be looked up. "
                "Add one there or enter the paper by hand."
            )
            continue

        record = crossref_work(work["doi"])

        # ORCID lists preprints as works in their own right. Appending one as
        # a preprint entry would leave it stale forever, since the rescue pass
        # above only sees entries that were already in the file. Resolve it to
        # its published version now, before deciding what to append.
        if record and record.get("type") == "posted-content":
            published, rejected = find_published_version(
                (record.get("title") or [""])[0],
                record_families(record),
                record.get("abstract") or "",
            )
            for note in rejected:
                skipped.append(f"- “{work['title']}”: considered and rejected {note}.")
            if published:
                skipped.append(
                    f"- “{work['title']}” is a preprint; used its published version in "
                    f"*{(published.get('container-title') or [''])[0]}* "
                    f"(doi:{published['DOI']}) instead."
                )
                record = published

        # Is this the published version of a preprint already listed?
        resolved_doi = (record or {}).get("DOI") or work["doi"]
        if resolved_doi.lower() in by_doi:
            continue

        superseded = find_superseded(record, by_doi, by_title) if record else None
        if superseded and superseded.key not in rewrites:
            if not superseded.is_preprint:
                skipped.append(
                    f"- doi:{work['doi']} supersedes `{superseded.key}`, but that entry is "
                    f"already recorded as *{superseded.journal}* — check it by hand."
                )
                continue
            fields = record_fields(record)
            if not fields["journal"] or is_preprint_journal(fields["journal"]):
                skipped.append(
                    f"- doi:{work['doi']} looked like the published version of "
                    f"`{superseded.key}` but names no journal; left unchanged."
                )
                continue
            upgraded_raw = superseded.with_fields(fields)
            rewrites[superseded.key] = upgraded_raw
            register(Entry(superseded.key, upgraded_raw, superseded.start, superseded.end))
            upgrades.append(
                f"- `{superseded.key}`: *{superseded.journal}* → **{fields['journal']}** "
                f"{fields['volume'] or ''}:{fields['pages'] or ''} (doi:{fields['doi']}). "
                f"Retitled “{work['title']}”. Abstract, authors, `preview`, and `selected` "
                "unchanged — update the title by hand if you want the new one."
            )
            continue
        if superseded:
            continue

        close = difflib.get_close_matches(key, by_title.keys(), n=1, cutoff=NEAR_MATCH_RATIO)
        if close:
            near_matches.append(
                f"- New work “{work['title']}” closely matches existing entry "
                f"`{by_title[close[0]].key}`. If it is the same paper, update that entry "
                "and drop the appended one."
            )

        # Fetch the resolved DOI, which may be the published version rather
        # than the preprint ORCID named.
        try:
            bibtex = fetch_bibtex(resolved_doi)
        except Exception as exc:  # noqa: BLE001 - skip this DOI, retried next run
            skipped.append(f"- BibTeX fetch failed for doi:{resolved_doi} ({exc}); will retry next run.")
            continue
        # Deduplicate on the *resolved* record, not on what ORCID reported.
        # ORCID routinely holds several records for one paper under slightly
        # different identifiers or author lists; they resolve to the same DOI
        # and title, which is the only reliable way to see they are one paper.
        fetched = parse_entries(bibtex)
        record_entry = fetched[0] if fetched else None
        if record_entry:
            if (record_entry.doi or "").lower() in by_doi:
                continue
            if normalize_title(record_entry.title or "") in by_title:
                continue
            register(record_entry)
        by_doi[resolved_doi.lower()] = record_entry or entries[0]
        new_bibtex.append(bibtex)
        added.append(f"- “{(record_entry.title if record_entry else work['title'])}” (doi:{resolved_doi})")

    # Upgrades and appends are one write, so the file is never left with only
    # half of a run's changes applied.
    if rewrites or new_bibtex:
        updated = replace_entries(text, rewrites)
        for entry in new_bibtex:
            updated += "\n" + entry + "\n"
        # Sort last, so an appended paper lands in date order rather than at
        # the bottom, and an upgraded entry moves to its publication year.
        with open(BIB_PATH, "w", encoding="utf-8") as f:
            f.write(sort_bibliography(updated))

    sections = [
        ("Appended to `papers.bib`", added),
        ("Preprints upgraded to the published version", upgrades),
        ("Possible duplicates", near_matches),
        ("Skipped — need a look", skipped),
    ]
    notes = [
        f"Automated ORCID sync: {len(added)} appended, {len(upgrades)} upgraded.",
        "",
        "Appended entries come from doi.org and carry no `preview`, `selected`, or"
        " co-first-author asterisks — add those by hand if wanted.",
    ]
    for heading, items in sections:
        if items:
            notes += ["", f"### {heading}", ""] + items
    if not any(items for _, items in sections):
        notes = ["Automated ORCID sync found nothing to change."]

    if args.notes_path:
        with open(args.notes_path, "w", encoding="utf-8") as f:
            f.write("\n".join(notes) + "\n")
    print("\n".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
