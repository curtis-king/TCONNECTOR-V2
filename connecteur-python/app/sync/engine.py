import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.config.manager import get_config, get_sfec_api_key
from app.core.timeutil import utcnow_iso
from app.integration.sage.database import (
    fetch_sales_invoices, fetch_purchase_invoices, fetch_contacts,
    fetch_tax_rates, fetch_ledger_accounts, fetch_certified_invoices,
    mark_certifying, mark_certified, mark_to_monitor,
    write_sfec_to_invoice, reset_stuck_en_cours, close_pool
)
from app.integration.sfec.endpoints import (
    certify_invoice, resolve_invoice_number, get_certified_by_sage_id,
    record_certified_invoice,
    _extract_cert_data as _extract_sfec_cert_data,
)
from app.integration.sfec.client import SfecClient, NetworkOfflineError
from app.integration.sfec.pending_writes import enqueue, flush
from app.storage.db import fetch_sfec_invoice_links, update_invoice_sfec_by_numero
from app.sync.connectivity import is_online, get_status
from app.sync.state import (
    _cache, _lock, _stop_event, _retry_queue, _retry_lock,
    _certify_run_lock, _retry_after, _retry_attempts,
    _logged_errors, get_cache, get_retry_queue, polling_active,
    polling_thread, set_polling_thread,
)
from app.sync.retry import (
    _should_retry, _has_certification_proof,
    _needs_certification_recovery, _schedule_retry, _reset_retry,
    _log_certify_error,
)
from app.sync.sfec_errors import (
    _sfec_error_json, overcredit_details, overcredit_message,
    on_account_reference_conflict,
)
from app.sync.invoice_rules import (
    _as_float, _as_bool, _is_credit_note, _has_zero_amount, _credit_label,
    _should_mark_credit_failure, _manual_links, _resolve_credit_parent,
    _parent_sfec_number, _prepare_credit_note, _build_credit_note_index,
)

logger = logging.getLogger("t-connector.sync")


# Le rejeu des ecritures Sage est une tache de fond independante du pull :
# il doit s'executer meme quand la lecture Sage echoue. Throttle pour ne pas
# multiplier les tentatives ODBC sur un intervalle de polling court
# (`sfec.pending_flush_interval_s`, defaut 30 s).
_flush_lock = threading.Lock()
_last_flush_ts = None

# Cache du lookup SFEC (numero -> facture) et reset des EN_COURS au demarrage :
# etat interne au moteur, jamais partage avec les autres modules.
_sfec_lookup_cache = {}
_sfec_lookup_ts = 0
_startup_reset_done = False


def _invalidate_sfec_lookup():
    """Force un rechargement du lookup SFEC au prochain appel.

    Un 409 prouve que la copie en cache est obsolete : la facture existe
    cote SFEC alors que le cache l'ignorait.
    """
    global _sfec_lookup_cache, _sfec_lookup_ts
    _sfec_lookup_cache = None
    _sfec_lookup_ts = 0


def _sfec_int(name, default, lo, hi):
    """Entier de config `sfec.*` borne ; retombe sur le defaut si invalide."""
    try:
        raw = (get_config().get("sfec") or {}).get(name, default)
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


def _flush_interval_s():
    return _sfec_int("pending_flush_interval_s", 30, 5, 3600)


def _sfec_lookup_ttl_s():
    return _sfec_int("lookup_ttl_s", 300, 5, 3600)


def _queue_pending_write(invoice, cert_number, sig, qr, cert_date, error):
    """Met une certification en file si l'ecriture Sage echoue.

    Retourne False si meme SQLite est indisponible : l'appelant doit alors
    conserver son chemin d'erreur normal (retry) pour ne rien perdre.
    """
    invoice_id = invoice.get("id", "") if isinstance(invoice, dict) else ""
    try:
        enqueue(invoice_id, cert_number, sig, qr, cert_date, str(error))
    except Exception as e:  # noqa: BLE001
        logger.error("Mise en file SFEC impossible pour %s: %s", invoice_id, e)
        return False
    if isinstance(invoice, dict):
        with _lock:
            invoice["sfec_statut"] = "CERTIFIE"
            invoice["sfec_pending_sage"] = True
            if cert_number:
                invoice["sfec_num_certif"] = cert_number
            invoice["sfec_signature"] = sig
            invoice["sfec_qr_code"] = qr
            invoice["sfec_date_certif"] = cert_date
    logger.warning(
        "Ecriture Sage differee pour %s (certification SFEC conservee): %s",
        invoice_id, error,
    )
    return True


def _pending_write_done(invoice_id, cert_number=""):
    """Callback appele par pending_writes.flush apres une ecriture reussie."""
    with _lock:
        for inv in _cache["sales_invoices"]:
            if inv.get("id") == invoice_id:
                inv["sfec_statut"] = "CERTIFIE"
                inv["sfec_pending_sage"] = False
                inv["sfec_pending_attempts"] = 0
                if cert_number:
                    inv["sfec_num_certif"] = cert_number
                break
    logger.info("Ecriture Sage en attente terminee: %s (certif: %s)",
                invoice_id, cert_number or "-")


