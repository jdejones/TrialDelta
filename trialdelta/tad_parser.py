"""Deterministic dates next to the exact target action date/date(s) marker.

Adapted from Healthcare_Data's plain-text/context helpers. Offsets refer to the
normalized plain text; the first evidence window is 100 characters on each side.
Unidentified mentions are retried within the sentence containing the marker.
Incomplete and ambiguous expressions remain reviewable instead of borrowing a
year or day from today's date. No service or model is used here.
"""

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date

from bs4 import BeautifulSoup


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|"
          r"November|December|Sept\.?|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|"
          r"Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?)")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?(?!\w)"
_DAYS = rf"{_DAY}(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*){_DAY})*"
_YEAR = r"\d{4}(?!\d)"
_SEPARATOR = re.compile(r"^\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)$", re.I)
_PATTERNS = [
    ("iso", re.compile(r"(?<![\w/.-])(?P<year>\d{4})\s*(?P<sep>[-/])\s*(?P<month>\d{1,2})\s*(?P=sep)\s*(?P<day>\d{1,2})(?!\w|[/.-]\d)")),
    ("ydm", re.compile(rf"(?<!\w)(?P<year>{_YEAR})[\s,]+(?P<day>{_DAY})\s+(?:of\s+)?(?P<month>{_MONTH})(?!\w)", re.I)),
    ("ym", re.compile(rf"(?<!\w)(?P<year>{_YEAR})[\s,]+(?P<month>{_MONTH})(?:\s+(?P<day>{_DAY}))?(?!\w|\s+\d)", re.I)),
    ("md", re.compile(rf"(?<!\w)(?P<month>{_MONTH})\s+(?P<days>{_DAYS})(?:\s*,?\s*(?P<year>{_YEAR}))?(?!\w)", re.I)),
    ("dm", re.compile(rf"(?<!\w)(?P<days>{_DAYS})\s+(?:of\s+)?(?P<month>{_MONTH})(?:\s*,?\s*(?P<year>{_YEAR}))?(?!\w)", re.I)),
    ("my", re.compile(rf"(?<!\w)(?P<month>{_MONTH})(?:\s*,\s*|\s+)(?P<year>{_YEAR})(?!\w)", re.I)),
    ("numeric", re.compile(r"(?<![\w/.-])(?P<first>\d{1,2})\s*(?P<sep>[/.-])\s*(?P<second>\d{1,2})(?:\s*(?P=sep)\s*(?P<year>\d{2,4}))?(?!\w|[/.-]\d)")),
    ("numeric_my", re.compile(r"(?<![\w/.-])(?P<month>\d{1,2})\s*[/.-]\s*(?P<year>\d{4})(?!\w|[/.-]\d)")),
    ("numeric_ym", re.compile(r"(?<![\w/.-])(?P<year>\d{4})\s*[-/]\s*(?P<month>\d{1,2})(?!\w|[/.-]\d)")),
]


@dataclass(frozen=True)
class _Date:
    year: int | None
    month: int
    day: int | None
    problem: str = ""
    ambiguous: bool = False


@dataclass(frozen=True)
class _Expression:
    start: int
    end: int
    text: str
    dates: tuple[_Date, ...]


def to_plain_text(markup: str) -> str:
    """Remove non-content tags and normalize Unicode/whitespace for matching."""
    soup = BeautifulSoup(markup or "", "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", soup.get_text(separator=" ", strip=True))).strip()


def occurrences_with_context(text: str, needle: str = "target action date", window: int = 100) -> list[dict]:
    """Locate singular/plural markers with the original fixed context window."""
    phrase = r"\s+".join(re.escape(word) for word in needle.split())
    if needle.casefold().strip() == "target action date":
        phrase += "s?"
    output = []
    for match in re.finditer(rf"\b{phrase}\b", text, re.I):
        start, end = match.span()
        context_start, context_end = max(0, start - window), min(len(text), end + window)
        output.append({"start": start, "end": end, "context_start": context_start,
                       "context_end": context_end, "context": text[context_start:context_end]})
    return output


