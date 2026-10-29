"""Pagination complète du cache des factures SFEC (/certified)."""
from app.sync import engine


def _batch(prefix, count):
    return [
        {
            "id": "{}-{}".format(prefix, index),
            "invoice_number": "{}-{}".format(prefix, index),
            "seller_invoice_number": "SAGE-{}-{}".format(prefix, index),
        }
        for index in range(count)
    ]


def test_sync_sfec_invoices_pagine_toutes_les_pages(monkeypatch):
    pages = {
        1: _batch("P1", 20),
        2: _batch("P2", 20),
        3: _batch("P3", 5),
    }
    calls = []

    class FakeClient:
        def list_invoices(self, page=1, page_size=20, **_kwargs):
            calls.append((page, page_size))
            return {
                "invoices": pages.get(page, []),
                "page": page,
                "pageSize": page_size,
                "totalPages": 3,
            }

    monkeypatch.setattr(engine, "SfecClient", FakeClient)
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(
        engine, "get_config",
        lambda: {"sfec": {"enabled": True, "api_key": "test",
                          "lookup_max_pages": 10}},
    )
    monkeypatch.setitem(engine._cache, "sfec_invoices", [])
    monkeypatch.setitem(engine._cache, "certified_count", 0)
    monkeypatch.setitem(engine._cache, "last_sfec_sync_at", None)

    engine.sync_sfec_invoices()

    assert calls == [(1, 20), (2, 20), (3, 20)]
    assert len(engine._cache["sfec_invoices"]) == 45
    assert engine._cache["certified_count"] == 45
    assert engine._cache["last_sfec_sync_at"]


def test_sync_sfec_invoices_arrete_sur_page_vide_sans_total_pages(monkeypatch):
    calls = []

    class FakeClient:
        def list_invoices(self, page=1, page_size=20, **_kwargs):
            calls.append((page, page_size))
            return {
                "invoices": _batch("P1", 20) if page == 1 else [],
                "page": page,
                "pageSize": page_size,
            }

    monkeypatch.setattr(engine, "SfecClient", FakeClient)
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(
        engine, "get_config",
        lambda: {"sfec": {"enabled": True, "api_key": "test"}},
    )
    monkeypatch.setitem(engine._cache, "sfec_invoices", [])
    monkeypatch.setitem(engine._cache, "certified_count", 0)
    monkeypatch.setitem(engine._cache, "last_sfec_sync_at", None)

    engine.sync_sfec_invoices()

    assert calls == [(1, 20), (2, 20)]
    assert len(engine._cache["sfec_invoices"]) == 20
    assert engine._cache["certified_count"] == 20
