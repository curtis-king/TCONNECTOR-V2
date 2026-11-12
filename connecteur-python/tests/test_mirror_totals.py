"""Proposition 07 : ventilation fiscale du miroir SQLite.

Le miroir doit porter la TVA reelle (hors centimes et taxes
additionnelles), le type de piece et les composantes T/R. Les cas
incoherents ou hors modele client restent suspects : aucune ventilation
n'est inventee.
"""
from app.integration.sage import database as sage_database
from app.integration.sage import writer as sage_writer
from app.integration.sage.writer import sync_sage_invoice_to_sqlite
from app.storage import db as sqlite_db
from app.sync import bidirectional


def _line(ht, tva, ttc, taux=18, qte=1, pu=None):
    return {
        "designation": "Article",
        "quantite": qte,
        "prix_unitaire": ht if pu is None else pu,
        "montant_ht": ht,
        "taux_tva": taux,
        "montant_tva": tva,
        "montant_ttc": ttc,
    }


def _invoice(lignes, **kw):
    inv = {
        "numero": "MIR-TEST",
        "type_doc": "vente",
        "montant_ht": sum(l["montant_ht"] for l in lignes),
        "montant_ttc": sum(l["montant_ttc"] for l in lignes),
        "lignes": lignes,
    }
    inv.update(kw)
    return inv


def test_mirror_18_pourcent():
    inv = _invoice([
        _line(10000, 1800, 11800),
    ], montant_ttc=11890)  # centime ajoute au niveau facture

    m = bidirectional._mirror_fields(inv)

    assert m["montant_tva"] == 1800
    assert m["additional_cent_tax"] == 90
    assert m["total_tax_t_amount"] == 1800
    assert m["total_tax_r_amount"] == 0
    assert m["type_doc"] == "vente"
    assert m["_reliable"] is True


def test_mirror_18_et_5_pourcent():
    inv = _invoice([
        _line(10000, 1800, 11800),
        _line(10000, 500, 10500, taux=5),
    ], montant_ttc=22415)

    m = bidirectional._mirror_fields(inv)

    assert m["total_tax_t_amount"] == 1800
    assert m["total_tax_r_amount"] == 500
    assert m["montant_tva"] == 2300
    assert m["additional_cent_tax"] == 115
    assert m["_reliable"] is True


def test_mirror_avoir_type_7():
    inv = _invoice([
        _line(1000, 180, 1180),
    ], numero="MIR-AVOIR", type_doc="avoir", sage_type=7,
       montant_ttc=1189)

    m = bidirectional._mirror_fields(inv)

    assert m["type_doc"] == "avoir"
    assert m["sage_type"] == 7
    assert m["montant_tva"] == 180
    assert m["additional_cent_tax"] == 9


def test_mirror_facture_negative_reste_negative():
    inv = _invoice([
        _line(-1000, -180, -1180),
    ], numero="MIR-NEG", type_doc="vente", sage_type=6,
       montant_ttc=-1189)

    m = bidirectional._mirror_fields(inv)

    assert m["type_doc"] == "avoir"
    assert m["montant_tva"] == -180
    assert m["additional_cent_tax"] == -9


def test_mirror_taxe_3_pourcent_en_ligne_n_est_pas_tva_reduite():
    # Une ligne a 3 % est une taxe additionnelle : elle ne doit pas alimenter
    # total_tax_r ni les centimes. Le miroir ne stocke pas encore cette taxe,
    # donc la ligne reste suspecte.
    m = bidirectional._mirror_fields({
        "numero": "MIR-3-LIGNE",
        "montant_ht": 10000,
        "montant_ttc": 12190,
        "lignes": [
            _line(10000, 1800, 11800, taux=18),
            _line(0, 300, 300, taux=3, pu=0),
        ],
    })

    assert m["montant_tva"] == 1800
    assert m["total_tax_r_amount"] == 0
    assert m["additional_cent_tax"] == 90
    assert m["_reliable"] is False
    assert "3" in m["_suspect_reason"]


def test_mirror_taxe_025_pourcent_n_est_pas_absorbee():
    m = bidirectional._mirror_fields({
        "numero": "MIR-025-LIGNE",
        "montant_ht": 10000,
        "montant_ttc": 11915,
        "lignes": [
            _line(10000, 1800, 11800, taux=18),
            _line(0, 25, 25, taux=0.25, pu=0),
        ],
    })

    assert m["montant_tva"] == 1800
    assert m["total_tax_r_amount"] == 0
    assert m["additional_cent_tax"] == 90
    assert m["_reliable"] is False
    assert "0.25" in m["_suspect_reason"]


