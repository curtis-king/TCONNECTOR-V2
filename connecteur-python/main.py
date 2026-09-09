import os
import sys
import logging
import signal
import threading
import subprocess

from app.core.paths import base_dir, data_dir
from app.core.logging_setup import setup_logging
from app.config.manager import load_config, get_config
from app.integration.sage.database import close_pool
from app.storage.db import init_database, get_cursor
from app.sync.connectivity import check_now, start_background_check, stop_background_check
from app.sync.scheduler import start_all, stop_all
from app.web.app import create_app

SCRIPT_DIR = base_dir()
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

app = create_app()


def _init_sqlite_and_seed(logger):
    try:
        init_database()
        logger.info("SQLite local initialise (data/tconnector.db)")
        try:
            from app.web.auth.user_auth import ensure_schema, migrate_admin_from_config
            ensure_schema()
            admin_id = migrate_admin_from_config()
            if admin_id:
                logger.info("Compte administrateur initial cree (id=%s)", admin_id)
        except Exception as e:
            logger.warning("Init authentification: %s", e)
        try:
            from app.sync.article_sync import sync_articles_from_sage
            result = sync_articles_from_sage()
            logger.info("Sync articles Sage au demarrage: %s", result)
        except Exception as e:
            logger.warning("Sync articles au demarrage: %s", e)
        try:
            from app.domain.seed import seed_vendeurs, seed_clients, seed_products
            seed_vendeurs()
            seed_clients()
            with get_cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM products")
                nb_articles = cur.fetchone()["c"]
            if not nb_articles:
                logger.warning("Aucun article disponible - seed de demonstration (fallback hors-ligne)")
                seed_products()
        except Exception as e:
            logger.warning("Seed data: %s", e)
    except Exception as e:
        logger.error("Erreur init SQLite: %s", e)


def main():
    load_config()
    setup_logging(log_level=get_config().get("log_level", "info"))
    logger = logging.getLogger("t-connector.main")

    logger.info("=" * 60)
    logger.info("T-CONNECTOR SFEC - Demarrage")
    logger.info("=" * 60)

    _init_sqlite_and_seed(logger)

    logger.info("Verification de la connectivite internet...")
    online = check_now()
    if online:
        logger.info("Internet: EN LIGNE")
    else:
        logger.warning("Internet: HORS LIGNE - le systeme fonctionnera en mode local")

    start_background_check()

    cfg = get_config()
    dash_cfg = cfg.get("dashboard", {})
    dash_port = dash_cfg.get("port", 3000)
    dash_host = dash_cfg.get("host", "0.0.0.0")

    def shutdown_handler(signum, frame):
        logger.info("Signal recu (%s) - Arret en cours...", signum)
        stop_all()
        close_pool()
        logger.info("T-CONNECTOR arrete proprement")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    init_thread = threading.Thread(target=start_all, daemon=True, name="sfec-init")
    init_thread.start()

    logger.info("Demarrage du dashboard sur %s:%d", dash_host, dash_port)

    try:
        try:
            from waitress import serve
            logger.info("Utilisation de waitress comme serveur WSGI")
            serve(app, host=dash_host, port=dash_port, threads=8)
        except ImportError:
            logger.warning("waitress non installe - utilisation du serveur Flask (dev)")
            app.run(host=dash_host, port=dash_port, use_reloader=False, debug=False, threaded=True)
    except KeyboardInterrupt:
        stop_all()
        close_pool()
        logger.info("T-CONNECTOR arrete")
    except Exception as e:
        logger.error("Erreur fatale: %s", e)
        stop_all()
        close_pool()
        raise


if __name__ == "__main__":
    if getattr(sys, 'frozen', False) and len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "--install-service":
            subprocess.run(["sc", "create", "TConnectorSFEC",
                           "binPath=", '"{}"'.format(sys.executable),
                           "start=", "auto",
                           "DisplayName=", "T-CONNECTOR SFEC"], check=False)
            print("Service installe.")
            sys.exit(0)
        elif cmd == "--uninstall-service":
            subprocess.run(["net", "stop", "TConnectorSFEC"], check=False)
            subprocess.run(["sc", "delete", "TConnectorSFEC"], check=False)
            print("Service desinstalle.")
            sys.exit(0)
        elif cmd == "--start-service":
            subprocess.run(["net", "start", "TConnectorSFEC"], check=False)
            print("Service demarre.")
            sys.exit(0)
        elif cmd == "--stop-service":
            subprocess.run(["net", "stop", "TConnectorSFEC"], check=False)
            print("Service arrete.")
            sys.exit(0)
    main()