"""Tests du correctif 05 : avoirs, references et pieces negatives.

Regles verifiees :
  - resolution d'une reference libre (exacte, prefixe, token, invalide) ;
  - lien manuel prioritaire ;
  - avoir sans reference -> A_SURVEILLER, jamais ERREUR ;
  - parent non certifie -> aucun marquage (attente du cycle suivant) ;
  - parent certifie -> creditNote avec les trois champs exiges ;
  - deuxieme avoir sur le meme parent, numero different -> certifie (retours multiples autorises) ;
  - redaction du meme numero d'avoir -> A_SURVEILLER (doublon) ;
  - facture a 0 -> A_SURVEILLER ;
  - facture negative -> creditNote a montants positifs.
"""
import pytest

from app.integration.sage.writer import sync_sage_invoice_to_sqlite
from app.sync.bidirectional import _update_from_sage
from app.integration.sfec.endpoints import (
    db_invoice_to_sfec, resolve_invoice_number, validate_sfec_payload,
)
from app.storage import db as sqlite_db
from app.sync import engine


BY = {"25VFA3122": {}, "9FAZ00124": {}, "P1": {}}


# ── Resolution des references ───────────────────────────────────────────────
def test_reference_exacte():
    assert resolve_invoice_number("25VFA3122", BY) == "25VFA3122"


def test_reference_avec_prefixe_libre():
    assert resolve_invoice_number("N° BC 25VFA3122 du 27/07", BY) == "25VFA3122"


def test_reference_invalide():
    assert resolve_invoice_number("KANE", BY) is None


def test_reference_ambigue():
    assert resolve_invoice_number("ZEN", BY) is None


def test_lien_manuel_prioritaire():
    assert resolve_invoice_number(
        "KANE", BY, manual_links={"AV1": "P1"}, child_numero="AV1"
    ) == "P1"


# ── Moteur de certification ─────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_retry_memory():
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()
    yield
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()


def _setup_pass(monkeypatch, invoices):
    monkeypatch.setattr(engine, "_cache", {
        **engine._cache,
        "sales_invoices": invoices,
    })
    monkeypatch.setattr(engine, "get_config", lambda: {
        "sfec": {"enabled": True, "api_key": "test"},
        "queue": {"max_concurrent": 1},
    })
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "_startup_reset_done", True)
    monkeypatch.setattr(engine, "_build_sfec_lookup", lambda: {})
    monkeypatch.setattr(engine, "fetch_sfec_invoice_links", lambda: {})
    monkeypatch.setattr(engine, "get_certified_by_sage_id", lambda _id: None)


def _parent(**kw):
    inv = {
        "id": "0-6-P1", "numero": "P1", "statut_code": 2,
        "sfec_statut": "CERTIFIE", "sfec_invoice_number": "CG-P1",
        "type_doc": "vente", "montant_ttc": 1189,
        "date_facture": "2026-01-01",
    }
    inv.update(kw)
    return inv


def _child(**kw):
    inv = {
        "id": "0-7-A1", "numero": "A1", "statut_code": 2,
        "sfec_statut": "", "type_doc": "avoir",
        "reference": "P1", "reference_invoice_id": "",
        "montant_ttc": -1189, "date_facture": "2026-01-02",
    }
    inv.update(kw)
    return inv


def _capture_worker(monkeypatch):
    calls = []

    def fake_worker(invoice, _lookup):
        calls.append(invoice)
        return ("ok", invoice["numero"], "CERT")

    monkeypatch.setattr(engine, "_certify_single_worker", fake_worker)
    return calls


def test_avoir_sans_reference_a_surveiller(monkeypatch):
    _setup_pass(monkeypatch, [_child(reference="")])
    calls = _capture_worker(monkeypatch)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    engine._auto_certify_invoices_locked()

    assert calls == []
    assert monitors and monitors[0][0] == "0-7-A1"
    assert "introuvable" in monitors[0][1]


def test_parent_non_certifie_attend_sans_marquage(monkeypatch):
    parent = _parent(sfec_statut="", sfec_invoice_number="")
    _setup_pass(monkeypatch, [parent, _child()])
    calls = _capture_worker(monkeypatch)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    engine._auto_certify_invoices_locked()

    # Le parent peut etre certifie dans la meme passe, mais l'avoir doit
    # attendre : il n'est pas envoye et n'est pas marque.
    assert [c["numero"] for c in calls] == ["P1"]
    assert monitors == []


