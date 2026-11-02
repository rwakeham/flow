"""Tests for the frontend's content-versioned /static URLs (asset_versions.py)."""

from __future__ import annotations

import re

from asset_versions import versioned

PAGE = '''<link rel="stylesheet" href="/static/cards.css">
<script src="/static/cards.js"></script>
<img src='/static/logo.png' alt="">
<script src="/static/missing.js"></script>
<script src="https://cdnjs.cloudflare.com/x.js"></script>
<a href="/static/../secret.txt">x</a>
<p>/static/cards.js in text</p>'''


def test_static_references_get_a_content_version(tmp_path):
    (tmp_path / "cards.js").write_text("one")
    (tmp_path / "cards.css").write_text("css")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    (tmp_path.parent / "secret.txt").write_text("s")
    out = versioned(PAGE, tmp_path)
    assert re.search(r'src="/static/cards\.js\?v=[0-9a-f]{10}"', out)
    assert re.search(r'href="/static/cards\.css\?v=[0-9a-f]{10}"', out)
    assert re.search(r"src='/static/logo\.png\?v=[0-9a-f]{10}'", out)
    assert 'src="/static/missing.js"' in out                      # unknown files left alone
    assert 'https://cdnjs.cloudflare.com/x.js"' in out            # other hosts left alone
    assert 'href="/static/../secret.txt"' in out                  # nothing outside the folder is read
    assert "<p>/static/cards.js in text</p>" in out               # only attributes change


def test_version_follows_the_content(tmp_path):
    f = tmp_path / "cards.js"
    f.write_text("one")
    first = versioned('<script src="/static/cards.js"></script>', tmp_path)
    assert versioned('<script src="/static/cards.js"></script>', tmp_path) == first
    f.write_text("two, longer")
    assert versioned('<script src="/static/cards.js"></script>', tmp_path) != first
