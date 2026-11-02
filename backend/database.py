import os
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker, DeclarativeBase


def database_url(url: str, password: str | None) -> URL:
    """DATABASE_URL with POSTGRES_PASSWORD added when the URL carries none. docker-compose.yml
    passes the password on its own, so characters such as @ / : # ? % can't break the address."""
    parsed = make_url(url)
    if password and parsed.password is None:
        parsed = parsed.set(password=password)
    return parsed


DATABASE_URL = database_url(os.environ["DATABASE_URL"], os.environ.get("POSTGRES_PASSWORD"))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
