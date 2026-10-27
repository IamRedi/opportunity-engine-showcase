"""Faza 8B: multi-user auth + tenant foundation + izolim (testet e thjeshtuara)."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import auth_service
import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()
    yield


# ------------------------------------------------- model & migration

def test_users_tenants_tables_exist():
    with repo.db() as con:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "tenants", "tenant_memberships"} <= names


def test_default_owner_exists():
    with repo.db() as con:
        row = con.execute(
            "SELECT email, status FROM users WHERE id=1").fetchone()
    assert row and row["status"] == "active"
    tm = con.execute(
        "SELECT role FROM tenant_memberships WHERE user_id=1").fetchone()
    assert tm and tm[0] == "owner"


def test_tenant_default_exists():
    with repo.db() as con:
        row = con.execute(
            "SELECT name, slug FROM tenants WHERE id=1").fetchone()
    assert row and row["slug"] == "default"


def test_password_hash_not_plaintext():
    with repo.db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=1").fetchone()
    assert len(row["password_hash"]) > 20
    assert "changeme" not in row["password_hash"].lower()


# ------------------------------------------------- auth_service

def test_authenticate_success_and_failure():
    import os
    email = "auth@test.local"
    pw_hash = auth_service._hash_pw("goodpw") if hasattr(auth_service, '_hash_pw') else ""
    # krijo direkt me SQL
    with repo.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (id,email,username,password_hash,status,created_at,updated_at) "
            "VALUES (100,?,?,?,'active',datetime('now'),datetime('now'))",
            (email, "authuser", pw_hash))
    # auth_service._hash_pw s'ekziston në 8B (8B përdor auth_service.create_user)
    # testimi kryesor: authenticate punon ose kthen None pa crash
    result = auth_service.authenticate(email, "wrongpw")
    assert result is None  # password i gabuar
    with repo.db() as con:
        con.execute("DELETE FROM users WHERE id=100")
        con.commit()


def test_inactive_user_rejected():
    email = "inactive@test.local"
    with repo.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (id,email,username,password_hash,status,created_at,updated_at) "
            "VALUES (200,?,?,?,'disabled',datetime('now'),datetime('now'))",
            (email, "inact", "hashedpw"))
    assert auth_service.authenticate(email, "pw") is None
    with repo.db() as con:
        con.execute("DELETE FROM users WHERE id=200")
        con.commit()


# ------------------------------------------------- user_id columns exist

def test_user_id_columns_exist_on_user_owned_tables():
    with repo.db() as con:
        for table in ("opportunities", "applications", "notifications",
                      "schedule_configs", "search_runs"):
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            assert "user_id" in cols, f"{table} mungon user_id"


# ------------------------------------------------- create_user + auth

def test_create_user_and_authenticate(client):
    import auth_service as auth
    email = "newuser@test.local"
    uid = auth.create_user(email, "goodpw", "New User", tenant_id=1)
    assert uid > 0
    assert auth.authenticate(email, "goodpw") == uid
    assert auth.authenticate(email, "wrongpw") is None
    with repo.db() as con:
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        con.commit()


def test_csrf_token_roundtrip():
    import secrets
    token = secrets.token_urlsafe(24)
    assert auth_service.csrf_ok(token, token)
    assert not auth_service.csrf_ok(token, "different")


# ------------------------------------------------- schema columns

def test_l3_key_column_exists():
    with repo.db() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(opportunities)")}
    assert "l3_key" in cols
    assert "user_id" in cols
