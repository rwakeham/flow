"""
Cache-busting for the static frontend: index.html's /static/... references get a
?v=<content hash> so a browser can never run an old script or stylesheet after a
deploy (main.py serves the page with Cache-Control: no-cache, and the files
themselves are revalidated by ETag).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_REF = re.compile(r'''(\b(?:src|href)=["'])/static/([^"'?#]+)(["'])''')
_cache: dict[tuple, str] = {}


def file_version(path: Path) -> str | None:
    """A short hash of a file's content, cached by (path, size, mtime); None when it doesn't exist."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _cache:
        _cache[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    return _cache[key]


def versioned(html: str, static_dir: str | Path) -> str:
    """index.html with every src/href="/static/<file>" carrying ?v=<that file's content hash>."""
    root = Path(static_dir)

    def sub(m: re.Match) -> str:
        name = m.group(2)
        target = (root / name).resolve()
        if root.resolve() not in target.parents:
            return m.group(0)                       # never read outside the static folder
        v = file_version(target)
        return m.group(0) if v is None else f"{m.group(1)}/static/{name}?v={v}{m.group(3)}"

    return _REF.sub(sub, html)
