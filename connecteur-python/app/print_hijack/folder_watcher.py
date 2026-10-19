"""Surveillance du dossier PDF et traitement des fichiers déposés."""
import logging
import os
import queue
import shutil
import threading
import time

from app.print_hijack import auto_printer, job_mapper
from app.domain.pdf import generate_invoice_pdf

logger = logging.getLogger("t-connector.print_hijack.watcher")


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

    def _process(self, path):
        if not os.path.exists(path):
            return
        if not self._wait_stable(path):
            logger.warning("Fichier non stabilise (verrouille ?) - retry au prochain scan: %s", path)
            return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > self.cfg["max_size_mb"]:
            logger.warning("PDF trop volumineux (%.1f Mo > %d Mo) -> QUARANTAINE: %s",
                           size_mb, self.cfg["max_size_mb"], path)
            self._move(path, self.quarantine_dir)
            return

        pages = _count_pages(path)
        if pages is not None and pages > self.cfg["max_pages"]:
            logger.warning("PDF trop de pages (%d > %d) -> QUARANTAINE: %s",
                           pages, self.cfg["max_pages"], path)
            self._move(path, self.quarantine_dir)
            return

        file_hash = auto_printer.compute_sha256(path)
        if not file_hash:
            return

        rec = auto_printer.get_print_record(file_hash)
        if rec and rec["status"] in ("IMPRIME", "EN_COURS"):
            logger.info("Fichier deja traite (hash %s, statut %s) - SKIP: %s",
                        file_hash[:12], rec["status"], os.path.basename(path))
            self._move(path, self.done_dir)
            return

        invoice, reason = job_mapper.map_pdf_to_invoice(path)
        if not invoice:
            logger.warning("Facture non identifiee (%s) -> QUARANTAINE: %s", reason, path)
            auto_printer.record_print(file_hash, "", path, "", self.cfg["printer_name"],
                                      "QUARANTAINE", reason)
            self._move(path, self.quarantine_dir)
            return

        numero = invoice.get("numero", "")
        if auto_printer.numero_printed_recently(numero, self.cfg["dedup_window_min"]):
            logger.info("Facture %s imprimee recemment (< %d min) - SKIP: %s",
                        numero, self.cfg["dedup_window_min"], os.path.basename(path))
            auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                      "SKIP_DEDUP", "doublon fenetre dedup")
            self._move(path, self.done_dir)
            return

        auto_printer.record_print(file_hash, numero, path, "", self.cfg["printer_name"],
                                  "EN_COURS", "")

        out_name = "{}_CONFORME_{}.pdf".format(numero or "DOC",
                                               auto_printer.timestamp_tag())
        out_path = os.path.join(self.out_dir, out_name)
        result = generate_invoice_pdf(invoice, out_path)
        if not result or not os.path.exists(out_path):
            logger.error("Generation du PDF conforme echouee pour %s", numero)
            auto_printer.record_print(file_hash, numero, path, out_path,
                                      self.cfg["printer_name"], "ERREUR",
                                      "generation PDF conforme echouee")
            self._move(path, self.quarantine_dir)
            return

        ok, err = auto_printer.print_pdf(
            out_path, self.cfg["printer_name"], self.cfg["sumatra_path"],
            dry_run=self.cfg["dry_run"])
        if not ok:
            logger.error("Impression echouee pour %s: %s (original conserve pour retry)", numero, err)
            auto_printer.record_print(file_hash, numero, path, out_path,
                                      self.cfg["printer_name"], "ERREUR_IMPRESSION", err)
            return

        auto_printer.record_print(file_hash, numero, path, out_path,
                                  self.cfg["printer_name"], "IMPRIME", "")
        self._move(path, self.done_dir)
        logger.info("Facture %s : conforme generee (%s) et imprimee sur '%s'",
                    numero, out_name, self.cfg["printer_name"] or "(dry_run)")
