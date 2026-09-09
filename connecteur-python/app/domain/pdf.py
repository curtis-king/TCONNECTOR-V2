import os
import logging
from io import BytesIO
from app.config.manager import get_config

logger = logging.getLogger("t-connector.pdf")

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
    addr_parts = [p for p in [company.get("address", ""), company.get("city", ""), company.get("country", "")] if p]
    if addr_parts:
        elements.append(Paragraph(", ".join(addr_parts), styles["SubTitle"]))
    if company.get("tax_number"):
        elements.append(Paragraph("NIU: {}".format(company["tax_number"]), styles["SubTitle"]))
    if company.get("phone") or company.get("email"):
        elements.append(Paragraph("Tel: {} | Email: {}".format(company.get("phone", ""), company.get("email", "")), styles["SubTitle"]))

    elements.append(Spacer(1, 6*mm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.Color(0.2, 0.5, 0.8)))
    elements.append(Spacer(1, 4*mm))

    elements.append(Paragraph("<b>FACTURE DE VENTE</b>", styles["Title2"]))

    info_data = [
        ["Numero:", invoice.get("numero", ""), "Date:", invoice.get("date_facture", "")],
        ["Reference:", invoice.get("reference", "") or "-", "Echeance:", invoice.get("date_echeance", "") or "-"],
        ["Statut:", invoice.get("statut", ""), "", ""],
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

    header = ["#", "Designation", "Qte", "Prix unit.", "TVA %", "HT", "TVA", "TTC"]
    lignes = invoice.get("lignes", [])
    table_data = [header]
    for i, l in enumerate(lignes):
        table_data.append([
            str(i + 1),
            l.get("designation", ""),
            "{:.2f}".format(l.get("quantite", 1)),
            "{:,.0f}".format(l.get("prix_unitaire", 0)),
            "{}%".format(l.get("taux_tva", 18)),
            "{:,.0f}".format(l.get("montant_ht", 0)),
            "{:,.0f}".format(l.get("montant_tva", 0)),
            "{:,.0f}".format(l.get("montant_ttc", 0)),
        ])

    table_data.append(["", "", "", "", "TOTAL HT", "{:,.0f}".format(invoice.get("montant_ht", 0)), "", ""])
    table_data.append(["", "", "", "", "TVA", "", "{:,.0f}".format(invoice.get("montant_tva", 0)), ""])
    table_data.append(["", "", "", "", "TOTAL TTC", "", "", "{:,.0f}".format(invoice.get("montant_ttc", 0))])

    col_widths = [8*mm, 50*mm, 15*mm, 22*mm, 15*mm, 22*mm, 22*mm, 22*mm]
    items_table = Table(table_data, colWidths=col_widths)

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.15, 0.35, 0.6)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, len(lignes)), 0.5, colors.Color(0.7, 0.7, 0.7)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, len(lignes)), [colors.white, colors.Color(0.95, 0.97, 1.0)]),
    ]

    total_row_idx = len(lignes) + 1
    style_cmds.append(("FONTNAME", (4, total_row_idx), (-1, -1), "Helvetica-Bold"))
    style_cmds.append(("LINEABOVE", (4, total_row_idx), (-1, total_row_idx), 1, colors.Color(0.2, 0.5, 0.8)))
    style_cmds.append(("BACKGROUND", (4, -3), (-1, -1), colors.Color(0.93, 0.96, 1.0)))

    items_table.setStyle(TableStyle(style_cmds))
    elements.append(items_table)

    if invoice.get("notes"):
        elements.append(Spacer(1, 6*mm))
        elements.append(Paragraph("<b>Notes:</b> {}".format(invoice["notes"]), styles["SmallLeft"]))

    sfec_num = invoice.get("sfec_num_certif") or invoice.get("sfec_certification_number", "")
    sfec_date = invoice.get("sfec_certification_date", "")
    if sfec_num:
        elements.append(Spacer(1, 6*mm))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        elements.append(Spacer(1, 3*mm))
        elements.append(Paragraph("<b>Certification SFEC</b>", styles["SmallLeft"]))
        elements.append(Paragraph("Numero: {}".format(sfec_num), styles["SmallLeft"]))
        if sfec_date:
            elements.append(Paragraph("Date: {}".format(sfec_date), styles["SmallLeft"]))

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