def _flush_pending_writes(force=False):
    """Rejoue les certifications qui n'avaient pas pu etre ecrites dans Sage.

    Appele au debut de chaque cycle de sync ET de chaque passe de
    certification. Le throttle evite de rejouer deux fois par cycle quand
    l'intervalle de polling est court.
    """
    global _last_flush_ts
    now = time.monotonic()
    with _flush_lock:
        if (not force and _last_flush_ts is not None
                and (now - _last_flush_ts) < _flush_interval_s()):
            return 0
        _last_flush_ts = now
    try:
        return flush(on_success=_pending_write_done)
    except Exception as e:  # noqa: BLE001
        logger.warning("Rejeu des ecritures SFEC en attente echoue: %s", e)
        return 0


def flush_pending_writes(force=True):
    """Point d'entree public (API /api/sfec/pending/flush)."""
    return _flush_pending_writes(force=force)


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
            "queued_at": utcnow_iso(),
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
    if not sfec_cfg.get("enabled") or not get_sfec_api_key(sfec_cfg):
        with _retry_lock:
            _retry_queue.extend(queue_copy)
        _cache["retry_queue_count"] = len(_retry_queue)
        return

    certified_in_db = []
    try:
        certified_in_db = fetch_certified_invoices()
    except Exception:
        pass
    certified_ids = {
        c["id"] for c in certified_in_db
        if str(c.get("certif_num") or "").strip()
    }

    sfec_lookup = _build_sfec_lookup()

    with _lock:
        retry_snapshot = list(_cache["sales_invoices"])
    manual_links = _manual_links()
    by_numero = {}
    for _x in retry_snapshot:
        _num = _x.get("numero")
        if _num and _num not in by_numero:
            by_numero[_num] = _x
    credit_index = _build_credit_note_index(
        retry_snapshot, by_numero, manual_links)

    success = 0
    errors = 0

    for item in queue_copy:
        invoice_id = item.get("id", "")
        numero = item.get("numero", "")

        if not _should_retry(numero):
            # Backoff en cours : ne pas rejouer a chaque cycle de polling.
            with _retry_lock:
                _retry_queue.append(item)
            continue

        if invoice_id in certified_ids:
            _reset_retry(numero)
            success += 1
            continue

        if sfec_lookup and numero in sfec_lookup:
            _handle_already_certified(item, sfec_lookup)
            _reset_retry(numero)
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

        if _has_zero_amount(invoice):
            try:
                mark_to_monitor(invoice_id, "Montant nul: non certifiable")
            except Exception:
                pass
            continue

        if _is_credit_note(invoice):
            state, ref, _parent, reason = _prepare_credit_note(
                invoice, by_numero, manual_links, credit_index)
            if state == "parent_pending":
                logger.debug("Retry queue: avoir %s en attente de %s", numero, ref)
                continue
            if state != "ok":
                if _should_mark_credit_failure(invoice):
                    try:
                        mark_to_monitor(invoice_id, reason)
                    except Exception:
                        pass
                else:
                    logger.debug("Retry queue: facture negative %s hors perimetre: %s",
                                 numero, reason)
                continue

        try:
            mark_certifying(invoice_id)
            result = certify_invoice(invoice)
            sfec_invoice_number = str(result.get("invoice_number") or "").strip()
            if sfec_invoice_number:
                invoice["sfec_invoice_number"] = sfec_invoice_number
            cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)

            try:
                write_sfec_to_invoice(
                    invoice_id=invoice_id,
                    certification_number=cert_number,
                    signature=sig,
                    qr_code=qr,
                    certification_date=cert_date,
                )
            except Exception as ex:  # noqa: BLE001
                if not _queue_pending_write(
                        invoice, cert_number, sig, qr, cert_date, ex):
                    raise
                _reset_retry(numero)
                success += 1
                logger.warning(
                    "Retry queue: certification SFEC %s conservee, ecriture Sage differee",
                    numero,
                )
                continue

            with _lock:
                invoice["sfec_statut"] = "CERTIFIE"
                invoice["sfec_num_certif"] = cert_number
            _reset_retry(numero)
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
            overcredit = overcredit_details(e)
            if overcredit:
                errors += 1
                try:
                    mark_to_monitor(invoice_id, overcredit_message(e))
                except Exception:
                    pass
                _reset_retry(numero)
                continue
            ref_conflict = on_account_reference_conflict(e)
            if ref_conflict:
                errors += 1
                try:
                    mark_to_monitor(
                        invoice_id,
                        "Reference d'avoir sur compte deja utilisee: {}".format(
                            ref_conflict),
                    )
                except Exception:
                    pass
                _reset_retry(numero)
                continue
            if status_code == 409:
                _handle_already_certified(item, sfec_lookup)
                _reset_retry(numero)
                success += 1
            elif status_code == 502:
                errors += 1
                try:
                    mark_to_monitor(invoice_id, "SFEC 502: {}".format(str(e)[:180]))
                except Exception:
                    pass
                _reset_retry(numero)
            elif status_code in (400, 422):
                errors += 1
                try:
                    mark_to_monitor(invoice_id, "SFEC {}: {}".format(status_code, str(e)[:180]))
                except Exception:
                    pass
                _reset_retry(numero)
            else:
                errors += 1
                if _retry_attempts.get(numero, 0) >= 3:
                    try:
                        mark_to_monitor(invoice_id, str(e)[:200])
                    except Exception:
                        pass
                    _reset_retry(numero)
                else:
                    _schedule_retry(numero)
                    with _retry_lock:
                        item["attempts"] = item.get("attempts", 0) + 1
                        _retry_queue.append(item)

        time.sleep(0.1)

    _cache["retry_queue_count"] = len(_retry_queue)
    if success > 0 or errors > 0:
        logger.info("Retry queue termine: %d succes, %d erreurs", success, errors)


