"""Sélection stricte des identifiants SFEC sandbox / production."""
import copy

from app.config.manager import (
    get_config,
    get_sfec_credentials,
    load_config,
    save_config,
)
from app.integration.sfec import client as sfec_client


SANDBOX_CFG = {
    "enabled": True,
    "use_sandbox": True,
    "api_key": "prod-key",
    "sandbox_api_key": "sandbox-key",
    "base_url": "https://api.example.test",
    "sandbox_url": "https://sandbox.example.test",
}

PROD_CFG = {
    "enabled": True,
    "use_sandbox": False,
    "api_key": "prod-key",
    "sandbox_api_key": "sandbox-key",
    "base_url": "https://api.example.test",
    "sandbox_url": "https://sandbox.example.test",
}


def test_credentials_sandbox_utilise_la_cle_sandbox():
    creds = get_sfec_credentials(SANDBOX_CFG)

    assert creds == {
        "environment": "sandbox",
        "base_url": "https://sandbox.example.test",
        "api_key": "sandbox-key",
    }


def test_credentials_production_utilise_la_cle_production():
    creds = get_sfec_credentials(PROD_CFG)

    assert creds == {
        "environment": "production",
        "base_url": "https://api.example.test",
        "api_key": "prod-key",
    }


def test_credentials_sandbox_ne_replie_jamais_sur_la_cle_prod():
    cfg = dict(SANDBOX_CFG)
    cfg["sandbox_api_key"] = ""

    creds = get_sfec_credentials(cfg)

    assert creds["api_key"] == ""
    assert creds["environment"] == "sandbox"


def test_credentials_normalise_url_et_valeurs_booleennes():
    cfg = dict(PROD_CFG)
    cfg["use_sandbox"] = "false"
    cfg["base_url"] = "https://api.example.test/"

    creds = get_sfec_credentials(cfg)

    assert creds["environment"] == "production"
    assert creds["base_url"] == "https://api.example.test"


def test_sfec_client_utilise_les_identifiants_sandbox(monkeypatch):
    monkeypatch.setattr(
        sfec_client, "get_sfec_config", lambda: dict(SANDBOX_CFG))

    client = sfec_client.SfecClient()

    assert client.environment == "sandbox"
    assert client.base_url == "https://sandbox.example.test"
    assert client.api_key == "sandbox-key"


def test_sfec_client_utilise_les_identifiants_production(monkeypatch):
    monkeypatch.setattr(
        sfec_client, "get_sfec_config", lambda: dict(PROD_CFG))

    client = sfec_client.SfecClient()

    assert client.environment == "production"
    assert client.base_url == "https://api.example.test"
    assert client.api_key == "prod-key"


def test_env_sfec_prioritaire_et_distincte(monkeypatch):
    monkeypatch.setenv("SFEC_API_KEY", "env-prod")
    monkeypatch.setenv("SFEC_SANDBOX_API_KEY", "env-sandbox")
    try:
        cfg = load_config()
        assert cfg["sfec"]["api_key"] == "env-prod"
        assert cfg["sfec"]["sandbox_api_key"] == "env-sandbox"

        monkeypatch.delenv("SFEC_API_KEY")
        monkeypatch.delenv("SFEC_SANDBOX_API_KEY")
        load_config()
    finally:
        # Restaure l'état de config chargé pour les tests suivants.
        monkeypatch.delenv("SFEC_API_KEY", raising=False)
        monkeypatch.delenv("SFEC_SANDBOX_API_KEY", raising=False)
        load_config()


def test_api_config_sfec_sauvegarde_et_masque_les_deux_cles(client):
    from app.config.manager import get_config as current_config

    original = copy.deepcopy(current_config().get("sfec", {}))
    token = client.get("/api/csrf").get_json()["token"]
    try:
        response = client.post(
            "/api/config/sfec",
            json={
                "api_key": "route-prod-key",
                "sandbox_api_key": "route-sandbox-key",
                "use_sandbox": True,
            },
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200, response.get_json()

        cfg = current_config()
        assert cfg["sfec"]["api_key"] == "route-prod-key"
        assert cfg["sfec"]["sandbox_api_key"] == "route-sandbox-key"

        body = client.get("/api/config").get_json()
        assert body["sfec"]["api_key"] == "***"
        assert body["sfec"]["sandbox_api_key"] == "***"

        exported = client.get("/api/config/export")
        assert exported.status_code == 200
        import json
        exported_body = json.loads(exported.get_data(as_text=True))
        assert exported_body["sfec"]["api_key"] == "***"
        assert exported_body["sfec"]["sandbox_api_key"] == "***"

        page = client.get("/config").get_data(as_text=True)
        assert 'name="sandbox_api_key"' in page
        assert "route-sandbox-key" not in page
    finally:
        cfg = current_config()
        cfg["sfec"] = original
        save_config(cfg)


def test_api_config_sfec_ne_clear_pas_une_cle_avec_vide(client):
    from app.config.manager import get_config as current_config

    original = copy.deepcopy(current_config().get("sfec", {}))
    token = client.get("/api/csrf").get_json()["token"]
    try:
        cfg = current_config()
        cfg.setdefault("sfec", {})["sandbox_api_key"] = "a-conserver"
        save_config(cfg)

        response = client.post(
            "/api/config/sfec",
            json={"sandbox_api_key": ""},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200, response.get_json()
        assert current_config()["sfec"]["sandbox_api_key"] == "a-conserver"
    finally:
        cfg = current_config()
        cfg["sfec"] = original
        save_config(cfg)
