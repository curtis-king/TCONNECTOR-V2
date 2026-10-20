"""Tests du bootstrap : check unique SumatraPDF, versioning, download."""
import os
import zipfile

import pytest

from app.core import bootstrap


def _no_system_sumatra(monkeypatch):
    """Neutralise la recherche candidats système (vraie SumatraPDF presente)."""
    import app.print_hijack.auto_printer as ap
    monkeypatch.setattr(ap, "resolve_sumatra_path", lambda configured: None)


def _isolate_config(monkeypatch):
    """Empeche _check_sumatra d'ecrire dans le vrai config.json."""
    import app.config.manager as cm
    monkeypatch.setattr(cm, "update_section", lambda s, d: d)


def test_compare_versions():
    assert bootstrap._compare_versions("3.5.2", "3.5.2") == 0
    assert bootstrap._compare_versions("v3.5.2", "3.5.2") == 0
    assert bootstrap._compare_versions("3.5.2", "3.4.0") == 1
    assert bootstrap._compare_versions("3.4", "3.5.2") == -1
    assert bootstrap._compare_versions("3.5.2-64", "3.5.2") == 0
    assert bootstrap._compare_versions("10.0", "9.9.9") == 1
    assert bootstrap._compare_versions("", "1.0") == -1


def test_parse_release_tag():
    assert bootstrap._parse_release_tag("v3.5.2") == "3.5.2"
    assert bootstrap._parse_release_tag("3.5.2") == "3.5.2"


def test_report_always_has_sumatra_key(monkeypatch):
    monkeypatch.setattr(bootstrap, "_check_sumatra",
                        lambda cfg: {"status": "ok", "path": "x", "version": "1",
                                     "action": "none", "error": ""})
    report = bootstrap.ensure_runtime_dependencies(config={})
    assert "sumatra" in report
    assert report["sumatra"]["status"] == "ok"


def test_check_error_is_not_fatal(monkeypatch):
    def _boom(cfg):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(bootstrap, "_check_sumatra", _boom)
    report = bootstrap.ensure_runtime_dependencies(config={})
    assert report["sumatra"]["status"] == "error"


def test_offline_when_absent(monkeypatch, tmp_path):
    _no_system_sumatra(monkeypatch)
    monkeypatch.setattr(bootstrap, "bootstrap_bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path",
                        lambda: str(tmp_path / "SumatraPDF.exe"))
    monkeypatch.setattr(bootstrap, "_read_installed_version", lambda: None)
    monkeypatch.setattr(bootstrap, "_fetch_latest_release",
                        lambda: (_ for _ in ()).throw(OSError("no network")))
    cfg = {"print_hijack": {"sumatra_path": "", "sumatra_min_version": "3.5.2",
                            "auto_install_sumatra": True}}
    r = bootstrap._check_sumatra(cfg)
    assert r["status"] == "offline"
    assert r["path"] is None


def test_ok_when_present_and_version_ok(monkeypatch, tmp_path):
    _no_system_sumatra(monkeypatch)
    exe = tmp_path / "SumatraPDF.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path", lambda: str(exe))
    monkeypatch.setattr(bootstrap, "_read_installed_version", lambda: "3.5.2")
    cfg = {"print_hijack": {"sumatra_path": str(exe), "sumatra_min_version": "3.5.2",
                            "auto_install_sumatra": True}}
    r = bootstrap._check_sumatra(cfg)
    assert r["status"] == "ok"
    assert r["action"] == "none"


def test_skipped_when_auto_install_disabled(monkeypatch, tmp_path):
    _no_system_sumatra(monkeypatch)
    monkeypatch.setattr(bootstrap, "bootstrap_bin_dir", lambda: str(tmp_path))
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path",
                        lambda: str(tmp_path / "SumatraPDF.exe"))
    monkeypatch.setattr(bootstrap, "_read_installed_version", lambda: None)
    calls = []

    def _fetch():
        calls.append(1)
        return "3.5.2", "http://x/SumatraPDF-3.5.2-64.zip"
    monkeypatch.setattr(bootstrap, "_fetch_latest_release", _fetch)
    cfg = {"print_hijack": {"sumatra_path": "", "sumatra_min_version": "3.5.2",
                            "auto_install_sumatra": False}}
    r = bootstrap._check_sumatra(cfg)
    assert r["status"] == "skipped"
    assert not calls