def _number(value: str) -> int:
    return int(re.match(r"\d+", value).group())


def _expressions(text: str) -> list[_Expression]:
    expressions = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < other.end and match.end() > other.start for other in expressions):
                continue
            values = match.groupdict()
            year_text = values.get("year")
            year = int(year_text) if year_text and len(year_text) == 4 else None
            if kind == "numeric":
                first, second = int(values["first"]), int(values["second"])
                ambiguous = first <= 12 and second <= 12 and first != second and first > 0 and second > 0
                month, day = (second, first) if first > 12 and second <= 12 else (first, second)
                problem = "Numeric day/month order is ambiguous." if ambiguous else ""
                if year_text and len(year_text) != 4:
                    problem, ambiguous = "A four-digit year is required.", True
                dates = (_Date(year, month, day, problem, ambiguous),)
            else:
                month_text = values["month"]
                month = _MONTHS[month_text[:3].lower()] if month_text[0].isalpha() else int(month_text)
                if values.get("days"):
                    days = [_number(item.group()) for item in re.finditer(_DAY, values["days"], re.I)]
                else:
                    days = [_number(values["day"]) if values.get("day") else None]
                dates = tuple(_Date(year, month, day) for day in days)
            expressions.append(_Expression(match.start(), match.end(), match.group(), dates))
    return sorted(expressions, key=lambda expression: expression.start)


_LINK_WORDS = (r"is|are|was|were|of|on|in|for|as|at|set|assigned|established|scheduled|"
               r"anticipated|expected|currently|now|remains|remain|has|have|had|been|"
               r"a|an|the|to|be|by|approximately|about|around|or|no|later|than")
_AFTER = re.compile(rf"^[\s,:()\-–—]*(?:(?:{_LINK_WORDS})\b[\s,:()\-–—]*)*$", re.I)
_BEFORE = re.compile(r"^[\s,:()\-–—]*(?:(?:(?:is|are|was|were|has been|had been)\s+)?(?:(?:set|assigned|established)\s+)?(?:as|for)\s+)?(?:(?:is|are|was|were)\s+)?(?:(?:the|its|our|their|a|an)\s+)?(?:FDA\s+)?$", re.I)


def _after_link(gap: str, *, sentence_fallback: bool = False) -> bool:
    if _AFTER.fullmatch(gap):
        return True
    # A named application may intervene, but must end in an explicit date link.
    application = r"[^;!?]+?" if sentence_fallback else r"[^.;!?]{1,70}?"
    return bool(re.fullmatch(rf"\s+for\s+{application}\s+(?:of|is|was|has been set for|is set for)\s*", gap, re.I))


def _before_link(gap: str) -> bool:
    # A comma/article does not establish that an announcement date is a TAD:
    # "On March 5, 2026, the target action date was announced."
    return bool(_BEFORE.fullmatch(gap) and (not gap.strip() or re.search(r"\b(?:as|for|is|are|was|were)\b", gap, re.I)))


def _different_event_follows(text: str) -> bool:
    return bool(re.match(r"\s*,?\s*(?:(?:is|was|were|are|marked)\s+)?(?:(?:the|a|an|our|its)\s+)?(?:announcement|submission|filing|acceptance|approval|meeting|conference|press release)\b", text, re.I))


