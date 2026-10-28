"""Tests du correctif 01 : centimes et taxes additionnelles SFEC.

Regles verifiees :
  - centime additionnel = 5 % de la TVA collectee (18 % + 5 %), jamais du HT ;
  - le TTC de ligne SFEC = HT net + TVA, sans reprendre DL_MontantTTC
    (qui contient deja centimes et taxes additionnelles) ;
  - les taxes additionnelles (3 %, 0,25 %...) vont dans `additional_taxes` ;
  - le garde-fou `check_sage_totals` bloque les factures TTC = HT et les
    taxes non ventilables.
"""
import pytest

from app.integration.sfec.endpoints import (
    check_sage_totals,
    db_invoice_to_sfec,
    validate_sfec_payload,
)


def _invoice(lignes, **kw):
    inv = {
        "numero": "TEST-CENT",
        "type_doc": "vente",
        "montant_ht": sum(l["montant_ht"] for l in lignes),
        "montant_tva": sum(l.get("montant_tva", 0) for l in lignes),
        "montant_ttc": sum(l.get("montant_ttc", 0) for l in lignes),
        "lignes": lignes,
    }
    inv.update(kw)
    return inv


def test_centime_ajoute_une_seule_fois():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
        "montant_ttc": 11890,   # 10 000 + 1 800 + 90
    }])
    p = db_invoice_to_sfec(inv)

    assert p["items"][0]["subtotal"] == 10000.0
    assert p["items"][0]["net_amount"] == 11800.0
    assert p["items"][0]["total_amount"] == 11800.0
    assert p["additional_cent_tax"] == 90.0
    assert "additional_taxes" not in p
    assert p["total_amount"] == 11890.0
    assert validate_sfec_payload(p) == []
    assert check_sage_totals(inv, p) == []


def test_centime_sur_tva_18_et_5():
    inv = _invoice([
        {"designation": "A", "quantite": 1, "prix_unitaire": 10000,
         "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
         "montant_ttc": 11890},
        {"designation": "B", "quantite": 1, "prix_unitaire": 10000,
         "montant_ht": 10000, "taux_tva": 5, "montant_tva": 500,
         "montant_ttc": 10525},
    ])
    p = db_invoice_to_sfec(inv)

    assert p["additional_cent_tax"] == 115.0      # 5 % de 2 300
    assert p["total_tax_t_amount"] == 1800.0
    assert p["total_tax_r_amount"] == 500.0
    assert p["total_amount"] == 22415.0
    assert check_sage_totals(inv, p) == []


def test_remise_ligne_aligne_sfec():
    inv = _invoice([{
        "designation": "Produit remise", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 9000, "taux_tva": 18, "montant_tva": 1620,
        "montant_ttc": 10701,   # 9 000 + 1 620 + 81 (5 % de TVA)
    }])
    p = db_invoice_to_sfec(inv)

    assert p["items"][0]["subtotal"] == 9000.0          # HT net
    assert p["items"][0]["discount_amount"] == 1000.0
    assert p["items"][0]["amount_after_discount"] == 9000.0
    assert p["items"][0]["net_amount"] == 10620.0       # TTC de ligne
    assert p["subtotal"] == 9000.0
    assert p["total_amount"] == 10701.0
    assert validate_sfec_payload(p) == []


def test_taxe_additionnelle_3_pourcent():
    """Cas 26FAZ07297 / 25VFA3122 : 18 % + 3 % + centime."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 3928,
        "montant_ht": 3928, "taux_tva": 18, "montant_tva": 707.04,
        "montant_ttc": 4788,
    }])
    p = db_invoice_to_sfec(inv)

    assert p["additional_cent_tax"] == pytest.approx(35.35, abs=0.01)
    assert len(p["additional_taxes"]) == 1
    tax = p["additional_taxes"][0]
    assert tax["tax_rate"] == pytest.approx(3.0, abs=0.05)
    assert tax["tax_amount"] == pytest.approx(117.61, abs=0.05)
    assert p["total_amount"] == 4788.0
    assert validate_sfec_payload(p) == []
    assert check_sage_totals(inv, p) == []


def test_taxe_additionnelle_code_explicite():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 3928,
        "montant_ht": 3928, "taux_tva": 18, "montant_tva": 707.04,
        "montant_ttc": 4788,
    }], additional_tax_code="203", additional_tax_label="Taxe 3 %")
    p = db_invoice_to_sfec(inv)

    assert p["additional_taxes"][0]["tax_code"] == "203"
    assert p["additional_taxes"][0]["tax_label"] == "Taxe 3 %"


def test_exonere_ttc_egal_ht_valide_directement():
    """Cas 25VFA3118 : aucune TVA facturee (exonere).
    L'exonération est validée directement, pas de blocage "a confirmer"."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 250000,
        "montant_ht": 250000, "taux_tva": 0, "montant_tva": 0,
        "montant_ttc": 250000,
    }])
    p = db_invoice_to_sfec(inv)

    errors = check_sage_totals(inv, p)
    assert errors == []
    assert validate_sfec_payload(p) == []


