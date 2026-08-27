import socket
import time
import threading
import logging
from datetime import datetime

logger = logging.getLogger("t-connector.connectivity")

_STATUS = "UNKNOWN"
_last_check = 0
_last_online_time = None
_last_offline_time = None
_offline_since = None
_lock = threading.RLock()

_CACHE_TTL = 10
_DNS_HOSTS = ["1.1.1.1", "8.8.8.8", "208.67.222.222"]
_HTTP_URLS = [
    ("https://1.1.1.1", 3),
    ("https://www.google.com", 3),
    ("https://api.sfec.gouv.cg", 5),
]


def _check_dns():
    for host in _DNS_HOSTS:
        try:
            sock = socket.create_connection((host, 53), timeout=2)
            sock.close()
            return True
        except (socket.timeout, OSError):
            continue
    return False


def _check_http():
    try:
        import requests
    except ImportError:
        return _check_dns()

    for url, timeout in _HTTP_URLS:
        try:
            resp = requests.get(url, timeout=timeout, allow_redirects=False)
            if resp.status_code < 500:
                return True
        except Exception:
            continue
    return False


def _do_check():
    global _STATUS, _last_check, _last_online_time, _last_offline_time, _offline_since

    dns_ok = _check_dns()
    http_ok = _check_http() if dns_ok else False
    online = dns_ok and http_ok
    now = time.time()

    with _lock:
        _last_check = now
        old_status = _STATUS

        if online:
            _STATUS = "ONLINE"
            _last_online_time = datetime.utcnow().isoformat() + "Z"
            if old_status == "OFFLINE" and _offline_since:
                duration = now - _offline_since
                logger.info("Internet RECONNECTE (offline pendant %.1fs)", duration)
                _offline_since = None
            elif old_status == "UNKNOWN":
                logger.info("Internet: EN LIGNE")
        else:
            _STATUS = "OFFLINE"
            _last_offline_time = datetime.utcnow().isoformat() + "Z"
            if _offline_since is None:
                _offline_since = now
                logger.warning("Internet: HORS LIGNE")
            elif old_status == "UNKNOWN":
                logger.warning("Internet: HORS LIGNE")

    return online


def check_now():
    return _do_check()


def is_online():
    with _lock:
        elapsed = time.time() - _last_check
        if elapsed < _CACHE_TTL:
            return _STATUS == "ONLINE"

    return _do_check()


def get_status():
    with _lock:
        elapsed = time.time() - _last_check
        if elapsed >= _CACHE_TTL:
            with _lock:
                pass
            _do_check()

    with _lock:
        offline_duration = None
        if _offline_since:
            offline_duration = round(time.time() - _offline_since, 0)

        return {
            "status": _STATUS,
            "online": _STATUS == "ONLINE",
            "last_check": datetime.fromtimestamp(_last_check).isoformat() if _last_check else None,
            "last_online": _last_online_time,
            "last_offline": _last_offline_time,
            "offline_duration_seconds": offline_duration,
        }


def wait_for_online(timeout=300, interval=5):
    start = time.time()
    while time.time() - start < timeout:
        if is_online():
            return True
        time.sleep(interval)
    return False


_background_thread = None
_stop_background = threading.Event()


def _background_loop():
    logger.info("Verification internet en arriere-plan demarree (toutes les 30s)")
    while not _stop_background.is_set():
        _do_check()
        _stop_background.wait(30)


def start_background_check():
    global _background_thread
    if _background_thread and _background_thread.is_alive():
        return
    _stop_background.clear()
    _background_thread = threading.Thread(target=_background_loop, daemon=True, name="connectivity-check")
    _background_thread.start()


def stop_background_check():
    _stop_background.set()
    if _background_thread and _background_thread.is_alive():
        _background_thread.join(timeout=3)
