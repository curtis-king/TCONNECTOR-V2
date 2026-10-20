"""Surveillance du dossier PDF et traitement des fichiers déposés."""
import logging
import os
import queue
import shutil
import threading
import time
from datetime import datetime

from app.print_hijack import auto_printer, job_mapper
from app.domain.pdf import generate_invoice_pdf

logger = logging.getLogger("t-connector.print_hijack.watcher")


def _age_minutes(first_seen):
    if not first_seen:
        return 0.0
    try:
        dt = datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 0.0
    return max((datetime.utcnow() - dt).total_seconds() / 60.0, 0.0)


def _count_pages(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:  # noqa: BLE001
        return None


class FolderWatcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.watch_dir = cfg["watch_dir"]
        self.out_dir = cfg["out_dir"]
        self.done_dir = cfg["done_dir"]
        self.quarantine_dir = cfg["quarantine_dir"]
        self._queue = queue.Queue()
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._worker_thread = None
        self._scan_thread = None
        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in (self.watch_dir, self.out_dir, self.done_dir, self.quarantine_dir):
            os.makedirs(d, exist_ok=True)

    # -- cycle de vie -----------------------------------------------------
    def start(self):
        self._scan_existing()
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory:
                        watcher.notify(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        watcher.notify(event.dest_path)

            self._observer = Observer()
            self._observer.schedule(_Handler(), self.watch_dir, recursive=False)
            self._observer.daemon = True
            self._observer.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("Observer watchdog indisponible (%s) - scan periodique seul", e)

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="print-hijack-worker", daemon=True)
        self._worker_thread.start()

        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="print-hijack-scan", daemon=True)
        self._scan_thread.start()

    def stop(self):
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._queue.put(None)

    def is_alive(self):
        return bool(self._worker_thread and self._worker_thread.is_alive())

    # -- détection --------------------------------------------------------
    def _scan_existing(self):
        try:
            names = os.listdir(self.watch_dir)
        except OSError:
            return
        for name in names:
            if name.lower().endswith(".pdf"):
                self.notify(os.path.join(self.watch_dir, name))

    def _scan_loop(self):
        interval = max(int(self.cfg.get("poll_interval_s", 5)), 1)
        while not self._stop.wait(interval):
            self._scan_existing()

    def notify(self, path):
        if not path or not path.lower().endswith(".pdf"):
            return
        with self._pending_lock:
            if path in self._pending:
                return
            self._pending.add(path)
        self._queue.put(path)

    # -- worker -----------------------------------------------------------
    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if path is None:
                break
            try:
                self._process(path)
            except Exception as e:  # noqa: BLE001
                logger.exception("Traitement PDF en erreur (%s): %s", path, e)
            finally:
                with self._pending_lock:
                    self._pending.discard(path)
                self._queue.task_done()

    def _wait_stable(self, path):
        delay = max(self.cfg["stable_delay_s"], 0.5)
        attempts = max(self.cfg["stable_retries"], 1)
        deadline = time.time() + delay * attempts + 3
        last_size = -1
        stable_since = None
        while time.time() < deadline:
            if not os.path.exists(path):
                return False
            try:
                size = os.path.getsize(path)
                with open(path, "rb"):
                    pass
            except OSError:
                stable_since = None
                time.sleep(0.25)
                continue
            if size <= 0:
                stable_since = None
                time.sleep(0.25)
                continue
            if size != last_size:
                last_size = size
                stable_since = time.time()
            elif stable_since and time.time() - stable_since >= delay:
                return True
            time.sleep(0.25)
        return False

    def _move(self, src, dest_dir, numero=""):
        try:
            os.makedirs(dest_dir, exist_ok=True)
            base = os.path.basename(src)
            dest = os.path.join(dest_dir, base)
            if os.path.exists(dest):
                dest = os.path.join(
                    dest_dir,
                    "{}_{}".format(auto_printer.timestamp_tag(), base))
            shutil.move(src, dest)
            return dest
        except OSError as e:
            logger.warning("Deplacement de %s vers %s impossible: %s", src, dest_dir, e)
            return None

    def _certification_decision(self, invoice, rec):
        """Retourne (status, non_conforme, raison) selon le statut SFEC."""
        statut = (invoice.get("sfec_statut") or "").upper()
        valide = invoice.get("valide", 0) == 1

        if statut in ("CERTIFIE", "DEJA_CERTIFIE"):
            return "IMPRIME", False, ""
        if not valide:
            return "REFUS", False, "Facture non validee (validez en A COMPTABILISER)"

        wait = bool(self.cfg.get("wait_for_certification", True))
        if wait and statut in ("", "EN_COURS", "A_SURVEILLER"):
            first_seen = ""
            if rec is not None:
                try:
                    first_seen = rec["first_seen_at"]
                except (KeyError, IndexError):
                    first_seen = ""
            age = _age_minutes(first_seen)
            timeout = int(self.cfg.get("certif_timeout_min", 15) or 15)
            if age < timeout:
                return "ATTENTE_CERTIF", False, ""
            return ("NON_CONFORME", True,
                    "Timeout certification ({} min, statut={})".format(timeout, statut or "vide"))

        if str(self.cfg.get("on_uncertified", "watermark")).lower() == "quarantine":
            return "QUARANTINE", False, "Non certifie (statut={})".format(statut or "vide")
        return "NON_CONFORME", True, "Non certifie (statut={})".format(statut or "vide")

    def _process(self, path):
        name = os.path.basename(path)
        if not os.path.exists(path):
            return
        if not self._wait_stable(path):
            logger.warning("[WATCH] Fichier non stabilise (verrouille ?) - retry: %s", name)
            return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > self.cfg["max_size_mb"]:
            logger.warning("[WATCH] PDF trop volumineux (%.1f Mo > %d Mo) -> QUARANTINE: %s",
                           size_mb, self.cfg["max_size_mb"], name)
            self._move(path, self.quarantine_dir)
            return

        pages = _count_pages(path)
        if pages is not None and pages > self.cfg["max_pages"]:
            logger.warning("[WATCH] PDF trop de pages (%d > %d) -> QUARANTINE: %s",
                           pages, self.cfg["max_pages"], name)
            self._move(path, self.quarantine_dir)
            return

        file_hash = auto_printer.compute_sha256(path)
        if not file_hash:
            return

        rec = auto_printer.get_print_record(file_hash)
        if rec and rec["status"] == "IMPRIME":
            logger.info("[WATCH] Deja imprime (hash %s) - SKIP -> DONE: %s",
                        file_hash[:12], name)
            self._move(path, self.done_dir)
            return
        if rec and rec["status"] == "EN_COURS":
            logger.warning("[WATCH] Impression interrompue (EN_COURS) - nouvelle tentative: %s", name)
        elif rec is None:
            logger.info("[WATCH] Nouveau PDF detecte: %s", name)

        invoice, reason = job_mapper.map_pdf_to_invoice(path)
        if not invoice:
            logger.warning("[WATCH] Facture non identifiee (%s) -> QUARANTINE: %s", reason, name)
            auto_printer.record_print(file_hash, "", path, "", self.cfg["printer_name"],
                                      "QUARANTINE", reason)
            self._move(path, self.quarantine_dir)
            return

        numero = invoice.get("numero", "")
        sfec_statut = (invoice.get("sfec_statut") or "").upper()
        sfec_num = invoice.get("sfec_num_certif") or ""
        logger.info("[WATCH] Facture identifiee: %s (statut SFEC=%s)",
                    numero, sfec_statut or "vide")

        if auto_printer.numero_printed_recently(numero, self.cfg["dedup_window_min"]):
            logger.info("[WATCH] Facture %s imprimee recemment (< %d min) - SKIP_DEDUP: %s",
                        numero, self.cfg["dedup_window_min"], name)
            auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                      "SKIP_DEDUP", "doublon fenetre dedup",
                                      sfec_statut, sfec_num)
            self._move(path, self.done_dir)
            return

        status, non_conforme, reason = self._certification_decision(invoice, rec)

        if status == "ATTENTE_CERTIF":
            auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                      "ATTENTE_CERTIF", "statut SFEC=" + (sfec_statut or "vide"),
                                      sfec_statut, sfec_num)
            logger.info("[WATCH] Certif en attente (statut=%s) - fichier conserve dans IN: %s",
                        sfec_statut or "vide", name)
            return

        if status in ("REFUS", "QUARANTINE"):
            logger.error("[WATCH] ETAT=%s numero=%s: %s -> QUARANTINE", status, numero, reason)
            auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                      status, reason, sfec_statut, sfec_num)
            self._move(path, self.quarantine_dir)
            return

        auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                  "EN_COURS", "", sfec_statut, sfec_num)

        tag = "CONFORME" if status == "IMPRIME" else "NON_CONFORME"
        out_name = "{}_{}_{}.pdf".format(numero or "DOC", tag, auto_printer.timestamp_tag())
        out_path = os.path.join(self.out_dir, out_name)
        logger.info("[WATCH] Generation du PDF %s -> %s", tag, out_name)
        result = generate_invoice_pdf(invoice, out_path, non_conforme=non_conforme)
        if not result or not os.path.exists(out_path):
            logger.error("[WATCH] ETAT=ERREUR generation PDF echouee pour %s", numero)
            auto_printer.record_print(file_hash, numero, path, out_path,
                                      self.cfg["printer_name"], "ERREUR",
                                      "generation PDF echouee", sfec_statut, sfec_num)
            self._move(path, self.quarantine_dir)
            return

        logger.info("[WATCH] Impression en cours sur '%s'...", self.cfg["printer_name"])
        ok, err = auto_printer.print_pdf(
            out_path, self.cfg["printer_name"], self.cfg["sumatra_path"],
            dry_run=self.cfg["dry_run"])
        if not ok:
            logger.error("[WATCH] ETAT=ERREUR_IMPRESSION numero=%s: %s (original conserve)",
                         numero, err)
            auto_printer.record_print(file_hash, numero, path, out_path,
                                      self.cfg["printer_name"], "ERREUR_IMPRESSION", err,
                                      sfec_statut, sfec_num)
            return

        auto_printer.record_print(file_hash, numero, path, out_path,
                                  self.cfg["printer_name"], status, "",
                                  sfec_statut, sfec_num)
        self._move(path, self.done_dir)
        logger.info("[WATCH] ETAT=%s numero=%s out=%s imprimante='%s'",
                    status, numero, out_name, self.cfg["printer_name"] or "(dry_run)")