def test_parent_certifie_renseigne_les_trois_champs(monkeypatch):
    _setup_pass(monkeypatch, [_parent(), _child()])
    calls = _capture_worker(monkeypatch)

    engine._auto_certify_invoices_locked()

    assert len(calls) == 1
    inv = calls[0]
    assert inv["reference_invoice_id"] == "P1"
    assert inv["external_credit_note_number"] == "A1"
    assert inv["original_sfec_invoice_number"] == "CG-P1"


def test_deuxieme_avoir_sur_meme_parent_est_accepte(monkeypatch):
    """Deux retours partiels (numeros differents) sur le meme parent P1
    sont certifies : seul la redition du meme numero est bloquee."""
    deja = _child(
        id="0-7-A0", numero="A0", sfec_statut="CERTIFIE",
        reference_invoice_id="P1",
    )
    _setup_pass(monkeypatch, [_parent(), deja, _child()])
    calls = _capture_worker(monkeypatch)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    engine._auto_certify_invoices_locked()

    # L'avoir A1 (numero different de A0) sur le meme parent P1 est certifie.
    assert [c["numero"] for c in calls] == ["A1"]
    assert monitors == []


def test_prepare_credit_note_accepte_autre_numero_meme_parent():
    """Meme parent P1, numero different : pas de doublon, etat ok."""
    credit_index = {"A0": ["0-7-A0"]}  # A0 deja certifie sur le parent P1
    enfant = _child(
        id="0-7-A1", numero="A1", sfec_statut="", reference_invoice_id="P1",
    )
    state, ref, parent, reason = engine._prepare_credit_note(
        enfant, {"P1": _parent()}, {}, credit_index)
    assert state == "ok"
    assert ref == "P1"


def test_prepare_credit_note_bloque_redition_meme_numero():
    """Meme numero d'avoir, id different : doublon, marque A_SURVEILLER."""
    credit_index = {"A0": ["0-7-A0"]}  # A0 deja certifie
    redite = _child(
        id="0-7-A0-bis", numero="A0", sfec_statut="", reference_invoice_id="P1",
    )
    state, ref, parent, reason = engine._prepare_credit_note(
        redite, {"P1": _parent()}, {}, credit_index)
    assert state == "duplicate"
    assert ref == "P1"
    assert "deja" in reason


def test_facture_zero_a_surveiller(monkeypatch):
    inv = {
        "id": "0-6-Z1", "numero": "Z1", "statut_code": 2,
        "sfec_statut": "", "type_doc": "vente", "montant_ttc": 0,
        "date_facture": "2026-01-01",
    }
    _setup_pass(monkeypatch, [inv])
    calls = _capture_worker(monkeypatch)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    engine._auto_certify_invoices_locked()

    assert calls == []
    assert monitors and "Montant nul" in monitors[0][1]


def test_facture_negative_sans_reference_non_marquee(monkeypatch):
    inv = _child(
        id="0-6-N1", numero="N1", type_doc="vente",
        reference="", reference_invoice_id="", montant_ttc=-1189,
    )
    _setup_pass(monkeypatch, [inv])
    calls = _capture_worker(monkeypatch)
    monitors = []
    monkeypatch.setattr(
        engine, "mark_to_monitor",
        lambda invoice_id, reason="": monitors.append((invoice_id, reason)),
    )

    engine._auto_certify_invoices_locked()

    # Les 5 585 factures negatives ne sont pas noyees dans A_SURVEILLER tant
    # que le client n'a pas tranche ; elles restent hors perimetre.
    assert calls == []
    assert monitors == []


def test_facture_negative_avec_parent_devient_credit_note(monkeypatch):
    inv = _child(
        id="0-6-N1", numero="N1", type_doc="vente",
        reference="P1", reference_invoice_id="", montant_ttc=-1189,
    )
    _setup_pass(monkeypatch, [_parent(), inv])
    calls = _capture_worker(monkeypatch)

    engine._auto_certify_invoices_locked()

    assert len(calls) == 1
    assert calls[0]["numero"] == "N1"
    assert calls[0]["reference_invoice_id"] == "P1"
    assert calls[0]["external_credit_note_number"] == "N1"
    assert calls[0]["original_sfec_invoice_number"] == "CG-P1"


