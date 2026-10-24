"""
Pathology Case & Slide Reports API.
Endpoints for managing draft, finalized, PDF, HTML, and CSV pathology reports.
"""

from contextlib import closing
import csv
from datetime import date, datetime, timezone
import html
from io import StringIO
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fpdf import FPDF
from pydantic import BaseModel, Field

from core.security import ADMIN_ROLES, CLINICAL_READ_ROLES, ROLE_PATHOLOGIST


def _pdf_text(value):
    """Keep user-provided report text safe for FPDF's core Latin-1 fonts."""
    return str(value or "").replace("\r\n", "\n").encode("latin-1", "replace").decode("latin-1")


def _format_pdf_date(value):
    if not value:
        return "Not recorded"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _pdf_text(value)
    return value.strftime("%d %b %Y")


def _patient_age(date_of_birth, at_date=None):
    if not isinstance(date_of_birth, date):
        return "Not recorded"
    reference = (at_date.date() if isinstance(at_date, datetime) else at_date) or date.today()
    return str(reference.year - date_of_birth.year - ((reference.month, reference.day) < (date_of_birth.month, date_of_birth.day)))


class ReportFindingCreate(BaseModel):
    slide_id: Optional[int] = None
    annotation_id: Optional[int] = None
    text: str = Field(default="", max_length=4000)
    sort_order: int = 0


class ReportUpdate(BaseModel):
    specimen_clinical: str = Field(default="", max_length=4000)
    gross_description: str = Field(default="", max_length=4000)
    microscopic_findings: str = Field(default="", max_length=4000)
    diagnosis: str = Field(default="", max_length=4000)
    comments: str = Field(default="", max_length=4000)
    reviewer_name: str = Field(default="", max_length=120)
    findings: List[ReportFindingCreate] = []


