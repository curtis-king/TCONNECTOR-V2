import base64
import os
import logging
from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape as _xml_escape
from app.config.manager import get_config

logger = logging.getLogger("t-connector.pdf")

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False
    logger.warning("qrcode non installe - generation QR desactivee")

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, A5
    from reportlab.lib.units import mm, cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False
    logger.warning("reportlab non installe - generation PDF desactivee")


def _get_company():
    cfg = get_config()
    return cfg.get("company", {})


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Title2", fontSize=16, alignment=TA_CENTER, spaceAfter=6, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="SubTitle", fontSize=10, alignment=TA_CENTER, textColor=colors.grey, spaceAfter=4))
    styles.add(ParagraphStyle(name="SmallLeft", fontSize=8, alignment=TA_LEFT, textColor=colors.Color(0.3, 0.3, 0.3)))
    styles.add(ParagraphStyle(name="SmallRight", fontSize=8, alignment=TA_RIGHT, textColor=colors.Color(0.3, 0.3, 0.3)))
    styles.add(ParagraphStyle(name="BoldRight", fontSize=11, alignment=TA_RIGHT, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="CellText", fontSize=8, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="CellRight", fontSize=8, alignment=TA_RIGHT))
    styles.add(ParagraphStyle(name="CellBold", fontSize=8, alignment=TA_LEFT, fontName="Helvetica-Bold"))
    # Gabarit facture standard (modele Sage "Facture cpta")
    styles.add(ParagraphStyle(name="NumeroDoc", fontSize=15, alignment=TA_RIGHT, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="DocLabel", fontSize=11, alignment=TA_RIGHT, fontName="Helvetica-Bold"))
    styles.add(ParagraphStyle(name="MetaHead", fontSize=7.5, alignment=TA_LEFT, textColor=colors.Color(0.35, 0.35, 0.35)))
    styles.add(ParagraphStyle(name="MetaVal", fontSize=8, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="SmallCenter", fontSize=8, alignment=TA_CENTER))
    styles.add(ParagraphStyle(name="TinyLeft", fontSize=6.5, alignment=TA_LEFT, textColor=colors.Color(0.3, 0.3, 0.3)))
    return styles

_UNITES = ["", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
           "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
           "dix-sept", "dix-huit", "dix-neuf"]
_DIZAINES = ["", "", "vingt", "trente", "quarante", "cinquante", "soixante", "soixante-dix", "quatre-vingt", "quatre-vingt-dix"]


def _centaines_en_lettres(n):
    if n == 0:
        return ""
    c, r = divmod(n, 100)
    parts = []
    if c > 0:
        parts.append("cent" if c == 1 else "{} cent".format(_UNITES[c]))
        if c > 1 and r == 0:
            parts[-1] += "s"
    if r > 0:
        if r < 20:
            parts.append(_UNITES[r])
        else:
            d, u = divmod(r, 10)
            if d in (7, 9):
                d -= 1
                u += 10
            dizaine = _DIZAINES[d]
            if u == 1 and d not in (8,):
                parts.append("{} et un".format(dizaine))
            elif u > 0:
                parts.append("{}-{}".format(dizaine, _UNITES[u]))
            else:
                parts.append(dizaine + "s" if d == 8 else dizaine)
    return " ".join(parts)


def _montant_en_lettres(amount):
    """Convertit un montant en toutes lettres (francais), sans decimales (XAF)."""
    try:
        n = int(round(amount))
    except (TypeError, ValueError):
        return ""
    if n == 0:
        return "Zero franc CFA"
    negatif = n < 0
    n = abs(n)

    tranches = [
        (1_000_000_000_000, "billion", "billions"),
        (1_000_000_000, "milliard", "milliards"),
        (1_000_000, "million", "millions"),
        (1_000, "mille", "mille"),
    ]
    parts = []
    reste = n
    for valeur, sing, plur in tranches:
        q, reste = divmod(reste, valeur)
        if q > 0:
            if valeur == 1_000 and q == 1:
                parts.append("mille")
            else:
                mot = _centaines_en_lettres(q)
                parts.append("{} {}".format(mot, sing if q == 1 else plur))
    if reste > 0:
        parts.append(_centaines_en_lettres(reste))

    texte = " ".join(p for p in parts if p)
    if negatif:
        texte = "moins " + texte
    return (texte[0].upper() + texte[1:] + " francs CFA") if texte else ""