def test_lien_manuel_parent_hors_cache(monkeypatch):
    _setup_pass(monkeypatch, [_child(reference="KANE")])
    monkeypatch.setattr(
        engine, "fetch_sfec_invoice_links", lambda: {"A1": "OLD-P"}
    )
    calls = _capture_worker(monkeypatch)

    class FakeClient:
        def verify_by_invoice_number(self, _ref):
            return {"invoice_number": "CG-OLD", "invoice_type": "salesInvoice"}

    monkeypatch.setattr(engine, "SfecClient", FakeClient)

    engine._auto_certify_invoices_locked()

    assert len(calls) == 1
    assert calls[0]["reference_invoice_id"] == "OLD-P"
    assert calls[0]["original_sfec_invoice_number"] == "CG-OLD"


def test_parent_sans_numero_sfec_utilise_get(monkeypatch):
    parent = _parent(sfec_invoice_number="")
    _setup_pass(monkeypatch, [parent, _child()])
    calls = _capture_worker(monkeypatch)

    class FakeClient:
        def verify_by_invoice_number(self, _ref):
            return {"invoice_number": "CG-9"}

    monkeypatch.setattr(engine, "SfecClient", FakeClient)

    engine._auto_certify_invoices_locked()

    assert len(calls) == 1
    assert calls[0]["original_sfec_invoice_number"] == "CG-9"


# ── Payload creditNote ──────────────────────────────────────────────────────
def _negative_invoice():
    return {
        "id": "0-6-N1", "numero": "N1", "type_doc": "vente",
        "montant_ht": -1000, "montant_ttc": -1189,
        "montant_restant": -1189, "reference": "P1",
        "reference_invoice_id": "P1",
        "external_credit_note_number": "N1",
        "original_sfec_invoice_number": "CG-P1",
        "nom_tiers": "Client",
        "recipient_niu": "M000000000000001",
        "lignes": [{
            "quantite": -1, "prix_unitaire": 1000,
            "montant_ht": -1000, "montant_tva": -180,
            "montant_ttc": -1189, "taux_tva": 18,
            "designation": "Article",
        }],
    }


def test_facture_negative_devient_credit_note_positif():
    payload = db_invoice_to_sfec(_negative_invoice())

    assert payload["invoice_type"] == "creditNote"
    assert payload["total_amount"] == 1189
    assert payload["subtotal"] == 1000
    assert payload["total_tax_amount"] == 180
    assert payload["additional_cent_tax"] == 9
    assert payload["amount_due"] == 1189
    assert payload["items"][0]["total_amount"] == 1180
    assert payload["reference_invoice_id"] == "P1"
    assert payload["external_credit_note_number"] == "N1"
    assert payload["original_sfec_invoice_number"] == "CG-P1"
    assert validate_sfec_payload(payload) == []


def test_credit_note_exige_les_deux_champs_openapi():
    payload = db_invoice_to_sfec(_negative_invoice())
    payload.pop("external_credit_note_number")
    payload.pop("original_sfec_invoice_number")

    errors = validate_sfec_payload(payload)

    assert any("external_credit_note_number" in e for e in errors)
    assert any("original_sfec_invoice_number" in e for e in errors)


# ── Miroir local et lien manuel ─────────────────────────────────────────────
PREFIX = "TEST-AV05-"


def _purge():
    with sqlite_db.get_cursor() as cur:
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM sfec_invoice_links WHERE numero LIKE ?",
                    (PREFIX + "%",))


def test_miroir_avoir_ecrit_type_et_sage():
    sqlite_db.init_database()
    _purge()
    numero = PREFIX + "001"
    try:
        invoice_id = sync_sage_invoice_to_sqlite({
            "numero": numero,
            "date_facture": "2026-01-15",
            "tiers_code": "C1",
            "type_doc": "avoir",
            "sage_domaine": 0,
            "sage_type": 7,
            "sage_piece": numero,
            "montant_ht": -1000,
            "montant_ttc": -1189,
            "montant_restant": -1189,
            "statut": "A COMPTABILISER",
            "valide": 1,
        })
        with sqlite_db.get_cursor() as cur:
            row = cur.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        assert row["type_doc"] == "avoir"
        assert row["sage_type"] == 7
        assert row["sage_domaine"] == 0
        assert row["sage_piece"] == numero
        assert row["montant_tva"] == -180
        assert row["additional_cent_tax"] == -9
    finally:
        _purge()