def test_mirror_lignes_contraires_au_ratio_ne_sont_pas_ecrasees():
    # En-tete a 1,189 mais lignes a 20 % : on ne remplace pas la TVA des
    # lignes par 18 %. La ligne reste suspecte et aucune ventilation n'est
    # ecrite.
    m = bidirectional._mirror_fields({
        "numero": "MIR-CONTRADICTION",
        "montant_ht": 10000,
        "montant_ttc": 11890,
        "lignes": [_line(10000, 2000, 12000, taux=20)],
    })

    assert m["montant_tva"] == 1890
    assert m["total_tax_t_amount"] == 0
    assert m["additional_cent_tax"] == 0
    assert m["_reliable"] is False
    assert "incompatibles" in m["_suspect_reason"]


def test_mirror_sans_lignes_ratio_connu():
    m = bidirectional._mirror_fields({
        "numero": "MIR-NOLINE",
        "montant_ht": 10000,
        "montant_ttc": 11890,
        "lignes": [],
    })

    assert m["montant_tva"] == 1800
    assert m["additional_cent_tax"] == 90
    assert m["_reliable"] is True


def test_mirror_sans_lignes_20_pourcent_sans_centime():
    m = bidirectional._mirror_fields({
        "numero": "MIR-20-NOLINE",
        "montant_ht": 10000,
        "montant_ttc": 12000,
        "lignes": [],
    })

    assert m["montant_tva"] == 2000
    assert m["total_tax_t_amount"] == 2000
    assert m["additional_cent_tax"] == 0
    assert m["_reliable"] is False
    assert "centime" in m["_suspect_reason"]


def test_mirror_sans_lignes_taxe_3_pourcent_signalee():
    m = bidirectional._mirror_fields({
        "numero": "MIR-3PCT",
        "montant_ht": 10000,
        "montant_ttc": 12190,
        "lignes": [],
    })

    assert m["montant_tva"] == 1800
    assert m["additional_cent_tax"] == 90
    assert "300" in m["_suspect_reason"]
    assert m["_reliable"] is False


def test_mirror_sans_lignes_exonere():
    m = bidirectional._mirror_fields({
        "numero": "MIR-EXO",
        "montant_ht": 10000,
        "montant_ttc": 10000,
        "lignes": [],
    })

    assert m["montant_tva"] == 0
    assert m["additional_cent_tax"] == 0
    assert m["total_exempt_amount"] == 10000
    assert m["_reliable"] is True


def test_mirror_20_pourcent_sans_centime_ne_devine_pas():
    # Cas BIJOU : lignes a 20 %, TTC = HT x 1,20, sans centime.
    m = bidirectional._mirror_fields({
        "numero": "MIR-20",
        "montant_ht": 10000,
        "montant_ttc": 12000,
        "lignes": [_line(10000, 2000, 12000, taux=20)],
    })

    assert m["montant_tva"] == 2000
    assert m["total_tax_t_amount"] == 2000
    assert m["additional_cent_tax"] == 0
    assert m["_reliable"] is False
    assert "centime" in m["_suspect_reason"]


def test_mirror_remise_globale_incoherente_reste_suspecte():
    # Cas FA00005 : les lignes ne recoupent pas l'en-tete (escompte global).
    m = bidirectional._mirror_fields({
        "numero": "MIR-ESCOMPTE",
        "montant_ht": 1760.88,
        "montant_ttc": 2067.66,
        "lignes": [_line(1760.88, 352.18, 2113.06, taux=20)],
    })

    assert m["montant_tva"] == 306.78
    assert m["additional_cent_tax"] == 0
    assert m["_reliable"] is False
    assert "incoherentes" in m["_suspect_reason"]


def test_update_from_sage_ecrit_la_ventilation(monkeypatch):
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            calls.append((sql, params))

        def fetchone(self):
            return {"sfec_statut": "", "sfec_num_certif": ""}

    monkeypatch.setattr(bidirectional, "get_cursor", lambda: FakeCursor())
    inv = _invoice([
        _line(10000, 1800, 11800),
    ], numero="MIR-UPDATE", montant_ttc=11890)

    bidirectional._update_from_sage(42, inv)

    update_sql, update_params = calls[-1]
    assert "montant_tva" in update_sql
    assert "additional_cent_tax" in update_sql
    assert update_params[1] == 1800
    assert update_params[10] == 90


