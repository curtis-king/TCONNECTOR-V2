import base64
import os
import logging
from io import BytesIO
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

def generate_invoice_pdf(invoice, output_path=None):
    if not HAS_REPORTLAB:
        logger.error("reportlab non disponible")
        return None

    company = _get_company()
    styles = _styles()

    if output_path:
        doc = SimpleDocTemplate(output_path, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=20*mm)
    else:
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=15*mm, rightMargin=15*mm,
                                topMargin=15*mm, bottomMargin=20*mm)

    elements = []

    logo_path = company.get("logo_file_name", "")
    if logo_path and os.path.exists(logo_path):
        try:
            elements.append(Image(logo_path, width=50*mm, height=25*mm))
            elements.append(Spacer(1, 4*mm))
        except Exception:
            pass

    elements.append(Paragraph(company.get("name", "Mon Entreprise"), styles["Title2"]))
    if company.get("legal_form"):
        elements.append(Paragraph(company["legal_form"], styles["SubTitle"]))
    addr_parts = [p for p in [company.get("address", ""), company.get("city", ""), company.get("country", "")] if p]
    if addr_parts:
        elements.append(Paragraph(", ".join(addr_parts), styles["SubTitle"]))

    legal_line = []
    if company.get("tax_number"):
        legal_line.append("NIU: {}".format(company["tax_number"]))
    if company.get("rc_number"):
        legal_line.append("RCCM: {}".format(company["rc_number"]))
    if company.get("tax_regime"):
        legal_line.append("Regime: {}".format(company["tax_regime"]))
    if company.get("capital"):
        legal_line.append("Capital: {} FCFA".format(company["capital"]))
    if legal_line:
        elements.append(Paragraph(" | ".join(legal_line), styles["SubTitle"]))

    if company.get("phone") or company.get("email"):
        elements.append(Paragraph("Tel: {} | Email: {}".format(company.get("phone", ""), company.get("email", "")), styles["SubTitle"]))

    if company.get("bank_account") or company.get("bank_iban"):
        rib_line = [x for x in [
            "Compte: {}".format(company["bank_account"]) if company.get("bank_account") else "",
            "IBAN: {}".format(company["bank_iban"]) if company.get("bank_iban") else "",
        ] if x]
        elements.append(Paragraph(" | ".join(rib_line), styles["SubTitle"]))

    elements.append(Spacer(1, 6*mm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.Color(0.2, 0.5, 0.8)))
    elements.append(Spacer(1, 4*mm))

    type_doc = invoice.get("type_doc", "vente")
    titre_doc = "FACTURE D'AVOIR" if type_doc == "avoir" else "FACTURE DE VENTE"
    elements.append(Paragraph("<b>{}</b>".format(titre_doc), styles["Title2"]))

    if type_doc == "avoir" and invoice.get("reference_invoice_id"):
        elements.append(Paragraph(
            "Avoir sur facture n° {}".format(invoice["reference_invoice_id"]),
            styles["SubTitle"]
        ))

    date_heure = invoice.get("created_at") or invoice.get("date_facture", "")

    info_data = [
        ["Numero:", invoice.get("numero", ""), "Date:", date_heure],
        ["Reference:", invoice.get("reference", "") or "-", "Echeance:", invoice.get("date_echeance", "") or "-"],
        ["Statut:", invoice.get("statut", ""), "Paiement:", invoice.get("payment_method", "") or "-"],
        ["Devise:", invoice.get("devise", "XAF"), "", ""],
    ]
    
    info_table = Table(info_data, colWidths=[25*mm, 55*mm, 25*mm, 55*mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 4*mm))

    elements.append(Paragraph("<b>CLIENT</b>", styles["SmallLeft"]))
    client_data = [
        ["Nom:", invoice.get("tiers_nom", "")],
        ["NIU:", invoice.get("tiers_niu", "") or "-"],
        ["Adresse:", invoice.get("tiers_adresse", "") or "-"],
        ["Tel:", invoice.get("tiers_telephone", "") or "-"],
        ["Email:", invoice.get("tiers_email", "") or "-"],
    ]
    client_table = Table(client_data, colWidths=[20*mm, 140*mm])
    client_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    elements.append(client_table)
    elements.append(Spacer(1, 6*mm))

    header = ["#", "Designation", "Qte", "PU HT", "Brut", "Remise", "Apres remise", "TVA %", "TVA", "TTC"]
    lignes = invoice.get("lignes", [])
    table_data = [header]
    for i, l in enumerate(lignes):
        brut = l.get("subtotal") or (l.get("quantite", 1) * l.get("prix_unitaire", 0))
        remise = l.get("discount_amount") or l.get("remise_montant", 0) or 0
        table_data.append([
            str(i + 1),
            l.get("designation", ""),
            "{:.2f}".format(l.get("quantite", 1)),
            "{:,.0f}".format(l.get("prix_unitaire", 0)),
            "{:,.0f}".format(brut),
            "{:,.0f}".format(remise) if remise else "-",
            "{:,.0f}".format(l.get("montant_ht", 0)),
            "{}%".format(l.get("taux_tva", 18)),
            "{:,.0f}".format(l.get("montant_tva", 0)),
            "{:,.0f}".format(l.get("montant_ttc", 0)),
        ])

    col_widths = [7*mm, 40*mm, 12*mm, 17*mm, 16*mm, 16*mm, 18*mm, 12*mm, 17*mm, 17*mm]
    items_table = Table(table_data, colWidths=col_widths)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.15, 0.35, 0.6)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.7, 0.7, 0.7)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.97, 1.0)]),
    ]
    items_table.setStyle(TableStyle(style_cmds))
    elements.append(items_table)

    elements.append(Spacer(1, 4*mm))

    totals_data = [
        ["Total HT (brut):", "{:,.0f}".format(invoice.get("montant_ht_brut", 0))],
        ["Remises lignes:", "{:,.0f}".format(invoice.get("total_line_discount_amount", 0))],
        ["Remise globale:", "{:,.0f}".format(invoice.get("discount_amount", 0))],
        ["Total HT (net):", "{:,.0f}".format(invoice.get("montant_ht", 0))],
        ["TVA 18%:", "{:,.0f}".format(invoice.get("total_tax_t_amount", 0))],
        ["TVA 5%:", "{:,.0f}".format(invoice.get("total_tax_r_amount", 0))],
        ["Exonere:", "{:,.0f}".format(invoice.get("total_exempt_amount", 0))],
        ["Centime additionnel:", "{:,.0f}".format(invoice.get("additional_cent_tax", 0))],
        ["Total TVA:", "{:,.0f}".format(invoice.get("montant_tva", 0))],
        ["TOTAL TTC:", "{:,.0f}".format(invoice.get("montant_ttc", 0))],
        ["Net a payer:", "{:,.0f}".format(invoice.get("montant_restant", invoice.get("montant_ttc", 0)))],
    ]
    totals_table = Table(totals_data, colWidths=[45*mm, 35*mm])
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, -2), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -2), (-1, -2), 1, colors.Color(0.2, 0.5, 0.8)),
    ]))
    wrapper = Table([[totals_table]], colWidths=[176*mm])
    wrapper.setStyle(TableStyle([("ALIGN", (0, 0), (0, 0), "RIGHT")]))
    elements.append(wrapper)
    elements.append(Spacer(1, 4*mm))

    montant_lettres = _montant_en_lettres(invoice.get("montant_ttc", 0))
    if montant_lettres:
        elements.append(Paragraph("Arrete la presente facture a la somme de : {}".format(montant_lettres), styles["SmallLeft"]))
   
    if invoice.get("notes"):
        elements.append(Spacer(1, 6*mm))
        elements.append(Paragraph("<b>Notes:</b> {}".format(invoice["notes"]), styles["SmallLeft"]))

    sfec_num = invoice.get("sfec_num_certif") or invoice.get("sfec_certification_number", "")
    sfec_date = invoice.get("sfec_date_certif") or invoice.get("sfec_certification_date", "")
    sfec_sig = invoice.get("sfec_signature", "")
    sfec_qr = invoice.get("sfec_qr_code", "")

    if sfec_num:
        elements.append(Spacer(1, 6*mm))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        elements.append(Spacer(1, 3*mm))
        elements.append(Paragraph("<b>SFEC - Systeme de Facturation Electronique Certifie</b>", styles["SmallLeft"]))

        cert_lines = ["Numero: {}".format(sfec_num)]
        if sfec_date:
            cert_lines.append("Date: {}".format(sfec_date))
        if sfec_sig:
            cert_lines.append("Signature: {}".format(sfec_sig))

        qr_buf = _decode_qr_image(sfec_qr) if sfec_qr else None
        if qr_buf is None and sfec_qr:
            qr_buf = _generate_qr_image(sfec_qr)
        if qr_buf:
            cert_table = Table(
                [[Paragraph("<br/>".join(cert_lines), styles["SmallLeft"]),
                  Image(qr_buf, width=25*mm, height=25*mm)]],
                colWidths=[140*mm, 30*mm]
            )
            cert_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
            elements.append(cert_table)
        else:
            for line in cert_lines:
                elements.append(Paragraph(line, styles["SmallLeft"]))

    elements.append(Spacer(1, 10*mm))
    footer_text = company.get("invoice_footer", "") or company.get("slogan", "")
    if footer_text:
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        elements.append(Spacer(1, 2*mm))
        elements.append(Paragraph(footer_text, styles["SubTitle"]))

    doc.build(elements)

    if output_path:
        return output_path
    else:
        buf.seek(0)
        return buf.read()


