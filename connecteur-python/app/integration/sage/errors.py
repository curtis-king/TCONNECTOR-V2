"""Taxonomie d'erreurs de l'adaptateur Sage.

Les classes sont introduites progressivement : le code existant continue de
lever `pyodbc.Error` ou `ValueError`, et les marqueurs texte restent reconnus
par `_is_transient()` pendant la transition. Les nouveaux chemins doivent
lever une de ces classes.
"""


class SageError(Exception):
    """Erreur de l'adaptateur Sage (base de la taxonomie)."""


class SageSchemaError(SageError, ValueError):
    """Table ou colonne Sage inutilisable (nom invalide, schéma inattendu)."""


class PermanentSfecWriteError(ValueError):
    """Ecriture SFEC impossible et non rejouable.

    Distincte d'un verrou ou d'une coupure reseau : rejouer ne changera rien
    (facture absente de Sage, identifiant invalide, table introuvable). La
    file `sfec_pending_writes` conserve la ligne pour l'audit mais cesse de
    la rejouer afin de ne pas boucler indefiniment.
    """
