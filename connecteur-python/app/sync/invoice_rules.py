"""Regles metier factures et avoirs (fonctions pures ou presque).

Aucune dependance au cache du moteur : ces helpers prennent les donnees en
argument et retournent un resultat. Le seul acces externe est la lecture des
liens manuels (SQLite) et les verifications SFEC du parent d'un avoir.
"""
import logging

logger = logging.getLogger("t-connector.sync.rules")


def _dependency(name):
    """Resout une dependance d'E/S via `app.sync.engine`, a l'appel.

    `engine` importe ce module : un import direct figerait la reference au
    chargement et les substitutions de test (`engine.SfecClient`, etc.)
    n'auraient plus d'effet.
    """
    from app.sync import engine

    return getattr(engine, name)


def _as_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "oui", "yes", "on")
    return bool(value)


def _is_credit_note(invoice):
    """Vrai pour un avoir DO_Type=7 ou une piece a montants negatifs.

    Une facture d'avoir saisie en DO_Type=6 (montants negatifs) doit partir
    en creditNote SFEC, jamais en salesInvoice. Un avoir sur compte est
    explicitement marque.
    """
    return (
        invoice.get("type_doc") in ("avoir", "avoir_compte")
        or _as_bool(invoice.get("credit_note_on_account"))
        or _as_float(invoice.get("montant_ttc"), 0) < 0
    )


def _has_zero_amount(invoice):
    """Vrai uniquement si un montant TTC est present et nul.

    Ne pas confondre un champ absent (tests, objets partiels) avec une
    facture a 0 : sinon on marque A_SURVEILLER des factures valides.
    """
    value = invoice.get("montant_ttc")
    if value is None or value == "":
        return False
    return abs(_as_float(value, 0)) < 0.01


def _credit_label(invoice):
    if invoice.get("type_doc") != "avoir":
        return "Facture negative"
    sage_type = invoice.get("sage_type")
    try:
        sage_type = int(sage_type) if sage_type not in (None, "") else None
    except (TypeError, ValueError):
        sage_type = None
    if sage_type == 8:
        return "Retour"
    return "Avoir"


def _should_mark_credit_failure(invoice):
    """Seuls les vrais avoirs (DO_Type=7) ou factures negatives sont marques
    A_SURVEILLER en cas d'echec.

    Les 5 585 factures negatives saisies en DO_Type=6 restent hors perimetre
    automatique tant que le client n'a pas tranche (rapport 2026-10-21, §6.4).
    Les marquer toutes A_SURVEILLER noierait la liste des anomalies ; elles
    sont certifiees en creditNote des qu'un parent est resolu (lien manuel).
    """
    return invoice.get("type_doc") == "avoir" or _as_float(invoice.get("montant_ttc"), 0) < 0


def _manual_links():
    try:
        return _dependency("fetch_sfec_invoice_links")()
    except Exception:
        return {}


def _resolve_credit_parent(invoice, by_numero, manual_links):
    raw_ref = invoice.get("reference_invoice_id") or invoice.get("reference", "")
    return _dependency("resolve_invoice_number")(
        raw_ref, by_numero,
        manual_links=manual_links,
        child_numero=invoice.get("numero", ""),
    )


def _parent_sfec_number(parent, ref):
    """Numero de la facture d'origine tel que connu par SFEC.

    Ordre : champ memorise en memoire -> fichier sfec-certified.json ->
    GET SFEC -> numero Sage. Ne jamais utiliser la signature de certification.
    """
    numero = str(parent.get("sfec_invoice_number") or "").strip()
    if numero:
        return numero

    try:
        cert = _dependency("get_certified_by_sage_id")(parent.get("id"))
        if cert:
            numero = str(cert.get("invoice_number") or "").strip()
            if numero:
                return numero
    except Exception:
        pass

    try:
        data = _dependency("SfecClient")().verify_by_invoice_number(ref)
        numero = str((data or {}).get("invoice_number") or "").strip()
        if numero:
            return numero
    except Exception as ex:
        logger.debug("GET SFEC parent %s echoue: %s", ref, ex)

    return ""


