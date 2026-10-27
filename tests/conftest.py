"""Test database setup. SQLite by default; set DATABASE_URL to run the same suite against Postgres."""

import os

import sqlalchemy


def _fresh_db(tmp_path, monkeypatch):
    url = os.environ.get("DATABASE_URL")
    if url:
        # Refuse to run against anything not named as a test database: the suite wipes it.
        assert url.rsplit("/", 1)[-1].endswith("_test"), "DATABASE_URL for tests must point at a *_test database"
        # Shared server: drop everything so each test starts clean.
        eng = sqlalchemy.create_engine(url)
        with eng.begin() as conn:
            conn.execute(sqlalchemy.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        eng.dispose()
    else:
        monkeypatch.setenv("ATTEST_DB", str(tmp_path / "t.db"))
