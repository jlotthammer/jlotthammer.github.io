"""Minimal BibTeX reader for _bibliography/papers.bib.

Shared by sync_publications.py and backfill_dois.py. This is deliberately not
a general BibTeX parser: it only needs to locate entries, read a few fields,
and insert a field, while leaving every other byte of the hand-curated file
untouched. Fields the site depends on but no external source provides
(`preview`, `selected`, co-first-author asterisks) survive because entries are
only ever appended to or modified by targeted insertion, never re-serialized.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Entry opener, e.g. `@ARTICLE{Lotthammer2024albatross,`. `@string{...}` macros
# have no comma-terminated key and are correctly skipped.
ENTRY_START_RE = re.compile(r"^@(?P<type>\w+)\s*\{\s*(?P<key>[^,\s]+)\s*,", re.MULTILINE)

PREPRINT_SERVERS = ("biorxiv", "arxiv", "medrxiv", "chemrxiv", "research square", "openrxiv")

FIELD_NAME_RE = re.compile(r"^[A-Za-z_][\w-]*$")


def _scan_fields(raw: str) -> list[tuple[str, int, int]]:
    """Locate every field in an entry as (name, name_start, value_end).

    Line-anchored regexes cannot be used here. Entries in this file are
    hand-written across many lines, but BibTeX fetched from doi.org arrives as
    a single line with every field on it, and entries mix quoted, braced, and
    bare values. A field that cannot be seen is treated as absent, which would
    silently defeat deduplication and duplicate fields on update, so values are
    delimiter-matched instead.
    """
    length = len(raw)
    start = raw.find("{")
    if start < 0:
        return []
    cursor = raw.find(",", start)  # skip the citation key
    if cursor < 0:
        return []
    cursor += 1

    fields = []
    while cursor < length:
        while cursor < length and (raw[cursor].isspace() or raw[cursor] == ","):
            cursor += 1
        if cursor >= length or raw[cursor] == "}":
            break
        name_start = cursor
        equals = raw.find("=", cursor)
        if equals < 0:
            break
        name = raw[cursor:equals].strip().lower()
        if not FIELD_NAME_RE.match(name):
            break
        cursor = equals + 1
        while cursor < length and raw[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        if raw[cursor] == "{":
            depth = 0
            while cursor < length:
                if raw[cursor] == "{":
                    depth += 1
                elif raw[cursor] == "}":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
        elif raw[cursor] == '"':
            cursor += 1
            while cursor < length and raw[cursor] != '"':
                cursor += 1
            cursor += 1
        else:  # bare number or month macro
            while cursor < length and raw[cursor] not in ",}":
                cursor += 1
        fields.append((name, name_start, cursor))
    return fields


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in '{"' and value[-1] in '}"':
        value = value[1:-1]
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(title: str) -> str:
    """Collapse a title to a comparison key.

    BibTeX titles carry line wrapping and brace-protected casing (`{MYH7b}`),
    and the same paper is punctuated inconsistently across sources, so only
    alphanumerics are significant.
    """
    return re.sub(r"[^a-z0-9]", "", title.lower())


@dataclass
class Entry:
    key: str
    raw: str
    start: int
    end: int

    def _span(self, name: str) -> tuple[int, int] | None:
        wanted = name.lower()
        for field_name, name_start, value_end in _scan_fields(self.raw):
            if field_name == wanted:
                return name_start, value_end
        return None

    def field(self, name: str) -> str | None:
        span = self._span(name)
        if not span:
            return None
        text = self.raw[span[0] : span[1]]
        return _clean(text.split("=", 1)[1]) if "=" in text else None

    @property
    def title(self) -> str | None:
        return self.field("title")

    @property
    def journal(self) -> str | None:
        return self.field("journal")

    @property
    def doi(self) -> str | None:
        return self.field("doi")

    @property
    def is_preprint(self) -> bool:
        journal = (self.journal or "").lower()
        return any(server in journal for server in PREPRINT_SERVERS)

    def with_fields(
        self, updates: dict[str, str | None] | None = None, remove: Iterable[str] = ()
    ) -> str:
        """Return this entry's text with `updates` set and `remove` deleted.

        An existing field is rewritten in place; a missing one is inserted
        after the opening line, which is valid BibTeX regardless of the
        entry's field order. Fields named in neither argument — notably
        `abstract`, `author`, `preview`, and `selected` — are left
        byte-for-byte alone.

        An empty value in `updates` is ignored, so an incomplete upstream
        record can never blank out a field that already holds good data.
        Deleting is therefore a separate, explicit request via `remove`,
        which matters for `selected`: jekyll-scholar reads the field's
        presence as truth, so unsetting it means removing the line.
        """
        entry = Entry(self.key, self.raw, self.start, self.end)
        for name, value in (updates or {}).items():
            if not value:
                continue
            span = entry._span(name)
            if span:
                entry.raw = f"{entry.raw[: span[0]]}{name} = {{{value}}}{entry.raw[span[1] :]}"
            else:
                insert_at = entry.raw.find(",", entry.raw.find("{")) + 1
                entry.raw = f"{entry.raw[:insert_at]}\n  {name} = {{{value}}},{entry.raw[insert_at:]}"
        for name in remove:
            span = entry._span(name)
            if not span:
                continue
            end = span[1]
            if end < len(entry.raw) and entry.raw[end] == ",":
                end += 1
            # Drop the blank line the removed field would otherwise leave.
            while end < len(entry.raw) and entry.raw[end] in " \t":
                end += 1
            if end < len(entry.raw) and entry.raw[end] == "\n":
                end += 1
            entry.raw = entry.raw[: span[0]] + entry.raw[end:]
        return entry.raw


def parse_entries(text: str) -> list[Entry]:
    """Return entries in file order. Each spans up to the next entry opener."""
    starts = list(ENTRY_START_RE.finditer(text))
    entries = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        entries.append(
            Entry(key=match.group("key"), raw=text[match.start() : end], start=match.start(), end=end)
        )
    return entries


MONTH_ORDER = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _chronological_key(entry: Entry) -> tuple[int, int, str]:
    """Newest first, then alphabetical by key so the order is deterministic.

    Months appear as macros in mixed case (`feb`, `July`, `Mar`) and cannot be
    compared as strings — alphabetically April precedes August precedes
    December. They are mapped to their number instead. An entry missing a year
    sorts last rather than crashing.
    """
    try:
        year = int(entry.field("year") or 0)
    except ValueError:
        year = 0
    month = MONTH_ORDER.get((entry.field("month") or "")[:3].lower(), 0)
    return (-year, -month, entry.key.lower())


def sort_bibliography(text: str) -> str:
    """Return `text` with entries newest first.

    jekyll-scholar builds the year headings on the publications page itself,
    but `sort_by` is unset, so order within a year comes from the file — and
    the homepage's selected-papers list passes `--group_by none`, making file
    order the only thing that orders it. Appended entries would otherwise sit
    at the bottom whatever their date.

    Anything before the first entry, such as the `@string` macro, is kept at
    the top. Entry text is moved verbatim, never re-serialized.
    """
    entries = parse_entries(text)
    if not entries:
        return text
    preamble = text[: entries[0].start].strip()
    body = "\n\n".join(e.raw.strip() for e in sorted(entries, key=_chronological_key))
    return f"{preamble}\n\n{body}\n" if preamble else f"{body}\n"


def replace_entries(text: str, replacements: dict[str, str]) -> str:
    """Rewrite `text`, substituting new raw text for entries by key."""
    if not replacements:
        return text
    out = []
    cursor = 0
    for entry in parse_entries(text):
        if entry.key in replacements:
            out.append(text[cursor : entry.start])
            out.append(replacements[entry.key])
            cursor = entry.end
    out.append(text[cursor:])
    return "".join(out)