def _prepare_credit_note(invoice, by_numero, manual_links, credit_index):
    """Resout et valide le parent d'un avoir avant envoi SFEC.

    Retourne (etat, ref, parent, motif). Etats :
      - no_ref        : reference libre non exploitable -> A_SURVEILLER
      - parent_absent : parent introuvable/non comptabilise -> A_SURVEILLER
      - parent_invalid: parent est lui-meme un avoir -> A_SURVEILLER
      - parent_pending: parent pas encore certifie -> ATTENDRE (aucun marquage)
      - duplicate     : un avoir certifie existe deja -> A_SURVEILLER
      - ok            : champs creditNote/creditNoteOnAccount renseignes

    Un avoir sans parent est un avoir sur compte (`creditNoteOnAccount`), pas
    un creditNote auto-reference. Un parent explicitement fourni mais
    introuvable reste `parent_absent` : on ne bascule jamais silencieusement
    vers un avoir sur compte.
    """
    label = _credit_label(invoice)
    child_numero = str(invoice.get("numero") or "").strip()
    raw_ref = invoice.get("reference_invoice_id") or invoice.get("reference", "")

    def _is_duplicate():
        ids_child = (credit_index or {}).get(child_numero, [])
        return any(_i != invoice.get("id") for _i in ids_child)

    def _mark_on_account(reference):
        invoice["credit_note_on_account"] = True
        invoice["reference_invoice_id"] = reference
        invoice["external_credit_note_number"] = ""
        invoice["original_sfec_invoice_number"] = ""
        logger.info("Avoir %s sur compte (reference %s)", child_numero, reference)
        return ("ok", reference, None, "")

    on_account = (
        _as_bool(invoice.get("credit_note_on_account"))
        or invoice.get("type_doc") == "avoir_compte"
    )
    if on_account:
        reference = str(raw_ref or child_numero or invoice.get("id") or "").strip()
        if not reference:
            return ("no_ref", "", None,
                    "{}: reference d'avoir sur compte vide".format(label))
        if _is_duplicate():
            return ("duplicate", reference, None,
                    "Un avoir/retour certifie existe deja pour '{}'".format(
                        child_numero))
        return _mark_on_account(reference)

    ref = _resolve_credit_parent(invoice, by_numero, manual_links)

    if not ref:
        # Aucun parent : c'est un avoir sur compte. SFEC exige seulement une
        # reference unique et ne cherche pas de facture parente.
        if _is_credit_note(invoice):
            reference = str(child_numero or invoice.get("id") or raw_ref or "").strip()
            if not reference:
                return ("no_ref", "", None,
                        "{}: reference d'avoir sur compte vide".format(label))
            if _is_duplicate():
                return ("duplicate", reference, None,
                        "Un avoir/retour certifie existe deja pour '{}'".format(
                            child_numero))
            return _mark_on_account(reference)
        return ("no_ref", "", None,
                "{}: reference d'origine introuvable ({!r})".format(label, raw_ref))

    parent = by_numero.get(ref)
    manual_parent = str((manual_links or {}).get(child_numero) or "").strip()

    # Lien manuel vers un parent plus ancien que la fenetre du cache : on
    # verifie son existence/certification directement sur SFEC.
    if not parent and manual_parent == ref:
        try:
            sfec_parent = _dependency("SfecClient")().verify_by_invoice_number(ref)
        except Exception as ex:
            logger.debug("GET SFEC parent manuel %s echoue: %s", ref, ex)
            sfec_parent = None
        if sfec_parent:
            parent_type = str(sfec_parent.get("invoice_type") or "").lower()
            if parent_type.replace("_", "") == "creditnote":
                return ("parent_invalid", ref, None,
                        "{}: la facture d'origine '{}' est elle-meme un avoir".format(
                            label, ref))
            if _is_duplicate():
                return ("duplicate", ref, None,
                        "Un avoir/retour certifie existe deja pour '{}'".format(
                            child_numero))
            invoice["credit_note_on_account"] = False
            invoice["reference_invoice_id"] = ref
            invoice["external_credit_note_number"] = child_numero
            invoice["original_sfec_invoice_number"] = (
                str(sfec_parent.get("invoice_number") or "").strip() or ref
            )
            logger.info("Avoir %s rattache manuellement a %s (SFEC %s)",
                        child_numero, ref,
                        invoice["original_sfec_invoice_number"])
            return ("ok", ref, None, "")

    if not parent or int(_as_float(parent.get("statut_code"), 0)) != 2:
        return ("parent_absent", ref, parent,
                "{}: facture d'origine '{}' absente".format(label, ref))

    if _is_credit_note(parent):
        return ("parent_invalid", ref, parent,
                "{}: la facture d'origine '{}' est elle-meme un avoir".format(label, ref))

    if parent.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
        # Le parent n'est pas encore certifie : on ATTEND. Ne pas marquer
        # ERREUR (rejeu infini) ni A_SURVEILLER (le cas se resout seul).
        return ("parent_pending", ref, parent,
                "{}: facture d'origine '{}' non certifiee (en attente)".format(label, ref))

    if _is_duplicate():
        return ("duplicate", ref, parent,
                "Un avoir/retour certifie existe deja pour '{}'".format(child_numero))

    # Champs exiges par l'OpenAPI SFEC pour un creditNote.
    invoice["credit_note_on_account"] = False
    invoice["reference_invoice_id"] = ref
    invoice["external_credit_note_number"] = str(invoice.get("numero") or "")
    invoice["original_sfec_invoice_number"] = _parent_sfec_number(parent, ref) or ref
    logger.info("Avoir %s rattache a %s (SFEC %s)",
                invoice.get("numero", ""), ref,
                invoice["original_sfec_invoice_number"])
    return ("ok", ref, parent, "")


def _build_credit_note_index(snapshot, by_numero, manual_links):
    """Index des avoirs/retours deja certifies, par NUMERO de piece.

    L'index est cle par le numero de l'avoir/retour (et non par la facture
    parente) : plusieurs retours partiels sur une meme facture d'origine sont
    donc autorises. Seule la réédition du meme numero est bloquée comme
    doublon.
    """
    index = {}
    for item in snapshot:
        if not _is_credit_note(item):
            continue
        if item.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
            continue
        child_numero = str(item.get("numero") or "").strip()
        if child_numero:
            index.setdefault(child_numero, []).append(item.get("id"))
    return index
