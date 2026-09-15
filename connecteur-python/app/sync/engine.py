import logging
import time
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.config.manager import get_config
from app.integration.sage.database import (
    fetch_sales_invoices, fetch_purchase_invoices, fetch_contacts,
    fetch_tax_rates, fetch_ledger_accounts, fetch_certified_invoices,
    mark_certifying, mark_certified, mark_certification_failed, mark_to_monitor,
    write_sfec_to_invoice, reset_stuck_en_cours, close_pool
)
from app.integration.sfec.endpoints import certify_invoice, _extract_cert_data as _extract_sfec_cert_data
from app.integration.sfec.client import SfecClient, NetworkOfflineError
from app.sync.connectivity import is_online, check_now, get_status

logger = logging.getLogger("t-connector.sync")


_cache = {
    "sales_invoices": [],
    "purchase_invoices": [],
    "contacts": [],
    "tax_rates": [],
    "ledger_accounts": [],
    "sfec_invoices": [],
    "last_sync_at": None,
    "last_sfec_sync_at": None,
    "db_connected": False,
    "last_error": None,
    "sync_count": 0,
    "certified_count": 0,
    "error_count": 0,
}

_lock = threading.Lock()
_polling_thread = None
_stop_event = threading.Event()

_retry_queue = []
_retry_lock = threading.Lock()

_sfec_lookup_cache = {}
_sfec_lookup_ts = 0
_SFEC_LOOKUP_TTL = 300


def get_cache():
    with _lock:
        return dict(_cache)


def get_retry_queue():
    with _retry_lock:
        return list(_retry_queue)


def _enqueue_for_retry(invoice):
    with _retry_lock:
        for item in _retry_queue:
            if item.get("id") == invoice.get("id"):
                return
        _retry_queue.append({
            "id": invoice.get("id", ""),
            "numero": invoice.get("numero", ""),
            "nom_tiers": invoice.get("nom_tiers", ""),
            "montant_ttc": invoice.get("montant_ttc", 0),
            "queued_at": datetime.utcnow().isoformat() + "Z",
            "attempts": 0,
        })
        count = len(_retry_queue)
    _cache["retry_queue_count"] = count
    logger.info("Facture mise en file d'attente (offline): %s (%s)",
                invoice.get("numero", ""), invoice.get("id", ""))


def _process_retry_queue():
    if not is_online():
        return

    with _retry_lock:
        if not _retry_queue:
            return
        queue_copy = list(_retry_queue)
        _retry_queue.clear()

    _cache["retry_queue_count"] = 0
    logger.info("Traitement de la file d'attente: %d factures", len(queue_copy))

    config = get_config()
    sfec_cfg = config.get("sfec", {})
    if not sfec_cfg.get("enabled") or not sfec_cfg.get("api_key"):
        with _retry_lock:
            _retry_queue.extend(queue_copy)
        _cache["retry_queue_count"] = len(_retry_queue)
        return

    certified_in_db = []
    try:
        certified_in_db = fetch_certified_invoices()
    except Exception:
        pass
    certified_ids = {c["id"] for c in certified_in_db}

    sfec_lookup = _build_sfec_lookup()

    success = 0
    errors = 0

    for item in queue_copy:
        invoice_id = item.get("id", "")
        numero = item.get("numero", "")

        if invoice_id in certified_ids:
            success += 1
            continue

        if sfec_lookup and numero in sfec_lookup:
            _handle_already_certified(item, sfec_lookup)
            success += 1
            continue

        invoice = None
        with _lock:
            for inv in _cache["sales_invoices"]:
                if inv["id"] == invoice_id:
                    invoice = inv
                    break

        if not invoice:
            errors += 1
            logger.warning("Retry queue: facture %s non trouvee dans le cache", numero)
            continue

        try:
            mark_certifying(invoice_id)
            result = certify_invoice(invoice)
            cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)

            write_sfec_to_invoice(
                invoice_id=invoice_id,
                certification_number=cert_number,
                signature=sig,
                qr_code=qr,
                certification_date=cert_date,
            )

            with _lock:
                invoice["sfec_statut"] = "CERTIFIE"
                invoice["sfec_num_certif"] = cert_number
            success += 1
            logger.info("Retry queue: facture certifiee avec succes: %s (certif: %s)", numero, cert_number)

        except NetworkOfflineError:
            with _retry_lock:
                item["attempts"] = item.get("attempts", 0) + 1
                _retry_queue.append(item)
            _cache["retry_queue_count"] = len(_retry_queue)
            logger.info("Retry queue: internet perdu, repositionnement en file d'attente")
            break

        except Exception as e:
            status_code = getattr(e, "status_code", None)
            if status_code == 409:
                _handle_already_certified(item, sfec_lookup)
                success += 1
            elif status_code == 502:
                errors += 1
                try:
                    mark_to_monitor(invoice_id)
                except Exception:
                    pass
            elif status_code in (400, 422):
                errors += 1
                try:
                    mark_certification_failed(invoice_id, "SFEC {}: {}".format(status_code, str(e)[:200]))
                except Exception:
                    pass
            else:
                errors += 1
                try:
                    mark_certification_failed(invoice_id, str(e)[:200])
                except Exception:
                    pass

        time.sleep(0.1)

    if success > 0 or errors > 0:
        logger.info("Retry queue termine: %d succes, %d erreurs", success, errors)


