"""Tests du correctif 04 : synchronisation des articles Sage.

Regles verifiees :
  - l'INSERT fournit bien les 12 parametres (dont `sage_ar_ref`) ;
  - un deuxieme passage met a jour sans dupliquer ;
  - un article sans `ref` est ignore ;
  - un `tva_code` vide retombe sur 18 %.
"""
import pytest

from app.storage import db
from app.sync import article_sync

PREFIX = "TEST-ART-04-"


@pytest.fixture(autouse=True)
def _clean_products():
    db.init_database()
    _purge()
    yield
    _purge()


def _purge():
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM products WHERE ref LIKE ?", (PREFIX + "%",))


def _article(ref, **kw):
    art = {
        "ref": ref,
        "barcode": "123",
        "designation": "Article test " + ref,
        "famille": "Divers",
        "nature": 0,
        "prix_vente": 1000,
        "prix_achat": 700,
        "tva_code": "18",
        "unite": "U",
        "stock": 5,
        "est_actif": 1,
    }
    art.update(kw)
    return art


def _sync(monkeypatch, articles):
    monkeypatch.setattr(article_sync, "fetch_articles", lambda: articles)
    return article_sync.sync_articles_from_sage()


def test_insert_12_parametres(monkeypatch):
    res = _sync(monkeypatch, [_article(PREFIX + "001")])

    assert res["ok"] is True, res
    assert res["inserted"] == 1
    assert res["updated"] == 0

    with db.get_cursor() as cur:
        row = cur.execute(
            "SELECT * FROM products WHERE ref = ?", (PREFIX + "001",)
        ).fetchone()
    assert row["sage_ar_ref"] == PREFIX + "001"
    assert row["synced_sage"] == 1
    assert row["designation"] == "Article test " + PREFIX + "001"


def test_update_existant_sans_duplication(monkeypatch):
    _sync(monkeypatch, [_article(PREFIX + "002", prix_vente=1000)])
    res = _sync(monkeypatch, [_article(PREFIX + "002", prix_vente=2500)])

    assert res["ok"] is True, res
    assert res["inserted"] == 0
    assert res["updated"] == 1

    with db.get_cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM products WHERE ref = ?", (PREFIX + "002",)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["prix_vente"] == 2500


def test_article_sans_ref_ignore(monkeypatch):
    res = _sync(monkeypatch, [
        _article(""),
        _article(PREFIX + "003"),
    ])

    assert res["ok"] is True, res
    assert res["inserted"] == 1
    with db.get_cursor() as cur:
        row = cur.execute(
            "SELECT * FROM products WHERE ref = ''"
        ).fetchone()
    assert row is None


def test_tva_vide_retombe_sur_18(monkeypatch):
    res = _sync(monkeypatch, [_article(PREFIX + "004", tva_code="")])

    assert res["ok"] is True, res
    with db.get_cursor() as cur:
        row = cur.execute(
            "SELECT tva_code FROM products WHERE ref = ?", (PREFIX + "004",)
        ).fetchone()
    assert row["tva_code"] == "18"


def test_erreur_ecriture_loggue_prepared(monkeypatch):
    def boom(_articles):
        raise RuntimeError("Incorrect number of bindings supplied")

    monkeypatch.setattr(article_sync, "fetch_articles",
                        lambda: [_article(PREFIX + "005")])
    monkeypatch.setattr(article_sync, "_upsert_products", boom)

    res = article_sync.sync_articles_from_sage()

    assert res["ok"] is False
    assert "Incorrect number of bindings" in res["error"]
    assert res["prepared"] == 1
