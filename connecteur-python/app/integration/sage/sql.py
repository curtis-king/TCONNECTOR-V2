"""Identifiants SQL Server : validation puis quoting systématique.

Les noms de tables proviennent de `INFORMATION_SCHEMA` (source externe) et
sont interpolés dans des requêtes : ils ne doivent jamais l'être tels quels.
`quote_ident()` refuse tout identifiant hors `[A-Za-z_][A-Za-z0-9_]*`
(éventuellement `schema.table`) puis l'entoure de crochets.
"""
import re

from app.integration.sage.errors import SageSchemaError

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def quote_ident(name):
    """Valide puis bracket-quote un identifiant SQL Server.

    >>> quote_ident("F_DOCENTETE")
    '[F_DOCENTETE]'
    >>> quote_ident("dbo.F_DOCENTETE")
    '[dbo].[F_DOCENTETE]'
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise SageSchemaError("Identifiant SQL invalide: {!r}".format(name))
    return ".".join(
        "[{}]".format(part.replace("]", "]]")) for part in name.split(".")
    )