def sync_all():
    with _lock:
        try:
            logger.info("Sync complet depuis Sage 100...")

            _cache["sales_invoices"] = fetch_sales_invoices()
            _cache["purchase_invoices"] = fetch_purchase_invoices()
            _cache["contacts"] = fetch_contacts()
            _cache["tax_rates"] = fetch_tax_rates()
            _cache["ledger_accounts"] = fetch_ledger_accounts()

            try:
                import app.sync.article_sync as article_sync
                articles_result = article_sync.sync_articles_from_sage()
                _cache["articles"] = articles_result
                logger.info("Sync articles: %s", articles_result)
            except Exception as ae:
                logger.warning("Sync articles (sync_all): %s", ae)

            _cache["db_connected"] = True
            _cache["last_error"] = None
            _cache["last_sync_at"] = datetime.utcnow().isoformat() + "Z"
            _cache["sync_count"] += 1

            logger.info("Sync termine: %d factures vente, %d factures achat, %d contacts, %d taux TVA, %d comptes",
                        len(_cache["sales_invoices"]), len(_cache["purchase_invoices"]),
                        len(_cache["contacts"]), len(_cache["tax_rates"]), len(_cache["ledger_accounts"]))

        except Exception as e:
            _cache["db_connected"] = False
            _cache["last_error"] = str(e)
            logger.error("Erreur sync complet: %s", e)
            return

    try:
        auto_certify_invoices()
    except Exception as e:
        logger.error("Erreur auto-certification: %s", e)

    if is_online():
        sync_sfec_invoices()
    else:
        logger.debug("Sync SFEC ignoree: pas de connexion internet")


def sync_sfec_invoices():
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not sfec_cfg.get("api_key"):
        return

    if not is_online():
        logger.debug("Sync SFEC sautee: pas de connexion internet")
        return

    try:
        client = SfecClient()
        all_invoices = []
        page = 1
        while True:
            result = client.list_invoices(page=page, page_size=500)
            batch = result.get("invoices", [])
            if not batch:
                break
            all_invoices.extend(batch)
            total = result.get("total", result.get("total_count", 0))
            if page * 500 >= total:
                break
            page += 1
        with _lock:
            _cache["sfec_invoices"] = all_invoices
            _cache["certified_count"] = len(all_invoices)
            _cache["last_sfec_sync_at"] = datetime.utcnow().isoformat() + "Z"
        logger.info("Sync SFEC: %d factures certifiees recuperees", len(all_invoices))
    except NetworkOfflineError:
        logger.info("Sync SFEC interrompue: connexion internet perdue")
    except Exception as e:
        logger.warning("Erreur sync SFEC: %s", e)