def sync_all():
    # Rejeu independant du pull : une panne de lecture Sage ne doit pas
    # bloquer l'ecriture des certifications deja obtenues.
    _flush_pending_writes()
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
            _cache["last_sync_at"] = utcnow_iso()
            _cache["sync_count"] += 1

            logger.info("Sync termine: %d factures vente, %d factures achat, %d contacts, %d taux TVA, %d comptes",
                        len(_cache["sales_invoices"]), len(_cache["purchase_invoices"]),
                        len(_cache["contacts"]), len(_cache["tax_rates"]), len(_cache["ledger_accounts"]))

        except Exception as e:
            _cache["db_connected"] = False
            _cache["last_error"] = str(e)
            logger.error("Erreur sync complet: %s", e)
            return False

    try:
        auto_certify_invoices()
    except Exception as e:
        logger.error("Erreur auto-certification: %s", e)

    if is_online():
        sync_sfec_invoices()
    else:
        logger.debug("Sync SFEC ignoree: pas de connexion internet")
    return True


def sync_sfec_invoices():
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not get_sfec_api_key(sfec_cfg):
        return

    if not is_online():
        logger.debug("Sync SFEC sautee: pas de connexion internet")
        return

    try:
        client = SfecClient()
        all_invoices = []
        page = 1
        page_size = 20
        max_pages = int(sfec_cfg.get("lookup_max_pages", 500) or 500)
        max_pages = max(1, min(max_pages, 5000))
        while page <= max_pages:
            result = client.list_invoices(page=page, page_size=page_size)
            batch = result.get("invoices", []) or []
            if not batch:
                break
            all_invoices.extend(batch)
            total_pages = int(result.get("totalPages") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
        else:
            logger.warning(
                "Sync SFEC: limite de %d pages atteinte, liste potentiellement "
                "incomplete (%d factures recuperees)", max_pages, len(all_invoices))
        with _lock:
            _cache["sfec_invoices"] = all_invoices
            _cache["certified_count"] = len(all_invoices)
            _cache["last_sfec_sync_at"] = utcnow_iso()
        logger.info("Sync SFEC: %d factures certifiees recuperees", len(all_invoices))
    except NetworkOfflineError:
        logger.info("Sync SFEC interrompue: connexion internet perdue")
    except Exception as e:
        logger.warning("Erreur sync SFEC: %s", e)


def sync_incremental():
    # Meme garantie que sync_all() : le rejeu ne depend pas du pull.
    _flush_pending_writes()
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
            _cache["last_sync_at"] = utcnow_iso()
            _cache["sync_count"] += 1

            logger.debug("Sync incrementale: +%d ventes, +%d achats",
                         len(new_sales), len(new_purchases))

        except Exception as e:
            _cache["db_connected"] = False
            _cache["last_error"] = str(e)
            logger.error("Erreur sync incrementale: %s", e)
            return False

    try:
        auto_certify_invoices()
    except Exception as e:
        logger.error("Erreur auto-certification (incrementale): %s", e)
    return True


def _build_sfec_lookup():
    global _sfec_lookup_cache, _sfec_lookup_ts
    now = time.time()
    if _sfec_lookup_cache and (now - _sfec_lookup_ts) < _sfec_lookup_ttl_s():
        return _sfec_lookup_cache

    lookup = {}
    if not is_online():
        return lookup
    try:
        cfg = get_config().get("sfec", {}) or {}
        max_pages = int(cfg.get("lookup_max_pages", 500) or 500)
        max_pages = max(1, min(max_pages, 5000))
        client = SfecClient()
        page = 1
        while page <= max_pages:
            result = client.list_invoices(page=page, page_size=20)
            invoices = result.get("invoices", [])
            if not invoices:
                break
            for inv in invoices:
                seller_num = str(inv.get("seller_invoice_number") or "").strip()
                inv_num = str(inv.get("invoice_number") or "").strip()
                for key in (seller_num, inv_num):
                    if key:
                        lookup[key] = inv
            total_pages = int(result.get("totalPages") or 0)
            if total_pages and page >= total_pages:
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


def _persist_local_certification(numero, cert_number, signature, qr_code,
                                  cert_date, sfec_id="", statut="CERTIFIE"):
    """Met a jour le miroir SQLite sans attendre le prochain pull Sage."""
    try:
        updated = update_invoice_sfec_by_numero(
            numero,
            sfec_id=sfec_id or "",
            certification_number=cert_number or "",
            signature=signature or "",
            qr_code=qr_code or "",
            certification_date=cert_date or "",
            statut=statut or "",
        )
        if not updated:
            logger.debug("Certification SFEC locale: facture %s absente du miroir",
                         numero)
    except Exception as ex:  # noqa: BLE001
        logger.warning("Mise a jour SQLite de la certification %s echouee: %s",
                       numero, ex)


def _certification_result_from_invoice(invoice):
    """Resultat uniforme pour une facture deja certifiee SFEC."""
    return {
        "success": True,
        "already_certified": True,
        "certification_number": invoice.get("sfec_num_certif", ""),
        "certification_date": invoice.get("sfec_date_certif", ""),
        "signature": invoice.get("sfec_signature", ""),
        "qr_code": invoice.get("sfec_qr_code", ""),
        "identifier": invoice.get("sfec_id", ""),
        "note": "Facture deja certifiee sur SFEC",
    }


def _handle_already_certified(invoice, sfec_lookup=None):
    """Ecrit dans Sage une certification deja existante cote SFEC.

    Retourne True uniquement si une preuve de certification a ete trouvee.
    Ne marque JAMAIS la facture DEJA_CERTIFIE sans numero/signature : cela
    la rendrait invisible alors que Sage n'aurait aucune preuve (audit
    2026-10-21 : 22 x 409 sans recuperation).
    """
    numero = invoice.get("numero", "")
    sfec_inv = None
    if sfec_lookup and numero in sfec_lookup:
        sfec_inv = sfec_lookup[numero]

    if not sfec_inv and numero:
        # Le cache vient d'etre contredit par un 409 (ou ne contenait pas la
        # facture) : il ne doit pas resservir une reponse perimee.
        _invalidate_sfec_lookup()
        try:
            client = SfecClient()
            sfec_inv = client.verify_by_invoice_number(numero)
        except Exception as ex:
            logger.warning("GET SFEC direct echoue pour %s: %s", numero, ex)

    if not sfec_inv:
        logger.warning(
            "Verification SFEC pour %s: aucune certification lisible, "
            "facture laissee en attente", numero,
        )
        return False

    # Persiste le numero SFEC d'origine (utile pour original_sfec_invoice_number
    # d'un avoir), sans le confondre avec la signature de certification.
    sfec_invoice_number = str(sfec_inv.get("invoice_number") or "").strip()
    if sfec_invoice_number:
        invoice["sfec_invoice_number"] = sfec_invoice_number
    sfec_id = str(sfec_inv.get("id") or sfec_inv.get("sfec_id") or "").strip()
    try:
        record_certified_invoice(invoice.get("id", ""), sfec_inv)
    except Exception as ex:  # noqa: BLE001
        logger.warning("Persistance certification SFEC %s echouee: %s", numero, ex)

    cert_number, sig, qr, cert_date = _extract_sfec_cert_data(sfec_inv)
    if not (cert_number or qr):
        logger.warning(
            "Facture %s presente sur SFEC mais sans donnee de certification "
            "exploitable: aucun marquage, nouvelle verification planifiee",
            numero,
        )
        return False

    with _lock:
        invoice["sfec_statut"] = "DEJA_CERTIFIE"
        invoice["sfec_num_certif"] = cert_number
        invoice["sfec_id"] = sfec_id
        invoice["sfec_signature"] = sig
        invoice["sfec_qr_code"] = qr
        invoice["sfec_date_certif"] = cert_date

    # Le miroir SQLite alimente /billing : il doit etre corrige tout de
    # suite, meme si l'ecriture Sage est differee.
    _persist_local_certification(
        numero, cert_number, sig, qr, cert_date,
        sfec_id=sfec_id, statut="DEJA_CERTIFIE",
    )

    try:
        write_sfec_to_invoice(
            invoice_id=invoice["id"],
            certification_number=cert_number,
            signature=sig,
            qr_code=qr,
            certification_date=cert_date,
        )
    except Exception as ex:  # noqa: BLE001
        # Sage est verrouille/indisponible : la certification est conservee
        # localement et sera rejouee au prochain cycle.
        if not _queue_pending_write(
                invoice, cert_number, sig, qr, cert_date, ex):
            return False
        return True

    logger.info("Certif SFEC recuperee et ecrite dans Sage: %s (certif: %s)",
                numero, cert_number)
    return True


def reconcile_sfec_status():
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not get_sfec_api_key(sfec_cfg):
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
            _reset_retry(numero)
            return ("skip", numero)

        mark_certifying(invoice["id"])

        result = certify_invoice(invoice)

        sfec_invoice_number = str(result.get("invoice_number") or "").strip()
        if sfec_invoice_number:
            invoice["sfec_invoice_number"] = sfec_invoice_number
        cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)
        sfec_id = str(result.get("sfec_id") or result.get("identifier") or "").strip()

        try:
            write_sfec_to_invoice(
                invoice_id=invoice["id"],
                certification_number=cert_number,
                signature=sig,
                qr_code=qr,
                certification_date=cert_date,
            )
        except Exception as ex:  # noqa: BLE001
            if not _queue_pending_write(
                    invoice, cert_number, sig, qr, cert_date, ex):
                raise
            _persist_local_certification(
                numero, cert_number, sig, qr, cert_date,
                sfec_id=sfec_id, statut="CERTIFIE",
            )
            with _lock:
                invoice["sfec_statut"] = "CERTIFIE"
                invoice["sfec_pending_sage"] = True
                invoice["sfec_num_certif"] = cert_number
                invoice["sfec_id"] = sfec_id
                invoice["sfec_signature"] = sig
                invoice["sfec_qr_code"] = qr
                invoice["sfec_date_certif"] = cert_date
            _reset_retry(numero)
            return ("queued", numero, cert_number)

        _persist_local_certification(
            numero, cert_number, sig, qr, cert_date,
            sfec_id=sfec_id, statut="CERTIFIE",
        )
        with _lock:
            invoice["sfec_statut"] = "CERTIFIE"
            invoice["sfec_num_certif"] = cert_number
            invoice["sfec_id"] = sfec_id
            invoice["sfec_signature"] = sig
            invoice["sfec_qr_code"] = qr
            invoice["sfec_date_certif"] = cert_date
        _reset_retry(numero)

        return ("ok", numero, cert_number)

    except NetworkOfflineError:
        _reset_retry(numero)
        _enqueue_for_retry(invoice)
        return ("offline", numero)

    except Exception as e:
        _log_certify_error(numero, e)
        status_code = getattr(e, "status_code", None)
        error_body = getattr(e, "error_body", "")
        overcredit = overcredit_details(e)
        if overcredit:
            try:
                mark_to_monitor(invoice["id"], overcredit_message(e))
            except Exception:
                pass
            _reset_retry(numero)
            return ("error_overcredit", numero)
        ref_conflict = on_account_reference_conflict(e)
        if ref_conflict:
            try:
                mark_to_monitor(
                    invoice["id"],
                    "Reference d'avoir sur compte deja utilisee: {}".format(
                        ref_conflict),
                )
            except Exception:
                pass
            _reset_retry(numero)
            return ("error_duplicate_reference", numero)
        if status_code == 409:
            _handle_already_certified(invoice, sfec_lookup)
            _reset_retry(numero)
            return ("skip", numero)
        elif status_code == 502:
            try:
                mark_to_monitor(invoice["id"], "SFEC 502: {}".format(str(e)[:180]))
            except Exception:
                pass
            _reset_retry(numero)
            return ("error_502", numero)
        elif status_code in (400, 422):
            # Rejet definitif : rejouer ne changera rien tant que la donnee
            # n'est pas corrigee. On sort la facture de la boucle de retry.
            try:
                mark_to_monitor(
                    invoice["id"],
                    "SFEC {}: {}".format(status_code, (error_body or str(e))[:180]),
                )
            except Exception:
                pass
            _reset_retry(numero)
            return ("error_reject", numero)
        else:
            if _retry_attempts.get(numero, 0) >= 3:
                try:
                    mark_to_monitor(invoice["id"], str(e)[:200])
                except Exception:
                    pass
                _reset_retry(numero)
            else:
                _schedule_retry(numero)
            return ("error", numero)


