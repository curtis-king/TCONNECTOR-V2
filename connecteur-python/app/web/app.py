"""Fabrique d'application Flask (application factory).

Étape A7 : create_app() construit l'instance Flask de zéro — plus d'instance
globale à l'import. La sécurité est branchée via app.web.auth.security.
init_security(app), les helpers partagés vivent dans app.web.common, et les 7
blueprints (app/web/routes/) sont enregistrés ici. Importer ce module ne
construit aucune application ; c'est main.py (ou les tests) qui appelle
create_app().

Résolution du cycle d'import historique (constaté en A6) : les modules routes
n'importent QUE des modules sans cycle (app.web.auth.security, app.web.common),
jamais la factory ni un module qui la contient.
"""
import os

from flask import Flask

from app.web.auth.security import _get_secret_key, init_security


def create_app():
    """Construit et configure l'application Flask T-CONNECTOR."""
    web_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(web_dir, "templates"),
        static_folder=os.path.join(web_dir, "static"),
    )
    app.secret_key = _get_secret_key()

    init_security(app)

    from app.web.routes import auth as _rt_auth
    from app.web.routes import billing as _rt_billing
    from app.web.routes import config as _rt_config
    from app.web.routes import dashboard as _rt_dashboard_pages
    from app.web.routes import directory as _rt_directory
    from app.web.routes import pos as _rt_pos
    from app.web.routes import sync_api as _rt_sync_api

    app.register_blueprint(_rt_config.bp)  # domaine configuration (/config, /api/config*, /api/db, /api/sfec, /api/tables, /api/tax-rates)
    app.register_blueprint(_rt_auth.bp)  # auth (/login, /logout, /compte/mot-de-passe, /api/compte/password)
    app.register_blueprint(_rt_dashboard_pages.bp)  # dashboard (/ /invoices /pending /certified /certified/<id>/print /sales /health /ready)
    app.register_blueprint(_rt_sync_api.bp)  # sync_api (/api/sync*, /api/connectivity, /api/metrics, /api/certified*, /api/ledger-accounts, /api/retry-queue, /api/auth*, /api/audit, /api/csrf)
    app.register_blueprint(_rt_billing.bp)    # facturation (/billing*, /api/invoices*)
    app.register_blueprint(_rt_pos.bp)        # point de vente (/pos*, /api/products*, /api/pos*)
    app.register_blueprint(_rt_directory.bp)  # annuaire (/clients, /vendeurs, /utilisateurs, /api/contacts*, /api/vendeurs*, /api/utilisateurs*)

    return app
