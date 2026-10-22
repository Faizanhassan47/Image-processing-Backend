"""
Pathology Case & Slide Reports API.
Endpoints for managing draft, finalized, PDF, HTML, and CSV pathology reports.
"""

from contextlib import closing
import csv
from datetime import datetime, timezone
import html
from io import StringIO
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fpdf import FPDF
from pydantic import BaseModel, Field

from core.security import ADMIN_ROLES, CLINICAL_READ_ROLES, ROLE_PATHOLOGIST


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
                "SELECT * FROM report_findings WHERE report_id = ? ORDER BY sort_order",
                (report["id"],)
            ).fetchall()
        report["findings"] = [dict(f) for f in findings]
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
            case = db.execute("SELECT * FROM cases WHERE id = ?", (report["case_id"],)).fetchone()
            if not case:
                raise HTTPException(404, "Case not found")
            require_assigned_case_fn(case, current_user)
            findings = db.execute("SELECT * FROM report_findings WHERE report_id = ? ORDER BY sort_order", (report_id,)).fetchall()

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", "B", 16)
        pdf.cell(0, 10, f"Pathology Report - Case {case['case_id']}", ln=True, align="C")

        pdf.set_font("helvetica", "", 10)
        pdf.cell(0, 6, f"Status: {report['status']} (Version {report['version']})", ln=True, align="C")
        if report['finalized_at']:
            pdf.cell(0, 6, f"Finalized: {report['finalized_at']}", ln=True, align="C")
        pdf.cell(0, 6, f"Reviewer: {report['reviewer_name']}", ln=True, align="C")
        pdf.ln(10)

        sections = [
            ("Specimen & Clinical", report['specimen_clinical']),
            ("Gross Description", report['gross_description']),
            ("Microscopic Findings", report['microscopic_findings']),
            ("Diagnosis", report['diagnosis']),
            ("Comments", report['comments']),
        ]

        for title, content in sections:
            if content:
                pdf.set_font("helvetica", "B", 12)
                pdf.cell(0, 8, title, ln=True)
                pdf.set_font("helvetica", "", 11)
                pdf.multi_cell(0, 6, content)
                pdf.ln(4)

        if findings:
            pdf.set_font("helvetica", "B", 12)
            pdf.cell(0, 8, "Specific Findings", ln=True)
            pdf.set_font("helvetica", "", 11)
            for idx, f in enumerate(findings, 1):
                text = f['text']
                if f['slide_id']:
                    text += f" [Linked to Slide {f['slide_id']}]"
                pdf.multi_cell(0, 6, f"{idx}. {text}")
                pdf.ln(2)

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