def test_sync_sage_invoice_ecrit_les_colonnes_fiscales():
    sqlite_db.init_database()
    numero = "TEST-MIR-07-001"
    with sqlite_db.get_cursor() as cur:
        cur.execute("DELETE FROM invoices WHERE numero = ?", (numero,))
    try:
        invoice_id = sync_sage_invoice_to_sqlite({
            "numero": numero,
            "date_facture": "2026-10-22",
            "tiers_code": "C1",
            "type_doc": "vente",
            "sage_type": 6,
            "montant_ht": 10000,
            "montant_ttc": 11890,
            "montant_restant": 11890,
            "statut": "A COMPTABILISER",
            "valide": 1,
            "lignes": [_line(10000, 1800, 11800)],
        })
        with sqlite_db.get_cursor() as cur:
            row = cur.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        assert row["montant_tva"] == 1800
        assert row["additional_cent_tax"] == 90
        assert row["total_tax_t_amount"] == 1800
        assert row["total_tax_r_amount"] == 0
        assert row["montant_ht_brut"] == 10000
    finally:
        with sqlite_db.get_cursor() as cur:
            cur.execute("DELETE FROM invoices WHERE numero = ?", (numero,))


def test_read_invoices_from_sage_charge_les_lignes_sur_le_meme_curseur(monkeypatch):
    header_cols = [
        "id", "numero", "sage_domaine", "sage_type", "sage_piece",
        "type_doc", "reference_invoice_id", "date_facture", "tiers_code",
        "tiers_nom", "tiers_niu", "tiers_adresse", "tiers_telephone",
        "tiers_email", "tiers_ville", "tiers_pays", "reference",
        "montant_ht", "montant_ttc", "montant_restant", "statut_code",
        "valide", "statut", "sfec_statut", "sfec_num_certif",
        "sfec_signature", "sfec_qr_code",
    ]
    header_row = (
        "0-6-MIR-READ", "MIR-READ", "0", "6", "MIR-READ", "vente", "",
        "2026-10-22", "C1", "Client", "", "", "", "", "", "", "",
        "10000", "11890", "11890", "2", "1", "A COMPTABILISER",
        "", "", "", "",
    )
    fake_cursor = None

    class _Desc:
        def __init__(self, name):
            self.name = name

        def __getitem__(self, index):
            return self.name

    class _FakeCursor:
        def __init__(self):
            self.description = []
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            compact = " ".join(sql.split())
            if "INFORMATION_SCHEMA.COLUMNS" in compact:
                self.description = [_Desc("COLUMN_NAME")]
                self.rows = []
            elif "FROM F_DOCENTETE d" in compact:
                self.description = [_Desc(c) for c in header_cols]
                self.rows = [header_row]
            else:
                raise AssertionError("requete inattendue: " + compact)

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    calls = []

    def fake_batch(domaine, keys, chunk_size=200, cursor=None):
        calls.append((domaine, keys, cursor))
        return {(6, "MIR-READ"): [_line(10000, 1800, 11800)]}

    fake_cursor = _FakeCursor()
    monkeypatch.setattr(sage_writer, "_get_sage_cursor", lambda: fake_cursor)
    monkeypatch.setattr(sage_database, "fetch_doc_lines_batch", fake_batch)

    invoices = sage_writer.read_invoices_from_sage()

    assert len(invoices) == 1
    assert invoices[0]["numero"] == "MIR-READ"
    assert invoices[0]["lignes"][0]["montant_ht"] == 10000
    assert calls and calls[0][0] == 0
    assert calls[0][1] == [(6, "MIR-READ")]
    assert calls[0][2] is fake_cursor


def test_backfill_3_pourcent_signale_sans_absorption(monkeypatch):
    from scripts.backfill_mirror_totals import compute_backfill_values

    values, motif = compute_backfill_values({
        "numero": "BACK-3PCT", "montant_ht": 10000,
        "montant_ttc": 12190, "type_doc": "vente", "sage_type": 6,
    })

    assert values["montant_tva"] == 1800
    assert values["additional_cent_tax"] == 90
    assert values["total_tax_t_amount"] == 1800
    assert "300" in motif


def test_backfill_ratio_inconnu_ne_corrige_pas():
    from scripts.backfill_mirror_totals import compute_backfill_values

    values, motif = compute_backfill_values({
        "numero": "BACK-UNKNOWN", "montant_ht": 10000,
        "montant_ttc": 11800, "type_doc": "vente", "sage_type": 6,
    })

    assert values is None
    assert "ratio inconnu" in motif


def test_backfill_5_pourcent_alimente_tax_r():
    from scripts.backfill_mirror_totals import compute_backfill_values

    values, motif = compute_backfill_values({
        "numero": "BACK-5", "montant_ht": 10000,
        "montant_ttc": 10525, "type_doc": "vente", "sage_type": 6,
    })

    assert values["montant_tva"] == 500
    assert values["additional_cent_tax"] == 25
    assert values["total_tax_t_amount"] == 0
    assert values["total_tax_r_amount"] == 500
    assert motif == ""
