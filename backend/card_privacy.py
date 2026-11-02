"""
Merchant sanitization: the privacy boundary for every Anthropic API call.

``sanitize`` turns a raw statement description into a ``merchant_key`` (the
classification cache key) and a ``label`` (the only per-merchant text that may
ever be sent off the server). Everything personal or identifying is removed:
store and reference numbers, phone numbers, URLs, emails, card fragments, dates,
zip codes, cardholder names, and the city and state the merchant sits in.

Every removal substitutes a space rather than deleting, because issuers glue
identifiers onto words: some write "CIRCLEK#1000043" and
"IRRIGATION IN555-0101234", and some pack the description into fixed-width
fields with the city running straight into a truncated merchant name
("PIPER & PINE - SPRINSPRINGFIELD         IL").

``is_safe_label`` is the last check before a label is sent; anything failing it
stays local and is left for the user to categorize.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import card_rules

# Fixed-width descriptions: a 20-character merchant field, a 20-character city field,
# then a two-letter state. Matching the layout lets us cut the city off cleanly
# instead of guessing where the merchant name ends.
_FIXED_WIDTH = re.compile(r"^(.{20})(.{20})([A-Za-z]{2})$")

_US_STATES = set(card_rules.US_STATES) | {"PR", "VI", "GU"}

_REDACTIONS = [
    re.compile(r"\S+@\S+"),                                       # emails
    re.compile(r"HTTPS?://\S+"),                                  # URLs
    re.compile(r"\bWWW\."),
    re.compile(r"\.(?:COM|NET|ORG|IO|CO|US)\b/?"),               # domains: "APPLE.COM/BIL" -> "APPLE BIL"
    re.compile(r"X{2,}[-X\d]*\d{2,}"),                            # masked card numbers
    re.compile(r"\*{2,}\d{2,}"),
    re.compile(r"\b(?:ENDING IN|ACCT|ACCOUNT|CARD)\s*#?\s*\d+"),
    re.compile(r"\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"),  # phone numbers, glued or not
    re.compile(r"\d{3}-\d{4,7}"),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),              # dates
    re.compile(r"\b[A-Z]{2}\s*\d{5}(?:-\d{4})?\b"),               # state + zip
    re.compile(r"\*(?=[A-Z]*\d)[A-Z0-9]{5,}"),                    # order codes: "PRIME*4G23O48F3", not "*DOORDASH"
    re.compile(r"#\s?\d*"),                                       # store numbers: "BP#9338336"
    re.compile(r"\bSTORE\s?\d+"),
    re.compile(r"\d{3,}"),                                        # any remaining digit run
    re.compile(r"(?<![\w-])\d{1,2}(?![\w-])"),                    # stray short numbers ("LIQUOR 1")
]

_STRIP_CHARS = " -*.,&/#'_:;"

# In-name separators: "CIRCLE_K", "CIRCLE-K" and "CIRCLE.K" all read as "CIRCLE K".
_SEPARATORS = re.compile(r"[_\-./*+]+")


def compact_key(text: str) -> str:
    """
    The merchant cache key: uppercase letters and digits only.

    Spacing and punctuation vary between issuers and exports ("CIRCLE K",
    "CIRCLEK", "CIRCLE_K", "CIRCLE-K"); the compact form makes them one merchant,
    so it is classified, cached and sent to the AI once.
    """
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())

# Redactions leave this marker token so the trailing-location logic can tell a
# removed phone or store number apart from a city name.
_M = "\x00"
_CORPORATE_TAIL = {"INC", "IN", "CO", "LLC", "CORP", "LTD"}


@dataclass
class Redaction:
    """Per-row context the sanitizer removes in addition to its patterns."""

    names: list[str] = field(default_factory=list)       # cardholder / card member names
    cities: list[str] = field(default_factory=list)      # merchant city from the issuer's City/State column
    account_digits: list[str] = field(default_factory=list)  # last4, Account # column
    extra_terms: list[str] = field(default_factory=list)  # user-configured extra redactions


@dataclass
class Sanitized:
    merchant_key: str   # compact_key of the cleaned name; the cache key
    label: str          # display-cased; the only text the AI ever sees for this merchant
    processor: str      # "Square", "Toast" ... (the processor, not the merchant)


def _remove_words(text: str, words: list[str], min_len: int = 3) -> str:
    for w in words:
        w = (w or "").strip().upper()
        if len(w) >= min_len:
            text = re.sub(rf"(?<![A-Z0-9]){re.escape(w)}(?![A-Z0-9])", f" {_M} ", text)
    return text


def _name_tokens(names: list[str]) -> list[str]:
    tokens: list[str] = []
    for n in names:
        tokens.extend(t for t in re.split(r"[\s,]+", (n or "").upper()) if len(t) >= 3)
    return tokens


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(_STRIP_CHARS).strip()


def sanitize(raw_description: str, redact: Redaction | None = None) -> Sanitized:
    redact = redact or Redaction()
    raw = (raw_description or "").rstrip()
    cities = [c.strip().upper() for c in redact.cities if c and c.strip()]

    # Fixed-width layout: keep the merchant field, remember the city field.
    m = _FIXED_WIDTH.match(raw)
    if m and m.group(3).upper() in _US_STATES:
        raw = m.group(1)
        city_field = _collapse(m.group(2).upper())
        if city_field and not re.search(r"\d", city_field):
            cities.append(city_field)

    text = re.sub(r"\s+", " ", raw.upper()).strip()
    text, processor = card_rules.strip_processor(text)

    for rx in _REDACTIONS:
        text = rx.sub(f" {_M} ", text)

    text = _remove_words(text, _name_tokens(redact.names))
    text = _remove_words(text, redact.extra_terms, min_len=2)
    for digits in redact.account_digits:
        d = re.sub(r"\D", "", digits or "")
        if len(d) >= 2:
            text = text.replace(d, f" {_M} ")

    # Known merchant city, wherever it occurs (including glued onto a word).
    for city in sorted(set(cities), key=len, reverse=True):
        if len(city) >= 3:
            text = text.replace(city, f" {_M} ")

    text = _SEPARATORS.sub(" ", text)
    text = re.sub(r"\s*&\s*", " & ", text)
    spaced = _collapse(" ".join(t for t in _strip_location_tail(text.split(), cities) if t != _M))
    key = compact_key(spaced)
    if len(key) < 3:
        return Sanitized(merchant_key="", label="", processor=processor)
    return Sanitized(merchant_key=key, label=card_rules.nice_case(spaced), processor=processor)


def _strip_location_tail(toks: list[str], cities: list[str]) -> list[str]:
    """
    Remove the trailing location statement descriptions end with.

    "MERCHANT CITY ST" loses "CITY ST"; "MERCHANT <phone> ST" only loses the
    state (the marker shows nothing but an identifier sat there);
    "MERCHANT#<store> OAK GROVE ST" loses everything after the store number,
    up to three words, because that run is the city.
    """
    def real() -> int:
        return sum(1 for t in toks if t != _M)

    def trim() -> None:
        while toks and toks[-1] == _M:
            toks.pop()

    trim()
    # A fixed-width export truncates the merchant field mid-city: "PIPER & PINE - SPRIN" + "SPRINGFIELD".
    if real() > 1 and len(toks[-1]) >= 3 and any(
        c.replace(" ", "").startswith(toks[-1]) for c in cities
    ):
        toks.pop()
        trim()
    if real() >= 2 and toks[-1] in _US_STATES:
        toks.pop()
        if toks and toks[-1] == _M:
            trim()
        else:
            last_marker = max((i for i, t in enumerate(toks) if t == _M), default=None)
            if last_marker is not None and last_marker > 0 and 1 <= len(toks) - last_marker - 1 <= 3:
                del toks[last_marker:]
            elif real() >= 2:
                toks.pop()
        trim()
    while real() >= 2 and toks[-1] in _CORPORATE_TAIL:
        toks.pop()
        trim()
    return toks


_UNSAFE = re.compile(r"\d{3,}|@|https?://|www\.", re.IGNORECASE)


def is_safe_label(label: str, names: list[str] | None = None) -> bool:
    """Final gate before a label is sent: non-trivial, no digit runs, no names."""
    if not label or len(label.replace(" ", "")) < 3 or len(label) > 80:
        return False
    if _UNSAFE.search(label):
        return False
    upper = label.upper()
    for tok in _name_tokens(names or []):
        if re.search(rf"(?<![A-Z0-9]){re.escape(tok)}(?![A-Z0-9])", upper):
            return False
    return True