def _generate_qr_image(data, box_size=4):
    """Genere une image QR (BytesIO PNG) a partir d'une chaine."""
    if not HAS_QRCODE or not data:
        return None
    try:
        qr = qrcode.QRCode(box_size=box_size, border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning("Erreur generation QR: %s", e)
        return None

def _decode_qr_image(data):
    """Decode un QR SFEC en image affichable (BytesIO PNG), sans le re-encoder.

    Cas 1 (courant) : data-URI 'data:image/png;base64,....' -> decode base64 -> PNG exact du SFEC.
    Cas 2 : base64 nu (sans prefixe 'data:') -> tentative de decode direct.
    Sinon : None (l'appelant bascule sur _generate_qr_image en repli texte court).
    """
    if not data or not isinstance(data, str):
        return None
    s = data.strip()
    try:
        if s.startswith("data:"):
            if "," not in s:
                return None
            header, _, b64 = s.partition(",")
            if "base64" not in header.lower() or not b64:
                return None
            raw = base64.b64decode(b64, validate=True)
        else:
            # base64 nu ? (doit ressembler a du base64 : longueur % 4 == 0, alphabet valide)
            if len(s) < 100 or len(s) % 4 != 0:
                return None
            raw = base64.b64decode(s, validate=True)
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        buf = BytesIO(raw)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning("QR SFEC non decodable en image: %s", e)
        return None


# ---------------------------------------------------------------------------
# Mentions du modele d'impression Sage ("Facture cpta" / ticket de caisse)
# ---------------------------------------------------------------------------

MENTION_PRIX_REVISABLES = "Prix revisables en fonction des données économiques"
VALIDITE_OFFRE = "Validité de l'offre : 30 Jours"
PARITE_EURO_L1 = "Tarifs établis sur la base d'une parité"
PARITE_EURO_L2 = "1 Euro = 655.957 F Cfa"
EURO_PARITY = 655.957

DOC_TITLE_FACTURE = "Facture cpta"
DOC_TITLE_AVOIR = "Facture d'avoir"
SIGNATURE_LABELS = ("Responsable Commercial", "Caisse", "Client", "Responsable Livraison")

CGV_TEXT = (
    "CONDITIONS GENERALES DE VENTES : Prix nets remise déduite pour marchandises prises en nos "
    "magasins ou expédiés à vos risques et périls payables en République du Congo (Pointe Noire "
    "ou Brazzaville). Nos traites et vos remises de traites ne sont pas une dérogation au lieu du "
    "paiement fixé en République du Congo (Pointe Noire ou Brazzaville). Toute marchandise "
    "commandée et non réceptionnée dans un délai d'un mois, fera l'objet d'une facturation pour "
    "frais de stockage au taux mensuel de 2% de la valeur de vente HT. Pour toutes contestations "
    "les tribunaux de Pointe Noire seront les seuls compétents."
)
CGR_TEXT = (
    "CONDITIONS DE REGLEMENTS : comptant à la livraison les prix étant nets sans escompte sauf "
    "dérogations expressément accordées par nous même. Dans ce cas le non paiement à la date "
    "fixée entraînera l'application d'intérêt de retard au taux légal. La seule déchéance du "
    "terme valant la mise en demeure."
)

_PAYMENT_LABELS = {
    "cash": "ESPECES",
    "especes": "ESPECES",
    "check": "CHEQUE",
    "cheque": "CHEQUE",
    "bank_transfer": "VIREMENT",
    "virement": "VIREMENT",
    "card": "CARTE",
    "carte": "CARTE",
    "mobile_money": "MOBILE MONEY",
    "mixte": "MIXTE",
    "credit": "CREDIT",
}


def payment_label(method):
    """Libelle d'impression du mode de paiement (modele Sage : CHEQUE...)."""
    raw = (method or "").strip()
    return _PAYMENT_LABELS.get(raw.lower(), raw.upper() or "-")


def frn_amount(val, decimals=0):
    """Montant au format francais (modele Sage) : milliers = espace, virgule decimale."""
    try:
        x = float(val or 0)
    except (TypeError, ValueError):
        x = 0.0
    return "{:,.{}f}".format(x, decimals).replace(",", " ").replace(".", ",")


def montant_en_lettres(amount):
    """Montant TTC en toutes lettres (alias public de _montant_en_lettres)."""
    return _montant_en_lettres(amount)


def _as_float(val, default=0.0):
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        try:
            return float(str(val).replace(",", "."))
        except (TypeError, ValueError):
            return default


def fr_date(raw):
    """'2026-08-25' -> '25/08/26' (format du modele d'impression)."""
    s = (raw or "").strip().replace("T", " ")[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return "{}/{}/{}".format(s[8:10], s[5:7], s[2:4])
    return s


def fr_date_long(raw):
    """'2026-08-25T10:42:11' -> '25/08/2026' (date de certification du modele)."""
    s = (raw or "").strip().replace("T", " ")[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return "{}/{}/{}".format(s[8:10], s[5:7], s[0:4])
    return s


def fmt_rate(pct):
    """Taux du tableau fiscal : 18 -> '18%', 0.9 -> '0,9%'."""
    x = _as_float(pct)
    if abs(x - round(x)) < 0.05:
        return "{}%".format(int(round(x)))
    return "{}%".format(frn_amount(x, 1))


def tirage_stamp():
    """Horodatage 'Date de tirage' : '19/10/26  12:29:22'."""
    return datetime.now().strftime("%d/%m/%y  %H:%M:%S")


def _fmt_dec(val, decimals=2):
    return frn_amount(val, decimals)


def _fmt_int(val):
    return frn_amount(val, 0)


def _barcode_drawing(value, bar_height=None):
    """Code-barres Code128 du numero de piece (en-tete du modele Sage)."""
    if not HAS_REPORTLAB or not value:
        return None
    try:
        from reportlab.graphics.barcode import createBarcodeDrawing
        return createBarcodeDrawing("Code128", value=str(value),
                                    barHeight=bar_height or 9*mm, barWidth=0.33*mm,
                                    humanReadable=True)
    except Exception as e:
        logger.warning("Code-barres %s impossible: %s", value, e)
        return None


def barcode_svg(value):
    """Code-barres en SVG inline, pour l'impression navigateur."""
    drawing = _barcode_drawing(value)
    if drawing is None:
        return ""
    try:
        from reportlab.graphics import renderSVG
        svg = renderSVG.drawToString(drawing)
    except Exception as e:
        logger.warning("Code-barres SVG %s impossible: %s", value, e)
        return ""
    start = svg.find("<svg")
    return svg[start:] if start >= 0 else ""


def company_image_path(company, key):
    """Chemin d'une image de la config societe (logo, bandeau de pied), si present.

    Un chemin relatif (ex. "data/branding/logo.png") part du dossier de
    l'application, pour fonctionner aussi en version .exe.
    """
    path = (company.get(key) or "").strip()
    if path and not os.path.isabs(path):
        from app.core.paths import base_dir
        path = os.path.join(base_dir(), path)
    return path if path and os.path.exists(path) else ""


def image_data_uri(path):
    """Image locale en data-URI (impression navigateur)."""
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
    try:
        with open(path, "rb") as fh:
            return "data:image/{};base64,{}".format(mime, base64.b64encode(fh.read()).decode("ascii"))
    except Exception as e:
        logger.warning("Image %s illisible: %s", path, e)
        return ""


def company_footer_blocks(company):
    """Pied de page societe (repli sans image) : (lignes gauche, lignes droite)."""
    left = [company.get("name", "")]
    addr = ", ".join(p for p in (company.get("address", ""), company.get("city", ""),
                                 company.get("country", "")) if p)
    left += [addr, company.get("phone", ""), company.get("email", ""), company.get("website", "")]

    right = []
    head = company.get("legal_form", "")
    if company.get("capital"):
        head = "{} au capital de : {} XAF".format(head or "Société", company["capital"])
    right.append(head)
    if company.get("tax_number"):
        right.append("NIU : {}".format(company["tax_number"]))
    rccm = "RCCM : {}".format(company["rc_number"]) if company.get("rc_number") else ""
    if company.get("tax_regime"):
        rccm = (rccm + "   " if rccm else "") + "Régime d'imposition : {}".format(company["tax_regime"])
    right.append(rccm)
    banque = " - ".join(p for p in (company.get("bank_name", ""), company.get("bank_account", ""),
                                     company.get("bank_iban", "")) if p)
    if banque:
        right.append("Banque : {}".format(banque))
    return [l for l in left if l], [r for r in right if r]


def invoice_operator(invoice):
    """Operateur imprime sur la facture (vendeur de la piece)."""
    invoice = invoice or {}
    name = (invoice.get("operateur") or "").strip()
    if name or not invoice.get("vendeur_id"):
        return name
    try:
        from app.storage.db import get_cursor
        with get_cursor() as cur:
            cur.execute("SELECT prenom, nom FROM vendeurs WHERE id = ?", (invoice["vendeur_id"],))
            row = cur.fetchone()
        if row:
            return " ".join(str(p) for p in (row[0], row[1]) if p).strip()
    except Exception as e:
        logger.warning("Vendeur %s introuvable: %s", invoice.get("vendeur_id"), e)
    return ""


def _fit_image(path, max_w, max_h):
    """Image reportlab redimensionnee en conservant ses proportions."""
    try:
        from reportlab.lib.utils import ImageReader
        iw, ih = ImageReader(path).getSize()
        ratio = min(max_w / float(iw), max_h / float(ih))
        return Image(path, width=iw * ratio, height=ih * ratio)
    except Exception as e:
        logger.warning("Image %s illisible: %s", path, e)
        return None


def _invoice_styles():
    base = getSampleStyleSheet()["Normal"]

    def ps(name, **kw):
        kw.setdefault("fontName", "Helvetica")
        kw.setdefault("fontSize", 7.5)
        kw.setdefault("leading", kw["fontSize"] * 1.2)
        return ParagraphStyle(name, parent=base, **kw)

    return {
        "title": ps("FcTitle", fontName="Helvetica-Bold", fontSize=20, leading=23, alignment=TA_RIGHT),
        "co_name": ps("FcCoName", fontName="Helvetica-Bold", fontSize=11, leading=14),
        "co_line": ps("FcCoLine", fontSize=7.5),
        "client": ps("FcClient", fontName="Helvetica-Bold", fontSize=11, leading=14, alignment=TA_RIGHT),
        "client_sub": ps("FcClientSub", fontSize=7.5, alignment=TA_RIGHT),
        "cell": ps("FcCell", fontSize=7),
        "cell_c": ps("FcCellC", fontSize=7, alignment=TA_CENTER),
        "cell_r": ps("FcCellR", fontSize=7, alignment=TA_RIGHT),
        "head_c": ps("FcHeadC", fontSize=8, alignment=TA_CENTER),
        "txt": ps("FcTxt", fontSize=8),
        "txt_r": ps("FcTxtR", fontSize=8, alignment=TA_RIGHT),
        "txt_bold_c": ps("FcTxtBoldC", fontName="Helvetica-Bold", fontSize=8.5, alignment=TA_CENTER),
        "small": ps("FcSmall", fontSize=7),
        "small_r": ps("FcSmallR", fontSize=7, alignment=TA_RIGHT),
        "cgv": ps("FcCgv", fontSize=5.8, leading=7),
    }


# Icones des coordonnees du bandeau (police ZapfDingbats en PDF)
_FOOTER_ICONS = (("phone", "☎"), ("email", "✉"), ("address", "➤"), ("website", "❂"))
# Couleurs de la charte (releves sur le logo Bernabe)
FOOTER_NAVY = "#253364"
FOOTER_RED = "#ed283a"


def _footer_markup(text):
    """Texte echappe ou '**gras**' devient <b>gras</b> (valable en HTML et reportlab)."""
    parts = _xml_escape(str(text or "")).split("**")
    return "".join("<b>{}</b>".format(p) if i % 2 else p for i, p in enumerate(parts))


def _footer_site(site):
    lines = [{"icon": icon, "text": str(site[key])} for key, icon in _FOOTER_ICONS if site.get(key)]
    return {"name": site.get("name", ""), "lines": lines}


def invoice_footer(company):
    """Bandeau societe du bas de facture (modele : slogan, sites, mentions, logo groupe).

    Config societe : footer_slogan, footer_sites [{name, phone, email, address,
    website}], footer_legal [lignes, '**gras**'], footer_banks [lignes],
    footer_logo (chemin). Sans sites ni mentions : champs societe standards.
    """
    sites = [_footer_site(s) for s in company.get("footer_sites") or []]
    legal = [_footer_markup(l) for l in company.get("footer_legal") or []]
    if not sites and not legal:
        addr = ", ".join(p for p in (company.get("address", ""), company.get("city", "")) if p)
        site = _footer_site({"name": company.get("name", ""), "phone": company.get("phone", ""),
                             "email": company.get("email", ""), "address": addr,
                             "website": company.get("website", "")})
        sites = [site] if site["lines"] else []
        legal = [_xml_escape(l) for l in company_footer_blocks(company)[1]]
    if not sites and not legal:
        return None
    return {
        "slogan": company.get("footer_slogan", "") or "",
        "sites": sites,
        "legal": legal,
        "banks": [str(b) for b in company.get("footer_banks") or []],
        "logo_path": company_image_path(company, "footer_logo"),
    }


def _invoice_footer_painter(company, width):
    """Bandeau societe dessine en bas de chaque page. Renvoie (hauteur reservee, onPage).

    Disposition du modele : coordonnees des sites (~40 %), filet rouge,
    mentions legales et banques (~45 %), logo du groupe ; filet marine en bas.
    """
    footer = invoice_footer(company)
    if not footer:
        return 0, None

    navy = colors.HexColor(FOOTER_NAVY)
    base = getSampleStyleSheet()["Normal"]
    txt = ParagraphStyle("FtTxt", parent=base, fontName="Helvetica", fontSize=5.3, leading=7.3,
                         textColor=navy)
    icon_w = 3.4*mm
    line = ParagraphStyle("FtLine", parent=txt, leftIndent=icon_w, firstLineIndent=-icon_w)
    slogan = ParagraphStyle("FtSlogan", parent=txt, fontName="Helvetica-Bold", fontSize=6.3,
                            leading=8.5)
    name = ParagraphStyle("FtName", parent=txt, fontName="Helvetica-Bold", fontSize=6.4, leading=8,
                          leftIndent=icon_w)
    pad0 = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP")]

    logo_w = 22*mm if footer["logo_path"] else 0
    left_w = 80*mm
    legal_w = width - logo_w - left_w

    left = []
    if footer["slogan"]:
        left += [Paragraph(_xml_escape(footer["slogan"]), slogan), Spacer(1, 1.2*mm)]
    if footer["sites"]:
        cols = []
        for site in footer["sites"]:
            cell = [Paragraph(_xml_escape(site["name"]), name)]
            for l in site["lines"]:
                cell.append(Paragraph('<font name="ZapfDingbats" size="5.6">{}</font>&nbsp;&nbsp;{}'.format(
                    l["icon"], _xml_escape(l["text"])), line))
            cols.append(cell)
        sites = Table([cols], colWidths=[(left_w - 3*mm) / len(cols)] * len(cols))
        sites.setStyle(TableStyle(pad0 + [("RIGHTPADDING", (0, 0), (-1, -1), 1.5*mm)]))
        left.append(sites)

    legal = [Spacer(1, 1*mm)] + [Paragraph(l, txt) for l in footer["legal"]]
    if footer["banks"]:
        banks = Table([[Paragraph("<b>Banques :</b>", txt),
                        [Paragraph(_xml_escape(b), txt) for b in footer["banks"]]]],
                      colWidths=[11*mm, legal_w - 14*mm])
        banks.setStyle(TableStyle(pad0))
        legal.append(banks)

    logo = _fit_image(footer["logo_path"], logo_w - 1*mm, 20*mm) if logo_w else None
    row = [left, legal] + ([logo or ""] if logo_w else [])
    band = Table([row], colWidths=[left_w, legal_w] + ([logo_w] if logo_w else []))
    band.setStyle(TableStyle(pad0 + [
        ("LEFTPADDING", (1, 0), (1, 0), 3*mm),
        ("LINEBEFORE", (1, 0), (1, 0), 0.9, colors.HexColor(FOOTER_RED)),
        ("VALIGN", (-1, 0), (-1, 0), "MIDDLE"),
        ("ALIGN", (-1, 0), (-1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2*mm),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, navy),
    ]))
    _, band_h = band.wrap(width, 100*mm)

    def paint(canvas, doc):
        canvas.saveState()
        band.drawOn(canvas, doc.leftMargin + 6, 7*mm)
        canvas.restoreState()

    return band_h + 4*mm, paint


def _draw_non_conforme_watermark(canv, doc):
    """Filigrane diagonal pour une copie non certifiee SFEC."""
    try:
        canv.saveState()
        canv.setFillColorRGB(0.85, 0.1, 0.1, alpha=0.18)
        canv.translate(doc.pagesize[0] / 2.0, doc.pagesize[1] / 2.0)
        canv.rotate(35)
        canv.setFont("Helvetica-Bold", 44)
        canv.drawCentredString(0, 10, "COPIE NON CERTIFIEE")
        canv.setFont("Helvetica-Bold", 16)
        canv.drawCentredString(0, -20, "NON CONFORME")
        canv.restoreState()
    except Exception as e:  # noqa: BLE001
        logger.warning("Filigrane non conforme impossible: %s", e)


def generate_invoice_pdf(invoice, output_path=None, non_conforme=False):
    """Facture A4 au format du modele Sage "Facture cpta".

    En-tete logo / code-barres + titre + bandeau Numero-Date-Compte-Reference
    + client ; certification SFEC ; tableau des lignes a colonnes ; tableau
    fiscal (codes 1/202/203) + recapitulatif NET A PAYER ; montant en
    lettres, parite euro, conditions de reglement ; cases de signature ;
    CGV ; bandeau societe en pied de page.
    """
    if not HAS_REPORTLAB:
        logger.error("reportlab non disponible")
        return None

    company = _get_company()
    st = _invoice_styles()
    type_doc = invoice.get("type_doc", "vente")
    numero = invoice.get("numero", "") or ""
    devise = (invoice.get("devise") or "XAF").upper()
    esc = lambda v: _xml_escape(str(v if v is not None else ""))  # noqa: E731
    line_color = colors.black

    W = A4[0] - 24*mm - 12  # le cadre reportlab garde 6 pt de marge de chaque cote
    footer_h, footer_paint = _invoice_footer_painter(company, W)
    target = output_path or BytesIO()
    doc = SimpleDocTemplate(target, pagesize=A4,
                            leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=10*mm, bottomMargin=7*mm + footer_h,
                            title="{} {}".format(DOC_TITLE_FACTURE, numero))
    elements = []

    # ---- En-tete : logo (gauche) / code-barres, titre, bandeau, client (droite)
    left_cell = []
    logo_path = company_image_path(company, "logo_file_name")
    logo = _fit_image(logo_path, 80*mm, 32*mm) if logo_path else None
    if logo is not None:
        logo.hAlign = "LEFT"
        left_cell.append(logo)
    else:
        left_cell.append(Paragraph(esc(company.get("name", "")), st["co_name"]))
        for line in company_footer_blocks(company)[0][1:4]:
            left_cell.append(Paragraph(esc(line), st["co_line"]))

    right_w = 104*mm
    right_cell = []
    barcode = _barcode_drawing(numero)
    if barcode is not None:
        barcode.hAlign = "RIGHT"
        right_cell.append(barcode)
    right_cell.append(Paragraph(DOC_TITLE_AVOIR if type_doc == "avoir" else DOC_TITLE_FACTURE,
                                st["title"]))
    right_cell.append(Spacer(1, 1.5*mm))
    ident = Table([
        [Paragraph(h, st["head_c"]) for h in ("Numéro", "Date", "Compte", "Référence")],
        [Paragraph(esc(v), st["head_c"]) for v in (
            numero, fr_date(invoice.get("date_facture", "")),
            invoice.get("tiers_code", "") or "", invoice.get("reference", "") or "")],
    ], colWidths=[32*mm, 20*mm, 24*mm, 26*mm])
    ident.hAlign = "RIGHT"
    ident.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, line_color),
        ("LINEBEFORE", (0, 0), (0, -1), 1.6, line_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    right_cell.append(ident)
    right_cell.append(Spacer(1, 2*mm))
    right_cell.append(Paragraph(
        "Client : &nbsp; {}".format(esc(invoice.get("tiers_nom", "") or "-")), st["client"]))
    sub = [x for x in (
        "NIU : {}".format(invoice["tiers_niu"]) if invoice.get("tiers_niu") else "",
        invoice.get("tiers_adresse", ""),
        "Tél : {}".format(invoice["tiers_telephone"]) if invoice.get("tiers_telephone") else "",
    ) if x]
    if sub:
        right_cell.append(Paragraph(esc(" - ".join(sub)), st["client_sub"]))
    if type_doc == "avoir" and invoice.get("reference_invoice_id"):
        right_cell.append(Paragraph(
            "Avoir sur facture n° {}".format(esc(invoice["reference_invoice_id"])), st["client_sub"]))

    header = Table([[left_cell, right_cell]], colWidths=[W - right_w, right_w])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header)
    elements.append(Spacer(1, 3*mm))

    # ---- Certification SFEC / mention prix revisables / QR
    sfec_num = invoice.get("sfec_num_certif") or invoice.get("sfec_certification_number", "")
    sfec_date = invoice.get("sfec_date_certif") or invoice.get("sfec_certification_date", "")
    sfec_qr = invoice.get("sfec_qr_code", "")
    cert_cell = [
        Paragraph("Date certification : &nbsp;<b>{}</b>".format(esc(fr_date_long(sfec_date))), st["txt"]),
        Paragraph("Numéro certification : <b>{}</b>".format(esc(sfec_num)), st["txt"]),
    ]
    qr_buf = _decode_qr_image(sfec_qr) if sfec_qr else None
    if qr_buf is None and sfec_qr:
        qr_buf = _generate_qr_image(sfec_qr)
    qr_cell = Image(qr_buf, width=20*mm, height=20*mm) if qr_buf else ""
    cert = Table([[cert_cell, Paragraph("Prix revisables en fonction<br/>des données économiques",
                                        st["txt_bold_c"]), qr_cell]],
                 colWidths=[W - 82*mm, 58*mm, 24*mm])
    cert.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("VALIGN", (0, 0), (0, 0), "BOTTOM"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(cert)
    operateur = invoice_operator(invoice)
    tirage = Table([[
        Paragraph("Opérateur : {}".format(esc(operateur)) if operateur else "", st["small"]),
        Paragraph("<u>Date de tirage</u> &nbsp;{} &nbsp;&nbsp;&nbsp;&nbsp; Page : &nbsp;1".format(tirage_stamp()),
                  st["txt_r"]),
    ]], colWidths=[W / 2.0, W / 2.0])
    tirage.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(tirage)
    elements.append(Spacer(1, 1.5*mm))

    # ---- Tableau des lignes (colonnes separees par des filets verticaux)
    lignes = invoice.get("lignes") or []
    headers = ["Référence", "Désignation", "Nb.", "Cond.", "Qté", "Unité",
               "Pds (Kg)", "P.U. HT", "Rem.", "P.U. Net", "Total HT"]
    rows = [[Paragraph(h, st["cell_c"]) for h in headers]]
    total_poids = 0.0
    for l in lignes:
        qte = _as_float(l.get("quantite", 1), 1)
        pu = _as_float(l.get("prix_unitaire", 0))
        brut = _as_float(l.get("subtotal", 0)) or qte * pu
        remise = _as_float(l.get("discount_amount", 0)) or _as_float(l.get("remise_montant", 0))
        remise_pct = _as_float(l.get("remise_pct", 0))
        if not remise_pct and brut and remise:
            remise_pct = remise / brut * 100
        montant = _as_float(l.get("montant_ht", 0))
        if not montant and brut > 0:
            montant = brut - remise
        pu_net = (montant / qte) if qte else pu
        poids = _as_float(l.get("poids_kg", 0))
        nb_colis = _as_float(l.get("nb_colis", 0))
        total_poids += poids
        rows.append([
            Paragraph(esc(l.get("code_article", "") or l.get("code_produit", "") or ""), st["cell"]),
            Paragraph(esc(l.get("designation", "") or l.get("description", "") or ""), st["cell"]),
            Paragraph(_fmt_dec(nb_colis) if nb_colis else "", st["cell_r"]),
            Paragraph(esc(l.get("conditionnement", "") or ""), st["cell"]),
            Paragraph(_fmt_dec(qte), st["cell_r"]),
            Paragraph(esc(l.get("unite", "") or ""), st["cell"]),
            Paragraph(_fmt_dec(poids) if poids else "", st["cell_r"]),
            Paragraph(_fmt_dec(pu), st["cell_r"]),
            Paragraph(fmt_rate(remise_pct) if remise_pct else "Prix Net", st["cell"]),
            Paragraph(_fmt_dec(pu_net), st["cell_r"]),
            Paragraph(_fmt_int(montant), st["cell_r"]),
        ])
    if not lignes:
        montant = _as_float(invoice.get("montant_ht", 0))
        rows.append(["", Paragraph("Facture", st["cell"]), "", "", Paragraph("1,00", st["cell_r"]),
                     "", "", Paragraph(_fmt_dec(montant), st["cell_r"]), Paragraph("Prix Net", st["cell"]),
                     Paragraph(_fmt_dec(montant), st["cell_r"]), Paragraph(_fmt_int(montant), st["cell_r"])])

    # Ligne de remplissage : le cadre descend comme sur le modele
    n_rows = len(rows) - 1
    rows.append([""] * len(headers))
    filler_h = max(3*mm, 60*mm - n_rows * 6*mm)
    col_widths = [15*mm, 38*mm, 11*mm, 19*mm, 14*mm, 20*mm, 12*mm, 13*mm, 12*mm, 13*mm, 14*mm]
    items_table = Table(rows, colWidths=col_widths, repeatRows=1,
                        rowHeights=[None] * (len(rows) - 1) + [filler_h])
    items_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, line_color),
        ("LINEAFTER", (0, 0), (-2, -1), 0.6, line_color),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, line_color),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("TOPPADDING", (0, 1), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2.5),
    ]))
    elements.append(items_table)
    if total_poids > 0:
        elements.append(Paragraph(
            "{} <b>Kg</b>".format(_fmt_dec(total_poids, 3)),
            ParagraphStyle("FcPoids", parent=st["txt_r"], rightIndent=50*mm, spaceBefore=1)))
    elements.append(Spacer(1, 2*mm))

    # ---- Totaux / tableau fiscal (codes 1, 1R, 202, 203...)
    montant_ht = _as_float(invoice.get("montant_ht", 0))
    remise_lignes = _as_float(invoice.get("total_line_discount_amount", 0))
    remise_globale = _as_float(invoice.get("discount_amount", 0))
    ht_brut = _as_float(invoice.get("montant_ht_brut", 0)) or (montant_ht + remise_lignes + remise_globale)
    remise_totale = remise_lignes + remise_globale
    # Sage tronque le pourcentage de remise a une decimale (16,17 -> 16,1)
    remise_pct_glob = int(remise_totale / ht_brut * 1000) / 10.0 if ht_brut else 0
    tva18 = _as_float(invoice.get("total_tax_t_amount", 0))
    tva5 = _as_float(invoice.get("total_tax_r_amount", 0))
    exonere = _as_float(invoice.get("total_exempt_amount", 0))
    centime = _as_float(invoice.get("additional_cent_tax", 0))
    timbre = _as_float(invoice.get("electronic_stamp_duty", 0))
    montant_tva = _as_float(invoice.get("montant_tva", 0)) or (tva18 + tva5)
    montant_ttc = _as_float(invoice.get("montant_ttc", 0))
    net_a_payer = _as_float(invoice.get("montant_restant", 0)) or montant_ttc
    total_taxes = montant_tva + centime + timbre

    base18 = sum(_as_float(l.get("montant_ht", 0)) for l in lignes
                 if round(_as_float(l.get("taux_tva", 18))) == 18)
    base5 = sum(_as_float(l.get("montant_ht", 0)) for l in lignes
                if round(_as_float(l.get("taux_tva", 18))) == 5)
    if not lignes or (not base18 and not base5):
        base18 = max(montant_ht - exonere, 0)

    tax_rows = []
    if tva18 or base18:
        tax_rows.append(["1", _fmt_int(base18), "18%", _fmt_int(tva18)])
    if tva5 or base5:
        tax_rows.append(["1R", _fmt_int(base5), "5%", _fmt_int(tva5)])
    if exonere:
        tax_rows.append(["0", _fmt_int(exonere), "Exonéré", "-"])
    if centime:
        tax_rows.append(["202", _fmt_int(montant_ht),
                         fmt_rate(centime / montant_ht * 100 if montant_ht else 0), _fmt_int(centime)])
    if timbre:
        tax_rows.append(["203", _fmt_int(montant_ht),
                         fmt_rate(timbre / montant_ht * 100 if montant_ht else 0), _fmt_int(timbre)])
    if not tax_rows:
        tax_rows.append(["1", _fmt_int(montant_ht), "18%", _fmt_int(montant_tva)])

    tax_data = [[Paragraph(h, st["head_c"]) for h in ("Code", "Base", "Taux", "Montant")]]
    for code, base, taux, mt in tax_rows:
        tax_data.append([Paragraph(code, st["cell_c"]), Paragraph(base, st["cell_r"]),
                         Paragraph(taux, st["cell_r"]), Paragraph(mt, st["cell_r"])])
    tax_data.append([Paragraph("Total taxes", st["txt"]), "", "",
                     Paragraph(_fmt_int(total_taxes), st["txt_r"])])
    left_w = 70*mm
    tax_table = Table(tax_data, colWidths=[12*mm, 22*mm, 14*mm, 22*mm])
    tax_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, line_color),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, line_color),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, line_color),
        ("SPAN", (0, -1), (2, -1)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, -1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 4),
    ]))
    left_block = [
        tax_table,
        Spacer(1, 2*mm),
        Paragraph("<u>Date de tirage</u> &nbsp;{}".format(tirage_stamp()), st["txt"]),
    ]

    right_w = W - left_w - 3*mm
    sum_table = Table([
        [Paragraph("Montant HT<br/>Brut", st["head_c"]), Paragraph("Remise", st["head_c"]), "",
         Paragraph("Montant HT<br/>Net", st["head_c"]), Paragraph("Total TVA", st["head_c"]),
         Paragraph("NET A PAYER", st["head_c"])],
        ["", Paragraph("(%)", st["head_c"]), Paragraph("Valeur", st["head_c"]), "", "", ""],
        [Paragraph(_fmt_int(ht_brut), st["txt_r"]),
         Paragraph(frn_amount(remise_pct_glob, 1), st["txt_r"]),
         Paragraph(_fmt_int(remise_totale), st["txt_r"]),
         Paragraph(_fmt_int(montant_ht), st["txt_r"]),
         Paragraph(_fmt_int(total_taxes), st["txt_r"]),
         Paragraph("<b>{}</b>".format(_fmt_int(net_a_payer)),
                   ParagraphStyle("FcNet", parent=st["txt_r"], fontSize=9.5, leading=12))],
    ], colWidths=[20*mm, 10*mm, 17*mm, 20*mm, 19*mm, right_w - 86*mm])
    sum_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, line_color),
        ("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (2, 0)), ("SPAN", (3, 0), (3, 1)),
        ("SPAN", (4, 0), (4, 1)), ("SPAN", (5, 0), (5, 1)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, 1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, 1), 1.5),
        ("TOPPADDING", (0, 2), (-1, 2), 4),
        ("BOTTOMPADDING", (0, 2), (-1, 2), 4),
    ]))

    euro_txt = _fmt_int(montant_ttc / EURO_PARITY) if devise in ("XAF", "XOF") else "-"
    parite = Table([[
        Paragraph("{}<br/>{}".format(PARITE_EURO_L1, PARITE_EURO_L2), st["txt"]),
        Paragraph("<b>En Euro</b>", st["txt_r"]),
        Paragraph("<b>{}</b>".format(euro_txt), st["txt_r"]),
    ]], colWidths=[right_w - 50*mm, 25*mm, 25*mm])
    conditions = Table([[
        Paragraph(esc(company.get("default_payment_terms", "") or ""), st["txt"]),
        Paragraph(esc(fr_date(invoice.get("date_echeance", ""))), st["txt"]),
        Paragraph(esc(payment_label(invoice.get("payment_method", ""))), st["txt"]),
        Paragraph(_fmt_int(net_a_payer), st["txt"]),
    ]], colWidths=[right_w - 72*mm, 22*mm, 22*mm, 28*mm])
    for t in (parite, conditions):
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
    montant_lettres = _montant_en_lettres(montant_ttc)
    right_block = [
        sum_table,
        Spacer(1, 2*mm),
        Paragraph("Arrêté le présent document à la somme de <i>{}</i>".format(esc(montant_lettres)), st["txt"]),
        Paragraph(VALIDITE_OFFRE, st["txt"]),
        parite,
        Spacer(1, 1*mm),
        Paragraph("<b>Conditions de règlement :</b>", st["txt"]),
        conditions,
    ]

    bottom = Table([[left_block, "", right_block]], colWidths=[left_w, 3*mm, right_w])
    bottom.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(bottom)
    elements.append(Spacer(1, 4*mm))

    # ---- Cases de signature
    signs = Table([[Paragraph(lbl, st["head_c"]) for lbl in SIGNATURE_LABELS], [""] * 4],
                  colWidths=[W / 4.0] * 4, rowHeights=[7*mm, 22*mm])
    signs.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, line_color),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
    ]))
    elements.append(signs)
    elements.append(Spacer(1, 4*mm))

    # ---- CGV (ou texte de pied de facture configure)
    footer_txt = (company.get("invoice_footer") or "").strip()
    for para in ([footer_txt] if footer_txt else [CGV_TEXT, CGR_TEXT]):
        elements.append(Paragraph(esc(para), st["cgv"]))
        elements.append(Spacer(1, 1.5*mm))
    if invoice.get("notes"):
        elements.append(Paragraph("<b>Notes :</b> {}".format(esc(invoice["notes"])), st["cgv"]))

    on_page = [f for f in (footer_paint, _draw_non_conforme_watermark if non_conforme else None) if f]
    if on_page:
        def _paint_page(canv, d):
            for paint in on_page:
                paint(canv, d)
        doc.build(elements, onFirstPage=_paint_page, onLaterPages=_paint_page)
    else:
        doc.build(elements)

    if output_path:
        return output_path
    target.seek(0)
    return target.read()