def sync_incremental():
    with _lock:
        try:
            if _cache["last_sync_at"]:
                updated_from = _cache["last_sync_at"]
                new_sales = fetch_sales_invoices(updated_from=updated_from)
                new_purchases = fetch_purchase_invoices(updated_from=updated_from)
            else:
                new_sales = fetch_sales_invoices()
                new_purchases = fetch_purchase_invoices()

            sales_index = {inv["id"]: i for i, inv in enumerate(_cache["sales_invoices"])}
            for inv in new_sales:
                idx = sales_index.get(inv["id"])
                if idx is not None:
                    _cache["sales_invoices"][idx] = inv
                else:
                    _cache["sales_invoices"].insert(0, inv)
                    sales_index[inv["id"]] = 0

            purchase_index = {inv["id"]: i for i, inv in enumerate(_cache["purchase_invoices"])}
            for inv in new_purchases:
                idx = purchase_index.get(inv["id"])
                if idx is not None:
                    _cache["purchase_invoices"][idx] = inv
                else:
                    _cache["purchase_invoices"].insert(0, inv)
                    purchase_index[inv["id"]] = 0

            _cache["db_connected"] = True
            _cache["last_error"] = None
            _cache["last_sync_at"] = datetime.utcnow().isoformat() + "Z"
            _cache["sync_count"] += 1

            logger.debug("Sync incrementale: +%d ventes, +%d achats",
                         len(new_sales), len(new_purchases))

        except Exception as e:
            _cache["db_connected"] = False
            _cache["last_error"] = str(e)
            logger.error("Erreur sync incrementale: %s", e)
            return

    try:
        auto_certify_invoices()
    except Exception as e:
        logger.error("Erreur auto-certification (incrementale): %s", e)


def _build_sfec_lookup():
    global _sfec_lookup_cache, _sfec_lookup_ts
    now = time.time()
    if _sfec_lookup_cache and (now - _sfec_lookup_ts) < _SFEC_LOOKUP_TTL:
        return _sfec_lookup_cache

    lookup = {}
    if not is_online():
        return lookup
    try:
        client = SfecClient()
        page = 1
        while True:
            result = client.list_invoices(page=page, page_size=500)
            invoices = result.get("invoices", [])
            if not invoices:
                break
            for inv in invoices:
                seller_num = inv.get("seller_invoice_number", "") or inv.get("invoice_number", "")
                if seller_num:
                    lookup[seller_num] = inv
            total = result.get("total", result.get("total_count", 0))
            if page * 500 >= total:
                break
            page += 1
    except NetworkOfflineError:
        logger.debug("Lookup SFEC ignore: pas de connexion internet")
    except Exception as ex:
        logger.debug("Impossible de charger la liste SFEC: %s", ex)
    if lookup:
        _sfec_lookup_cache = lookup
        _sfec_lookup_ts = now
    return lookup


def _handle_already_certified(invoice, sfec_lookup=None):
    numero = invoice.get("numero", "")
    sfec_inv = None
    if sfec_lookup and numero in sfec_lookup:
        sfec_inv = sfec_lookup[numero]

    if not sfec_inv and numero:
        try:
            client = SfecClient()
            sfec_inv = client.verify_by_invoice_number(numero)
        except Exception as ex:
            logger.debug("GET SFEC direct echoue pour %s: %s", numero, ex)

    if sfec_inv:
        cert_number, sig, qr, cert_date = _extract_sfec_cert_data(sfec_inv)
        if cert_number or qr:
            try:
                write_sfec_to_invoice(
                    invoice_id=invoice["id"],
                    certification_number=cert_number,
                    signature=sig,
                    qr_code=qr,
                    certification_date=cert_date,
                )
                with _lock:
                    invoice["sfec_statut"] = "DEJA_CERTIFIE"
                    invoice["sfec_num_certif"] = cert_number
                logger.info("Certif SFEC recuperee et ecrite dans Sage: %s (certif: %s)",
                            numero, cert_number)
            except Exception as ex:
                logger.error("Ecriture SFEC echouee (deja certifiee) pour %s: %s", numero, ex)
        else:
            try:
                mark_certified(invoice["id"], "DEJA_CERTIFIE")
            except Exception:
                pass
            logger.debug("Facture %s deja certifiee SFEC (pas de donnees certif dans GET)", numero)
    else:
        try:
            mark_certified(invoice["id"], "DEJA_CERTIFIE")
        except Exception:
            pass