def auto_certify_invoices():
    # Empeche deux passes concurrentes : sync_all() relache _lock avant
    # d'appeler la certification, donc le scheduler pouvait demarrer une
    # nouvelle sync pendant qu'une certification tournait (audit 21/10 :
    # meme facture tentee 6 fois en 400 ms -> 409 en serie).
    if not _certify_run_lock.acquire(blocking=False):
        logger.info("Auto-certification deja en cours - passe ignoree")
        return
    try:
        _auto_certify_invoices_locked()
    finally:
        _certify_run_lock.release()


def _auto_certify_invoices_locked():
    global _startup_reset_done

    # Une certification peut avoir ete obtenue alors que Sage etait verrouille.
    # On rejoue ces ecritures avant toute nouvelle certification, meme si
    # SFEC est desactive ou internet indisponible : l'ecriture Sage ne depend
    # pas du reseau.
    _flush_pending_writes()

    config = get_config()
    sfec_cfg = config.get("sfec", {})

    if not sfec_cfg.get("enabled"):
        logger.debug("SFEC desactivee - auto-certification ignoree")
        return

    if not get_sfec_api_key(sfec_cfg):
        logger.debug("Cle API SFEC de l'environnement actif non configuree - "
                     "auto-certification ignoree")
        return

    if not is_online():
        logger.info("Pas de connexion internet - certification SFEC reportee")
        _queue_uncertified_for_retry()
        return

    if not _startup_reset_done:
        # Un EN_COURS ne peut provenir que d'un process interrompu : on le
        # libere une seule fois au demarrage, jamais a chaque passe.
        try:
            reset_stuck_en_cours()
        except Exception:
            pass
        _startup_reset_done = True

    with _lock:
        invoices_snapshot = list(_cache["sales_invoices"])

    sfec_lookup = _build_sfec_lookup()
    logger.info("Lookup SFEC: %d factures sur SFEC", len(sfec_lookup))

    to_certify = []
    sfec_matched = 0
    to_account = 0

    # Index O(1) : 1 facture->scan -> dict (construits une seule fois)
    manual_links = _manual_links()
    by_numero = {}
    for _x in invoices_snapshot:
        _num = _x.get("numero")
        if _num and _num not in by_numero:
            by_numero[_num] = _x  # comme next() : premiere occurrence gagne
    avoir_ids_par_ref = _build_credit_note_index(
        invoices_snapshot, by_numero, manual_links)

    # Motifs de non-certification, pour affichage sur la page /pending.
    pending_reasons = {}


    _seen_ids = set()
    _seen_numeros = set()
    for inv in invoices_snapshot:
        if inv.get("statut_code", 0) != 2:
            continue

        _id = inv.get("id")
        _num = inv.get("numero", "")
        if _id in _seen_ids:
            continue
        if _num and _num in _seen_numeros:
            continue
        _seen_ids.add(_id)
        _seen_numeros.add(_num)

        to_account += 1
        numero = inv.get("numero", "")
        sfec_statut = inv.get("sfec_statut", "")

        if numero in sfec_lookup:
            sfec_matched += 1
            if (sfec_statut not in ("DEJA_CERTIFIE", "CERTIFIE")
                    or _needs_certification_recovery(inv)):
                # Un A_SURVEILLER peut avoir ete certifie directement sur SFEC
                # (ou par une ancienne version du connecteur). On verifie donc
                # SFEC avant de le considerer comme definitivement en anomalie.
                try:
                    _handle_already_certified(inv, sfec_lookup)
                except Exception:
                    pass
            continue

        if sfec_statut in ("EN_COURS", "ERREUR", "A_SURVEILLER"):
            pending_reasons[_id] = "Statut SFEC : {}".format(sfec_statut)
            continue

        # Ne recertifier que si Sage contient deja une preuve. Un
        # DEJA_CERTIFIE sans SFEC_NUM_CERTIF doit repasser par la
        # recuperation/409 au lieu de rester invisible a vie.
        if (sfec_statut in ("CERTIFIE", "DEJA_CERTIFIE")
                and not _needs_certification_recovery(inv)):
            pending_reasons[_id] = "Déjà certifiée sur SFEC"
            continue

        if _has_zero_amount(inv):
            try:
                mark_to_monitor(inv["id"], "Montant nul: non certifiable")
            except Exception:
                pass
            continue

        if _is_credit_note(inv):
            state, ref, _parent, reason = _prepare_credit_note(
                inv, by_numero, manual_links, avoir_ids_par_ref)
            if state == "parent_pending":
                pending_reasons[_id] = (
                    "En attente de certification de la facture d'origine ('{}')"
                    .format(ref))
                logger.debug("Avoir %s en attente de certification de %s",
                             numero, ref)
                continue
            if state != "ok":
                if _should_mark_credit_failure(inv):
                    try:
                        mark_to_monitor(inv["id"], reason)
                    except Exception:
                        pass
                else:
                    pending_reasons[_id] = (
                        "Facture négative hors périmètre automatique : {}"
                        .format(reason))
                    logger.debug("Facture negative %s hors perimetre: %s",
                                 numero, reason)
                continue

        if not _should_retry(numero):
            pending_reasons[_id] = "Nouvelle tentative désactivée pour ce numéro"
            continue

        to_certify.append(inv)

    logger.info("Auto-certif: %d a comptabiliser, %d sur SFEC (OK), %d a certifier",
                to_account, sfec_matched, len(to_certify))

    # Persiste les motifs de non-certification pour affichage sur /pending.
    with _lock:
        _cache["pending_reasons"] = pending_reasons

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
                if status in ("ok", "queued"):
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
                if status in ("ok", "queued"):
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
            if inv.get("sfec_statut", "") not in (
                    "EN_COURS", "ERREUR", "A_SURVEILLER",
                    "CERTIFIE", "DEJA_CERTIFIE"):
                if inv["id"] not in existing_ids:
                    _enqueue_for_retry(inv)
                    queued += 1

    if queued > 0:
        logger.info("File d'attente: %d factures ajoutees (offline)", queued)


