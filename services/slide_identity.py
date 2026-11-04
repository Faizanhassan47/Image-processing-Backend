"""Match a slide barcode to the case accession. A missing barcode is not a match."""

from __future__ import annotations


def slide_barcode(validation: dict) -> str | None:
    metadata = validation.get("normalized_metadata") or {}
    value = metadata.get("barcode") or metadata.get("slide_label")
    text = str(value).strip() if value else ""
    return text or None


def identity_conflict(connect, case_pk: int, validation: dict) -> str | None:
    barcode = slide_barcode(validation)
    if not barcode:
        return None
    with connect() as db:
        case_row = db.execute("SELECT case_id FROM cases WHERE id=?", (case_pk,)).fetchone()
        accessions = db.execute("SELECT accession_number FROM accessions WHERE case_id=?", (case_pk,)).fetchall()
    allowed = set()
    if case_row and case_row.get("case_id"):
        allowed.add(str(case_row["case_id"]))
    allowed.update(str(row["accession_number"]) for row in accessions if row.get("accession_number"))
    if barcode not in allowed:
        return f"Slide barcode {barcode} does not match the case or accession"
    return None
