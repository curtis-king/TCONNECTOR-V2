"""Mocks Sage en memoire : 200 factures + lignes, 0 ODBC, 0 reseau.

Patche les noms importes directement par app.sync.engine et
app.sync.bidirectional (from ... import ...).
"""
from unittest.mock import patch as _patch

_NB = 200

_patchers = []


def _mk_invoice(i):
    n = "FA-PERF-{:04d}".format(i)
    return {
        "id": "0-6-{}".format(n),
        "numero": n,
        "statut_code": 2,
        "statut": "A COMPTABILISER",
        "sfec_statut": "",
        "type_doc": "vente",
        "reference_invoice_id": "",
        "reference": "",
        "date_facture": "2026-09-{:02d}".format((i % 28) + 1),
        "tiers_code": "CLI-PERF",
        "tiers_nom": "Client Perf",
        "montant_ht": 1000.0,
        "montant_tva": 180.0,
        "montant_ttc": 1180.0,
        "montant_restant": 1180.0,
        "lignes": [],
    }


def _mk_avoir(i, ref):
    n = "AV-PERF-{:04d}".format(i)
    inv = _mk_invoice(i)
    inv.update({"id": "0-7-{}".format(n), "numero": n, "type_doc": "avoir",
                "reference_invoice_id": ref})
    return inv


SALES = [_mk_invoice(i) for i in range(1, _NB + 1)]
SALES[0]["type_doc"] = "avoir"
SALES[0]["numero"] = "AV-PERF-0001"
SALES[0]["id"] = "0-7-AV-PERF-0001"
SALES[0]["reference_invoice_id"] = "FA-PERF-0002"
SALES[1]["sfec_statut"] = "CERTIFIE"


def _mk_lines(piece):
    return [{
        "id": "0-6-{}-{}".format(piece, k),
        "numero_ligne": k,
        "designation": "Article {}".format(k),
        "quantite": 1,
        "prix_unitaire": 100,
        "montant_ht": 100.0,
        "montant_tva": 18.0,
        "montant_ttc": 118.0,
        "taux_tva": 18,
        "code_compte": "",
        "code_article": "ART-PERF",
    } for k in (1, 2, 3)]


def fetch_sales_invoices(*a, **k):
    return [dict(x) for x in SALES]


def fetch_purchase_invoices(*a, **k):
    return []


def fetch_contacts(*a, **k):
    return []


def fetch_tax_rates(*a, **k):
    return [{"id": "C18", "code": "C18", "libelle": "TVA 18%", "taux": 18.0}]


def fetch_ledger_accounts(*a, **k):
    return []


def fetch_certified_invoices(*a, **k):
    return []


def fetch_doc_lines(domaine, type_doc, piece):
    return _mk_lines(piece)


def fetch_doc_lines_batch(domaine, keys, chunk_size=200):
    return {(dt, p): _mk_lines(p) for dt, p in keys if p}


def _noop(*a, **k):
    return None


def write_sfec_to_invoice(*a, **k):
    return {"invoice_id": a[0] if a else "", "ok": True}


_WRITE_CALLS = {"n": 0}


def write_invoice_to_sage(invoice):
    _WRITE_CALLS["n"] += 1
    return {"success": True, "numero": invoice.get("numero", "")}


def read_invoices_from_sage():
    return []


def sync_sage_invoice_to_sqlite(sage_inv):
    return None


def write_contact_to_sage(contact):
    return {"success": True, "ct_num": contact.get("code", "")}


def start():
    import app.sync.engine as engine
    import app.sync.bidirectional as bidir
    targets = [
        (engine, "fetch_sales_invoices", fetch_sales_invoices),
        (engine, "fetch_purchase_invoices", fetch_purchase_invoices),
        (engine, "fetch_contacts", fetch_contacts),
        (engine, "fetch_tax_rates", fetch_tax_rates),
        (engine, "fetch_ledger_accounts", fetch_ledger_accounts),
        (engine, "fetch_certified_invoices", fetch_certified_invoices),
        (engine, "mark_certifying", _noop),
        (engine, "mark_certified", _noop),
        (engine, "mark_certification_failed", _noop),
        (engine, "mark_to_monitor", _noop),
        (engine, "write_sfec_to_invoice", write_sfec_to_invoice),
        (engine, "reset_stuck_en_cours", _noop),
        (engine, "close_pool", _noop),
        (bidir, "write_invoice_to_sage", write_invoice_to_sage),
        (bidir, "read_invoices_from_sage", read_invoices_from_sage),
        (bidir, "sync_sage_invoice_to_sqlite", sync_sage_invoice_to_sqlite),
        (bidir, "write_contact_to_sage", write_contact_to_sage),
    ]
    for module, name, func in targets:
        p = _patch.object(module, name, func)
        p.start()
        _patchers.append(p)
    _WRITE_CALLS["n"] = 0


def stop():
    while _patchers:
        _patchers.pop().stop()


def write_calls():
    return _WRITE_CALLS["n"]