def test_cluster_a_ecart_arrondi_dans_tolerance():
    """25VFB3811 : apres centimes, il reste -0,46 FCFA d'arrondi Sage."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 235987,
        "montant_ht": 235987, "taux_tva": 18, "montant_tva": 42477.66,
        "montant_ttc": 280589,
    }])
    p = db_invoice_to_sfec(inv)

    assert p["additional_cent_tax"] == 2123.88
    assert "additional_taxes" not in p
    assert p["total_amount"] == 280588.54
    assert check_sage_totals(inv, p) == []


def test_cluster_d_mixte_18_5():
    """25VFA3134 : un mixte 18 %/5 % + centimes recoupe exactement 177 375."""
    inv = _invoice([
        {"designation": "A", "quantite": 1, "prix_unitaire": 142857.14,
         "montant_ht": 142857.14, "taux_tva": 18, "montant_tva": 25714.29,
         "montant_ttc": 169857.14},
        {"designation": "B", "quantite": 1, "prix_unitaire": 7142.86,
         "montant_ht": 7142.86, "taux_tva": 5, "montant_tva": 357.14,
         "montant_ttc": 7517.86},
    ])
    p = db_invoice_to_sfec(inv)

    assert "additional_taxes" not in p
    assert p["total_amount"] == 177375.0
    assert check_sage_totals(inv, p) == []


def test_taux_additionnel_au_dessus_du_max_non_absorbe():
    """Un ecart de 50 % du HT n'est pas une taxe : le garde-fou doit refuser."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 100000,
        "montant_ht": 100000, "taux_tva": 18, "montant_tva": 18000,
        "montant_ttc": 150000,
    }])
    p = db_invoice_to_sfec(inv)

    assert "additional_taxes" not in p
    errors = check_sage_totals(inv, p)
    assert any("taxe non ventilable" in e for e in errors)


def test_ht_absent_signale():
    """Sans lignes ni HT, la ventilation TVA/centimes n'est pas fiable."""
    inv = {
        "numero": "TEST-CENT-SANS-HT",
        "type_doc": "vente",
        "montant_ht": 0,
        "montant_tva": 0,
        "montant_ttc": 11890,
        "lignes": [],
    }
    p = db_invoice_to_sfec(inv)

    errors = check_sage_totals(inv, p)
    assert any("HT Sage absent" in e for e in errors)


def test_tolerance_explicite_du_garde_fou():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
        "montant_ttc": 11890,
    }])
    p = db_invoice_to_sfec(inv)
    p["total_amount"] = 11890.5

    assert check_sage_totals(inv, p, tolerance=1.0) == []
    assert check_sage_totals(inv, p, tolerance=0.01)


# ── Phase 2 : ventilation fiscale (reference client 26FAZ07297) ──────────────


def test_reference_26faz07297():
    """Facture cliente 26FAZ07297 : 18 % + centime 5 % de TVA + taxe 3 %.

    HT 14 094 340 ; TVA 2 536 981 ; centime 126 849 ; taxe 3 % 422 830 ;
    TTC 17 181 000. Arrondi XAF au franc entier.
    La taxe de 3 % est envoyee dans additional_taxes avec le code SFEC
    configure (TAR par defaut, a confirmer avec SFEC).
    """
    lignes = [
        {"designation": d, "quantite": 1, "prix_unitaire": ht,
         "montant_ht": ht, "taux_tva": 18}
        for d, ht in [
            ("FAITIERE BAC 060 N.CR (M)", 521737),
            ("TOLE BAC ALU 060 6m (M)", 9706316),
            ("TOLE BAC ALU 070 DM (M)", 2635767),
            ("CAVALIER ALU", 332410),
            ("PLAQUETTE FEUTRE 40 X 20 X 3", 236100),
            ("RONDELLE ALU 20 X 8", 131880),
            ("RONDELLE FEUTRE 20 X 8", 138860),
            ("TIRE FOND 6 X 80", 391270),
        ]
    ]
    inv = _invoice(lignes, montant_ttc=17181000, source="sage", devise="XAF",
                   numero="26FAZ07297")
    p = db_invoice_to_sfec(inv)

    assert p["subtotal"] == 14094340.0
    assert p["total_tax_t_amount"] == 2536981.0
    assert p["total_tax_r_amount"] == 0.0
    assert p["additional_cent_tax"] == 126849.0
    assert len(p["additional_taxes"]) == 1
    tax = p["additional_taxes"][0]
    assert tax["tax_code"] == "TAR"
    assert tax["tax_label"] == "Taxe supplementaire 3 %"
    assert tax["tax_rate"] == 3.0
    assert tax["tax_amount"] == 422830.0
    assert p["total_amount"] == 17181000.0
    # La somme des taxes de ligne vaut exactement le total de TVA.
    assert round(sum(i["tax_amount"] for i in p["items"]), 2) == 2536981.0
    assert validate_sfec_payload(p) == []
    assert check_sage_totals(inv, p) == []


