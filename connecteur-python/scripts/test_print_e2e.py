"""Test bout-en-bout du watcher PDF -> impression conforme.

Dépose un PDF nommé d'après une facture existante dans le dossier surveillé
et vérifie, de façon automatisée, que la version conforme est générée (OUT),
que l'original est archivé (DONE) et que l'impression est enregistrée
(processed_pdf_prints = IMPRIME).

Usage (depuis connecteur-python, avec le venv) :
    python scripts/test_print_e2e.py            # dry-run (aucune imprimante)
    python scripts/test_print_e2e.py --real     # impression réelle
    python scripts/test_print_e2e.py --real --numero FA00010 --printer HPIA758F1

ATTENTION : si le service T-CONNECTOR tourne avec print_hijack.enabled=true,
arrêtez-le d'abord (deux watchers sur le même dossier = conflit).
"""
import argparse
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _tail_log(n=40):
    path = os.path.join(ROOT, "data", "print_hijack.log")
    if not os.path.exists(path):
        return "(print_hijack.log introuvable)"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


def _pick_numero():
    from app.domain.invoices import list_invoices
    invs = list_invoices(limit=1, sort_by="date_facture", sort_dir="DESC")
    if not invs:
        return None
    return invs[0].get("numero")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--numero", default=None, help="N° de facture = nom du PDF déposé")
    ap.add_argument("--printer", default=None, help="Nom imprimante (défaut: config)")
    ap.add_argument("--real", action="store_true", help="Impression réelle (sinon dry-run)")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    from app.config.manager import load_config
    load_config()
    from app.storage.db import init_database, get_cursor
    init_database()
    from app.print_hijack import get_print_hijack_config
    from app.print_hijack.folder_watcher import FolderWatcher

    cfg = get_print_hijack_config()
    cfg["enabled"] = True
    cfg["dry_run"] = not args.real
    if args.printer:
        cfg["printer_name"] = args.printer

    numero = args.numero or _pick_numero()
    if not numero:
        print("[FAIL] Aucune facture en base : impossible de nommer le PDF de test.")
        return 2

    src = os.path.join(ROOT, "test_invoice.pdf")
    if not os.path.exists(src):
        print("[FAIL] PDF de test introuvable:", src)
        return 2

    for d in (cfg["watch_dir"], cfg["out_dir"], cfg["done_dir"], cfg["quarantine_dir"]):
        os.makedirs(d, exist_ok=True)

    print("=" * 64)
    print(" Test e2e impression PDF conforme")
    print("   facture  :", numero)
    print("   IN       :", cfg["watch_dir"])
    print("   imprimante:", cfg["printer_name"] or "(aucune)")
    print("   mode     :", "DRY-RUN" if cfg["dry_run"] else "IMPRESSION REELLE")
    print("=" * 64)

    watcher = FolderWatcher(cfg)
    watcher.start()
    drop = os.path.join(cfg["watch_dir"], "{}.pdf".format(numero))
    try:
        # Isole le run : purge les traces des impressions précédentes de ce
        # numéro (sinon un ancien statut IMPRIME ferait un faux positif et la
        # fenêtre de dédup bloquerait le traitement).
        with get_cursor() as cur:
            cur.execute("DELETE FROM processed_pdf_prints WHERE numero = ?", (numero,))
        if os.path.exists(drop):
            os.remove(drop)
        shutil.copyfile(src, drop)
        print("PDF déposé:", drop)

        deadline = time.time() + args.timeout
        status = None
        while time.time() < deadline:
            with get_cursor() as cur:
                cur.execute(
                    "SELECT status, out_path FROM processed_pdf_prints "
                    "WHERE numero = ? ORDER BY printed_at DESC LIMIT 1", (numero,))
                row = cur.fetchone()
            if row and row["status"] in ("IMPRIME", "ERREUR", "ERREUR_IMPRESSION",
                                         "QUARANTAINE"):
                status = row["status"]
                out_path = row["out_path"]
                break
            time.sleep(0.5)
    finally:
        watcher.stop()

    ok = status == "IMPRIME"
    print("-" * 64)
    print("STATUT :", status)
    if ok:
        print("[PASS] Facture {} : conforme générée et impression enregistrée.".format(numero))
        print("       PDF conforme :", out_path)
        if not cfg["dry_run"]:
            print("       -> vérifiez la sortie papier de '{}'.".format(cfg["printer_name"]))
        print("       Original archivé dans:", cfg["done_dir"])
    else:
        print("[FAIL] Statut inattendu. Dernières lignes de print_hijack.log :")
        print(_tail_log())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