def reconcile_sfec_status():
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not sfec_cfg.get("api_key"):
        return

    if not is_online():
        logger.debug("Reconciliation ignoree: pas de connexion internet")
        return

    try:
        certified_in_db = fetch_certified_invoices()
    except Exception as e:
        logger.warning("Reconciliation: impossible de lire les certif depuis Sage: %s", e)
        return

    if not certified_in_db:
        return

    sfec_lookup = _build_sfec_lookup()
    if not sfec_lookup:
        logger.warning("Reconciliation: impossible de charger la liste SFEC")
        return

    reconciled = 0
    issues = 0

    for cert in certified_in_db:
        invoice_id = cert["id"]
        cert_num = cert.get("certif_num", "")
        numero = invoice_id.rsplit("-", 1)[-1] if "-" in invoice_id else ""

        sfec_inv = sfec_lookup.get(numero) or sfec_lookup.get(cert_num)

        if sfec_inv:
            reconciled += 1
        else:
            issues += 1
            logger.warning("Reconciliation: facture %s marquee CERTIFIE en local "
                           "mais ABSENTE de SFEC (certif: %s)", invoice_id, cert_num)
            try:
                mark_to_monitor(invoice_id)
            except Exception:
                pass

    if issues > 0:
        logger.warning("Reconciliation: %d factures avec ecart detecte (sur %d verifiees)",
                       issues, reconciled + issues)
    else:
        logger.info("Reconciliation: %d factures OK sur SFEC", reconciled)


def _certify_single_worker(invoice, sfec_lookup):
    numero = invoice.get("numero", "")
    try:
        if sfec_lookup and numero in sfec_lookup:
            _handle_already_certified(invoice, sfec_lookup)
            return ("skip", numero)

        mark_certifying(invoice["id"])

        result = certify_invoice(invoice)

        cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)

        write_sfec_to_invoice(
            invoice_id=invoice["id"],
            certification_number=cert_number,
            signature=sig,
            qr_code=qr,
            certification_date=cert_date,
        )

        with _lock:
            invoice["sfec_statut"] = "CERTIFIE"
            invoice["sfec_num_certif"] = cert_number

        return ("ok", numero, cert_number)

    except NetworkOfflineError:
        _enqueue_for_retry(invoice)
        return ("offline", numero)

    except Exception as e:
        logger.error("Certification echouee pour %s: %s", numero, e)
        status_code = getattr(e, "status_code", None)
        error_body = getattr(e, "error_body", "")
        if status_code == 409:
            _handle_already_certified(invoice, sfec_lookup)
            return ("skip", numero)
        elif status_code == 502:
            try:
                mark_to_monitor(invoice["id"])
            except Exception:
                pass
            return ("error_502", numero)
        elif status_code in (400, 422):
            try:
                mark_certification_failed(invoice["id"], "SFEC {}: {}".format(status_code, error_body[:200]))
            except Exception:
                pass
            return ("error_reject", numero)
        else:
            try:
                mark_certification_failed(invoice["id"], str(e)[:200])
            except Exception:
                pass
            return ("error", numero)


