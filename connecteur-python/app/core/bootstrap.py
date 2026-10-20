"""Bootstrap runtime : check unique des dépendances externes au démarrage.

Vérifie en un seul passage la présence et la version de SumatraPDF, le
télécharge depuis les releases officielles GitHub (zip portable) si absent
ou trop ancien, et logge un rapport unique. Jamais bloquant : hors-ligne
ou échec réseau -> warning, le connecteur démarre quand même.
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from app.core.paths import data_dir

logger = logging.getLogger("t-connector.bootstrap")

_SUMATRA_GITHUB_LATEST = (
    "https://api.github.com/repos/sumatrapdfreader/sumatrapdf/releases/latest"
)
_DOWNLOAD_TIMEOUT_S = 30
_MAX_DOWNLOAD_MB = 100


def bootstrap_bin_dir():
    return os.path.join(data_dir(), "bin", "SumatraPDF")


def bootstrap_sumatra_path():
    return os.path.join(bootstrap_bin_dir(), "SumatraPDF.exe")


def _version_file():
    return os.path.join(bootstrap_bin_dir(), "version.txt")


def _compare_versions(a, b):
    """Compare '3.5.2' / 'v3.5.2-64' -> -1, 0, 1 (a vs b). Tolérant suffixes."""
    def _key(v):
        nums = re.findall(r"\d+", str(v or ""))
        return tuple(int(n) for n in nums[:3]) if nums else (0,)
    ka, kb = _key(a), _key(b)
    n = max(len(ka), len(kb))
    ka = ka + (0,) * (n - len(ka))
    kb = kb + (0,) * (n - len(kb))
    return (ka > kb) - (ka < kb)


def _read_installed_version():
    """Version depuis version.txt (install bootstrap) puis FileVersionInfo."""
    try:
        with open(_version_file(), "r", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    exe = bootstrap_sumatra_path()
    if os.path.exists(exe):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-Item '{}').VersionInfo.ProductVersion".format(exe)],
                capture_output=True, timeout=10,
            )
            v = (out.stdout or b"").decode("utf-8", "replace").strip()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
    return None


def _parse_release_tag(tag):
    """'v3.5.2' -> '3.5.2'."""
    return re.sub(r"^v", "", str(tag or "").strip())


def _fetch_latest_release():
    """Retourne (version, zip_url) ou (None, None) si indisponible."""
    import requests
    resp = requests.get(_SUMATRA_GITHUB_LATEST, timeout=_DOWNLOAD_TIMEOUT_S,
                        headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    data = resp.json()
    version = _parse_release_tag(data.get("tag_name"))
    zip_url = None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if re.match(r"^SumatraPDF-{}-64\.zip$".format(re.escape(version)), name):
            zip_url = asset.get("browser_download_url")
            break
    if not zip_url:
        logger.warning("Asset zip portable introuvable dans la release %s", version)
    return version, zip_url


def _download_and_extract(zip_url, dest_dir, version):
    """Télécharge le zip portable, extrait SumatraPDF.exe (anti zip-slip)."""
    import requests
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as tmp:
            with requests.get(zip_url, stream=True, timeout=_DOWNLOAD_TIMEOUT_S) as r:
                r.raise_for_status()
                size = 0
                for chunk in r.iter_content(chunk_size=65536):
                    tmp.write(chunk)
                    size += len(chunk)
                    if size > _MAX_DOWNLOAD_MB * 1024 * 1024:
                        raise RuntimeError(
                            "telechargement trop volumineux (> {} Mo)".format(_MAX_DOWNLOAD_MB))
        with zipfile.ZipFile(tmp_path) as zf:
            exe_members = [
                n for n in zf.namelist()
                if os.path.basename(n).lower() == "sumatrapdf.exe"
                and ".." not in n.split("/")
            ]
            if not exe_members:
                raise RuntimeError("SumatraPDF.exe absent du zip")
            target = bootstrap_sumatra_path()
            with zf.open(exe_members[0]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        with open(_version_file(), "w", encoding="utf-8") as f:
            f.write(version)
        return target
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _check_sumatra(cfg):
    """Check unique SumatraPDF : présence + version + download si besoin."""
    ph = cfg.get("print_hijack", {})
    min_version = str(ph.get("sumatra_min_version") or "").strip()
    auto_install = bool(ph.get("auto_install_sumatra", True))
    configured = ph.get("sumatra_path") or ""

    from app.print_hijack.auto_printer import resolve_sumatra_path

    local_exe = bootstrap_sumatra_path()
    exe = resolve_sumatra_path(configured)
    if not exe and os.path.exists(local_exe):
        exe = local_exe

    installed_version = _read_installed_version() if exe else None

    def _result(status, path=None, version=None, action="none", error=""):
        return {"status": status, "path": path, "version": version,
                "action": action, "error": error}

    if exe and (not min_version or not installed_version
                or _compare_versions(installed_version, min_version) >= 0):
        return _result("ok", path=exe, version=installed_version)

    if not auto_install:
        return _result("skipped", path=exe, version=installed_version,
                       error="auto_install_sumatra=false")

    try:
        version, zip_url = _fetch_latest_release()
    except Exception as e:  # noqa: BLE001
        if exe:
            return _result("offline", path=exe, version=installed_version,
                           error="release GitHub indisponible: {}".format(e))
        return _result("offline", error="hors ligne et SumatraPDF absent: {}".format(e))

    if not zip_url:
        if exe:
            return _result("ok", path=exe, version=installed_version,
                           error="asset zip introuvable")
        return _result("error", error="asset zip introuvable")

    # Present mais trop ancien : met a jour. Absent : installe.
    action = "update" if exe else "install"
    if exe and installed_version and _compare_versions(version, installed_version) <= 0:
        return _result("ok", path=exe, version=installed_version)

    try:
        target = _download_and_extract(zip_url, bootstrap_bin_dir(), version)
    except Exception as e:  # noqa: BLE001
        if exe:
            return _result("offline", path=exe, version=installed_version,
                           error="echec telechargement: {}".format(e))
        return _result("error", error="echec telechargement: {}".format(e))

    logger.info("[BOOTSTRAP] SumatraPDF %s %s -> %s", version, action, target)
    if os.path.abspath(target) != os.path.abspath(configured or ""):
        try:
            from app.config.manager import get_config, update_section
            ph = dict((get_config() or {}).get("print_hijack") or {})
            ph["sumatra_path"] = target
            update_section("print_hijack", ph)
        except Exception:  # noqa: BLE001
            pass
    return _result("ok", path=target, version=version, action=action)


def ensure_runtime_dependencies(config=None):
    """Check unique des dépendances externes. Retourne le rapport.

    Un seul point d'appel au démarrage (main.py) : tout est centralisé ici.
    """
    if config is None:
        from app.config.manager import get_config
        config = get_config() or {}
    report = {"sumatra": None}
    try:
        report["sumatra"] = _check_sumatra(config)
    except Exception as e:  # noqa: BLE001
        logger.warning("[BOOTSTRAP] check sumatra en echec: %s", e)
        report["sumatra"] = {"status": "error", "path": None, "version": None,
                             "action": "none", "error": str(e)}
    s = report["sumatra"]
    logger.info("[BOOTSTRAP] Rapport dependances: sumatra=%s version=%s action=%s",
                s.get("status"), s.get("version") or "?", s.get("action"))
    return report
