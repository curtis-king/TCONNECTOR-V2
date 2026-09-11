import json
import logging
import requests
from app.config.manager import get_sfec_config
from app.sync.connectivity import is_online, check_now

logger = logging.getLogger("t-connector.sfec")


class NetworkOfflineError(Exception):
    def __init__(self, message="Pas de connexion internet disponible"):
        self.message = message
        self.status_code = 0
        super().__init__(self.message)


class SfecClient:
    def __init__(self):
        cfg = get_sfec_config()
        self.api_key = cfg.get("api_key", "")
        if cfg.get("use_sandbox"):
            self.base_url = cfg.get("sandbox_url", "https://sandbox.api.sfec.gouv.cg")
        else:
            self.base_url = cfg.get("base_url", "https://api.sfec.gouv.cg")
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
            raise exc

        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def certify(self, invoice_data):
        logger.info("SFEC: certification facture %s", invoice_data.get("invoice_id"))
        return self._request("POST", "/api/v1/invoices", invoice_data)

    def list_invoices(self, page=1, page_size=20, invoice_type=None, date_start=None, date_end=None):
        params = []
        params.append("page={}".format(page))
        params.append("pageSize={}".format(page_size))
        if invoice_type:
            params.append("invoice_type={}".format(invoice_type))
        if date_start:
            params.append("date_start={}".format(date_start))
        if date_end:
            params.append("date_end={}".format(date_end))
        query = "&".join(params)
        return self._request("GET", "/api/v1/invoices?{}".format(query))

    def get_invoice(self, sfec_id):
        return self._request("GET", "/api/v1/invoices/{}".format(sfec_id))

    def verify_certification(self, identifier):
        try:
            result = self.get_invoice(identifier)
            cert_num = result.get("certification_number", "") or result.get("short_signature", "")
            if cert_num:
                logger.info("SFEC verification OK: %s -> certif: %s", identifier, cert_num)
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
        try:
            page = 1
            while True:
                result = self.list_invoices(page=page, page_size=500)
                invoices = result.get("invoices", [])
                if not invoices:
                    break
                for inv in invoices:
                    seller_num = inv.get("seller_invoice_number", "")
                    inv_num = inv.get("invoice_number", "")
                    if seller_num == invoice_number or inv_num == invoice_number:
                        return inv
                total = result.get("total", result.get("total_count", 0))
                if page * 500 >= total:
                    break
                page += 1
            return None
        except Exception as e:
            if isinstance(e, NetworkOfflineError):
                return None
            logger.warning("SFEC verification par numero error pour %s: %s", invoice_number, e)
            return None

    def ping(self):
        try:
            if not is_online():
                return False
            self.list_invoices(page=1, page_size=1)
            return True
        except Exception:
            return False
