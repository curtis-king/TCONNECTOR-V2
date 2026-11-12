"""Tests Vague 4 dashboard : metriques, /pending, robustesse API et health."""
from app.storage.db import get_cursor
from app.sync import engine


def test_metrics_exclut_avoirs_et_compte_statut_comptable(monkeypatch):
    monkeypatch.setattr(engine, "fetch_certified_invoices", lambda: [])
    monkeypatch.setattr(engine, "get_status", lambda *a, **k: {"online": True})
    monkeypatch.setitem(engine._cache, "sales_invoices", [
        {"id": "1", "type_doc": "vente", "statut_code": 2},
        {"id": "2", "type_doc": "avoir", "statut_code": 2},
        {"id": "3", "type_doc": "vente", "statut_code": 0},
    ])
    metrics = engine.get_metrics()
    assert metrics["sales_invoices"] == 2
    assert metrics["credit_notes"] == 1
    assert metrics["pending_accounting"] == 1


def test_pending_rendu_echappe_et_filtre(client, monkeypatch):
    from app.web.routes import dashboard
    cache = {
        "sales_invoices": [
            {"id": "1", "numero": "F-OK", "statut_code": 2, "valide": 1,
             "sfec_statut": "", "date_facture": "2026-10-20",
             "nom_tiers": "<script>x</script>", "montant_ttc": "abc"},
            {"id": "2", "numero": "F-SURV", "statut_code": 2, "valide": 1,
             "sfec_statut": "A_SURVEILLER", "date_facture": "2026-10-20",
             "nom_tiers": "Surveille", "montant_ttc": 1000},
        ],
        "sfec_invoices": [],
    }
    monkeypatch.setattr(dashboard, "get_cache", lambda: cache)
    monkeypatch.setattr(dashboard, "get_metrics", lambda: {"pending_accounting": 2})
    resp = client.get("/pending")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "F-OK" in html
    assert "F-SURV" not in html
    assert "<script>x</script>" not in html


def test_api_tickets_parametres_invalides(client):
    assert client.get("/api/pos/tickets?limit=abc&offset=-4").status_code == 200


def test_health_et_ready(client):
    health = client.get("/health")
    ready = client.get("/ready")
    assert health.status_code == 200, health.get_json()
    assert ready.status_code == 200, ready.get_json()
    assert health.get_json()["checks"]["sqlite"] == "up"
    assert ready.get_json()["ready"] is True


def test_sales_export_csv(client):
    from app.domain import pos as pos_engine

    prefix = "TEST-DASH-EX"
    with get_cursor() as cur:
        cur.execute("DELETE FROM pos_ticket_lines WHERE ticket_id IN "
                    "(SELECT id FROM pos_tickets WHERE numero LIKE ?)", (prefix + "%",))
        cur.execute("DELETE FROM pos_tickets WHERE numero LIKE ?", (prefix + "%",))
    with get_cursor() as cur:
        cur.execute("SELECT id FROM vendeurs WHERE est_actif = 1 LIMIT 1")
        vendeur = cur.fetchone()
    result = pos_engine.create_ticket({
        "numero": prefix + "-1",
        "tiers_code": prefix + "-C1",
        "tiers_nom": "Acheteur Export",
        "tiers_type": "individual",
        "mode_paiement": "virement",
        "vendeur_id": vendeur["id"] if vendeur else None,
        "lignes": [{"designation": "Export test", "quantite": 1,
                    "prix_unitaire": 1000, "taux_tva": 18}],
    })
    assert result["success"] is True, result.get("error")
    r = client.get("/sales/export?search=Acheteur Export")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("Content-Type", "")
    body = r.get_data(as_text=True)
    assert prefix + "-1" in body
    r2 = client.get("/sales/export?search=Acheteur+Aucun")
    assert r2.status_code == 200
    assert "ticket" in r2.get_data(as_text=True)
