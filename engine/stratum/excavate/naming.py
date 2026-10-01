"""Turning `CFNM1` into `customer.first_name`.

SNAILS showed that cryptic identifiers measurably hurt LLMs and that naturalising them
helps. Rather than spend tokens asking a model what `CSTMR_MSTR` means, expand what can
be expanded deterministically — mainframe abbreviations are a small, stable vocabulary —
and leave only the genuinely opaque names for the LLM enrichment step.

This module never touches the database. It is a string function with a dictionary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Abbreviations that appear in nearly every legacy schema. Longest first at match time.
ABBREVIATIONS: dict[str, str] = {
    # entities
    "mstr": "master", "hdr": "header", "dtl": "detail", "ln": "line", "trx": "transaction",
    "trans": "transaction", "txn": "transaction", "ord": "order", "so": "sales order",
    "po": "purchase order", "inv": "invoice", "cust": "customer", "cstmr": "customer",
    "custmr": "customer", "emp": "employee", "empl": "employee", "vend": "vendor",
    "vndr": "vendor", "prd": "product", "prod": "product", "itm": "item", "acct": "account",
    "org": "organisation", "dept": "department", "addr": "address", "cntct": "contact",
    "whse": "warehouse", "loc": "location", "carr": "carrier", "usr": "user",
    "mgr": "manager", "rep": "representative", "attr": "attribute", "attrs": "attributes",
    "pmt": "payment", "rtn": "return", "rfnd": "refund", "shpmt": "shipment",
    "artst": "artist", "albm": "album", "gnr": "genre", "plst": "playlist",
    "trk": "track", "aud": "audit", "evt": "event", "log": "log", "rec": "record",
    # attributes
    "id": "id", "no": "number", "nbr": "number", "num": "number", "cd": "code",
    "nm": "name", "fnm": "first name", "lnm": "last name", "desc": "description",
    "descr": "description", "dt": "date", "ts": "timestamp", "tm": "time",
    "amt": "amount", "qty": "quantity", "prc": "price", "cst": "cost", "tot": "total",
    "sts": "status", "stat": "status", "flg": "flag", "typ": "type", "ref": "reference",
    "pct": "percent", "avg": "average", "cnt": "count", "seq": "sequence",
    "crt": "created", "upd": "updated", "mod": "modified", "del": "deleted",
    "dlt": "deleted", "actv": "active", "inactv": "inactive", "exp": "expiry",
    "eff": "effective", "strt": "start", "fin": "finish", "curr": "currency",
    "crncy": "currency", "fx": "exchange rate", "gl": "general ledger", "ar": "receivable",
    "ap": "payable", "tax": "tax", "frt": "freight", "dsc": "discount", "disc": "discount",
    "bal": "balance", "ytd": "year to date", "mtd": "month to date", "sz": "size",
    "dur": "duration", "ms": "milliseconds", "byt": "bytes", "cmpsr": "composer",
    "mtyp": "media type", "pstl": "postal", "cty": "city", "ctry": "country",
    "st": "state", "pc": "postal code", "phn": "phone", "eml": "email", "fax": "fax",
    "co": "company", "ttl": "title", "brth": "birth", "hire": "hire", "mthd": "method",
    "rsn": "reason", "apprv": "approved", "src": "source", "sys": "system",
    "impt": "import", "xport": "export", "recon": "reconciled", "prj": "project",
    "bill": "billing", "ship": "shipping", "unt": "unit", "lst": "last",
    "prefs": "preferences", "xml": "xml", "txt": "text", "val": "value", "vals": "values",
    "med": "media", "chan": "channel", "stg": "staging", "tmp": "temporary",
    "bak": "backup", "wrk": "work", "hist": "history", "arch": "archive",
    "rpt": "report", "snap": "snapshot", "xref": "cross reference", "cnv": "conversion",
    "ifc": "interface", "batch": "batch", "hold": "hold", "trm": "terms",
}

# Hungarian and other type prefixes carried by the name rather than the type system.
TYPE_PREFIXES = {
    "tbl": "table", "vw": "view", "sp": "procedure", "fn": "function", "idx": "index",
    "str": "text", "int": "integer", "flt": "float", "dbl": "double", "dt": "date",
    "bln": "boolean", "bit": "boolean", "chr": "character", "arr": "array",
    "n": "", "b": "", "c": "", "i": "", "f": "", "s": "",
}

# Tags that name the kind of *object*, not a data type: safe to drop in any casing.
OBJECT_PREFIXES = {"tbl", "vw", "sp", "fn", "idx"}

# The rest of classic Hungarian notation (VB, Access, early SQL Server): a lower-case
# type tag running straight into a capitalised name — `dtmDeleted`, `curPrice`,
# `lngCount`. Recognised only in exactly that shape. `cur_bal` in snake_case is a
# *current* balance, and stripping `cur` there would lose the only word that says so.
HUNGARIAN_PREFIXES = {
    "dtm": "datetime", "dt": "date", "cur": "currency", "mny": "money", "byt": "byte",
    "lng": "long", "sng": "single", "dbl": "double", "flt": "float", "int": "integer",
    "str": "text", "chr": "character", "bln": "boolean", "bit": "boolean",
    "obj": "object", "var": "variant",
}
_HUNGARIAN = re.compile(r"^(?P<prefix>[a-z]{2,3})(?=[A-Z])")

# Suffixes that say "this is one of several" rather than carrying meaning.
_NUMERIC_SUFFIX = re.compile(r"^(?P<stem>.*?)(?P<digits>\d+)$")

_VOWELLESS = re.compile(r"^[A-Z][BCDFGHJKLMNPQRSTVWXZ]{2,}$", re.IGNORECASE)

# Four consonants in a row: the shape vowel-dropping leaves behind. `cntry`, `invtry`
# and `ctgry` each keep a `y` or a leading vowel, so the check above read them as
# English words and they were never expanded or sent to enrichment — `cntry` became an
# entity called `cntry`. A real word with such a run ("strength") only costs one extra
# question to enrichment, which will hand the same word back.
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{4,}", re.IGNORECASE)


@dataclass
class ExpandedName:
    original: str
    tokens: list[str] = field(default_factory=list)      # raw tokens, lowercased
    words: list[str] = field(default_factory=list)       # expanded words
    logical: str = ""                                    # snake_case logical name
    readable: str = ""                                   # human sentence
    confidence: float = 0.0
    type_prefix: str = ""
    ordinal: int | None = None                           # ADDR1 -> 1
    unexpanded: list[str] = field(default_factory=list)  # tokens we could not expand

    @property
    def is_opaque(self) -> bool:
        """True when a human still could not guess it — send these to the LLM."""
        return self.confidence < 0.5


def tokenize(name: str) -> list[str]:
    """Split an identifier however it happens to be written.

    Handles snake_case, camelCase, PascalCase, ALLCAPS, and the runs of consonants
    that mainframe naming produces.
    """
    text = name.strip()
    # camelCase / PascalCase boundaries, keeping acronym runs together
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    parts = re.split(r"[^A-Za-z0-9]+", text)
    return [p.lower() for p in parts if p]


def expand_token(token: str) -> tuple[str, float]:
    """Expand one token. Returns (expansion, confidence)."""
    if not token:
        return "", 0.0
    if token in ABBREVIATIONS:
        return ABBREVIATIONS[token], 0.95
    if token.isdigit():
        return token, 1.0
    if len(token) > 3 and token.endswith("s") and token[:-1] in ABBREVIATIONS:
        return ABBREVIATIONS[token[:-1]] + "s", 0.85
    if len(token) >= 4 and not _VOWELLESS.match(token) and not _CONSONANT_RUN.search(token):
        # An ordinary English-looking word; leave it be.
        return token, 0.9
    # A short consonant run we do not know. Try re-inserting vowels against the
    # dictionary: 'cstmr' -> 'customer' is caught above, but 'shpr' is not.
    for full in ABBREVIATIONS.values():
        squeezed = re.sub(r"[aeiou\s]", "", full)
        if squeezed and squeezed == token:
            return full, 0.7
    return token, 0.25


def expand(name: str, *, context: str = "") -> ExpandedName:
    """Expand an identifier into a logical name.

    `context` is the table's own expanded name; a column called `NM` inside `CUST_MSTR`
    is `customer.name`, and passing the context lets the caller drop a redundant
    leading entity token (`CUST_ID` inside `CUST_MSTR` is just `id`).
    """
    out = ExpandedName(original=name)
    tokens = tokenize(name)
    if not tokens:
        return out

    # A trailing digit run is an ordinal, not meaning: ADDR1, CFNM1, DEDUPE_1.
    match = _NUMERIC_SUFFIX.match(tokens[-1])
    if match and match.group("stem"):
        out.ordinal = int(match.group("digits"))
        tokens[-1] = match.group("stem")
    elif tokens[-1].isdigit() and len(tokens) > 1:
        out.ordinal = int(tokens[-1])
        tokens = tokens[:-1]

    # Hungarian / object-type prefix.
    hungarian = _HUNGARIAN.match(name.strip())
    is_hungarian = bool(
        hungarian and tokens[0] == hungarian.group("prefix")
        and tokens[0] in HUNGARIAN_PREFIXES
    )
    # Snake case only sheds *object* tags (`tbl_`, `vw_`, `sp_`). A data-type tag is
    # Hungarian only in camelCase, which `is_hungarian` covers: in `str_no` the `str` is
    # a store, in `int_rate` an interest rate, and stripping them lost the only word
    # that said which table the column points at.
    object_tag = tokens[0] in OBJECT_PREFIXES
    if len(tokens) > 1 and (is_hungarian or object_tag):
        out.type_prefix = tokens[0]
        tokens = tokens[1:]
    elif len(tokens) == 1:
        # One unbroken token — `tblprd`, `strnm`. Only strip the tag when what remains
        # is a known abbreviation: otherwise `street` becomes "eet", `interest` "erest"
        # and `strength` "ength", because each merely starts with a type tag.
        for prefix in ("tbl", "str", "int", "flt", "dbl", "bln"):
            rest = tokens[0][len(prefix):]
            if tokens[0].startswith(prefix) and len(rest) >= 2 and rest in ABBREVIATIONS:
                out.type_prefix = prefix
                tokens = [rest]
                break

    out.tokens = list(tokens)
    words: list[str] = []
    confidences: list[float] = []
    for token in tokens:
        word, confidence = expand_token(token)
        words.append(word)
        confidences.append(confidence)
        if confidence < 0.5:
            out.unexpanded.append(token)

    # Drop the noise words that say nothing about meaning.
    meaningful = [w for w in words if w not in ("master", "header", "table")]
    if not meaningful:
        meaningful = words

    # Drop a leading entity word that just repeats the table.
    if context:
        context_words = set(context.replace("_", " ").split())
        while len(meaningful) > 1 and meaningful[0] in context_words:
            meaningful = meaningful[1:]

    out.words = meaningful
    out.readable = " ".join(meaningful)
    logical = re.sub(r"[^a-z0-9]+", "_", out.readable.lower()).strip("_")
    if out.ordinal is not None and out.ordinal > 1:
        logical = f"{logical}_{out.ordinal}"
    out.logical = logical or name.lower()
    out.confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    return out


def similarity(left: str, right: str) -> float:
    """How likely two identifiers mean the same thing.

    Compares the *expanded* forms, so `CUST_NO` and `CustomerId` score highly even
    though their raw spellings share almost nothing. This is what lets `joins.py`
    propose candidate pairs across a schema where nobody agreed on a convention.
    """
    from rapidfuzz import fuzz

    left_expanded = expand(left)
    right_expanded = expand(right)
    a, b = left_expanded.readable, right_expanded.readable
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    token_score = fuzz.token_set_ratio(a, b) / 100.0
    raw_score = fuzz.ratio(left.lower(), right.lower()) / 100.0

    # Identifier-shaped words ('id', 'number', 'code') carry the join signal; two names
    # that agree on the entity but disagree on the role should not score as a match.
    id_words = {"id", "number", "code", "key"}
    left_words = set(left_expanded.words)
    right_words = set(right_expanded.words)
    both_identifiers = bool(left_words & id_words) and bool(right_words & id_words)
    shared_entity = bool((left_words - id_words) & (right_words - id_words))
    bonus = 0.15 if (both_identifiers and shared_entity) else 0.0

    return round(min(1.0, max(token_score, raw_score) + bonus), 3)


# Words that describe a column's *role* as a key rather than what it refers to.
ROLE_WORDS = {"id", "number", "code", "key", "reference", "sequence"}


def entity_words(name: str) -> set[str]:
    """What an identifier refers to, with the key-role words stripped.

    `CUST_NO` -> {'customer'}, `ORD_NO` -> {'order'}, `ID` -> set().
    """
    return set(expand(name).words) - ROLE_WORDS


def fk_name_score(child_column: str, parent_table: str, parent_column: str) -> float:
    """How strongly a column's name says "this points at that table".

    Deliberately not a fuzzy string ratio. On short mainframe identifiers, character
    overlap is mostly noise — `REC_ID` and `PRD_ID` are 67% similar as strings and
    have nothing to do with each other — so the comparison is made on the expanded
    entity words instead, and a bare fuzzy score can never on its own clear the bar.
    """
    from rapidfuzz import fuzz

    if child_column.lower() == parent_column.lower():
        return 1.0

    child_entity = entity_words(child_column)
    parent_entity = entity_words(parent_column)
    table_entity = entity_words(parent_table) | set(expand(parent_table).words)

    # The child names the parent's entity: CUST_NO -> CUST_MSTR.CUST_ID
    if child_entity and child_entity & table_entity:
        return 0.9
    # The child names the parent's key column: REP_NO -> ... .REP_ID
    if child_entity and parent_entity and child_entity == parent_entity:
        return 0.9
    if child_entity and parent_entity and child_entity & parent_entity:
        return 0.75

    # No entity agreement at all. Fall back to a fuzzy score on the readable forms,
    # capped below the acceptance threshold so it can only ever break a tie.
    left, right = expand(child_column).readable, expand(parent_column).readable
    if not left or not right:
        return 0.0
    return round(min(0.45, fuzz.token_sort_ratio(left, right) / 100.0), 3)