def test_install_from_mocked_release(monkeypatch, tmp_path):
    """Absent -> download du zip mocké -> exe extrait + version.txt."""
    _no_system_sumatra(monkeypatch)
    _isolate_config(monkeypatch)
    bin_dir = tmp_path / "bin"
    exe_path = bin_dir / "SumatraPDF.exe"

    monkeypatch.setattr(bootstrap, "bootstrap_bin_dir", lambda: str(bin_dir))
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path", lambda: str(exe_path))
    monkeypatch.setattr(bootstrap, "_read_installed_version", lambda: None)
    monkeypatch.setattr(bootstrap, "_fetch_latest_release",
                        lambda: ("3.5.2", "http://mock/SumatraPDF-3.5.2-64.zip"))

    # Zip factice contenant SumatraPDF.exe
    import io

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self._payload), chunk_size):
                yield self._payload[i:i + chunk_size]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SumatraPDF-3.5.2-64/SumatraPDF.exe", b"MZ-fake-exe")
    payload = buf.getvalue()

    import requests as _requests
    monkeypatch.setattr(_requests, "get",
                        lambda *a, **k: _FakeResp(payload))

    cfg = {"print_hijack": {"sumatra_path": "", "sumatra_min_version": "3.5.2",
                            "auto_install_sumatra": True}}
    r = bootstrap._check_sumatra(cfg)
    assert r["status"] == "ok"
    assert r["action"] == "install"
    assert r["version"] == "3.5.2"
    assert os.path.exists(str(exe_path))
    assert (bin_dir / "version.txt").read_text(encoding="utf-8") == "3.5.2"
    assert open(str(exe_path), "rb").read() == b"MZ-fake-exe"


def test_update_when_too_old(monkeypatch, tmp_path):
    """Exe présent mais version < min -> update."""
    _no_system_sumatra(monkeypatch)
    _isolate_config(monkeypatch)
    bin_dir = tmp_path / "bin"
    exe_path = bin_dir / "SumatraPDF.exe"
    bin_dir.mkdir()
    exe_path.write_bytes(b"MZ-old")
    (bin_dir / "version.txt").write_text("3.4.0", encoding="utf-8")

    monkeypatch.setattr(bootstrap, "bootstrap_bin_dir", lambda: str(bin_dir))
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path", lambda: str(exe_path))
    monkeypatch.setattr(bootstrap, "_read_installed_version", lambda: "3.4.0")
    monkeypatch.setattr(bootstrap, "_fetch_latest_release",
                        lambda: ("3.5.2", "http://mock/SumatraPDF-3.5.2-64.zip"))

    import io

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self._payload), chunk_size):
                yield self._payload[i:i + chunk_size]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SumatraPDF.exe", b"MZ-new-exe")
    import requests as _requests
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _FakeResp(buf.getvalue()))

    cfg = {"print_hijack": {"sumatra_path": str(exe_path),
                            "sumatra_min_version": "3.5.2",
                            "auto_install_sumatra": True}}
    r = bootstrap._check_sumatra(cfg)
    assert r["status"] == "ok"
    assert r["action"] == "update"
    assert r["version"] == "3.5.2"
    assert open(str(exe_path), "rb").read() == b"MZ-new-exe"


def test_zip_slip_rejected(monkeypatch, tmp_path):
    """Un zip sans SumatraPDF.exe (ex: path malveillant) est rejeté."""
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(bootstrap, "bootstrap_bin_dir", lambda: str(bin_dir))
    monkeypatch.setattr(bootstrap, "bootstrap_sumatra_path",
                        lambda: str(bin_dir / "SumatraPDF.exe"))

    import io
    import requests as _requests

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=65536):
            yield self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.exe", b"x")
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _FakeResp(buf.getvalue()))

    with pytest.raises(RuntimeError, match="absent du zip"):
        bootstrap._download_and_extract("http://x", str(bin_dir), "3.5.2")
