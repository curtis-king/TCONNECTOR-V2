"""Tests print_hijack (watcher PDF -> facture conforme -> impression).

Mode dry-run uniquement : aucune impression réelle. Vérifie le mapping du
numéro depuis le nom de fichier, le chemin complet (génération PDF conforme,
déplacement IN -> DONE, enregistrement SQLite) et la déduplication.
"""
import os
import shutil
import time

import pytest

from app.print_hijack import get_print_hijack_config
from app.print_hijack import auto_printer
from app.print_hijack.job_mapper import (
    candidates_from_filename,
    candidates_from_text,
)


@pytest.fixture(scope="module", autouse=True)
def _sqlite():
    from app.storage.db import init_database
    init_database()


def test_candidates_from_filename_variants():
    for name, expected in (
        ("FA00010.pdf", "FA00010"),
        ("C:/x/y/FV-0001.pdf", "FV-0001"),
        ("scan_FA00005_final.pdf", "FA00005"),
    ):
        cands = candidates_from_filename(name)
        assert expected in cands, (name, cands)


def test_candidates_from_text():
    cands = candidates_from_text("Facture n FA00007 du 01/01/2026 - Client X")
    assert "FA00007" in cands


def test_config_resolution_defaults():
    cfg = get_print_hijack_config()
    assert set(("watch_dir", "out_dir", "done_dir", "quarantine_dir",
                "printer_name", "dry_run",
                "wait_for_certification", "certif_timeout_min",
                "on_uncertified")).issubset(cfg.keys())


def test_print_pdf_dry_run():
    ok, err = auto_printer.print_pdf("inexistant.pdf", "Imprimante", "", dry_run=True)
    assert ok is True and err == ""


def test_print_pdf_without_printer_is_error():
    ok, err = auto_printer.print_pdf("inexistant.pdf", "", "", dry_run=False)
    assert ok is False
    assert "imprimante" in err.lower()