def auto_certify_invoices():
    config = get_config()
    sfec_cfg = config.get("sfec", {})

    if not sfec_cfg.get("enabled"):
        logger.debug("SFEC desactivee - auto-certification ignoree")
        return

    if not sfec_cfg.get("api_key"):
        logger.debug("Cle API SFEC non configuree - auto-certification ignoree")
        return

    if not is_online():
        logger.info("Pas de connexion internet - certification SFEC reportee")
        _queue_uncertified_for_retry()
        return

    try:
        reset_stuck_en_cours()
    except Exception:
        pass

    with _lock:
        invoices_snapshot = list(_cache["sales_invoices"])

    sfec_lookup = _build_sfec_lookup()
    logger.info("Lookup SFEC: %d factures sur SFEC", len(sfec_lookup))

    to_certify = []
    sfec_matched = 0
    to_account = 0

    for inv in invoices_snapshot:
        if inv.get("statut_code", 0) != 2:
            continue
        to_account += 1
        numero = inv.get("numero", "")
        sfec_statut = inv.get("sfec_statut", "")

        type_doc = inv.get("type_doc", "vente")

        if sfec_statut in ("EN_COURS", "ERREUR"):
            continue

        if numero in sfec_lookup:
            sfec_matched += 1
            if sfec_statut not in ("DEJA_CERTIFIE", "CERTIFIE"):
                try:
                    _handle_already_certified(inv, sfec_lookup)
                except Exception:
                    pass
            continue

        if type_doc == "avoir":
            ref = inv.get("reference", "")  # HYPOTHESE: DO_Ref porte le numero de la facture d'origine
            if not ref:
                try:
                    mark_certification_failed(inv["id"], "Avoir sans reference facture d'origine")
                except Exception:
                    pass
                continue
            ref_invoice = next((x for x in invoices_snapshot if x.get("numero") == ref), None)
            if not ref_invoice or ref_invoice.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
                try:
                    mark_certification_failed(inv["id"], "Facture d'origine '{}' non certifiee".format(ref))
                except Exception:
                    pass
                continue
            deja_avoir = any(
                x.get("type_doc") == "avoir" and x.get("reference") == ref and x.get("id") != inv.get("id")
                and x.get("sfec_statut") in ("CERTIFIE", "DEJA_CERTIFIE")
                for x in invoices_snapshot
            )
            if deja_avoir:
                try:
                    mark_certification_failed(inv["id"], "Un avoir deja certifie existe pour '{}'".format(ref))
                except Exception:
                    pass
                continue

        to_certify.append(inv)

        if sfec_statut in ("EN_COURS", "ERREUR"):
            continue

        if numero in sfec_lookup:
            sfec_matched += 1
            if sfec_statut not in ("DEJA_CERTIFIE", "CERTIFIE"):
                try:
                    _handle_already_certified(inv, sfec_lookup)
                except Exception:
                    pass
            continue

        to_certify.append(inv)

    logger.info("Auto-certif: %d a comptabiliser, %d sur SFEC (OK), %d a certifier",
                to_account, sfec_matched, len(to_certify))

    if not to_certify:
        return

    to_certify.sort(key=lambda x: x.get("date_facture", ""), reverse=True)

    queue_cfg = config.get("queue", {})
    max_concurrent = queue_cfg.get("max_concurrent", 3)

    newly_certified = 0
    errors = 0
    offline_detected = False

    if max_concurrent > 1:
        logger.info("Certification parallele: max_concurrent=%d", max_concurrent)
        processed_ids = set()
        with ThreadPoolExecutor(max_workers=min(max_concurrent, len(to_certify))) as executor:
            futures = {}
            for invoice in to_certify:
                future = executor.submit(_certify_single_worker, invoice, sfec_lookup)
                futures[future] = invoice

            for future in as_completed(futures):
                result = future.result()
                inv = futures[future]
                processed_ids.add(inv["id"])
                status = result[0]
                if status == "ok":
                    newly_certified += 1
                elif status == "offline":
                    offline_detected = True
                    break
                elif status != "skip":
                    errors += 1
                time.sleep(0.02)

        if offline_detected:
            for inv in to_certify:
                if inv["id"] not in processed_ids:
                    _enqueue_for_retry(inv)
    else:
        for idx, invoice in enumerate(to_certify):
            try:
                result = _certify_single_worker(invoice, sfec_lookup)
                status = result[0]
                if status == "ok":
                    newly_certified += 1
                elif status == "offline":
                    for remaining in to_certify[idx + 1:]:
                        _enqueue_for_retry(remaining)
                    break
                elif status != "skip":
                    errors += 1

            except Exception as e:
                errors += 1
                logger.error("Erreur inattendue certification: %s - %s", invoice.get("numero"), e)

            time.sleep(0.1)

    _cache["error_count"] += errors
    logger.info("Auto-certification terminee: %d certifiees, %d erreurs", newly_certified, errors)


