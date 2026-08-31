import os
import sys
import logging
import signal
import threading
from logging.handlers import RotatingFileHandler

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from config_manager import load_config, get_config
from database import close_pool
from sync_engine import start_polling, stop_polling
from sync_bidirectional import start_bi_sync
from dashboard import app
from connectivity import check_now, start_background_check, stop_background_check

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
MAX_LOG_SIZE = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def setup_logging():
    log_dir = os.path.join(SCRIPT_DIR, "data")
    os.makedirs(log_dir, exist_ok=True)

    log_level = get_config().get("log_level", "info").upper()
    numeric_level = getattr(logging, log_level, logging.INFO)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "output.log"),
        maxBytes=MAX_LOG_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )

    stream_handler = logging.StreamHandler(sys.stdout)

    logging.basicConfig(level=numeric_level, format=LOG_FORMAT, handlers=[file_handler, stream_handler])

    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=MAX_LOG_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(error_handler)


def main():
    load_config()
    setup_logging()
    logger = logging.getLogger("t-connector.main")

    logger.info("=" * 60)
    logger.info("T-CONNECTOR SFEC - Demarrage")
    logger.info("=" * 60)

    try:
        import sqlite_db
        sqlite_db.init_database()
        logger.info("SQLite local initialise (data/tconnector.db)")
        try:
            import user_auth
            user_auth.ensure_schema()
            admin_id = user_auth.migrate_admin_from_config()
            if admin_id:
                logger.info("Compte administrateur initial cree (id=%s)", admin_id)
        except Exception as e:
            logger.warning("Init authentification: %s", e)
        try:
            import article_sync
            result = article_sync.sync_articles_from_sage()
            logger.info("Sync articles Sage au demarrage: %s", result)
        except Exception as e:
            logger.warning("Sync articles au demarrage: %s", e)
        try:
            import seed_data
            seed_data.seed_vendeurs()
            seed_data.seed_clients()
            import sqlite_db as _sdb
            with _sdb.get_cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM products")
                nb_articles = cur.fetchone()["c"]
            if not nb_articles:
                logger.warning("Aucun article disponible - seed de demonstration (fallback hors-ligne)")
                seed_data.seed_products()
        except Exception as e:
            logger.warning("Seed data: %s", e)
    except Exception as e:
        logger.error("Erreur init SQLite: %s", e)

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
        stop_polling()
        stop_background_check()
        close_pool()
        logger.info("T-CONNECTOR arrete proprement")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    def _background_init():
        logger.info("Demarrage du polling intelligent...")
        start_polling()
        _db_cfg = get_config().get("database", {})
        _interval = max(_db_cfg.get("polling_interval_ms", 30000) // 1000, 15)
        logger.info("Demarrage de la sync bidirectionnelle (intervalle: %ds)...", _interval)
        start_bi_sync(interval=_interval)

    init_thread = threading.Thread(target=_background_init, daemon=True, name="sfec-init")
    init_thread.start()

    from sync_engine import sync_sfec_invoices
    sfec_thread = threading.Thread(target=sync_sfec_invoices, daemon=True, name="sfec-cache-init")
    sfec_thread.start()

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
        stop_polling()
        stop_background_check()
        close_pool()
        logger.info("T-CONNECTOR arrete")
    except Exception as e:
        logger.error("Erreur fatale: %s", e)
        stop_polling()
        stop_background_check()
        close_pool()
        raise


if __name__ == "__main__":
    import subprocess
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
