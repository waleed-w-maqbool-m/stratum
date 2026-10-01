"""Pattern signatures for string columns.

A legacy string column rarely holds "a string". It holds a date in one of three
formats, or a zero-padded integer, or a whole XML document, or `k=v|k=v`. Classifying
the *shape* of the values is what lets the later detectors work without an LLM: the
format, type, quality and drift detectors all consume signatures computed once here.

Every classifier is a pure function over a sample of values, and returns the fraction
of non-null values that matched. Fractions, not booleans — real columns are mixed, and
"87% of these look like YYYYMMDD" is the actually useful fact.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

# Ordered most specific first: the first match wins when assigning a single label.
SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("empty", re.compile(r"^\s*$")),
    ("date_yyyymmdd", re.compile(r"^\d{8}$")),
    ("date_iso", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("datetime_iso", re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")),
    ("date_dmy_slash", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("date_mdy_slash", re.compile(r"^\d{1,2}/\d{1,2}/\d{2}$")),
    ("datetime_dmy", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}[ T]\d{1,2}:\d{2}")),
    ("date_dmy_dash", re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")),
    ("xml", re.compile(r"^\s*<[a-zA-Z_][\w:.-]*[\s>]")),
    ("json_object", re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)),
    ("json_array", re.compile(r"^\s*\[.*\]\s*$", re.DOTALL)),
    ("kv_pipe", re.compile(r"^[\w .-]+=[^|]*(\|[\w .-]+=[^|]*)+$")),
    ("kv_semicolon", re.compile(r"^[\w .-]+=[^;]*(;[\w .-]+=[^;]*)+$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")),
    ("url", re.compile(r"^https?://\S+$")),
    ("uuid", re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")),
    ("zero_padded_int", re.compile(r"^0\d+$")),
    ("integer", re.compile(r"^-?\d+$")),
    ("decimal", re.compile(r"^-?\d+\.\d+$")),
    ("currency", re.compile(r"^[$£€]\s?-?[\d,]+(\.\d{2})?$")),
    ("percent", re.compile(r"^-?[\d.]+\s?%$")),
    ("phone", re.compile(r"^[+(]?[\d][\d\s()+.-]{5,}\d$")),
    ("postcode_us", re.compile(r"^\d{5}(-\d{4})?$")),
    ("yes_no", re.compile(r"^[YNyn]$")),
    ("bit_char", re.compile(r"^[01]$")),
    ("single_letter_code", re.compile(r"^[A-Za-z]$")),
    ("upper_code", re.compile(r"^[A-Z0-9_]{2,10}$")),
]

DATE_SIGNATURES = {
    "date_yyyymmdd", "date_iso", "datetime_iso", "date_dmy_slash",
    "date_mdy_slash", "datetime_dmy", "date_dmy_dash",
}

# Values that are spelled like data but mean "no value".
NULL_MARKERS = {
    "", "n/a", "na", "-", "--", "none", "null", "nil", "unknown", "unk", "?",
    "tbd", "n.a.", "not applicable", "(none)", "(blank)", "#n/a",
}


@dataclass
class ValueShape:
    """What a column's values look like, computed once from a sample."""

    sampled: int = 0
    non_null: int = 0
    signatures: dict[str, float] = field(default_factory=dict)
    dominant: str = ""
    dominant_fraction: float = 0.0
    mixed_date_formats: list[str] = field(default_factory=list)
    null_marker_fraction: float = 0.0
    null_markers_seen: list[str] = field(default_factory=list)
    has_leading_trailing_space: float = 0.0
    case_variants: int = 0
    min_length: int = 0
    max_length: int = 0
    avg_length: float = 0.0
    padded_to: int | None = None      # every value is exactly this long and space-padded

    def is_mostly(self, signature: str, threshold: float = 0.9) -> bool:
        return self.signatures.get(signature, 0.0) >= threshold

    def looks_structured(self) -> bool:
        """XML, JSON or k=v — something with fields inside it."""
        return any(
            self.signatures.get(s, 0.0) >= 0.6
            for s in ("xml", "json_object", "json_array", "kv_pipe", "kv_semicolon")
        )


def _plausible_date(value: str, signature: str) -> bool:
    """Does a date-shaped string actually denote a date?

    `'00000003'` matches \\d{8} perfectly and is a zero-padded employee number. Treating
    it as YYYYMMDD gives the column a date normalisation that corrupts every comparison
    on it, so the shape has to be confirmed against the calendar before it counts.
    """
    try:
        if signature == "date_yyyymmdd":
            year, month, day = int(value[:4]), int(value[4:6]), int(value[6:8])
        elif signature in ("date_dmy_slash", "date_dmy_dash", "datetime_dmy"):
            separator = "/" if "/" in value else "-"
            parts = value.split(" ")[0].split("T")[0].split(separator)
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        elif signature in ("date_iso", "datetime_iso"):
            year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
        elif signature == "date_mdy_slash":
            parts = value.split("/")
            month, day, year = int(parts[0]), int(parts[1]), 2000 + int(parts[2])
        else:
            return True
    except (ValueError, IndexError):
        return False
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def classify(value: str) -> str:
    """The single most specific signature for one value."""
    for name, pattern in SIGNATURES:
        if pattern.match(value):
            if name in DATE_SIGNATURES and not _plausible_date(value, name):
                continue        # shaped like a date, but not one
            return name
    return "text"