def _queue_uncertified_for_retry():
    certified_in_db = []
    try:
        certified_in_db = fetch_certified_invoices()
    except Exception:
        pass
    certified_ids = {c["id"] for c in certified_in_db if c.get("certif_num", "").strip()}

    with _retry_lock:
        existing_ids = {item["id"] for item in _retry_queue}

    queued = 0
    for inv in _cache["sales_invoices"]:
        if inv["id"] not in certified_ids and inv.get("valide", 0) == 1:
            if inv.get("sfec_statut", "") not in ("EN_COURS", "ERREUR"):
                if inv["id"] not in existing_ids:
                    _enqueue_for_retry(inv)
                    queued += 1

    if queued > 0:
        logger.info("File d'attente: %d factures ajoutees (offline)", queued)


def certify_single(invoice_id):
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not sfec_cfg.get("api_key"):
        return {"success": False, "error": "SFEC non configuree"}

    invoice = None
    with _lock:
        for inv in _cache["sales_invoices"]:
            if inv["id"] == invoice_id:
                invoice = inv
                break

    if not invoice:
        return {"success": False, "error": "Facture {} non trouvee".format(invoice_id)}

    if not is_online():
        _enqueue_for_retry(invoice)
        return {"success": False, "error": "Pas de connexion internet - facture mise en file d'attente",
                "queued": True}

    try:
        mark_certifying(invoice_id)
        result = certify_invoice(invoice)
        cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)

        write_sfec_to_invoice(
            invoice_id=invoice_id,
            certification_number=cert_number,
            signature=sig,
            qr_code=qr,
            certification_date=cert_date,
        )

        with _lock:
            invoice["sfec_statut"] = "CERTIFIE"
            invoice["sfec_num_certif"] = cert_number

        return {
            "success": True,
            "certification_number": cert_number,
            "qr_code": qr,
        }
    except NetworkOfflineError:
        _enqueue_for_retry(invoice)
        return {"success": False, "error": "Pas de connexion internet - facture mise en file d'attente",
                "queued": True}
    except Exception as e:
        status_code = getattr(e, "status_code", None)
        if status_code == 409:
            try:
                mark_certified(invoice_id, "DEJA_CERTIFIE")
            except Exception:
                pass
            try:
                numero = invoice.get("numero", "")
                sfec_lookup = _build_sfec_lookup()
                sfec_inv = sfec_lookup.get(numero) if sfec_lookup else None
                if sfec_inv:
                    cert_number, sig, qr, cert_date = _extract_sfec_cert_data(sfec_inv)
                    if cert_number or qr:
                        write_sfec_to_invoice(
                            invoice_id=invoice_id,
                            certification_number=cert_number,
                            signature=sig,
                            qr_code=qr,
                            certification_date=cert_date,
                        )
                        with _lock:
                            invoice["sfec_statut"] = "DEJA_CERTIFIE"
                            invoice["sfec_num_certif"] = cert_number
            except Exception as ex:
                logger.error("Ecriture SFEC echouee (manuel deja certifiee) pour %s: %s", invoice_id, ex)
            return {"success": True, "certification_number": "DEJA_CERTIFIE", "note": "Facture deja certifiee sur SFEC"}
        if status_code == 502:
            try:
                mark_to_monitor(invoice_id)
            except Exception:
                pass
            return {"success": False, "error": "Certification recue mais non verifiee sur SFEC", "note": "EN_ATTENTE"}
        try:
            mark_certification_failed(invoice_id, str(e))
        except Exception:
            pass
        return {"success": False, "error": str(e)}