def certify_single(invoice_id):
    sfec_cfg = get_config().get("sfec", {})
    if not sfec_cfg.get("enabled") or not get_sfec_api_key(sfec_cfg):
        return {"success": False, "error": "SFEC non configuree"}

    invoice = None
    snapshot = []
    with _lock:
        snapshot = list(_cache["sales_invoices"])
        for inv in snapshot:
            if inv["id"] == invoice_id:
                invoice = inv
                break

    if not invoice:
        return {"success": False, "error": "Facture {} non trouvee".format(invoice_id)}

    numero = invoice.get("numero", "")

    if _has_certification_proof(invoice):
        return _certification_result_from_invoice(invoice)

    # Une facture peut avoir ete certifiee directement sur SFEC alors que le
    # miroir local est encore vide ou A_SURVEILLER. Ce controle doit passer
    # avant la validation du payload : des donnees Sage devenues invalides ne
    # doivent pas empecher la recuperation d'une certification existante.
    if is_online() and numero:
        existing = None
        try:
            existing = SfecClient().verify_by_invoice_number(numero)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Verification SFEC avant certification %s echouee: %s",
                           numero, ex)
        if existing:
            if _handle_already_certified(invoice, {numero: existing}):
                _reset_retry(numero)
                return _certification_result_from_invoice(invoice)
            return {
                "success": False,
                "pending": True,
                "error": "Facture presente sur SFEC mais certification illisible: "
                         "verification replanifiee",
            }

    if _has_zero_amount(invoice):
        try:
            mark_to_monitor(invoice_id, "Montant nul: non certifiable")
        except Exception:
            pass
        return {"success": False, "error": "Montant nul: non certifiable"}

    # Memes regles qu'en auto-certification pour les avoirs (DB-30).
    if _is_credit_note(invoice):
        manual_links = _manual_links()
        by_numero = {}
        for _x in snapshot:
            _num = _x.get("numero")
            if _num and _num not in by_numero:
                by_numero[_num] = _x
        credit_index = _build_credit_note_index(
            snapshot, by_numero, manual_links)
        state, ref, _parent, reason = _prepare_credit_note(
            invoice, by_numero, manual_links, credit_index)
        if state == "parent_pending":
            return {"success": False, "error": reason, "pending": True}
        if state != "ok":
            if _should_mark_credit_failure(invoice):
                try:
                    mark_to_monitor(invoice_id, reason)
                except Exception:
                    pass
            return {"success": False, "error": reason}

    if not is_online():
        _enqueue_for_retry(invoice)
        return {"success": False, "error": "Pas de connexion internet - facture mise en file d'attente",
                "queued": True}

    try:
        mark_certifying(invoice_id)
        result = certify_invoice(invoice)
        sfec_invoice_number = str(result.get("invoice_number") or "").strip()
        if sfec_invoice_number:
            invoice["sfec_invoice_number"] = sfec_invoice_number
        cert_number, sig, qr, cert_date = _extract_sfec_cert_data(result)
        sfec_id = str(result.get("sfec_id") or result.get("identifier") or "").strip()

        try:
            write_sfec_to_invoice(
                invoice_id=invoice_id,
                certification_number=cert_number,
                signature=sig,
                qr_code=qr,
                certification_date=cert_date,
            )
        except Exception as ex:  # noqa: BLE001
            if not _queue_pending_write(
                    invoice, cert_number, sig, qr, cert_date, ex):
                raise
            _persist_local_certification(
                numero, cert_number, sig, qr, cert_date,
                sfec_id=sfec_id, statut="CERTIFIE",
            )
            with _lock:
                invoice["sfec_statut"] = "CERTIFIE"
                invoice["sfec_pending_sage"] = True
                invoice["sfec_num_certif"] = cert_number
                invoice["sfec_id"] = sfec_id
                invoice["sfec_signature"] = sig
                invoice["sfec_qr_code"] = qr
                invoice["sfec_date_certif"] = cert_date
            _reset_retry(numero)
            return {
                "success": True,
                "queued": True,
                "certification_number": cert_number,
                "certification_date": cert_date,
                "signature": sig,
                "qr_code": qr,
                "identifier": sfec_id,
                "note": "Certification SFEC conservee, ecriture Sage en attente",
            }

        _persist_local_certification(
            numero, cert_number, sig, qr, cert_date,
            sfec_id=sfec_id, statut="CERTIFIE",
        )
        with _lock:
            invoice["sfec_statut"] = "CERTIFIE"
            invoice["sfec_num_certif"] = cert_number
            invoice["sfec_id"] = sfec_id
            invoice["sfec_signature"] = sig
            invoice["sfec_qr_code"] = qr
            invoice["sfec_date_certif"] = cert_date
        _reset_retry(numero)

        return {
            "success": True,
            "certification_number": cert_number,
            "certification_date": cert_date,
            "signature": sig,
            "qr_code": qr,
            "identifier": sfec_id,
        }
    except NetworkOfflineError:
        _reset_retry(numero)
        _enqueue_for_retry(invoice)
        return {"success": False, "error": "Pas de connexion internet - facture mise en file d'attente",
                "queued": True}
    except Exception as e:
        status_code = getattr(e, "status_code", None)
        overcredit = overcredit_details(e)
        if overcredit:
            try:
                mark_to_monitor(invoice_id, overcredit_message(e))
            except Exception:
                pass
            _reset_retry(numero)
            return {"success": False, "error": overcredit_message(e),
                    "overcredit": overcredit}
        ref_conflict = on_account_reference_conflict(e)
        if ref_conflict:
            message = "Reference d'avoir sur compte deja utilisee: {}".format(
                ref_conflict)
            try:
                mark_to_monitor(invoice_id, message)
            except Exception:
                pass
            _reset_retry(numero)
            return {"success": False, "error": message}
        if status_code == 409:
            # Ne jamais marquer DEJA_CERTIFIE sans preuve : on relit SFEC et
            # on n'ecrit dans Sage que si un numero/signature est disponible.
            recovered = _handle_already_certified(invoice)
            _reset_retry(numero)
            if recovered:
                return _certification_result_from_invoice(invoice)
            return {
                "success": False,
                "pending": True,
                "error": "Certification SFEC existante mais illisible: verification replanifiee",
            }
        if status_code == 502:
            try:
                mark_to_monitor(invoice_id, "SFEC 502: {}".format(str(e)[:180]))
            except Exception:
                pass
            _reset_retry(numero)
            return {"success": False, "error": "Certification recue mais non verifiee sur SFEC", "note": "EN_ATTENTE"}
        if status_code in (400, 422):
            try:
                mark_to_monitor(
                    invoice_id,
                    "SFEC {}: {}".format(status_code, str(e)[:180]),
                )
            except Exception:
                pass
            _reset_retry(numero)
            return {"success": False, "error": str(e)}
        if _retry_attempts.get(numero, 0) >= 3:
            try:
                mark_to_monitor(invoice_id, str(e)[:200])
            except Exception:
                pass
            _reset_retry(numero)
        else:
            _schedule_retry(numero)
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
    config = get_config()

    if not config.get("db", {}).get("polling_enabled", True):
        logger.info("Polling desactive dans la configuration")
        return

    current = polling_thread()
    if current is not None and current.is_alive():
        logger.warning("Polling deja en cours")
        return

    _stop_event.clear()
    thread = threading.Thread(target=_polling_loop, daemon=True, name="sfec-polling")
    set_polling_thread(thread)
    thread.start()
    logger.info("Thread de polling intelligent demarre")


