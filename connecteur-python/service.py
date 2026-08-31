import os
import sys
import logging
import servicemanager
import win32serviceutil
import win32service
import win32event

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from config_manager import load_config
from database import ping_database, discover_tables, close_pool
from sync_engine import start_polling, stop_polling, sync_all
from sync_bidirectional import start_bi_sync, stop_bi_sync
from dashboard import app
from connectivity import start_background_check, stop_background_check

logger = logging.getLogger("t-connector.service")


class TConnectorService(win32serviceutil.ServiceFramework):
    _svc_name_ = "TConnectorSFEC"
    _svc_display_name_ = "T-CONNECTOR SFEC - Connecteur Facturation Electronique"
    _svc_description_ = (
        "Connecteur Python pour la certification SFEC (Systeme de Facturation "
        "Electronique Certifie) - Synchronise Sage 100 et certifie les factures."
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = False
        self.dashboard_thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logger.info("Arret du service T-CONNECTOR...")
        self.running = False
        win32event.SetEvent(self.stop_event)
        try:
            stop_polling()
        except Exception:
            pass
        try:
            stop_bi_sync()
        except Exception:
            pass
        try:
            stop_background_check()
        except Exception:
            pass
        try:
            close_pool()
        except Exception:
            pass
        logger.info("Service T-CONNECTOR arrete")

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logger.info("Demarrage du service T-CONNECTOR SFEC...")
        self.running = True
        self.main()

    def main(self):
        log_dir = os.path.join(SCRIPT_DIR, "data")
        os.makedirs(log_dir, exist_ok=True)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[
                logging.FileHandler(os.path.join(log_dir, "service.log"), encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )

        load_config()

        logger.info("Test de connexion a la base de donnees...")
        result = ping_database()
        if result.get("ok"):
            logger.info("Base de donnees connectee")
        else:
            logger.error("Echec connexion DB: %s", result.get("error"))

        discover_tables()

        logger.info("Demarrage du polling...")
        start_polling()
        _db_cfg = load_config().get("database", {})
        _interval = max(_db_cfg.get("polling_interval_ms", 30000) // 1000, 15)
        logger.info("Demarrage de la sync bidirectionnelle (intervalle: %ds)...", _interval)
        start_bi_sync(interval=_interval)
        start_background_check()

        cfg = load_config()
        dash_cfg = cfg.get("dashboard", {})
        dash_port = dash_cfg.get("port", 3000)
        dash_host = dash_cfg.get("host", "0.0.0.0")

        import threading
        def _serve():
            try:
                from waitress import serve
                serve(app, host=dash_host, port=dash_port, threads=8)
            except ImportError:
                app.run(host=dash_host, port=dash_port, use_reloader=False, debug=False)

        self.dashboard_thread = threading.Thread(
            target=_serve,
            daemon=True,
            name="sfec-dashboard",
        )
        self.dashboard_thread.start()
        logger.info("Dashboard demarre sur %s:%d", dash_host, dash_port)

        sync_all()

        while self.running:
            rc = win32event.WaitForSingleObject(self.stop_event, 5000)
            if rc == win32event.WAIT_OBJECT_0:
                break


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(TConnectorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(TConnectorService)