def _polling_loop():
    consecutive_errors = 0
    max_backoff = 300
    cycle_count = 0
    RECONCILE_EVERY = 8

    logger.info("Demarrage du polling intelligent avec verification internet")

    while not _stop_event.is_set():
        config = get_config()
        db_cfg = config.get("db", {})
        sfec_cfg = config.get("sfec", {})

        if not db_cfg.get("polling_enabled", True):
            logger.debug("Polling desactive - attente 60s")
            _stop_event.wait(60)
            continue

        interval_ms = db_cfg.get("polling_interval_ms", 30000)
        interval_s = max(interval_ms / 1000, 5)

        online = is_online()

        try:
            if _cache["last_sync_at"]:
                sync_incremental()
            else:
                sync_all()
            cycle_count += 1

            if cycle_count % RECONCILE_EVERY == 0:
                try:
                    reconcile_sfec_status()
                except Exception as e:
                    logger.warning("Erreur reconciliation: %s", e)

            if online and sfec_cfg.get("enabled"):
                retry_count = len(get_retry_queue())
                if retry_count > 0:
                    try:
                        _process_retry_queue()
                    except Exception as e:
                        logger.warning("Erreur traitement file d'attente: %s", e)

            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.error("Erreur polling: %s", e)

        backoff = min(interval_s * (2 ** consecutive_errors), max_backoff) if consecutive_errors > 0 else interval_s
        if consecutive_errors > 0:
            logger.info("Prochaine tentative dans %ds (erreur #%d)", backoff, consecutive_errors)
        _stop_event.wait(backoff)


def start_polling():
    global _polling_thread
    config = get_config()

    if not config.get("db", {}).get("polling_enabled", True):
        logger.info("Polling desactive dans la configuration")
        return

    if _polling_thread is not None and _polling_thread.is_alive():
        logger.warning("Polling deja en cours")
        return

    _stop_event.clear()
    _polling_thread = threading.Thread(target=_polling_loop, daemon=True, name="sfec-polling")
    _polling_thread.start()
    logger.info("Thread de polling intelligent demarre")


def stop_polling():
    _stop_event.set()
    if _polling_thread and _polling_thread.is_alive():
        _polling_thread.join(timeout=5)
    logger.info("Polling arrete")
    close_pool()


def apply_config():
    close_pool()
    logger.info("Configuration rechargee, pool DB ferme (reconnexion au prochain acces)")
    return {"ok": True, "message": "Configuration appliquee. Pool DB reinitialise."}


def get_metrics():
    real_certified = 0
    try:
        real_certified = len(fetch_certified_invoices())
    except Exception:
        real_certified = _cache["certified_count"]

    conn_status = get_status()
    retry_count = len(get_retry_queue())

    return {
        "sales_invoices": len(_cache["sales_invoices"]),
        "purchase_invoices": len(_cache["purchase_invoices"]),
        "contacts": len(_cache["contacts"]),
        "tax_rates": len(_cache["tax_rates"]),
        "ledger_accounts": len(_cache["ledger_accounts"]),
        "db_connected": _cache["db_connected"],
        "last_sync_at": _cache["last_sync_at"],
        "last_error": _cache["last_error"],
        "sync_count": _cache["sync_count"],
        "certified_count": real_certified,
        "error_count": _cache["error_count"],
        "internet_status": conn_status,
        "retry_queue_count": retry_count,
    }
