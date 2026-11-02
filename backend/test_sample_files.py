"""
Every sample file in testdata/sample_files.py reads, with its import format, to exactly the
rows in testdata/expected_rows/<name>.json: dates, amounts, descriptions, pending and skipped
counts, statement period, cardholders, card numbers and the sign convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import import_profile as ip
from testdata.sample_files import FORMATS, plain, samples, spec

EXPECTED = Path(__file__).parent / "testdata" / "expected_rows"


@pytest.mark.parametrize("sample", samples(), ids=lambda s: s.name)
def test_each_sample_reads_to_its_expected_rows(sample):
    expected = json.loads((EXPECTED / f"{sample.name}.json").read_text())
    s = spec(sample.format)
    parsed = ip.parse(sample.content, sample.filename, s)
    if sample.reads_as == "card":
        got = plain(ip.to_card_result(parsed, s, "x"))
        for key, value in expected.items():
            assert got[key] == value, key
    elif sample.reads_as == "ledger":
        assert s.row_source == "manual" and plain(ip.to_ledger_rows(parsed)) == expected["ledger_rows"]
    else:
        assert s.row_source == "bank" and plain(ip.to_bank_rows(parsed)) == expected["bank_rows"]


def test_every_sample_has_expected_rows_and_every_format_is_used():
    assert {s.name for s in samples()} == {p.stem for p in EXPECTED.glob("*.json")}
    # csv_preamble shares its columns with xlsx_preamble and differs only in file type.
    assert set(FORMATS) - {s.format for s in samples()} == {"csv_preamble"}


def test_formats_round_trip_and_their_fingerprints_differ():
    seen: dict[tuple, str] = {}
    for key in FORMATS:
        if FORMATS[key].get("has_header") and not FORMATS[key].get("header_cells"):
            continue                    # the two generic formats name their columns but pin no header
        s = spec(key)
        assert s.to_json() == ip.Spec.from_json(s.to_json()).to_json()
        k = (s.kind, s.file_type, s.fingerprint)
        assert k not in seen or {key, seen[k]} <= {"xlsx_preamble", "csv_preamble"}, (key, seen.get(k))
        seen.setdefault(k, key)
