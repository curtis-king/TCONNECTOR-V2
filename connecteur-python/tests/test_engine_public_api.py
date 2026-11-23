"""Contrat d'API publique de `app.sync.engine` (façade de compatibilité).

`engine.py` reste le point d'entrée historique : ses fonctions d'orchestration
sont consommées par les routes web, le service Windows et les tests. Après le
découpage (LOT 1), l'état et les règles vivent dans `state`, `retry`,
`invoice_rules` et `sfec_errors`, mais `engine` doit continuer d'exposer les
mêmes noms. Ce test verrouille ce contrat.
"""
import pytest

from app.sync import engine


# Consommé par app/web/routes/*.py, app/core/health.py et app/service/*.
API_PUBLIQUE = [
    "sync_all",
    "sync_incremental",
    "sync_sfec_invoices",
    "certify_single",
    "apply_config",
    "flush_pending_writes",
    "mark_invoice_to_monitor",
    "get_cache",
    "get_metrics",
    "get_retry_queue",
    "polling_active",
    "start_polling",
    "stop_polling",
]

# État et helpers ré-exportés pour les tests et les modules voisins.
ETAT_ET_HELPERS = [
    "_cache",
    "_lock",
    "_retry_after",
    "_retry_attempts",
    "_logged_errors",
    "_retry_queue",
    "_should_retry",
    "_schedule_retry",
    "_reset_retry",
    "_has_certification_proof",
    "_needs_certification_recovery",
    "_sfec_error_json",
    "overcredit_details",
    "overcredit_message",
    "on_account_reference_conflict",
    "_invalidate_sfec_lookup",
    "_build_sfec_lookup",
    "_pending_write_done",
    "_flush_pending_writes",
    "_certify_single_worker",
    "_auto_certify_invoices_locked",
    "auto_certify_invoices",
    "_prepare_credit_note",
    "_build_credit_note_index",
    "_log_certify_error",
]


@pytest.mark.parametrize("name", API_PUBLIQUE + ETAT_ET_HELPERS)
def test_symbole_expose(name):
    assert hasattr(engine, name), f"engine.{name} doit rester exposé"


def test_etat_partage_est_le_meme_objet_que_state():
    from app.sync import state

    assert engine._cache is state._cache
    assert engine._retry_attempts is state._retry_attempts
    assert engine._retry_after is state._retry_after
    assert engine._logged_errors is state._logged_errors


def test_retry_reutilise_l_etat_partage():
    """`retry` et `engine` doivent voir les mêmes dictionnaires de backoff."""
    from app.sync import retry

    engine._retry_attempts.clear()
    engine._retry_after.clear()

    retry._schedule_retry("CONTRAT-1")
    assert engine._retry_attempts.get("CONTRAT-1") == 1
    assert not engine._should_retry("CONTRAT-1")

    retry._reset_retry("CONTRAT-1")
    assert "CONTRAT-1" not in engine._retry_attempts
    assert engine._should_retry("CONTRAT-1")