def generate_ticket_pdf(ticket, output_path=None):
    if not HAS_REPORTLAB:
        return None

    company = _get_company()
    styles = _styles()
    page_size = A5

    if output_path:
        doc = SimpleDocTemplate(output_path, pagesize=page_size,
                                leftMargin=10*mm, rightMargin=10*mm,
                                topMargin=10*mm, bottomMargin=10*mm)
    else:
        buf = BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=page_size,
                                leftMargin=10*mm, rightMargin=10*mm,
                                topMargin=10*mm, bottomMargin=10*mm)

    elements = []

    logo_path = company.get("logo_file_name", "")
    if logo_path and os.path.exists(logo_path):
        try:
            elements.append(Image(logo_path, width=35*mm, height=18*mm))
            elements.append(Spacer(1, 3*mm))
        except Exception:
            pass

    elements.append(Paragraph(company.get("name", "Mon Entreprise"), styles["Title2"]))
    if company.get("phone"):
        elements.append(Paragraph("Tel: {}".format(company["phone"]), styles["SubTitle"]))

    elements.append(Spacer(1, 4*mm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.black))
    elements.append(Spacer(1, 3*mm))

    elements.append(Paragraph("<b>TICKET DE VENTE</b>", styles["Title2"]))
    elements.append(Paragraph("N: {}".format(ticket.get("numero", "")), styles["SmallLeft"]))
    elements.append(Paragraph("Date: {}".format(ticket.get("date_ticket", "")), styles["SmallLeft"]))
    elements.append(Paragraph("Client: {}".format(ticket.get("tiers_nom", "Client comptoir")), styles["SmallLeft"]))
    elements.append(Paragraph("Caissier: {}".format(ticket.get("caissier", "") or "-"), styles["SmallLeft"]))
    elements.append(Spacer(1, 4*mm))

    header = ["Article", "Qte", "Prix", "Total"]
    table_data = [header]
    for l in ticket.get("lignes", []):
        table_data.append([
            l.get("designation", ""),
            str(l.get("quantite", 1)),
            "{:,.0f}".format(l.get("prix_unitaire", 0)),
            "{:,.0f}".format(l.get("montant_ttc", 0)),
        ])

    col_widths = [60*mm, 15*mm, 25*mm, 30*mm]
    items_table = Table(table_data, colWidths=col_widths)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.15, 0.35, 0.6)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.Color(0.7, 0.7, 0.7)),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 4*mm))

    total = ticket.get("montant_ttc", 0)
    recu = ticket.get("montant_recu", 0)
    monnaie = ticket.get("monnaie_rendue", 0)

    summary_data = [
        ["TOTAL TTC:", "{:,.0f} FCFA".format(total)],
        ["Mode:", ticket.get("mode_paiement", "Especes")],
        ["Recu:", "{:,.0f} FCFA".format(recu)],
        ["Monnaie:", "{:,.0f} FCFA".format(monnaie)],
    ]
    summary_table = Table(summary_data, colWidths=[30*mm, 50*mm])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
    ]))
    elements.append(summary_table)

    message = ""
    try:
        from app.storage import db as sqlite_db
        message = sqlite_db.get_setting("ticket_message", "")
    except Exception:
        pass
    if not message:
        message = "Merci pour votre achat !"

    elements.append(Spacer(1, 8*mm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Paragraph(message, styles["SubTitle"]))

    doc.build(elements)

    if output_path:
        return output_path
    else:
        buf.seek(0)
        return buf.read()
