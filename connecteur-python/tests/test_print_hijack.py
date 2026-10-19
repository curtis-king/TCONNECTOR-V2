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
                "printer_name", "dry_run")).issubset(cfg.keys())


def test_print_pdf_dry_run():
    ok, err = auto_printer.print_pdf("inexistant.pdf", "Imprimante", "", dry_run=True)
    assert ok is True and err == ""


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
        "dry_run": True,
    }
    for d in (watch, out, done, quarantine):
        d.mkdir(parents=True, exist_ok=True)

    src_pdf = os.path.join(os.path.dirname(__file__), "..", "test_invoice.pdf")
    src_pdf = os.path.abspath(src_pdf)
    assert os.path.exists(src_pdf), src_pdf

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