# ---------------------------------------------------------------------------
# Ticket de caisse (modele Sage : en-tete etoile, lignes "qte x PU = total")
# ---------------------------------------------------------------------------

_PAIEMENT_LABELS = {
    "especes": "Espèces", "mobile_money": "Mobile Money", "virement": "Virement",
    "carte": "Carte", "cheque": "Chèque", "mixte": "Mixte",
}
TICKET_STARS = "*" * 33


def _fmt_date_parts(raw):
    """'2026-08-25 08:52:55' -> ('25/08/26', '08:52:55')."""
    raw = (raw or "").strip().replace("T", " ")
    if not raw:
        return "", ""
    parts = raw.split()
    jour = parts[0]
    if len(jour) == 10 and jour[4] == "-":
        jour = "{}/{}/{}".format(jour[8:10], jour[5:7], jour[2:4])
    heure = parts[1][:8] if len(parts) > 1 else ""
    return jour, heure


def _fmt_cert_date(raw):
    s = (raw or "").strip()
    if not s:
        return ""
    return s.replace("T", " ").replace("Z", "").strip()[:19]


def _ticket_sfec(invoice_id):
    """Bloc SFEC (numero facture, certification, QR) de la facture liee au ticket."""
    if not invoice_id:
        return {}
    try:
        from app.domain import invoices as invoice_engine
        inv = invoice_engine.get_invoice(invoice_id) or {}
    except Exception as e:
        logger.warning("Ticket: facture %s illisible: %s", invoice_id, e)
        return {}
    return {
        "facture": inv.get("numero") or "",
        "numero": inv.get("sfec_num_certif") or "",
        "date": inv.get("sfec_date_certif") or "",
        "qr": inv.get("sfec_qr_code") or "",
        "statut": inv.get("sfec_statut") or "",
    }