def test_compute_sha256(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    h = auto_printer.compute_sha256(str(f))
    assert h and len(h) == 64


def _wait_for(predicate, timeout=30.0, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_watcher_end_to_end_dry_run(tmp_path):
    """Dépose un PDF nommé d'après une facture existante -> OUT + DONE + IMPRIME."""
    from app.domain.invoices import get_invoice_by_numero
    from app.print_hijack.folder_watcher import FolderWatcher
    from app.storage.db import get_cursor

    numero = None
    for cand in ("FA00010", "FA00005", "FA00009", "FA000001"):
        if get_invoice_by_numero(cand):
            numero = cand
            break
    if not numero:
        pytest.skip("Aucune facture de référence en base pour le test e2e")

    watch = tmp_path / "IN"
    out = tmp_path / "OUT"
    done = tmp_path / "DONE"
    quarantine = tmp_path / "QUARANTINE"
    cfg = {
        "enabled": True,
        "watch_dir": str(watch), "out_dir": str(out),
        "done_dir": str(done), "quarantine_dir": str(quarantine),
        "printer_name": "Imprimante-Test", "sumatra_path": "",
        "stable_delay_s": 0.5, "stable_retries": 3, "poll_interval_s": 1,
        "dedup_window_min": 0, "max_pages": 50, "max_size_mb": 20,
        "wait_for_certification": True, "certif_timeout_min": 15,
        "on_uncertified": "watermark",
        "dry_run": True,
    }
    for d in (watch, out, done, quarantine):
        d.mkdir(parents=True, exist_ok=True)

    src_pdf = os.path.join(os.path.dirname(__file__), "..", "test_invoice.pdf")
    src_pdf = os.path.abspath(src_pdf)
    assert os.path.exists(src_pdf), src_pdf

    # Isole le run : purge l'éventuelle trace du même fichier (dédup par hash),
    # sinon le watcher le considère déjà imprimé et ne génère rien.
    src_hash = auto_printer.compute_sha256(src_pdf)
    with get_cursor() as cur:
        cur.execute("DELETE FROM processed_pdf_prints WHERE file_hash = ?", (src_hash,))

    watcher = FolderWatcher(cfg)
    watcher.start()
    try:
        shutil.copyfile(src_pdf, str(watch / "{}.pdf".format(numero)))

        def _done():
            if not any(out.glob("*.pdf")):
                return False
            if any(watch.glob("*.pdf")):
                return False
            with get_cursor() as cur:
                cur.execute(
                    "SELECT status FROM processed_pdf_prints WHERE numero = ? "
                    "ORDER BY printed_at DESC LIMIT 1", (numero,))
                row = cur.fetchone()
            return bool(row and row["status"] == "IMPRIME")

        assert _wait_for(_done, timeout=40), "flux watcher non abouti"
        assert any(done.glob("*.pdf")), "original non archive dans DONE"
    finally:
        watcher.stop()


# ── Certification (politique sfec_statut) ────────────────────────────────────
def _watcher_for_cert(tmp_path, **overrides):
    from app.print_hijack.folder_watcher import FolderWatcher

    base = {
        "enabled": True,
        "watch_dir": str(tmp_path / "IN"), "out_dir": str(tmp_path / "OUT"),
        "done_dir": str(tmp_path / "DONE"), "quarantine_dir": str(tmp_path / "Q"),
        "printer_name": "", "sumatra_path": "",
        "stable_delay_s": 0.5, "stable_retries": 1, "poll_interval_s": 1,
        "dedup_window_min": 0, "max_pages": 50, "max_size_mb": 20,
        "wait_for_certification": True, "certif_timeout_min": 15,
        "on_uncertified": "watermark", "dry_run": True,
    }
    base.update(overrides)
    for key in ("watch_dir", "out_dir", "done_dir", "quarantine_dir"):
        os.makedirs(base[key], exist_ok=True)
    return FolderWatcher(base)


def test_certification_decision_matrix(tmp_path):
    w = _watcher_for_cert(tmp_path)

    # Certifiee -> impression immediate (meme si valide=0)
    assert w._certification_decision({"sfec_statut": "CERTIFIE", "valide": 0}, None)[0] == "IMPRIME"
    assert w._certification_decision({"sfec_statut": "DEJA_CERTIFIE", "valide": 0}, None)[0] == "IMPRIME"

    # Non validee -> refus
    assert w._certification_decision({"sfec_statut": "", "valide": 0}, None)[0] == "REFUS"

    # En attente (EN_COURS / vide) mais validee -> ATTENTE_CERTIF
    assert w._certification_decision({"sfec_statut": "", "valide": 1}, None)[0] == "ATTENTE_CERTIF"
    assert w._certification_decision({"sfec_statut": "EN_COURS", "valide": 1}, None)[0] == "ATTENTE_CERTIF"

    # Timeout depasse -> NON CONFORME (avec filigrane)
    rec = {"first_seen_at": "2000-01-01 00:00:00"}
    status, non_conforme, _ = w._certification_decision({"sfec_statut": "", "valide": 1}, rec)
    assert status == "NON_CONFORME" and non_conforme is True


def test_certification_decision_quarantine_policy(tmp_path):
    w = _watcher_for_cert(tmp_path, on_uncertified="quarantine")
    status, non_conforme, _ = w._certification_decision({"sfec_statut": "ERREUR", "valide": 1}, None)
    assert status == "QUARANTINE" and non_conforme is False


def test_record_and_list_print_records(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    h = auto_printer.compute_sha256(str(f))
    auto_printer.record_print(h, "FA00099", str(f), "out.pdf", "Imp-T", "IMPRIME",
                              sfec_statut="CERTIFIE", sfec_num_certif="CERT123")
    rows = [r for r in auto_printer.list_print_records(limit=500) if r["file_hash"] == h]
    assert rows, "ligne non enregistree"
    assert rows[0]["status"] == "IMPRIME"
    assert rows[0]["sfec_statut"] == "CERTIFIE"
    assert rows[0]["sfec_num_certif"] == "CERT123"


def test_generate_invoice_pdf_non_conforme(tmp_path):
    from app.domain.invoices import get_invoice_by_numero
    from app.domain.pdf import generate_invoice_pdf

    inv = get_invoice_by_numero("FA00010") or get_invoice_by_numero("FA000001")
    if not inv:
        pytest.skip("Aucune facture de reference en base")
    out = str(tmp_path / "nc.pdf")
    res = generate_invoice_pdf(inv, out, non_conforme=True)
    assert res and os.path.exists(out) and os.path.getsize(out) > 0


def test_impressions_page_authenticated(client):
    resp = client.get("/print")
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    assert "Impressions" in body
    assert "Etat impression" in body
