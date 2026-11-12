"""Tests de la fenetre de pull Sage (7 jours par defaut)."""
from datetime import datetime, timedelta

import pytest

from app.config import manager as config_manager
from app.integration.sage import database as sage_database
from app.integration.sage import writer as sage_writer
from app.sync import bidirectional as bidi


class _Desc:
    def __init__(self, name):
        self.name = name

    def __getitem__(self, index):
        return self.name


class _FakeCursor:
    def __init__(self, doc_cols=None):
        self.doc_cols = list(doc_cols or [])
        self.description = []
        self.rows = []
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        self.calls.append((compact, tuple(params)))

        if "INFORMATION_SCHEMA.COLUMNS" in compact and "F_DOCENTETE" in compact:
            self.description = [_Desc("COLUMN_NAME")]
            self.rows = [(col,) for col in self.doc_cols]
        elif "INFORMATION_SCHEMA.COLUMNS" in compact and "F_COMPTET" in compact:
            self.description = [_Desc("COLUMN_NAME")]
            self.rows = []
        elif "FROM F_DOCENTETE d" in compact:
            self.description = []
            self.rows = []
        else:
            self.description = []
            self.rows = []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def _read_invoices(monkeypatch, doc_cols=None, **kwargs):
    cursor = _FakeCursor(doc_cols=doc_cols)
    monkeypatch.setattr(sage_writer, "_get_sage_cursor", lambda: cursor)
    monkeypatch.setattr(
        sage_database,
        "fetch_doc_lines_batch",
        lambda *args, **call_kwargs: {},
    )
    sage_writer.read_invoices_from_sage(**kwargs)
    queries = [
        (sql, params)
        for sql, params in cursor.calls
        if "FROM F_DOCENTETE d" in sql
    ]
    assert len(queries) == 1
    return queries[0]


SFEC_COLS = ["SFEC_STATUT", "SFEC_NUM_CERTIF", "SFEC_SIGNATURE", "SFEC_QR_CODE"]


def test_sage_pull_days_zero_aucune_clause(monkeypatch):
    sql, params = _read_invoices(monkeypatch, SFEC_COLS, sage_pull_days=0)
    assert "DO_Date >=" not in sql
    assert "cbModification >=" not in sql
    assert params == ()


def test_sage_pull_days_sept_filtre_do_date(monkeypatch):
    sql, params = _read_invoices(monkeypatch, SFEC_COLS, sage_pull_days=7)
    assert "d.DO_Date >= ?" in sql
    assert "cbModification >=" not in sql
    assert len(params) == 1
    assert isinstance(params[0], datetime)
    expected = datetime.now() - timedelta(days=7)
    assert abs(params[0] - expected) < timedelta(seconds=5)


def test_sage_pull_days_avec_cbmodification(monkeypatch):
    cols = SFEC_COLS + ["cbModification"]
    sql, params = _read_invoices(monkeypatch, cols, sage_pull_days=7)
    assert "(d.DO_Date >= ? OR d.cbModification >= ?)" in sql
    assert len(params) == 2
    assert params[0] == params[1]


def test_force_all_ignore_la_fenetre(monkeypatch):
    sql, params = _read_invoices(
        monkeypatch, SFEC_COLS, sage_pull_days=7, force_all=True
    )
    assert "DO_Date >=" not in sql
    assert "cbModification >=" not in sql
    assert params == ()


def test_sans_argument_pull_complet_pour_compatibilite(monkeypatch):
    sql, params = _read_invoices(monkeypatch, SFEC_COLS)
    assert "DO_Date >=" not in sql
    assert params == ()


def _capture_pull(monkeypatch, db_cfg):
    captured = {}

    def fake_read(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(bidi, "read_invoices_from_sage", fake_read)
    monkeypatch.setattr(
        config_manager,
        "get_config",
        lambda: {"db": db_cfg},
    )
    result = bidi.pull_from_sage()
    return captured, result


def test_pull_from_sage_lit_la_config(monkeypatch):
    captured, result = _capture_pull(monkeypatch, {"sage_pull_days": 7})
    assert captured == {"sage_pull_days": 7, "force_all": False}
    assert result == {"pulled": 0, "new": 0, "updated": 0}


def test_pull_from_sage_defaut_sept_si_absent(monkeypatch):
    captured, _ = _capture_pull(monkeypatch, {})
    assert captured["sage_pull_days"] == 7


@pytest.mark.parametrize("raw_days", ["abc", None, -1])
def test_pull_from_sage_valeur_invalide_repli_sept(monkeypatch, raw_days):
    captured, _ = _capture_pull(monkeypatch, {"sage_pull_days": raw_days})
    assert captured["sage_pull_days"] == 7


def test_pull_from_sage_force_all(monkeypatch):
    captured = {}

    def fake_read(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(bidi, "read_invoices_from_sage", fake_read)
    monkeypatch.setattr(
        config_manager,
        "get_config",
        lambda: {"db": {"sage_pull_days": 7}},
    )
    bidi.pull_from_sage(force_all=True)
    assert captured == {"sage_pull_days": 7, "force_all": True}


def _csrf(client):
    return client.get("/api/csrf").get_json()["token"]


def test_config_sync_sage_pull_days_borne(client):
    old_cfg = config_manager.get_config()
    old_value = (old_cfg.get("db") or {}).get("sage_pull_days", 7)
    token = _csrf(client)
    try:
        resp = client.post(
            "/api/config/sync",
            json={"sage_pull_days": 7},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert config_manager.get_config()["db"]["sage_pull_days"] == 7

        resp = client.post(
            "/api/config/sync",
            json={"sage_pull_days": 0},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert config_manager.get_config()["db"]["sage_pull_days"] == 0

        for invalid in (-1, 3651):
            resp = client.post(
                "/api/config/sync",
                json={"sage_pull_days": invalid},
                headers={"X-CSRF-Token": token},
            )
            assert resp.status_code == 400
    finally:
        cfg = config_manager.get_config()
        cfg.setdefault("db", {})["sage_pull_days"] = old_value
        config_manager.save_config(cfg)