def ticket_header_lines(company):
    """Lignes centrees sous le nom societe (modele : site, adresse, tel, regime)."""
    custom = (company.get("ticket_header") or "").strip()
    if custom:
        return [l.strip() for l in custom.splitlines() if l.strip()]
    addr = ", ".join(p for p in (company.get("address", ""), company.get("city", "")) if p)
    legal = []
    if company.get("tax_regime") or company.get("legal_form"):
        legal.append("REGIME {}".format(company.get("tax_regime") or company.get("legal_form")))
    if company.get("tax_number"):
        legal.append("NIU N° {}".format(company["tax_number"]))
    lines = [company.get("website", ""), addr, company.get("phone", ""), " -".join(legal)]
    return [l for l in lines if l]


def ticket_message():
    message = ""
    try:
        from app.storage import db as sqlite_db
        message = sqlite_db.get_setting("ticket_message", "")
    except Exception:
        pass
    return message or "Merci pour votre achat !"


def ticket_view(ticket):
    """Donnees d'impression d'un ticket, communes au HTML et au PDF."""
    company = _get_company()
    jour, heure = _fmt_date_parts(ticket.get("date_ticket"))
    lignes = []
    for l in ticket.get("lignes") or []:
        qte = _as_float(l.get("quantite", 1), 1)
        total = _as_float(l.get("montant_ttc", 0))
        pu = (total / qte) if qte else _as_float(l.get("prix_unitaire", 0))
        lignes.append({
            "designation": str(l.get("designation", "") or ""),
            "qte": frn_amount(qte, 2),
            "pu": frn_amount(pu, 3),
            "total": frn_amount(total, 0),
        })
    mode = ticket.get("mode_paiement", "") or ""
    recu = _as_float(ticket.get("montant_recu", 0))
    sfec = _ticket_sfec(ticket.get("invoice_id"))
    return {
        "company_name": company.get("name", "") or "Mon Entreprise",
        "logo_path": company_image_path(company, "logo_file_name"),
        "header_lines": ticket_header_lines(company),
        "date": jour,
        "heure": heure,
        "numero": ticket.get("numero", "") or "",
        "facture": sfec.get("facture", ""),
        "lignes": lignes,
        "sous_total": frn_amount(ticket.get("montant_ttc", 0)),
        "total": frn_amount(ticket.get("montant_ttc", 0)),
        "dont_tva": frn_amount(ticket.get("montant_tva", 0)),
        "paiement": _PAIEMENT_LABELS.get(mode, mode.capitalize()),
        "recu": frn_amount(recu) if recu else "",
        "rendu": frn_amount(ticket.get("monnaie_rendue", 0)),
        "sfec_num": sfec.get("numero", ""),
        "sfec_date": _fmt_cert_date(sfec.get("date", "")),
        "sfec_qr": sfec.get("qr", ""),
        "sfec_statut": sfec.get("statut", ""),
        "message": ticket_message(),
    }


