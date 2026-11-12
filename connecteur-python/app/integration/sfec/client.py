import json
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import requests

from app.config.manager import get_sfec_config, get_sfec_credentials
from app.sync.connectivity import is_online, check_now

logger = logging.getLogger("t-connector.sfec")

_VERIFY_RETRY_DELAYS = (2, 5)


def _retry_after_seconds(exc):
    """Retourne le delai Retry-After en secondes, si present et exploitable."""
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _retry_delay(exc, attempt):
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(retry_after, 60.0)
    return _VERIFY_RETRY_DELAYS[min(attempt, len(_VERIFY_RETRY_DELAYS) - 1)]


def _is_retryable_verify_error(exc):
    """Erreurs qui justifient de relire SFEC (reseau, timeout, 429, 5xx)."""
    if isinstance(exc, NetworkOfflineError):
        return True
    try:
        status_code = int(getattr(exc, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if 500 <= status_code < 600 or status_code == 429:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "timeout", "timed out", "connexion", "connection", "reseau", "network",
    ))


class NetworkOfflineError(Exception):
    def __init__(self, message="Pas de connexion internet disponible"):
        self.message = message
        self.status_code = 0
        super().__init__(self.message)


class SfecClient:
    def __init__(self):
        cfg = get_sfec_config()
        credentials = get_sfec_credentials(cfg)
        self.environment = credentials["environment"]
        self.api_key = credentials["api_key"]
        self.base_url = credentials["base_url"]
        self.timeout = 30
        try:
            self.timeout = int(cfg.get("timeout", 30) or 30)
        except (TypeError, ValueError):
            self.timeout = 30

    def _check_connectivity(self):
        online = is_online()
        if not online:
            result = check_now()
            if not result:
                raise NetworkOfflineError(
                    "Pas de connexion internet. La certification SFEC sera "
                    "mise en file d'attente et reprise automatiquement "
                    "lorsque la connexion sera restauree."
                )
        return True

    def _request(self, method, path, body=None):
        self._check_connectivity()

        url = "{}{}".format(self.base_url, path)
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        logger.info("SFEC %s %s", method, url)

        if body:
            safe_keys = ("invoice_id", "invoice_type", "recipient_type", "total_amount", "items")
            summary = {k: v for k, v in body.items() if k in safe_keys}
            summary["items_count"] = len(body.get("items", []))
            if "items" in summary:
                del summary["items"]
            logger.debug("SFEC payload: %s", json.dumps(summary, ensure_ascii=False))

        try:
            if method == "POST":
                resp = requests.post(url, json=body, headers=headers, timeout=self.timeout)
            elif method == "GET":
                resp = requests.get(url, headers=headers, timeout=self.timeout)
            else:
                raise ValueError("Method HTTP non supportee: {}".format(method))
        except requests.exceptions.Timeout:
            raise Exception("SFEC API: timeout apres {}s".format(self.timeout))
        except requests.exceptions.ConnectionError as e:
            logger.warning("SFEC: erreur connexion reseau, verification internet...")
            if not is_online():
                raise NetworkOfflineError("Connexion internet perdue pendant la requete SFEC")
            raise Exception("SFEC API: erreur de connexion - {}".format(e))
        except requests.exceptions.RequestException as e:
            if not is_online():
                raise NetworkOfflineError("Connexion internet perdue pendant la requete SFEC")
            raise Exception("SFEC API: erreur reseau - {}".format(e))

        if not resp.ok:
            error_body = resp.text
            logger.error("SFEC API error %s: %s", resp.status_code, error_body)
            exc = Exception("SFEC API {}: {}".format(resp.status_code, error_body))
            exc.status_code = resp.status_code
            exc.error_body = error_body
            try:
                error_json = resp.json()
            except Exception:
                error_json = None
            exc.error_json = error_json if isinstance(error_json, dict) else {}
            exc.error_message = (
                exc.error_json.get("error")
                or exc.error_json.get("message")
                or error_body
            )
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None:
                exc.retry_after = retry_after
            raise exc

        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def certify(self, invoice_data):
        logger.info("SFEC: certification facture %s", invoice_data.get("invoice_id"))
        return self._request("POST", "/api/v1/invoices", invoice_data)

    def list_invoices(self, page=1, page_size=20, invoice_type=None, date_start=None, date_end=None):
        params = {"page": page, "pageSize": page_size}
        if invoice_type:
            params["invoice_type"] = invoice_type
        if date_start:
            params["date_start"] = date_start
        if date_end:
            params["date_end"] = date_end
        return self._request("GET", "/api/v1/invoices?{}".format(urlencode(params)))

    def get_invoice(self, sfec_id):
        value = str(sfec_id or "").strip()
        if not value:
            raise ValueError("sfec_id requis")
        return self._request(
            "GET", "/api/v1/invoices/{}".format(quote(value, safe=""))
        )

    def verify_certification(self, identifier):
        """Vrai si le GET expose une preuve de certification.

        Le detail SFEC expose `certification_short_signature` et
        `certification_signature` ; il n'expose pas `certification_number`
        ni `short_signature`.
        """
        try:
            result = self.get_invoice(identifier)
            cert = (
                result.get("certification_short_signature")
                or result.get("certification_signature")
                or result.get("certification_status") == "certified"
            )
            if cert:
                logger.info("SFEC verification OK: %s certifiee", identifier)
                return True
            logger.info("SFEC verification: pas encore de certif pour %s (en attente)", identifier)
            return False
        except Exception as e:
            if isinstance(e, NetworkOfflineError):
                return False
            status_code = getattr(e, "status_code", None)
            if status_code == 404:
                logger.warning("SFEC verification FAILED: facture %s non trouvee (404)", identifier)
                return False
            logger.warning("SFEC verification error pour %s: %s", identifier, e)
            return False

    def verify_by_invoice_number(self, invoice_number):
        """Retrouve une facture SFEC par numero Sage/SFEC.

        Le endpoint de detail accepte directement un UUID ou un
        `invoice_number` : pas besoin de parcourir la liste paginee.

        3 tentatives (2 s puis 5 s, ou Retry-After) sur erreur reseau,
        timeout, 429 et 5xx. Un 404 signifie que la certification n'existe
        pas encore : retour immediat a None, sans retry. Apres epuisement,
        None est retourne pour que l'appelant NE marque PAS la facture
        comme certifiee.
        """
        invoice_number = str(invoice_number or "").strip()
        if not invoice_number:
            return None

        for attempt in range(3):
            try:
                return self.get_invoice(invoice_number)
            except Exception as e:  # noqa: BLE001
                try:
                    status_code = int(getattr(e, "status_code", 0) or 0)
                except (TypeError, ValueError):
                    status_code = 0
                if status_code == 404:
                    logger.warning(
                        "SFEC verification: facture %s non trouvee (404)",
                        invoice_number,
                    )
                    return None
                if status_code in (400, 422):
                    logger.warning(
                        "SFEC verification: facture %s refusee (%s)",
                        invoice_number, status_code,
                    )
                    return None
                if not _is_retryable_verify_error(e) or attempt >= 2:
                    logger.warning(
                        "SFEC verification par numero error pour %s: %s",
                        invoice_number, e,
                    )
                    return None
                delay = _retry_delay(e, attempt)
                logger.warning(
                    "SFEC verification %s: %s - retry dans %ss",
                    invoice_number, e, delay,
                )
                time.sleep(delay)
        return None

    def ping(self):
        try:
            if not is_online():
                return False
            self.list_invoices(page=1, page_size=1)
            return True
        except Exception:
            return False