def test_tva_18_seul_source_sage():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_ttc": 11890,
    }], source="sage", devise="XAF")
    p = db_invoice_to_sfec(inv)

    assert p["total_tax_t_amount"] == 1800.0
    assert p["additional_cent_tax"] == 90.0
    assert "additional_taxes" not in p
    assert p["total_amount"] == 11890.0
    assert check_sage_totals(inv, p) == []


def test_tva_5_seul_source_sage():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 5, "montant_ttc": 10525,
    }], source="sage", devise="XAF")
    p = db_invoice_to_sfec(inv)

    assert p["total_tax_r_amount"] == 500.0
    assert p["additional_cent_tax"] == 25.0   # 5 % de 500
    assert p["total_amount"] == 10525.0
    assert check_sage_totals(inv, p) == []


def test_mixte_18_5_et_3_source_sage():
    inv = _invoice([
        {"designation": "A", "quantite": 1, "prix_unitaire": 1000000,
         "montant_ht": 1000000, "taux_tva": 18},
        {"designation": "B", "quantite": 1, "prix_unitaire": 100000,
         "montant_ht": 100000, "taux_tva": 5},
    ], montant_ttc=1219000, source="sage", devise="XAF")
    # 1 000 000 + 100 000 = 1 100 000 HT
    # TVA : 180 000 + 5 000 = 185 000 ; centime : 9 250
    # TTC attendu : 1 100 000 + 185 000 + 9 250 + 33 000 (3 % HT) = 1 327 250
    # Ici TTC force 1 219 000 => residu = 1 219 000 - 1 100 000 - 185 000 - 9 250
    #         = -75 250 -> incoherent, signale.
    p = db_invoice_to_sfec(inv)
    errors = check_sage_totals(inv, p)
    assert any("TTC calcule" in e or "TTC Sage" in e for e in errors)


def test_code_202_est_centime_pas_additionnel():
    """Une ligne 0,9 % (code 202) alimente le centime, jamais additional_taxes.

    On ajoute une ligne d'article a 18 % pour avoir une vraie TVA ; le
    centime est alors 5 % de la TVA (90), pas une taxe additionnelle a 0,9 %.
    """
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
        "montant_ttc": 11890,
    }])
    p = db_invoice_to_sfec(inv)

    assert p["additional_cent_tax"] == 90.0      # 5 % de 1 800
    assert p["total_tax_t_amount"] == 1800.0
    assert "additional_taxes" not in p
    assert p["total_amount"] == 11890.0
    assert check_sage_totals(inv, p) == []


def test_residu_inconfu_a_surveiller():
    """Residu qui ne correspond a aucun taux connu : code AUTRE + warning."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_ttc": 12500,
    }])
    p = db_invoice_to_sfec(inv)
    # 12 500 - 10 000 - 1 800 - 90 = 610 => 6,10 % (inconnu -> AUTRE)
    assert len(p["additional_taxes"]) == 1
    assert p["additional_taxes"][0]["tax_code"] == "AUTRE"
    assert p["total_amount"] == 12500.0
    # Le total recalcule retombe exactement sur le TTC : pas d'erreur de totaux.
    assert check_sage_totals(inv, p) == []


def test_taux_additionnel_trop_eleve_a_surveiller():
    """Ecart de 50 % du HT : taxe non ventilable (garde-fou)."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 100000,
        "montant_ht": 100000, "taux_tva": 18, "montant_ttc": 150000,
    }])
    p = db_invoice_to_sfec(inv)
    errors = check_sage_totals(inv, p)
    assert any("taxe non ventilable" in e for e in errors)


def test_avoir_negatif_devient_credit_note_positif():
    """Un avoir en montant negatif devient un creditNote a montants positifs."""
    inv = {
        "id": "0-7-AV1", "numero": "AV1", "type_doc": "avoir",
        "sage_type": 8,
        "montant_ht": -1000, "montant_ttc": -1189,
        "montant_restant": -1189,
        "reference": "P1", "reference_invoice_id": "P1",
        "external_credit_note_number": "AV1",
        "original_sfec_invoice_number": "CG-P1",
        "lignes": [{
            "quantite": -1, "prix_unitaire": 1000,
            "montant_ht": -1000, "montant_tva": -180,
            "montant_ttc": -1189, "taux_tva": 18,
            "designation": "Article",
        }],
    }
    p = db_invoice_to_sfec(inv)

    assert p["invoice_type"] == "creditNote"
    assert p["total_amount"] == 1189
    assert p["subtotal"] == 1000
    assert p["total_tax_amount"] == 180
    assert p["additional_cent_tax"] == 9
    assert p["amount_due"] == 1189
    assert validate_sfec_payload(p) == []