def create_reports_router(connect, require_roles, get_current_user, get_case_fn, require_assigned_case_fn, audit):
    router = APIRouter(tags=["Reports"])

    pathologist_and_admin = ADMIN_ROLES + [ROLE_PATHOLOGIST]

    def serialize_report(row):
        report = dict(row)
        with closing(connect()) as db:
            findings = db.execute(
                """SELECT rf.*, s.label AS slide_label, a.label AS annotation_label,
                          a.category AS annotation_category, a.source_type AS annotation_source_type
                   FROM report_findings rf
                   LEFT JOIN slides s ON s.id = rf.slide_id
                   LEFT JOIN annotations a ON a.id = rf.annotation_id
                   WHERE rf.report_id = ?
                   ORDER BY rf.sort_order""",
                (report["id"],)
            ).fetchall()
            patient = db.execute(
                """SELECT TOP 1 p.display_label, p.patient_identifier, p.medical_record_number
                   FROM accessions a
                   JOIN patients p ON p.id = a.patient_id
                   WHERE a.case_id = ?
                   ORDER BY a.created_at DESC""",
                (report["case_id"],),
            ).fetchone()
        report["findings"] = [dict(f) for f in findings]
        if patient:
            report["patient_name"] = patient["display_label"]
            report["patient_identifier"] = patient["patient_identifier"]
            report["medical_record_number"] = patient["medical_record_number"]
        return report

    @router.get("/api/cases/{case_pk}/reports", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def list_reports(case_pk: int, current_user: dict = Depends(get_current_user)):
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        with closing(connect()) as db:
            rows = db.execute(
                "SELECT * FROM reports WHERE case_id = ? ORDER BY version DESC",
                (case_pk,)
            ).fetchall()
        return [serialize_report(row) for row in rows]

    @router.get("/api/cases/{case_pk}/ai-report-draft", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def ai_report_draft(case_pk: int, current_user: dict = Depends(get_current_user)):
        """Create a review-only microscopic-findings draft from completed AI outputs."""
        case = get_case_fn(case_pk)
        require_assigned_case_fn(case, current_user)
        with closing(connect()) as db:
            rows = db.execute(
                """SELECT r.result_type, r.measurements_json, r.raw_output_json, r.confidence, r.review_status,
                          j.completed_at, s.label AS slide_label, s.stain,
                          m.name AS model_name, m.version AS model_version, m.limitations
                   FROM slides s
                   JOIN ai_jobs j ON j.slide_id = s.id
                   JOIN ai_results r ON r.ai_job_id = j.id
                   JOIN ai_models m ON m.id = j.model_id
                   WHERE s.case_id = ? AND j.status = 'Completed'
                   ORDER BY s.id, j.completed_at, r.id""",
                (case_pk,),
            ).fetchall()
        if not rows:
            raise HTTPException(404, "No completed AI analysis is available for this case")

        lines = [
            "AI-ASSISTED MICROSCOPIC FINDINGS DRAFT",
            "Research-use output only. This draft must be reviewed, edited, and clinically correlated by the reporting pathologist; it is not a final diagnosis.",
            "",
        ]
        for row in rows:
            confidence = f"; confidence {float(row['confidence']) * 100:.1f}%" if row['confidence'] is not None else ""
            review = f"; review status {row['review_status']}" if row['review_status'] else ""
            slide = row['slide_label'] or "Unlabelled slide"
            stain = f" ({row['stain']})" if row['stain'] else ""
            lines.append(f"- {slide}{stain}: {row['model_name']} {row['model_version'] or ''} reported {row['result_type']}{confidence}{review}.")
            try:
                measurements = json.loads(row['measurements_json'] or "{}")
            except (TypeError, json.JSONDecodeError):
                measurements = {}
            if isinstance(measurements, dict):
                values = []
                for key, value in measurements.items():
                    if isinstance(value, (str, int, float, bool)) and value not in (None, ""):
                        values.append(f"{str(key).replace('_', ' ')}: {value}")
                    if len(values) == 5:
                        break
                if values:
                    lines.append(f"  Measurements: {'; '.join(values)}.")
            if row['limitations']:
                lines.append(f"  Model limitation: {row['limitations']}")

        draft = "\n".join(lines)
        return {"microscopic_findings": draft[:4000], "result_count": len(rows), "disclaimer": lines[1]}

    @router.post("/api/cases/{case_pk}/reports", status_code=201, dependencies=[Depends(require_roles(pathologist_and_admin))])
    def create_draft_report(case_pk: int, current_user: dict = Depends(get_current_user)):
        require_assigned_case_fn(get_case_fn(case_pk), current_user)
        with closing(connect()) as db:
            existing_draft = db.execute(
                "SELECT * FROM reports WHERE case_id = ? AND status = 'Draft'", (case_pk,)
            ).fetchone()
            if existing_draft:
                return serialize_report(existing_draft)

            latest = db.execute(
                "SELECT TOP 1 version FROM reports WHERE case_id = ? ORDER BY version DESC",
                (case_pk,)
            ).fetchone()
            next_version = (latest["version"] + 1) if latest else 1

            prev = db.execute(
                "SELECT TOP 1 * FROM reports WHERE case_id = ? AND status = 'Finalized' ORDER BY version DESC",
                (case_pk,)
            ).fetchone()

            if prev:
                cursor = db.execute(
                    """INSERT INTO reports 
                       (case_id, version, status, specimen_clinical, gross_description, 
                        microscopic_findings, diagnosis, comments, reviewer_name)
                       VALUES (?, ?, 'Draft', ?, ?, ?, ?, ?, ?)""",
                    (case_pk, next_version, prev["specimen_clinical"], prev["gross_description"],
                     prev["microscopic_findings"], prev["diagnosis"], prev["comments"], current_user["display_name"])
                )
                new_id = cursor.lastrowid
                findings = db.execute("SELECT * FROM report_findings WHERE report_id = ?", (prev["id"],)).fetchall()
                for f in findings:
                    db.execute(
                        "INSERT INTO report_findings (report_id, slide_id, annotation_id, text, sort_order) VALUES (?, ?, ?, ?, ?)",
                        (new_id, f["slide_id"], f["annotation_id"], f["text"], f["sort_order"])
                    )
            else:
                cursor = db.execute(
                    "INSERT INTO reports (case_id, version, status, reviewer_name) VALUES (?, ?, 'Draft', ?)",
                    (case_pk, next_version, current_user['display_name'])
                )
                new_id = cursor.lastrowid

            audit(db, current_user["username"], "report.create_draft", "report", new_id, f"case_id={case_pk}; version={next_version}")
            db.commit()
            row = db.execute("SELECT * FROM reports WHERE id = ?", (new_id,)).fetchone()
        return serialize_report(row)

    @router.patch("/api/reports/{report_id}", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def update_report(report_id: int, update: ReportUpdate, current_user: dict = Depends(get_current_user)):
        with closing(connect()) as db:
            report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(404, "Report not found")
            require_assigned_case_fn(get_case_fn(report["case_id"]), current_user)
            if report["status"] != "Draft":
                raise HTTPException(400, "Cannot edit a finalized report")

            db.execute(
                """UPDATE reports SET 
                   specimen_clinical=?, gross_description=?, microscopic_findings=?, 
                   diagnosis=?, comments=?, reviewer_name=?
                   WHERE id = ?""",
                (update.specimen_clinical.strip(), update.gross_description.strip(),
                 update.microscopic_findings.strip(), update.diagnosis.strip(),
                 update.comments.strip(), current_user["display_name"], report_id)
            )

            db.execute("DELETE FROM report_findings WHERE report_id = ?", (report_id,))
            for f in update.findings:
                if f.slide_id:
                    slide = db.execute("SELECT id, case_id FROM slides WHERE id = ?", (f.slide_id,)).fetchone()
                    if not slide or slide["case_id"] != report["case_id"]:
                        raise HTTPException(400, f"Slide {f.slide_id} is invalid or not in this case")
                if f.annotation_id:
                    ann = db.execute("SELECT id, slide_id FROM annotations WHERE id = ?", (f.annotation_id,)).fetchone()
                    if not ann or ann["slide_id"] != f.slide_id:
                        raise HTTPException(400, f"Annotation {f.annotation_id} is invalid or not in slide {f.slide_id}")

                db.execute(
                    "INSERT INTO report_findings (report_id, slide_id, annotation_id, text, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (report_id, f.slide_id, f.annotation_id, f.text.strip(), f.sort_order)
                )
            audit(db, current_user["username"], "report.update", "report", report_id, f"findings={len(update.findings)}")
            db.commit()
            row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return serialize_report(row)

    @router.post("/api/reports/{report_id}/finalize", dependencies=[Depends(require_roles(pathologist_and_admin))])
    def finalize_report(report_id: int, current_user: dict = Depends(get_current_user)):
        with closing(connect()) as db:
            report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(404, "Report not found")
            require_assigned_case_fn(get_case_fn(report["case_id"]), current_user)
            if report["status"] == "Finalized":
                return serialize_report(report)

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            db.execute("UPDATE reports SET status = 'Finalized', finalized_at = ? WHERE id = ?", (now, report_id))
            db.execute(
                "UPDATE cases SET status = 'Reviewed' WHERE id = ? AND CAST(status AS NVARCHAR(100)) <> 'Reviewed'",
                (report["case_id"],),
            )
            audit(db, current_user["username"], "report.finalize", "report", report_id, f"case_id={report['case_id']}; version={report['version']}")
            db.commit()
            row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return serialize_report(row)

    @router.get("/api/reports/{report_id}/pdf", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def export_report_pdf(report_id: int, current_user: dict = Depends(get_current_user)):
        with closing(connect()) as db:
            report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(404, "Report not found")
            case = db.execute(
                """SELECT c.*, a.accession_number, a.created_at AS accession_created_at,
                          p.patient_identifier, p.medical_record_number, p.display_label AS patient_name,
                          p.date_of_birth, p.sex
                   FROM cases c
                   OUTER APPLY (
                       SELECT TOP 1 ax.patient_id, ax.accession_number, ax.created_at
                       FROM accessions ax WHERE ax.case_id = c.id ORDER BY ax.created_at DESC
                   ) a
                   LEFT JOIN patients p ON p.id = a.patient_id
                   WHERE c.id = ?""",
                (report["case_id"],),
            ).fetchone()
            if not case:
                raise HTTPException(404, "Case not found")
            require_assigned_case_fn(case, current_user)
            specimens = db.execute(
                """SELECT s.specimen_code, s.specimen_type, s.description
                   FROM accessions a JOIN specimens s ON s.accession_id = a.id
                   WHERE a.case_id = ? ORDER BY s.created_at, s.id""",
                (case["id"],),
            ).fetchall()
            slides = db.execute(
                """SELECT id, label, stain, specimen_description, created_at
                   FROM slides WHERE case_id = ? ORDER BY created_at, id""",
                (case["id"],),
            ).fetchall()
            findings = db.execute(
                """SELECT rf.*, s.label AS slide_label, a.label AS annotation_label,
                          a.category AS annotation_category, a.source_type AS annotation_source_type
                   FROM report_findings rf
                   LEFT JOIN slides s ON s.id = rf.slide_id
                   LEFT JOIN annotations a ON a.id = rf.annotation_id
                   WHERE rf.report_id = ?
                   ORDER BY rf.sort_order""",
                (report_id,),
            ).fetchall()

        pdf = FPDF(format="A4", unit="mm")
        pdf.set_auto_page_break(auto=True, margin=16)
        pdf.set_margins(12, 12, 12)
        pdf.add_page()
        page_width = pdf.w - pdf.l_margin - pdf.r_margin
        light_blue = (228, 239, 248)
        medium_blue = (47, 111, 162)
        dark_blue = (19, 60, 104)
        border_blue = (159, 191, 218)

        def line_break(height=4):
            pdf.ln(height)

        def section(title, content, emphasized=False):
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.set_fill_color(*(medium_blue if emphasized else light_blue))
            pdf.set_text_color(*(255, 255, 255) if emphasized else dark_blue)
            pdf.set_draw_color(*border_blue)
            pdf.set_font("helvetica", "B", 10.5)
            pdf.cell(page_width, 7, _pdf_text(title), border=1, ln=True, fill=True)
            start_y = pdf.get_y()
            pdf.set_text_color(48, 68, 88)
            pdf.set_font("helvetica", "", 9.5)
            pdf.multi_cell(page_width, 5, _pdf_text(content or "Not recorded."), border=0)
            end_y = pdf.get_y()
            pdf.rect(pdf.l_margin, start_y, page_width, max(11, end_y - start_y), style="D")
            if end_y - start_y < 11:
                pdf.set_y(start_y + 11)
            line_break(3)

        pdf.set_text_color(*dark_blue)
        pdf.set_font("helvetica", "B", 19)
        pdf.cell(68, 12, "HELLO LICA", ln=0)
        pdf.set_font("helvetica", "", 8)
        pdf.set_xy(pdf.l_margin, pdf.get_y() + 8)
        pdf.cell(68, 5, "Digital Pathology Laboratory")
        pdf.set_xy(pdf.l_margin + 74, 13)
        pdf.set_font("helvetica", "B", 16)
        pdf.cell(76, 9, "Surgical Pathology Report", align="C")
        pdf.set_xy(pdf.l_margin + 74, 23)
        pdf.set_font("helvetica", "", 8)
        pdf.set_text_color(100, 125, 151)
        pdf.cell(76, 5, "Precision Pathology | Digital Intelligence | Better Care", align="C")
        pdf.set_text_color(48, 68, 88)
        header_details_x = pdf.l_margin + 132
        pdf.set_xy(header_details_x, 12)
        pdf.set_font("helvetica", "", 8.5)
        report_number = f"REP-{datetime.now(timezone.utc).year}-{report['id']:04d}"
        header_rows = (("Report No.", report_number), ("Case No.", case['case_id']), ("Report Date", _format_pdf_date(report['finalized_at'] or report['created_at'])), ("Status", report['status']))
        for label, value in header_rows:
            pdf.cell(20, 5, f"{label}:")
            pdf.set_font("helvetica", "B", 8.5)
            pdf.cell(34, 5, _pdf_text(value), ln=True)
            pdf.set_x(header_details_x)
            pdf.set_font("helvetica", "", 8.5)
        pdf.set_y(39)

        pdf.set_fill_color(*light_blue)
        pdf.set_text_color(*dark_blue)
        pdf.set_font("helvetica", "B", 10.5)
        pdf.cell(page_width, 7, "Patient / Case Information", border=1, ln=True, fill=True)
        info_top = pdf.get_y()
        left_x = pdf.l_margin
        mid_x = left_x + (page_width / 2)
        report_date = report['finalized_at'] or report['created_at']
        left_rows = (("Patient Name", case['patient_name'] or "Not recorded"), ("Patient ID / MRN", case['patient_identifier'] or case['medical_record_number'] or "Not recorded"), ("Age / Sex", f"{_patient_age(case['date_of_birth'], report_date)} years / {case['sex'] or 'Not recorded'}"), ("Accession No.", case['accession_number'] or case['case_id']))
        right_rows = (("Specimen Type", ", ".join(str(s['specimen_type']) for s in specimens if s['specimen_type']) or "Not recorded"), ("Collection Date", _format_pdf_date(case['accession_created_at'] or case['created_at'])), ("Reported Date", _format_pdf_date(report['finalized_at'] or report['created_at'])), ("Pathologist", report['reviewer_name'] or case['reviewer'] or "Not assigned"))
        pdf.set_font("helvetica", "", 8.5)
        for index in range(4):
            y = info_top + index * 6.5
            pdf.set_xy(left_x + 4, y); pdf.set_font("helvetica", "", 8.5); pdf.cell(32, 5, f"{left_rows[index][0]}:")
            pdf.set_font("helvetica", "B", 8.5); pdf.cell(55, 5, _pdf_text(left_rows[index][1]))
            pdf.set_xy(mid_x + 4, y); pdf.set_font("helvetica", "", 8.5); pdf.cell(30, 5, f"{right_rows[index][0]}:")
            pdf.set_font("helvetica", "B", 8.5); pdf.cell(55, 5, _pdf_text(right_rows[index][1]))
        pdf.set_draw_color(*border_blue)
        pdf.rect(left_x, info_top, page_width, 29, style="D")
        pdf.line(mid_x, info_top, mid_x, info_top + 29)
        pdf.set_y(info_top + 33)

        specimen_text = report['specimen_clinical'] or "\n".join(filter(None, [f"{item['specimen_code']}: {item['description'] or item['specimen_type'] or ''}" for item in specimens]))
        section("Clinical History / Specimen Submitted", specimen_text)
        slide_lines = []
        for slide in slides:
            details = f": {slide['specimen_description']}" if slide['specimen_description'] else ""
            slide_lines.append(f"{slide['label']} - {slide['stain'] or 'Stain not recorded'}{details}")
        slide_summary = "\n".join(slide_lines)
        section("Digital Slides Reviewed", slide_summary or "No digital slides linked to this case.")
        section("Gross Description", report['gross_description'])
        section("Microscopic Description", report['microscopic_findings'])
        section("Final Diagnosis", report['diagnosis'], emphasized=True)
        section("Special Studies / IHC / Biomarkers", report['comments'])

        if findings:
            finding_text = []
            for index, finding in enumerate(findings, 1):
                links = []
                if finding['slide_id']:
                    links.append(f"Slide: {finding['slide_label'] or finding['slide_id']}")
                if finding['annotation_id']:
                    source = f" ({finding['annotation_source_type']})" if finding['annotation_source_type'] else ""
                    links.append(f"Annotation: {finding['annotation_label'] or finding['annotation_id']}{source}")
                suffix = f" [{'; '.join(links)}]" if links else ""
                finding_text.append(f"{index}. {finding['text'] or 'Finding not described.'}{suffix}")
            section("Linked Findings and Supporting Annotations", "\n".join(finding_text))

        ai_summary = "No AI-assisted annotations were linked to this report."
        ai_labels = [f['annotation_label'] for f in findings if f['annotation_source_type'] and 'ai' in f['annotation_source_type'].lower() and f['annotation_label']]
        if ai_labels:
            ai_summary = f"AI-assisted annotations included: {', '.join(ai_labels)}. These findings are supportive and require pathologist review and clinical correlation."
        section("AI-Assisted Review Summary", ai_summary)

        pdf.set_draw_color(*dark_blue)
        pdf.line(pdf.l_margin, pdf.get_y() + 2, pdf.l_margin + page_width, pdf.get_y() + 2)
        pdf.ln(5)
        pdf.set_font("helvetica", "B", 10)
        pdf.set_text_color(*dark_blue)
        pdf.cell(page_width, 6, "Report Sign-off", ln=True)
        pdf.set_font("helvetica", "", 8.5)
        pdf.cell(page_width / 2, 6, f"Reviewing Pathologist: {_pdf_text(report['reviewer_name'] or case['reviewer'] or 'Not assigned')}")
        pdf.cell(page_width / 2, 6, f"Verification status: {_pdf_text(report['status'])}", ln=True)
        pdf.set_font("helvetica", "I", 7.5)
        pdf.set_text_color(75, 95, 115)
        pdf.cell(page_width, 5, "Confidential Medical Record - Intended for authorized clinical use only.")

        output = bytes(pdf.output())
        return Response(content=output, media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=report_{case['case_id']}_v{report['version']}.pdf"
        })

    @router.get("/api/reports/{report_id}/csv", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def export_report_csv(report_id: int, current_user: dict = Depends(get_current_user)):
        with closing(connect()) as db:
            report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(404, "Report not found")
            case = db.execute("SELECT * FROM cases WHERE id = ?", (report["case_id"],)).fetchone()
            if not case:
                raise HTTPException(404, "Case not found")
            require_assigned_case_fn(case, current_user)
            findings = db.execute("SELECT * FROM report_findings WHERE report_id = ? ORDER BY sort_order", (report_id,)).fetchall()

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["case_id", "report_version", "status", "reviewer", "section", "value", "slide_id", "annotation_id"])
        for key, label in (
            ("specimen_clinical", "Specimen and clinical"),
            ("gross_description", "Gross description"),
            ("microscopic_findings", "Microscopic findings"),
            ("diagnosis", "Diagnosis"),
            ("comments", "Comments"),
        ):
            writer.writerow([case["case_id"], report["version"], report["status"], report["reviewer_name"], label, report[key], "", ""])
        for finding in findings:
            writer.writerow([case["case_id"], report["version"], report["status"], report["reviewer_name"], "Finding", finding["text"], finding["slide_id"] or "", finding["annotation_id"] or ""])
        return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=report_{case['case_id']}_v{report['version']}.csv"})

    @router.get("/api/reports/{report_id}/html", dependencies=[Depends(require_roles(CLINICAL_READ_ROLES))])
    def export_report_html(report_id: int, current_user: dict = Depends(get_current_user)):
        with closing(connect()) as db:
            report = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not report:
                raise HTTPException(404, "Report not found")
            case = db.execute("SELECT * FROM cases WHERE id = ?", (report["case_id"],)).fetchone()
            if not case:
                raise HTTPException(404, "Case not found")
            require_assigned_case_fn(case, current_user)

        sections = "".join(f"<section><h2>{html.escape(label)}</h2><p>{html.escape(str(report[key] or ''))}</p></section>" for key, label in (
            ("specimen_clinical", "Specimen and clinical"),
            ("gross_description", "Gross description"),
            ("microscopic_findings", "Microscopic findings"),
            ("diagnosis", "Diagnosis"),
            ("comments", "Comments"),
        ))
        document = f"<!doctype html><html><head><meta charset='utf-8'><title>Report {html.escape(case['case_id'])}</title></head><body><main><h1>Pathology report</h1><p>Case {html.escape(case['case_id'])} · Version {report['version']} · {html.escape(report['status'])}</p>{sections}<footer>Reviewer: {html.escape(report['reviewer_name'])}</footer></main></body></html>"
        return Response(document, media_type="text/html", headers={"Content-Disposition": f"attachment; filename=report_{case['case_id']}_v{report['version']}.html"})

    return router