def _result(occurrence: dict, expression: _Expression | None, value: _Date | None, reason: str = "", status: str = "unidentifiable") -> dict:
    row = {**occurrence, "tad": None, "precision": "unknown", "status": status,
           "matched_text": expression.text if expression else "", "reason": reason}
    if value is None:
        return row
    if value.problem:
        row.update(status="ambiguous" if value.ambiguous else "unidentifiable", reason=value.problem)
        return row
    if value.year is None:
        row["reason"] = "Date expression has no explicit four-digit year."
        return row
    try:
        date(value.year, value.month, value.day if value.day is not None else 1)
    except ValueError:
        row["reason"] = "Date expression is not a valid calendar date."
        return row
    row.update(tad=f"{value.year:04d}-{value.month:02d}" + (f"-{value.day:02d}" if value.day is not None else ""),
               precision="day" if value.day is not None else "month", status="identified",
               reason=reason or ("Explicit adjacent date." if value.day is not None else "Explicit month and year; no day supplied."))
    return row


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split normalized filing text without splitting dates or abbreviations."""
    # Only spans are needed here. Avoid building/validating every date in a
    # potentially large filing just to protect periods inside date expressions.
    date_spans = sorted((match.start(), match.end()) for _, pattern in _PATTERNS
                        for match in pattern.finditer(text))
    span_index = 0
    protected_end = 0
    spans = []
    left = 0
    for boundary in re.finditer(r'''[.!?]+["'\u201d\u2019)\]]*(?=\s|$)''', text):
        position = boundary.start()
        while span_index < len(date_spans) and date_spans[span_index][0] <= position:
            protected_end = max(protected_end, date_spans[span_index][1])
            span_index += 1
        if text[position] == "." and text[position:position + 2] != "..":
            # Protect periods inside Nov. 14, 2026 and 14. 11. 2026, but
            # allow the final period in a date such as 2026 14 Nov. to end it.
            if position < protected_end - 1:
                continue
            prefix = text[max(0, position - 15):position + 1]
            next_word = re.compile(r"\s*(\w+)").match(text, boundary.end())
            word = next_word[1] if next_word else ""
            lower_continuation = word[:1].islower()
            date_continuation = re.compile(r"\s*,?\s*(?:and\b|or\b|&)", re.I).match(text, boundary.end())
            if re.search(r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.$", prefix, re.I) and (lower_continuation or date_continuation):
                continue
            if re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.$", prefix):
                continue
            if re.search(r"\b(?:[A-Za-z]\.){2,}$", prefix):
                # Preserve ordinary continuations and familiar agency names;
                # other capitalized text may start a new sentence after U.S.
                agency_continuation = re.compile(
                    r"\s*(?:Food and Drug Administration|Securities and Exchange Commission|FDA|SEC)\b"
                ).match(text, boundary.end())
                if lower_continuation or date_continuation or agency_continuation:
                    continue
            # A corporate abbreviation followed by a lower-case continuation
            # stays in the sentence; 'Inc. The ...' starts a new sentence.
            if re.search(r"\b(?:Inc|Corp|Ltd|Co|vs|etc)\.$", prefix, re.I) and lower_continuation:
                continue
        spans.append((left, boundary.end()))
        left = boundary.end()
        while left < len(text) and text[left].isspace():
            left += 1
    if left < len(text):
        spans.append((left, len(text)))
    return spans


def _extract_occurrence(text: str, occurrence: dict, *, sentence_fallback: bool = False) -> list[dict]:
    """Apply the same association and validation rules inside either context."""
    start, end = occurrence["start"], occurrence["end"]
    left, right = occurrence["context_start"], occurrence["context_end"]
    before, after = text[left:start], text[end:right]
    if re.search(r"\bno\s+(?:new\s+)?$", before, re.I) or re.match(r"\s+(?:(?:has|have|had)\s+)?(?:not\s+(?:yet\s+)?(?:been\s+)?(?:assigned|set|established)|(?:is|was)\s+not\s+(?:assigned|set|established))\b", after, re.I):
        return [_result(occurrence, None, None, "The text explicitly says no target action date has been assigned.")]
    # Extra surrounding characters recognize a date cut by the evidence
    # window without accepting a shortened fragment of that expression.
    scan_start, scan_end = (left, right) if sentence_fallback else (max(0, left - 40), min(len(text), right + 40))
    expressions = [replace(item, start=item.start + scan_start, end=item.end + scan_start)
                   for item in _expressions(text[scan_start:scan_end])]
    following = [item for item in expressions if end <= item.start < right]
    preceding = [item for item in expressions if left < item.end <= start]
    chosen, reason = [], ""
    if following:
        first = following[0]
        gap = text[end:first.start]
        if _after_link(gap, sentence_fallback=sentence_fallback):
            chosen = [first]
        elif re.fullmatch(r"\s+(?:(?:has been|was|is)\s+)?(?:extended|moved|changed|revised)\s+from\s+|\s+from\s+", gap, re.I) and len(following) > 1:
            second = following[1]
            if re.fullmatch(r"\s+to\s+", text[first.end:second.start], re.I):
                chosen, reason = [second], "Explicit revised destination in a from/to date relation."
    if not chosen and preceding:
        previous = preceding[-1]
        if _before_link(text[previous.end:start]):
            chosen = [previous]
    if chosen:
        next_item = next((item for item in expressions if item.start >= chosen[-1].end and item.start < right), None)
        if next_item is not None and re.fullmatch(r"\s*,?\s*(?:or|and/or)\s+", text[chosen[-1].end:next_item.start], re.I):
            alternatives = _Expression(chosen[0].start, min(next_item.end, right), text[chosen[0].start:min(next_item.end, right)], ())
            return [_result(occurrence, alternatives, None, "Alternative dates are stated; no single target date is established.", status="ambiguous")]
    if chosen and not reason:
        # Expand only directly coordinated dates, never another clause.
        while True:
            previous = next((item for item in reversed(expressions) if left < item.end <= chosen[0].start), None)
            if previous is None or not _SEPARATOR.fullmatch(text[previous.end:chosen[0].start]):
                break
            chosen.insert(0, previous)
        while True:
            next_item = next((item for item in expressions if item.start >= chosen[-1].end and item.start < right), None)
            if next_item is None or not _SEPARATOR.fullmatch(text[chosen[-1].end:next_item.start]) or _different_event_follows(text[next_item.end:right]):
                break
            chosen.append(next_item)
        if len(chosen) > 1:
            explicit_years = {value.year for item in chosen for value in item.dates if value.year is not None}
            # A trailing year can scope a coordinated list, e.g. March 1
            # and April 2, 2027. Never borrow a year from another sentence.
            if len(explicit_years) == 1 and all(value.year is not None for value in chosen[-1].dates):
                shared_year = explicit_years.pop()
                chosen = [replace(item, dates=tuple(replace(value, year=shared_year) if value.year is None else value for value in item.dates)) for item in chosen]
                reason = "Explicit coordinated date list with a shared year."
    if not chosen:
        scope = "the same sentence" if sentence_fallback else "the 100-character context"
        return [_result(occurrence, None, None, f"No directly associated explicit date within {scope}.")]
    if any(item.start < left or item.end > right for item in chosen):
        return [_result(occurrence, None, None, "Associated date expression crosses the 100-character context boundary.")]
    output = []
    for expression in chosen:
        for value in expression.dates:
            output.append(_result(occurrence, expression, value, reason))
    return output


def extract_target_action_dates(markup: str) -> list[dict]:
    """Try the 100-character context, then the marker's sentence if unresolved.

    Only literal target action date/date(s) anchors are searched. Missing years
    are never inferred from the clock. Coordinated lists may share an explicit
    year. Successful initial matches keep their original context and behavior.
    """
    text = to_plain_text(markup)
    output = []
    sentences = None
    for occurrence in occurrences_with_context(text):
        rows = _extract_occurrence(text, occurrence)
        if all(row["status"] == "unidentifiable" for row in rows):
            if sentences is None:
                sentences = _sentence_spans(text)
            left, right = next((left, right) for left, right in sentences
                               if left <= occurrence["start"] < right)
            sentence = {**occurrence, "context_start": left, "context_end": right,
                        "context": text[left:right]}
            rows = _extract_occurrence(text, sentence, sentence_fallback=True)
            for row in rows:
                row["reason"] = "Same-sentence fallback: " + row["reason"]
        output.extend(rows)
    return output
