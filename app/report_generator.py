import hashlib
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Dict, Any, List, Optional

import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether, Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT


def _d(value, places: str = "0.01") -> str:
    """Format a value (which may arrive as Decimal, str, float, or None)
    for display, without ever routing pass/fail-relevant numbers through
    a float. Returns a plain string; no rounding is applied to anything
    used for the pass/fail decision itself -- this is display-only."""
    if value is None or value == "":
        return "N/A"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return str(value)
    quant = Decimal(places)
    return str(dec.quantize(quant)) if dec == dec else str(dec)


def _dsigned(value) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return str(value)
    formatted = _d(dec)
    return f"+{formatted}" if dec >= 0 else formatted


def _verification_id(report_data: Dict[str, Any]) -> str:
    """Short, deterministic tamper-evidence hash. Not a substitute for real
    digital signatures, but lets a reviewer confirm a printed certificate's
    core facts (report id, instrument serial, result, approver) weren't
    altered after the fact, and gives the QR code something to encode."""
    raw = "|".join(str(x) for x in [
        report_data.get("id"),
        report_data.get("serial_number"),
        report_data.get("overall_result"),
        report_data.get("approved_by"),
        report_data.get("rule_version"),
        report_data.get("created_at"),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()


def _qr_flowable(data: str, size: float = 60) -> Image:
    qr = qrcode.QRCode(border=1, box_size=6)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Image(buf, width=size, height=size)


def generate_pdf_certificate(report_data: Dict[str, Any], observations: List[Dict[str, Any]]) -> bytes:
    buffer = BytesIO()

    is_approved = str(report_data.get("status", "")).upper() == "APPROVED"
    is_rejected = str(report_data.get("status", "")).upper() == "REJECTED"

    def draw_watermark(canvas, doc):
        canvas.saveState()
        if not is_approved:
            label = "REJECTED - NOT VALID" if is_rejected else "DRAFT - NOT YET CERTIFIED"
            canvas.setFont("Helvetica-Bold", 60)
            canvas.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.18))
            canvas.translate(letter[0] / 2, letter[1] / 2)
            canvas.rotate(38)
            canvas.drawCentredString(0, 0, label)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=18,
        leading=22, textColor=colors.HexColor('#0f172a'), alignment=TA_LEFT,
    )
    subtitle_style = ParagraphStyle(
        'DocSubTitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9,
        leading=12, textColor=colors.HexColor('#475569'), alignment=TA_LEFT,
    )
    section_heading = ParagraphStyle(
        'SectionHeading', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=11,
        leading=14, textColor=colors.HexColor('#1e293b'), spaceAfter=6,
    )
    cell_bold = ParagraphStyle('CellBold', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#0f172a'))
    cell_regular = ParagraphStyle('CellRegular', fontName='Helvetica', fontSize=8, leading=10, textColor=colors.HexColor('#334155'))
    cell_pass = ParagraphStyle('CellPass', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#059669'))
    cell_fail = ParagraphStyle('CellFail', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.HexColor('#dc2626'))
    cell_recorded = ParagraphStyle('CellRecorded', fontName='Helvetica-Oblique', fontSize=8, leading=10, textColor=colors.HexColor('#64748b'))
    footnote = ParagraphStyle('Foot', parent=cell_regular, fontSize=7, textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)

    status_style_map = {"PASS": cell_pass, "FAIL": cell_fail, "RECORDED": cell_recorded}

    verification_id = _verification_id(report_data)
    created_at = report_data.get("created_at") or "N/A"
    approved_at = report_data.get("approved_at") or "Pending"

    story = []

    # 1. Header
    header_table_data = [[
        Paragraph(
            "<b>LEGAL METROLOGY DIRECTORIES</b><br/><font size=7 color='#64748b'>OIML R-76 Technical Compliance Authority</font>",
            cell_regular,
        ),
        Paragraph(
            f"<b>CERTIFICATE NO:</b> CERT-{report_data['id']:05d}<br/><b>GENERATED:</b> {created_at}",
            ParagraphStyle('RightText', parent=cell_regular, alignment=TA_RIGHT),
        ),
    ]]
    header_table = Table(header_table_data, colWidths=[300, 240])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#4f46e5'), spaceAfter=15))

    # 2. Title
    doc_title = "VERIFICATION CERTIFICATE OF CONFORMITY" if is_approved else f"TEST REPORT ({report_data.get('status', 'DRAFT')} - NOT A CERTIFICATE)"
    story.append(Paragraph(doc_title, title_style))
    story.append(Paragraph("Non-Automatic Weighing Instrument (NAWI) - Legal Metrology Standard OIML R-76-1", subtitle_style))
    if report_data.get('revision_number', 1) and report_data.get('revision_number', 1) > 1:
        story.append(Paragraph(
            f"Revision {report_data['revision_number']} - supersedes Report #{report_data.get('parent_report_id')}",
            subtitle_style,
        ))
    story.append(Spacer(1, 15))

    # 3. Instrument specifications
    story.append(Paragraph("1. INSTRUMENT & VERIFICATION SPECIFICATIONS", section_heading))
    inst_specs = [
        [Paragraph("Manufacturer:", cell_bold), Paragraph(str(report_data.get('manufacturer', 'N/A')), cell_regular),
         Paragraph("Accuracy Class:", cell_bold), Paragraph(f"Class {report_data.get('accuracy_class', 'N/A')}", cell_regular)],
        [Paragraph("Model Number:", cell_bold), Paragraph(str(report_data.get('model', 'N/A')), cell_regular),
         Paragraph("Max Capacity (Max):", cell_bold), Paragraph(f"{_d(report_data.get('capacity'))} {report_data.get('unit')}", cell_regular)],
        [Paragraph("Serial Number:", cell_bold), Paragraph(str(report_data.get('serial_number', 'N/A')), cell_regular),
         Paragraph("Scale Interval (d):", cell_bold), Paragraph(f"{_d(report_data.get('d'))} {report_data.get('unit')}", cell_regular)],
        [Paragraph("Report Session ID:", cell_bold), Paragraph(f"#{report_data['id']}", cell_regular),
         Paragraph("Verification Interval (e):", cell_bold), Paragraph(f"{_d(report_data.get('e'))} {report_data.get('unit')}", cell_regular)],
        [Paragraph("Inspection Status:", cell_bold), Paragraph(str(report_data.get('status', 'DRAFT')), cell_bold),
         Paragraph("Resolution Count (n):", cell_bold), Paragraph(f"{_d(report_data.get('n'), '1')} e", cell_regular)],
        [Paragraph("Test Location:", cell_bold), Paragraph(str(report_data.get('test_location') or 'N/A'), cell_regular),
         Paragraph("Reference Equipment:", cell_bold), Paragraph(str(report_data.get('reference_equipment') or 'N/A'), cell_regular)],
    ]
    spec_table = Table(inst_specs, colWidths=[120, 150, 130, 140])
    spec_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(spec_table)
    story.append(Spacer(1, 15))

    # 4. Observations
    story.append(Paragraph("2. EVALUATION & METROLOGICAL OBSERVATIONS", section_heading))
    obs_headers = ["Test Category", "Load Position", "Reference Load", "Indicated Value", "Error (E)", "Allowed MPE", "Result"]
    obs_rows = [[Paragraph(f"<b>{h}</b>", cell_bold) for h in obs_headers]]

    for o in observations:
        status_style = status_style_map.get(o['status'], cell_regular)
        obs_rows.append([
            Paragraph(str(o['test_type']), cell_regular),
            Paragraph(str(o.get('position') or 'Centre'), cell_regular),
            Paragraph(f"{_d(o['reference_load'])} {report_data.get('unit')}", cell_regular),
            Paragraph(f"{_d(o['observed_value'])} {report_data.get('unit')}", cell_regular),
            Paragraph(_dsigned(o['calculated_error']), cell_regular),
            Paragraph(f"\u00b1{_d(o['mpe'])}", cell_regular),
            Paragraph(str(o['status']), status_style),
        ])

    obs_table = Table(obs_rows, colWidths=[130, 80, 75, 75, 60, 60, 60], repeatRows=1)
    obs_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(obs_table)
    story.append(Paragraph(
        "Note: individual Repeatability run readings are marked RECORDED (informational only). "
        "The repeatability PASS/FAIL verdict is carried solely by the 'Repeatability (Spread Summary)' row, "
        "which compares max-min spread against the load-appropriate MPE.",
        footnote,
    ))
    story.append(Spacer(1, 15))

    # 5. Environmental conditions present?
    env_rows = [o for o in observations if str(o['test_type']).startswith("Environmental")]
    if env_rows:
        story.append(Paragraph("3. ENVIRONMENTAL / TILT CONDITIONS RECORDED", section_heading))
        env_headers = ["Condition", "Temp (\u00b0C)", "Tilt (\u00b0)", "Humidity (%)", "Result"]
        env_table_rows = [[Paragraph(f"<b>{h}</b>", cell_bold) for h in env_headers]]
        for o in env_rows:
            status_style = status_style_map.get(o['status'], cell_regular)
            env_table_rows.append([
                Paragraph(str(o['test_type']).replace("Environmental (", "").rstrip(")"), cell_regular),
                Paragraph(_d(o.get('temperature_c')), cell_regular),
                Paragraph(_d(o.get('tilt_degrees')), cell_regular),
                Paragraph(_d(o.get('humidity_pct')), cell_regular),
                Paragraph(str(o['status']), status_style),
            ])
        env_table = Table(env_table_rows, colWidths=[170, 90, 90, 90, 100])
        env_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e2e8f0')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(env_table)
        story.append(Spacer(1, 15))

    # 6. Compliance determination
    section_num = "4" if env_rows else "3"
    story.append(Paragraph(f"{section_num}. COMPLIANCE DETERMINATION & AUTHORIZATION", section_heading))

    overall_res = str(report_data.get('overall_result', 'PENDING')).upper()
    if not is_approved:
        display_res = f"{overall_res} (PROVISIONAL - PENDING APPROVAL)" if not is_rejected else f"{overall_res} (REJECTED)"
        result_bg = colors.HexColor('#fef3c7')
        result_color = colors.HexColor('#b45309')
    else:
        display_res = overall_res
        result_bg = colors.HexColor('#dcfce7') if overall_res == 'PASS' else colors.HexColor('#fee2e2')
        result_color = colors.HexColor('#15803d') if overall_res == 'PASS' else colors.HexColor('#b91c1c')

    summary_box_data = [
        [Paragraph("<b>OVERALL COMPLIANCE STATUS:</b>", cell_bold),
         Paragraph(f"<b>{display_res}</b>", ParagraphStyle('Res', parent=cell_bold, fontSize=12, textColor=result_color))],
        [Paragraph("Evaluating Technician:", cell_bold), Paragraph(str(report_data.get('technician_name', 'N/A')), cell_regular)],
        [Paragraph("Approval Lead / Signatory:", cell_bold), Paragraph(str(report_data.get('approved_by') or 'Pending Approval'), cell_regular)],
        [Paragraph("Approval Date:", cell_bold), Paragraph(str(approved_at), cell_regular)],
        [Paragraph("Rule / Table Version Applied:", cell_bold), Paragraph(str(report_data.get('rule_version', 'N/A')), cell_regular)],
    ]
    if is_rejected and report_data.get('rejection_reason'):
        summary_box_data.append([
            Paragraph("Rejection Reason:", cell_bold), Paragraph(str(report_data['rejection_reason']), cell_regular),
        ])

    summary_table = Table(summary_box_data, colWidths=[180, 360])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BACKGROUND', (1, 0), (1, 0), result_bg),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 20))

    # 7. Signature + QR verification block
    sig_block = []
    qr_flowable = _qr_flowable(f"NAWI-CERT|report={report_data['id']}|status={report_data.get('status')}|vid={verification_id}")
    verify_table = Table(
        [[
            qr_flowable,
            Paragraph(
                f"<b>Verification ID:</b> {verification_id}<br/>"
                f"<font size=7 color='#64748b'>Scan or quote this ID to confirm this certificate's report id, "
                f"result, and approver have not been altered since generation. This is a content-integrity check, "
                f"not a cryptographic digital signature.</font>",
                cell_regular,
            ),
        ]],
        colWidths=[70, 470],
    )
    verify_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    sig_block.append(verify_table)
    sig_block.append(Spacer(1, 20))

    sig_data = [[
        Paragraph("__________________________________________<br/><b>Verified By (Technician)</b>", cell_regular),
        Paragraph("__________________________________________<br/><b>Authorized Lead Signatory</b>", cell_regular),
    ]]
    sig_table = Table(sig_data, colWidths=[270, 270])
    sig_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ]))
    sig_block.append(sig_table)
    sig_block.append(Spacer(1, 15))
    sig_block.append(Paragraph(
        "This certificate verifies compliance with legal metrology directives. Any tampering or uncalibrated "
        "adjustment renders this verification invalid.",
        footnote,
    ))

    story.append(KeepTogether(sig_block))

    doc.build(story, onFirstPage=draw_watermark, onLaterPages=draw_watermark)
    pdf_val = buffer.getvalue()
    buffer.close()
    return pdf_val