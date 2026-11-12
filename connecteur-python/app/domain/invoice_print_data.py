"""Donnees d'impression communes aux PDF et a la page navigateur.

La base SQLite ne conserve pas toujours les lignes Sage ni les informations
client d'une facture issue de Sage. Ce module construit une copie enrichie de
la facture uniquement pour l'affichage : aucune donnee SQLite ou Sage n'est
modifiee.
"""
import json
import logging

logger = logging.getLogger(__name__)


def _as_float(value, default=0.0):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).replace("%", "").replace(",", ".").strip())
        except (TypeError, ValueError):
            return default


def normalize_print_line(line):
    """Uniformise les noms de champs Sage / SQLite pour l'impression."""
    if not isinstance(line, dict):
        return {}
    normalized = dict(line)
    normalized["code_article"] = (
        line.get("code_article")
        or line.get("code_produit")
        or line.get("AR_Ref")
        or ""
    )
    normalized["designation"] = (
        line.get("designation")
        or line.get("description")
        or line.get("article_design")
        or ""
    )
    normalized["unite"] = line.get("unite") or line.get("article_unit") or ""
    return normalized


def sfec_invoice_items(inv):
    """Retourne les lignes SFEC, y compris le format `items_json` du GET."""
    if not isinstance(inv, dict):
        return []
    items = inv.get("items")
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict)]

    raw = inv.get("items_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def sfec_item_ht(item):
    """Montant HT d'une ligne SFEC, sans confondre `net_amount` avec le TTC."""
    if not isinstance(item, dict):
        return None
    after_discount = _as_float(item.get("amount_after_discount"), None)
    if after_discount is not None:
        return after_discount

    subtotal = _as_float(item.get("subtotal"), None)
    net = _as_float(item.get("net_amount"), None)
    tax = _as_float(item.get("tax_amount"), 0.0) or 0.0
    if subtotal is not None and net is not None:
        # Les payloads recents exposent net_amount = HT + TVA. Les anciens
        # payloads peuvent exposer net_amount = HT apres remise.
        if net >= subtotal + tax - 0.01:
            return subtotal
        return net
    if subtotal is not None:
        return subtotal
    if net is not None:
        return net - tax
    return None


def fetch_sage_lines(numero, type_doc="vente", domaine=None, sage_type=None,
                     fetcher=None):
    """Lignes Sage d'une piece absente de SQLite, normalisees pour l'impression."""
    if not numero:
        return []
    try:
        if fetcher is None:
            from app.integration.sage.database import fetch_doc_lines
            fetcher = fetch_doc_lines

        cfg = None
        if sage_type not in (None, ""):
            types = [int(sage_type)]
        else:
            from app.integration.sage.database import get_domain_config
            cfg = get_domain_config()
            if type_doc == "avoir":
                types = [cfg["avoir_type"], cfg["retour_type"], cfg["facture_type"]]
            else:
                types = [cfg["facture_type"], cfg["avoir_type"]]

        if domaine not in (None, ""):
            dom = int(domaine)
        else:
            if cfg is None:
                from app.integration.sage.database import get_domain_config
                cfg = get_domain_config()
            dom = cfg["vente_domain"]

        for doc_type in types:
            lignes = fetcher(dom, doc_type, numero)
            if lignes:
                return [normalize_print_line(line) for line in lignes]
    except Exception as e:  # noqa: BLE001
        logger.warning("Lignes Sage indisponibles pour %s: %s", numero, e)
    return []


def _fetch_sfec_detail(inv, client=None):
    sfec_id = str((inv or {}).get("sfec_id") or "").strip()
    if not sfec_id:
        return None
    try:
        if client is None:
            from app.integration.sfec.client import SfecClient
            client = SfecClient()
        return client.get_invoice(sfec_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Detail SFEC indisponible pour %s: %s", sfec_id, e)
        return None


def _fetch_contact(code, fetcher=None):
    code = str(code or "").strip()
    if not code:
        return {}
    try:
        if fetcher is None:
            from app.integration.sage.database import fetch_contact_by_code
            fetcher = fetch_contact_by_code
        return fetcher(code) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Contact Sage indisponible pour %s: %s", code, e)
        return {}


def _merge_print_lines(items, lignes):
    merged = []
    for i in range(max(len(items), len(lignes))):
        it = items[i] if i < len(items) else {}
        lg = lignes[i] if i < len(lignes) else {}
        if not it and not lg:
            continue

        qte = _as_float(
            it.get("quantity") if it.get("quantity") is not None else lg.get("quantite"),
            1.0)
        pu = _as_float(
            it.get("unit_price") if it.get("unit_price") is not None
            else lg.get("prix_unitaire"), 0.0)
        taux = _as_float(
            it.get("tax_rate") if it.get("tax_rate") not in (None, "")
            else lg.get("taux_tva"), 18.0)
        montant_ht = sfec_item_ht(it)
        if montant_ht is None:
            montant_ht = _as_float(lg.get("montant_ht"), 0.0)
        montant_tva = _as_float(lg.get("montant_tva"), None)
        if montant_tva is None:
            montant_tva = _as_float(it.get("tax_amount"), 0.0)
        montant_ttc = _as_float(lg.get("montant_ttc"), None)
        if montant_ttc is None:
            montant_ttc = _as_float(it.get("total_amount"), montant_ht + montant_tva)

        remise = _as_float(it.get("discount_amount"), None)
        if remise is None:
            remise = _as_float(lg.get("remise_montant"), 0.0)
        brut_ligne = _as_float(it.get("subtotal"), None)
        if brut_ligne is None:
            brut_ligne = _as_float(lg.get("subtotal"), None)
        if brut_ligne is None:
            brut_ligne = _as_float(lg.get("montant_ht"), 0.0) or qte * pu
        remise_pct = _as_float(lg.get("remise_pct"), 0.0)
        if not remise_pct and brut_ligne and remise:
            remise_pct = remise / brut_ligne * 100

        designation = (
            it.get("designation")
            or it.get("description")
            or it.get("article_design")
            or lg.get("designation")
            or lg.get("description")
            or lg.get("article_design")
            or ""
        )
        reference = (
            lg.get("code_article")
            or lg.get("code_produit")
            or it.get("code_article")
            or it.get("code_produit")
            or it.get("article_code")
            or it.get("reference")
            or ""
        )
        unite = (
            lg.get("unite")
            or lg.get("article_unit")
            or it.get("unite")
            or it.get("unit")
            or ""
        )

        line = dict(lg)
        line.update({
            "code_article": reference,
            "designation": designation,
            "quantite": qte,
            "prix_unitaire": pu,
            "taux_tva": taux,
            "montant_ht": montant_ht,
            "montant_tva": montant_tva,
            "montant_ttc": montant_ttc,
            "unite": unite,
            "subtotal": brut_ligne,
            "discount_amount": remise,
            "remise_montant": remise,
            "remise_pct": remise_pct,
            "nb_colis": _as_float(lg.get("nb_colis"), 0.0),
            "poids_kg": _as_float(lg.get("poids_kg"), 0.0),
            "conditionnement": lg.get("conditionnement", "") or "",
        })
        merged.append(line)
    return merged


def enrich_invoice_for_print(inv, sfec_client=None, sage_lines_fetcher=None,
                             contact_fetcher=None):
    """Copie d'une facture enrichie pour le PDF ou la page d'impression."""
    if not isinstance(inv, dict):
        return inv

    enriched = dict(inv)
    sfec = _fetch_sfec_detail(enriched, sfec_client)
    items = sfec_invoice_items(sfec) if sfec else []

    lignes = [
        normalize_print_line(line)
        for line in (enriched.get("lignes") or [])
        if isinstance(line, dict)
    ]
    if not lignes:
        lignes = fetch_sage_lines(
            enriched.get("numero") or enriched.get("sage_piece"),
            enriched.get("type_doc", "vente"),
            enriched.get("sage_domaine"),
            enriched.get("sage_type"),
            fetcher=sage_lines_fetcher,
        )
    enriched["lignes"] = _merge_print_lines(items, lignes)

    # Pour une facture certifiee, le client SFEC fait foi. Sinon on complete
    # depuis Sage quand la synchronisation locale n'a pas conserve le contact.
    if sfec:
        enriched["tiers_nom"] = sfec.get("buyer_name") or enriched.get("tiers_nom") or ""
        enriched["tiers_adresse"] = (
            sfec.get("buyer_address") or enriched.get("tiers_adresse") or ""
        )
        enriched["tiers_niu"] = sfec.get("buyer_niu") or enriched.get("tiers_niu") or ""
        enriched["tiers_telephone"] = (
            sfec.get("buyer_phone") or enriched.get("tiers_telephone") or ""
        )
        if not enriched.get("date_facture"):
            enriched["date_facture"] = sfec.get("invoice_date") or ""

    if not enriched.get("tiers_nom") and enriched.get("tiers_code"):
        contact = _fetch_contact(enriched.get("tiers_code"), contact_fetcher)
        enriched["tiers_nom"] = contact.get("nom") or ""
        enriched["tiers_adresse"] = contact.get("adresse") or enriched.get("tiers_adresse") or ""
        enriched["tiers_niu"] = contact.get("numero_fiscal") or enriched.get("tiers_niu") or ""
        enriched["tiers_telephone"] = (
            contact.get("telephone") or enriched.get("tiers_telephone") or ""
        )
        enriched["tiers_email"] = contact.get("email") or enriched.get("tiers_email") or ""

    # Le champ Reference de l'en-tete reprend la reference article, comme sur
    # la page navigateur. Repli sur la reference de facture si aucune ligne.
    article_reference = next(
        (line.get("code_article") for line in enriched["lignes"]
         if line.get("code_article")), "")
    enriched["reference"] = article_reference or enriched.get("reference") or ""
    return enriched
