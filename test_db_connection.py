"""Checks the database connection. Works for local PostgreSQL and Aiven: only the DB_* env vars change.

    uv run python test_db_connection.py
"""

import os

import db

version, _ = db.run("SELECT version() AS version", ())
current, _ = db.run("SELECT current_database() AS name", ())
print("version():          ", version[0]["version"])
print("current_database():", current[0]["name"])

expected = os.environ.get("DB_NAME", "documind")
assert current[0]["name"] == expected, f"connected to {current[0]['name']!r}, expected {expected!r}"
print("OK")
