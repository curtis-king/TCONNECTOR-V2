"""Tests du bootstrap : verification des dependances au démarrage.

Version allégée après suppression du module print_hijack (watcher PDF,
impression silencieuse SumatraPDF, multi-sites).
"""
import pytest

from app.core import bootstrap


def test_ensure_runtime_dependencies_returns_empty_report():
    """Aucune dépendance externe n'est vérifiée : rapport vide."""
    report = bootstrap.ensure_runtime_dependencies(config={})
    assert report == {}