def shape(values: list, sample_size: int | None = None) -> ValueShape:
    """Profile a sample of values.

    `values` may contain None and non-strings; non-strings are stringified, because a
    column declared INTEGER in SQLite can still hold text, and that is exactly the
    sort of thing we need to notice.
    """
    out = ValueShape(sampled=len(values))
    texts: list[str] = []
    for v in values:
        if v is None:
            continue
        texts.append(v if isinstance(v, str) else str(v))
    out.non_null = len(texts)
    if not texts:
        return out

    counts: Counter[str] = Counter()
    markers: Counter[str] = Counter()
    spaced = 0
    lengths = []
    for text in texts:
        counts[classify(text)] += 1
        lengths.append(len(text))
        if text != text.strip():
            spaced += 1
        if text.strip().casefold() in NULL_MARKERS:
            markers[text] += 1

    n = float(len(texts))
    out.signatures = {k: round(v / n, 4) for k, v in counts.items()}
    out.dominant, dominant_count = counts.most_common(1)[0]
    out.dominant_fraction = round(dominant_count / n, 4)
    out.has_leading_trailing_space = round(spaced / n, 4)
    out.null_marker_fraction = round(sum(markers.values()) / n, 4)
    out.null_markers_seen = sorted({m.strip() for m in markers})
    out.min_length = min(lengths)
    out.max_length = max(lengths)
    out.avg_length = round(sum(lengths) / n, 2)

    # Several date formats living in one column (anti-pattern #13).
    present_dates = [s for s in DATE_SIGNATURES if out.signatures.get(s, 0) > 0.02]
    if len(present_dates) > 1:
        out.mixed_date_formats = sorted(present_dates)

    # CHAR(n) padding: a fixed width where trailing spaces are common.
    if out.min_length == out.max_length and spaced > n * 0.3:
        out.padded_to = out.max_length

    # How many distinct casings the same value appears in — 'New York' vs 'NEW YORK'.
    folded: dict[str, set[str]] = {}
    for text in texts:
        folded.setdefault(text.strip().casefold(), set()).add(text.strip())
    out.case_variants = sum(1 for variants in folded.values() if len(variants) > 1)
    return out


# ------------------------------------------------------------------ structure parsing


def xml_paths(value: str, prefix: str = "") -> dict[str, str]:
    """Flatten a small XML document into {path: text}.

    A deliberately small hand-rolled reader: these are legacy address blobs, not
    documents, and pulling in a parser that resolves entities would be a liability.
    """
    out: dict[str, str] = {}
    for match in re.finditer(r"<([\w:.-]+)\s*>(.*?)</\1>", value, re.DOTALL):
        tag, body = match.group(1), match.group(2)
        path = f"{prefix}/{tag}" if prefix else tag
        if "<" in body:
            out.update(xml_paths(body, path))
        else:
            out[path] = body.strip()
    return out


def json_paths(value: str) -> dict[str, str]:
    """Flatten a JSON object into {dotted.path: stringified scalar}."""
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return {}

    out: dict[str, str] = {}

    def walk(node, prefix: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(node, list):
            if node:
                walk(node[0], f"{prefix}[]")
        else:
            out[prefix] = "" if node is None else str(node)

    walk(data, "")
    return out


def kv_pairs(value: str, pair_sep: str = "|", assign: str = "=") -> dict[str, str]:
    """Parse `color=Red|size=M`."""
    out: dict[str, str] = {}
    for chunk in value.split(pair_sep):
        if assign in chunk:
            key, _, val = chunk.partition(assign)
            out[key.strip()] = val.strip()
    return out


def detect_kv_separators(values: list[str]) -> tuple[str, str] | None:
    """Work out which separators a k=v column uses, if any."""
    for pair_sep in ("|", ";", ",", "\n"):
        for assign in ("=", ":"):
            hits = sum(
                1 for v in values
                if v and pair_sep in v and assign in v
                and all(assign in part for part in v.split(pair_sep) if part.strip())
            )
            if values and hits / len(values) > 0.8:
                return pair_sep, assign
    return None


def extract_structure(values: list[str]) -> tuple[str, dict[str, str]]:
    """Infer the format of a structured column and the union of its field paths.

    Returns (format, {path: example value}). Format is one of xml, json, kv or "".
    """
    non_empty = [v for v in values if v and v.strip()]
    if not non_empty:
        return "", {}
    probe = shape(non_empty)

    if probe.signatures.get("xml", 0) > 0.6:
        merged: dict[str, str] = {}
        for value in non_empty[:50]:
            for path, text in xml_paths(value).items():
                merged.setdefault(path, text)
        return "xml", merged
    if probe.signatures.get("json_object", 0) > 0.6:
        merged = {}
        for value in non_empty[:50]:
            for path, text in json_paths(value).items():
                merged.setdefault(path, text)
        return "json", merged
    if probe.signatures.get("kv_pipe", 0) > 0.6 or probe.signatures.get("kv_semicolon", 0) > 0.6:
        separators = detect_kv_separators(non_empty[:100]) or ("|", "=")
        merged = {}
        for value in non_empty[:50]:
            for key, text in kv_pairs(value, *separators).items():
                merged.setdefault(key, text)
        return "kv", merged
    return "", {}