def test_pull_corrige_une_ligne_existante_en_avoir():
    sqlite_db.init_database()
    _purge()
    numero = PREFIX + "002"
    try:
        invoice_id = sync_sage_invoice_to_sqlite({
            "numero": numero,
            "date_facture": "2026-01-15",
            "tiers_code": "C1",
            "type_doc": "vente",
            "sage_domaine": 0,
            "sage_type": 6,
            "sage_piece": numero,
            "montant_ht": 1000,
            "montant_ttc": 1189,
            "montant_restant": 1189,
            "statut": "A COMPTABILISER",
            "valide": 1,
        })
        _update_from_sage(invoice_id, {
            "numero": numero,
            "type_doc": "avoir",
            "sage_domaine": 0,
            "sage_type": 7,
            "sage_piece": numero,
            "montant_ht": -1000,
            "montant_ttc": -1189,
            "montant_restant": -1189,
        })
        with sqlite_db.get_cursor() as cur:
            row = cur.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        assert row["type_doc"] == "avoir"
        assert row["sage_type"] == 7
        assert row["montant_tva"] == -180
        assert row["additional_cent_tax"] == -9
    finally:
        _purge()


def test_lien_manuel_avoir_parent():
    sqlite_db.init_database()
    _purge()
    numero = PREFIX + "AV1"
    try:
        sqlite_db.set_sfec_invoice_link(numero, "P1")
        assert sqlite_db.get_sfec_invoice_link(numero) == "P1"
        assert sqlite_db.fetch_sfec_invoice_links()[numero] == "P1"
    finally:
        _purge()


# ── Type 8 : retour de vente ──────────────────────────────────────────────────


def test_credit_label_retour_pour_type8():
    assert engine._credit_label({"type_doc": "avoir", "sage_type": 8}) == "Retour"
    assert engine._credit_label({"type_doc": "avoir", "sage_type": 7}) == "Avoir"
    assert engine._credit_label({"type_doc": "vente"}) == "Facture negative"


def test_miroir_retour_type8_ecrit_sage_type():
    sqlite_db.init_database()
    _purge()
    numero = PREFIX + "R001"
    try:
        invoice_id = sync_sage_invoice_to_sqlite({
            "numero": numero,
            "date_facture": "2026-01-15",
            "tiers_code": "C1",
            "type_doc": "avoir",
            "sage_domaine": 0,
            "sage_type": 8,
            "sage_piece": numero,
            "montant_ht": -1000,
            "montant_ttc": -1189,
            "montant_restant": -1189,
            "statut": "A COMPTABILISER",
            "valide": 1,
        })
        with sqlite_db.get_cursor() as cur:
            row = cur.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        assert row["type_doc"] == "avoir"
        assert row["sage_type"] == 8
        assert row["sage_domaine"] == 0
        assert row["sage_piece"] == numero
    finally:
        _purge()


def test_route_lien_manuel(client, monkeypatch):
    from app.web.routes import billing

    sqlite_db.init_database()
    _purge()
    parent = PREFIX + "P-ROUTE"
    child = PREFIX + "AV-ROUTE"
    try:
        sync_sage_invoice_to_sqlite({
            "numero": parent,
            "date_facture": "2026-01-15",
            "tiers_code": "C1",
            "type_doc": "vente",
            "sage_type": 6,
            "montant_ht": 1000,
            "montant_ttc": 1189,
            "montant_restant": 1189,
            "statut": "A COMPTABILISER",
            "valide": 1,
        })
        monkeypatch.setattr(billing, "reset_sfec_monitor", lambda _piece: 0)
        csrf = client.get("/api/csrf").get_json()["token"]
        resp = client.post(
            "/api/invoices/{}/link-parent".format(child),
            json={"parent_numero": parent},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert sqlite_db.get_sfec_invoice_link(child) == parent
    finally:
        _purge()