def generate_ticket_pdf(ticket, output_path=None):
    """Ticket pour imprimante thermique 80 mm (zone imprimable ~72 mm), modele Sage."""
    if not HAS_REPORTLAB:
        return None

    v = ticket_view(ticket)
    width = 80*mm
    margin = 4*mm
    cw = width - 2 * margin - 12  # marge interne du cadre reportlab

    base = getSampleStyleSheet()["Normal"]
    mono = ParagraphStyle("TkMono", parent=base, fontName="Courier", fontSize=8, leading=10)
    sans_c = ParagraphStyle("TkSansC", parent=base, fontName="Helvetica", fontSize=7, leading=8.5,
                            alignment=TA_CENTER)
    st = {
        "mono": mono,
        "mono_r": ParagraphStyle("TkMonoR", parent=mono, alignment=TA_RIGHT),
        "mono_c": ParagraphStyle("TkMonoC", parent=mono, alignment=TA_CENTER),
        "stars": ParagraphStyle("TkStars", parent=mono, fontSize=7.5),
        "company": ParagraphStyle("TkCompany", parent=sans_c, fontName="Helvetica-Bold",
                                  fontSize=8, leading=10),
        "sans_c": sans_c,
        "sans_c_bold": ParagraphStyle("TkSansCB", parent=sans_c, fontName="Helvetica-Bold"),
        "total": ParagraphStyle("TkTotal", parent=mono, fontName="Courier-Bold", fontSize=10, leading=13),
        "total_r": ParagraphStyle("TkTotalR", parent=mono, fontName="Courier-Bold", fontSize=11,
                                  leading=13, alignment=TA_RIGHT),
    }

    def p(text, style):
        return Paragraph(_xml_escape(str(text or "")), st[style])

    def row(left, right, ls="mono", rs="mono_r", split=0.5):
        t = Table([[p(left, ls), p(right, rs)]], colWidths=[cw * split, cw * (1 - split)])
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ]))
        return t

    def rule(space=1.5*mm):
        return HRFlowable(width="100%", thickness=0.7, color=colors.black,
                          spaceBefore=space, spaceAfter=space)

    el = [p(TICKET_STARS, "stars")]
    logo = _fit_image(v["logo_path"], cw, 22*mm) if v["logo_path"] else None
    if logo is not None:
        logo.hAlign = "CENTER"
        el += [Spacer(1, 1.5*mm), logo, Spacer(1, 1.5*mm)]
    else:
        el += [p(v["company_name"], "company"), Spacer(1, 1.5*mm)]
    for line in v["header_lines"]:
        el.append(p(line, "sans_c"))
    el += [Spacer(1, 1*mm), p(TICKET_STARS, "stars"),
           row("Le      {}".format(v["date"]), "à {}".format(v["heure"]), split=0.6),
           Spacer(1, 3*mm),
           row("Ticket N°", v["numero"], rs="mono", split=0.32)]
    if v["facture"] and v["facture"] != v["numero"]:
        el.append(row("Facture N°", v["facture"], rs="mono", split=0.32))
    el.append(rule())

    for l in v["lignes"]:
        el.append(p(l["designation"], "mono"))
        calc = Table([[p(l["qte"], "mono_r"), p("x", "mono_c"), p(l["pu"], "mono_r"),
                       p("=", "mono_c"), p(l["total"], "mono_r")]],
                     colWidths=[cw * 0.24, cw * 0.06, cw * 0.28, cw * 0.06, cw * 0.36])
        calc.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ]))
        el.append(calc)

    el += [rule(),
           row("Sous-Total", v["sous_total"]),
           Spacer(1, 2*mm),
           row("TOTAL", v["total"], ls="total", rs="total_r"),
           row("Dont TVA", v["dont_tva"]),
           rule(),
           row("Règlement", " ".join(x for x in (v["paiement"], v["recu"]) if x)),
           Spacer(1, 1.5*mm),
           row("Rendu", v["rendu"]),
           rule()]

    if v["sfec_num"]:
        el.append(p("CERTIFIE SFEC", "sans_c_bold"))
        el.append(p("N° {}".format(v["sfec_num"]), "sans_c"))
        if v["sfec_date"]:
            el.append(p("Date : {}".format(v["sfec_date"]), "sans_c"))
        qr_buf = _decode_qr_image(v["sfec_qr"])
        if qr_buf is None and v["sfec_qr"]:
            qr_buf = _generate_qr_image(v["sfec_qr"])
        if qr_buf:
            qr_img = Image(qr_buf, width=20*mm, height=20*mm)
            qr_img.hAlign = "CENTER"
            el += [Spacer(1, 1*mm), qr_img]
        el.append(Spacer(1, 2*mm))

    el.append(Paragraph("<br/>".join(_xml_escape(l) for l in v["message"].splitlines()), st["sans_c"]))

    # Hauteur de page = hauteur reelle du contenu (rouleau : pas de papier perdu)
    frame_pad = 6  # marges internes du cadre reportlab (pt)
    height = 8*mm + 2 * frame_pad + 2*mm
    for f in el:
        height += f.wrap(cw, 10000)[1] + f.getSpaceBefore() + f.getSpaceAfter()

    target = output_path or BytesIO()
    doc = SimpleDocTemplate(target, pagesize=(width, height),
                            leftMargin=margin, rightMargin=margin,
                            topMargin=4*mm, bottomMargin=4*mm,
                            title="Ticket {}".format(v["numero"]))
    doc.build(el)

    if output_path:
        return output_path
    target.seek(0)
    return target.read()
