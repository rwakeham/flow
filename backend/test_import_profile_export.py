"""The format export's download name works for a format named in any language."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # never connects
os.environ.setdefault("SECRET_KEY", "test-" * 8)

from urllib.parse import unquote  # noqa: E402

import pytest  # noqa: E402
from starlette.responses import Response  # noqa: E402

from routers.import_profiles import download_disposition  # noqa: E402


@pytest.mark.parametrize("name, ascii_name", [
    ("Example Bank CSV", "Example Bank CSV"),
    ("Banque Société", "Banque Soci_t"),
    ("Сбербанк выписка", "format"),
    ("招商银行", "format"),
    ('Quote " and / slash', "Quote _ and _ slash"),
])
def test_any_name_makes_a_valid_header(name, ascii_name):
    value = download_disposition(name, ".flow-format.json")
    Response("{}", headers={"Content-Disposition": value})          # used to raise UnicodeEncodeError
    assert value.isascii()
    assert f'filename="{ascii_name}.flow-format.json"' in value
    real = unquote(value.split("filename*=UTF-8''", 1)[1])
    assert real == name.replace('"', "").replace("/", "") + ".flow-format.json"
