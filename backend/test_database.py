"""The database password is passed apart from DATABASE_URL, so any character in it works."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # never connects

from database import database_url  # noqa: E402


def test_password_with_url_characters_is_kept_whole():
    pw = "p@ss/w:rd#?%20 $x 'q\" \\"
    url = database_url("postgresql+psycopg2://flow@db:5432/flow", pw)
    assert url.password == pw
    assert (url.username, url.host, url.port, url.database) == ("flow", "db", 5432, "flow")
    # Rendered and parsed again (as the driver receives it), it is still the same password.
    from sqlalchemy.engine import make_url
    assert make_url(url.render_as_string(hide_password=False)).password == pw


def test_url_that_already_has_a_password_is_left_alone():
    url = database_url("postgresql+psycopg2://flow:inurl@db:5432/flow", "other")
    assert url.password == "inurl"


def test_no_password_given():
    assert database_url("postgresql+psycopg2://test@localhost/test", None).password is None
    assert database_url("postgresql+psycopg2://test@localhost/test", "").password is None
