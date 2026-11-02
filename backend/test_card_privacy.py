"""
Tests for merchant sanitization, the privacy boundary for every AI call.

The properties that matter: no identifier survives (digit runs, phones, emails,
names, the merchant's city), keys are stable across store numbers and export
quirks, and anything that could not be cleaned is never offered for sending.
Runnable directly (``python3 test_card_privacy.py``) or via pytest.
"""

from __future__ import annotations

import re

from card_privacy import Redaction, compact_key, is_safe_label, sanitize

# (raw description, issuer City/State city or None, expected label; the key is its compact form)
CASES = [
    ("PIPER & PINE - SPRINSPRINGFIELD         IL", "SPRINGFIELD", "Piper & Pine"),
    ("COMCAST / XFINITY   800-555-0123        IL", "SPRINGFIELD", "Comcast Xfinity"),
    ("AVIS RENT A CAR     SAN FRANCISCO       CA", "PARSIPPANY", "Avis Rent A Car"),
    ("www.REI.com         KENT                WA", "KENT", "REI"),
    ("EXTRA SPACE 1865    SHELBYVILLE         IL", None, "Extra Space"),
    ("DD *DOORDASH CHICK-FSAN FRANCISCO       CA", None, "Dd Doordash Chick F"),
    ("CIRCLEK#1000043 OAK GROVE IL", None, "Circlek"),
    ("MAPLE LEAF IRRIGATION IN555-0101234 IL", None, "Maple Leaf Irrigation"),
    ("MAPLE LEAF IRRIGATION CO555-0101234 IN", None, "Maple Leaf Irrigation"),
    ("OAK GROVE LIQUOR 1 555-0105678 IL", None, "Oak Grove Liquor"),
    ("SQ *FRIENDS OF MAPLE & KSalem OR", None, "Friends Of Maple"),
    ("BP#1000336JOE & ANN'S SALEM OR", None, "BP Joe & Ann's"),
    ("NETFLIX.COM LOS GATOS CA", None, "Netflix"),
    ("IRC DONATION RESCUE.ORG NY", None, "Irc Donation Rescue"),
    ("AMAZON PRIME*4G23O48F3 Amzn.com/billWA", None, "Amazon Prime Amzn Billwa"),
]


def test_sample_descriptions():
    for raw, city, expected in CASES:
        got = sanitize(raw, Redaction(cities=[city] if city else []))
        assert got.label == expected, (raw, got.label, expected)
        assert got.merchant_key == compact_key(expected), (raw, got.merchant_key)


def test_spelling_variants_share_one_key():
    """Spacing and separators differ between issuers; the merchant is still one key, looked up once."""
    variants = ["CIRCLE K #1000043 OAK GROVE IL", "CIRCLEK#1000043 OAK GROVE IL",
                "CIRCLE_K 1000043 OAK GROVE IL", "Circle K Store 41", "CIRCLE-K 0042 SALEM OR",
                "CIRCLE.K 77 SALEM OR"]
    assert {sanitize(v).merchant_key for v in variants} == {"CIRCLEK"}
    assert sanitize("CIRCLE_K 1000043 OAK GROVE IL").label == "Circle K"
    assert sanitize("FRESH&FINE SPRIN SPRINGFIELD IL").label == "Fresh & Fine Sprin"


def test_compact_key():
    assert compact_key("Circle K") == compact_key("circle_k") == compact_key("CIRCLE-K") == "CIRCLEK"
    assert compact_key("BP Joe & Ann's") == "BPJOEANNS"


def test_store_numbers_do_not_split_a_merchant():
    """Two stores of one chain share a key, so a merchant is classified (and sent) once."""
    a = sanitize("CIRCLEK#1000043 OAK GROVE IL").merchant_key
    b = sanitize("CIRCLEK#1000086 OAK GROVE IL").merchant_key
    assert a == b


def test_processor_is_separated():
    s = sanitize("SQ *FRIENDS OF MAPLE & KSalem OR")
    assert s.processor == "Square" and "SQ" not in s.merchant_key.split()


def test_names_emails_phones_and_cards_removed():
    raw = "VENMO PAYMENT JANE EXAMPLE jane@example.com +1 217-555-0199 XXXX-XXXX-1234 REF 99812"
    s = sanitize(raw, Redaction(names=["Jane Example"], account_digits=["1234"]))
    upper = s.merchant_key
    for bad in ("JANE", "EXAMPLE", "@", "217", "0199", "1234", "99812"):
        assert bad not in upper, (bad, upper)


def test_no_digit_runs_survive_a_corpus():
    corpus = [raw for raw, _, _ in CASES] + [
        "UBER   *TRIP HELP.UBER.COM CA 94103", "SHELL OIL 57444 SPRINGFIELD IL", "PARKMOBILE 12-3456 IL",
        "STARBUCKS STORE 12345 SEATTLE WA", "TST* THE CORNER TABLE 77 SPRINGFIELD IL", "AMZN MKTP US*2K4ZZ81 WA",
        "ORDER 10/14 HOTEL 4455", "UNITED 0162345678901 HOUSTON", "CHECKCARD 1012 PANERA 5520 SALEM OR",
    ]
    for raw in corpus:
        key = sanitize(raw).merchant_key
        assert not re.search(r"\d{3,}", key), (raw, key)


def test_sanitize_is_idempotent():
    for raw, city, _ in CASES:
        once = sanitize(raw, Redaction(cities=[city] if city else []))
        again = sanitize(once.label)
        assert (again.merchant_key, again.label) == (once.merchant_key, once.label), (raw, once, again)


def test_unusable_keys_are_empty_and_unsafe():
    assert sanitize("#123456 IL").merchant_key == ""
    assert sanitize("").merchant_key == ""
    assert not is_safe_label("")
    assert not is_safe_label("Ab")


def test_safe_label_gate():
    assert is_safe_label("Piper & Pine")
    assert not is_safe_label("Store 12345")
    assert not is_safe_label("jane@example.com")
    assert not is_safe_label("Jane Example Catering", names=["JANE EXAMPLE"])


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)
