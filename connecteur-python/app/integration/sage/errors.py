"""Taxonomie d'erreurs de l'adaptateur Sage.

Une erreur = une classe = une politique de reprise. La conversion est
progressive : le code existant continue de lever `pyodbc.Error` ou
`ValueError`, et `classify_sage_error()` reconnaît les marqueurs texte pendant
la transition. Les nouveaux chemins doivent lever une de ces classes.
"""

# Marqueurs ODBC / SQL Server. L'ordre d'évaluation compte : un verrou peut
# arriver avec le code 42000, il ne doit pas être classé « schéma ».
LOCKED_MARKERS = (
    "en cours d'utilisation", "80003", "deadlock", "deadlock victim",
    "locked", "verrou",
)
UNAVAILABLE_MARKERS = (
    "timeout expired", "hyt00", "08001", "08s01",
    "communication link failure", "server is unavailable",
    "configuration base de donnees incomplete",
)
SCHEMA_MARKERS = (
    "invalid column name", "invalid object name", "42000",
    "column name or number of supplied values",
)


class SageError(Exception):
    """Erreur de l'adaptateur Sage (base de la taxonomie)."""


class SageUnavailableError(SageError):
    """Connexion impossible ou coupée (timeout, réseau, pool épuisé)."""


class SageLockedError(SageError):
    """Ressource Sage verrouillée (80003, « en cours d'utilisation »)."""


class SageSchemaError(SageError, ValueError):
    """Table ou colonne Sage inutilisable (nom invalide, schéma inattendu)."""


class SageWriteConflictError(SageError):
    """Écriture refusée sans erreur technique (0 ligne impactée)."""


class PermanentSfecWriteError(SageWriteConflictError, ValueError):
    """Ecriture SFEC impossible et non rejouable.

    Distincte d'un verrou ou d'une coupure reseau : rejouer ne changera rien
    (facture absente de Sage, identifiant invalide, table introuvable). La
    file `sfec_pending_writes` conserve la ligne pour l'audit mais cesse de
    la rejouer afin de ne pas boucler indefiniment.
    """


def _has_marker(message, markers):
    return any(marker in message for marker in markers)


def classify_sage_error(exc):
    """Retourne une erreur typée pour `exc`, ou `exc` si inconnue.

    Les erreurs déjà typées sont renvoyées telles quelles. Un message
    inconnu reste inchangé : la classification est progressive, on ne
    transforme pas une erreur métier en erreur de transport.
    """
    if isinstance(exc, SageError):
        return exc
    message = str(exc or "").lower()
    if not message:
        return exc
    if _has_marker(message, LOCKED_MARKERS):
        return SageLockedError(str(exc))
    if _has_marker(message, UNAVAILABLE_MARKERS):
        return SageUnavailableError(str(exc))
    if _has_marker(message, SCHEMA_MARKERS):
        return SageSchemaError(str(exc))
    return exc


def is_transient(exc):
    """Vrai si l'erreur justifie une nouvelle tentative courte.

    Un verrou ou une indisponibilité sont transitoires ; un problème de
    schéma ne l'est pas. Les marqueurs texte restent reconnus pour les
    erreurs pyodbc non encore converties.
    """
    if isinstance(exc, (SageUnavailableError, SageLockedError)):
        return True
    if isinstance(exc, (SageSchemaError, SageWriteConflictError)):
        return False
    typed = classify_sage_error(exc)
    if typed is not exc:
        return isinstance(typed, (SageUnavailableError, SageLockedError))
    message = str(exc or "").lower()
    return (isinstance(exc, ValueError) and
            _has_marker(message, ("configuration base de donnees incomplete",)))
