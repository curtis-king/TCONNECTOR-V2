"""Blueprints Flask — handlers de routes par domaine.

7 blueprints : auth, dashboard, config, billing, pos, directory, sync_api.
Ils importent uniquement des modules sans cycle (app.web.auth.security pour
la sécurité, app.web.common pour les helpers partagés) et sont enregistrés
par la factory app.web.app.create_app().
"""