def stop_polling():
    _stop_event.set()
    thread = polling_thread()
    if thread and thread.is_alive():
        thread.join(timeout=5)
    logger.info("Polling arrete")
    close_pool()


def apply_config():
    close_pool()
    logger.info("Configuration rechargee, pool DB ferme (reconnexion au prochain acces)")
    return {"ok": True, "message": "Configuration appliquee. Pool DB reinitialise."}


def mark_invoice_to_monitor(invoice_id, reason=""):
    """Marque a surveiller dans Sage ET dans le cache (sinon /pending reste inchange)."""
    mark_to_monitor(invoice_id, reason)
    with _lock:
        for inv in _cache["sales_invoices"]:
            if inv.get("id") == invoice_id:
                inv["sfec_statut"] = "A_SURVEILLER"
                break


def get_metrics():
    # DB-07 : utiliser le compteur maintenu par sync_sfec_invoices au lieu
    # de relire Sage a chaque affichage (la sync complete recalcule).
    real_certified = _cache["certified_count"]

    # DB-04 : pas de test reseau depuis une vue serveur ; le thread de fond
    # maintient l'etat de connectivite, get_status(refresh=False) est_cache.
    conn_status = get_status(refresh=False)
    retry_count = len(get_retry_queue())
    sales = _cache["sales_invoices"]
    sales_only = [i for i in sales if i.get("type_doc", "vente") != "avoir"]
    credit_notes = len(sales) - len(sales_only)
    pending_accounting = sum(1 for i in sales_only if i.get("statut_code") == 2)

    return {
        "sales_invoices": len(sales_only),
        "credit_notes": credit_notes,
        "pending_accounting": pending_accounting,
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
