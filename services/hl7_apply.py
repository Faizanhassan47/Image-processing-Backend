"""Commit inbound HL7 records with the same matching rules as the sandbox.

An acknowledgement is built only after the database commit succeeds.
A repeated message control ID does not create another patient, accession, or case.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

import os

from services.hl7_engine import (
    build_oru_r01,
    extract_canonical_records,
    generate_hl7_ack,
    parse_raw_hl7,
    validate_hl7_structure,
)
from services.hl7_mllp import send_mllp

logger = logging.getLogger("pathology.hl7.apply")


def apply_inbound_message(raw_payload: str, connect=None) -> str:
    if connect is None:
        from core.database import connect as connect

    parsed = parse_raw_hl7(raw_payload)
    errors = validate_hl7_structure(parsed)
    if parsed.get("type_code") == "OML":
        segments = parsed.get("segments", {})
        if "ORC" not in segments and "OBR" not in segments:
            errors.append("OML message requires an ORC or OBR segment.")
    if errors:
        return generate_hl7_ack(parsed, accepted=False, error_msg="; ".join(errors))

    canonical = extract_canonical_records(parsed)
    control_id = parsed.get("message_control_id") or uuid4().hex[:12]
    message_type = parsed.get("message_type") or "UNKNOWN"
    correlation_id = uuid4().hex

    with connect() as db:
        duplicate = db.execute(
            "SELECT acknowledgement FROM integration_events WHERE message_control_id=? AND status='Processed'",
            (control_id,),
        ).fetchone()
        if duplicate and duplicate.get("acknowledgement"):
            return duplicate["acknowledgement"]

        patient = canonical.get("patient")
        patient_id = None
        if patient and patient.get("medical_record_number"):
            mrn = patient["medical_record_number"]
            existing = db.execute("SELECT id FROM patients WHERE medical_record_number=?", (mrn,)).fetchone()
            if existing:
                patient_id = existing["id"]
                if parsed.get("trigger_event") == "A08":
                    db.execute(
                        "UPDATE patients SET first_name=?, last_name=?, display_label=?, date_of_birth=?, sex=?, phone=?, address=? WHERE id=?",
                        (
                            patient.get("first_name"),
                            patient.get("last_name"),
                            patient.get("display_label"),
                            patient.get("date_of_birth"),
                            patient.get("sex"),
                            patient.get("phone"),
                            patient.get("address"),
                            patient_id,
                        ),
                    )
            else:
                cursor = db.execute(
                    "INSERT INTO patients (medical_record_number, patient_identifier, display_label, first_name, last_name, date_of_birth, sex, phone, address, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        mrn,
                        patient.get("patient_identifier"),
                        patient.get("display_label"),
                        patient.get("first_name"),
                        patient.get("last_name"),
                        patient.get("date_of_birth"),
                        patient.get("sex"),
                        patient.get("phone"),
                        patient.get("address"),
                        "hl7-mllp",
                    ),
                )
                patient_id = cursor.lastrowid

        accession = canonical.get("accession")
        if accession and accession.get("accession_number") and patient_id:
            number = accession["accession_number"]
            existing_accession = db.execute(
                "SELECT id, case_id FROM accessions WHERE accession_number=?",
                (number,),
            ).fetchone()
            if existing_accession is None:
                case_code = number
                case_row = db.execute("SELECT id FROM cases WHERE case_id=?", (case_code,)).fetchone()
                if case_row is None:
                    case_cursor = db.execute(
                        "INSERT INTO cases (case_id, description, filename, status) VALUES (?,?, '', 'New')",
                        (case_code, accession.get("order_id") or "HL7 laboratory order"),
                    )
                    case_pk = case_cursor.lastrowid
                else:
                    case_pk = case_row["id"]
                db.execute(
                    "INSERT INTO accessions (patient_id, accession_number, order_id, priority, case_id) VALUES (?,?,?,?,?)",
                    (patient_id, number, accession.get("order_id"), accession.get("priority") or "Routine", case_pk),
                )

        ack = generate_hl7_ack(parsed, accepted=True)
        db.execute(
            "INSERT INTO integration_events (correlation_id, message_control_id, message_type, direction, raw_payload, normalized_json, status, acknowledgement, processed_at) VALUES (?,?,?,?,?,?, 'Processed', ?, SYSUTCDATETIME())",
            (correlation_id, control_id, message_type, "inbound", raw_payload, json.dumps(canonical), ack),
        )
        db.commit()
    logger.info("Committed HL7 %s control %s", message_type, control_id)
    return ack


def deliver_final_result(case_pk: int, diagnosis: str, connect=None) -> dict:
    """Queue and send an ORU for a finalized report. Failure is recorded and does not roll back the report."""
    if connect is None:
        from core.database import connect as connect

    with connect() as db:
        row = db.execute(
            """SELECT TOP 1 p.medical_record_number AS mrn, a.accession_number, a.order_id
               FROM accessions a
               JOIN patients p ON p.id = a.patient_id
               WHERE a.case_id = ?
               ORDER BY a.id DESC""",
            (case_pk,),
        ).fetchone()
        if not row or not row.get("mrn") or not row.get("accession_number"):
            return {"status": "Skipped", "reason": "Case has no HL7 accession"}
        control_id = f"ORU-{uuid4().hex[:10]}"
        message = build_oru_r01(row["mrn"], row.get("order_id") or "", row["accession_number"], diagnosis, control_id)
        correlation_id = uuid4().hex
        cursor = db.execute(
            "INSERT INTO integration_events (correlation_id, message_control_id, message_type, direction, raw_payload, status) VALUES (?,?, 'ORU^R01', 'outbound', ?, 'Queued')",
            (correlation_id, control_id, message),
        )
        event_id = cursor.lastrowid
        db.commit()

    host = os.getenv("HL7_RESULT_HOST", "127.0.0.1")
    port = int(os.getenv("HL7_RESULT_PORT", "2576"))
    try:
        ack = send_mllp(host, port, message, timeout=float(os.getenv("HL7_MLLP_TIMEOUT_SECONDS", "15")))
        status = "Processed" if "MSA|AA|" in ack else "Failed"
        error = None if status == "Processed" else ack[:1000]
    except Exception as exc:
        ack = ""
        status = "Failed"
        error = str(exc)[:1000]
        logger.warning("ORU delivery failed for case %s: %s", case_pk, error)
    with connect() as db:
        db.execute(
            "UPDATE integration_events SET status=?, acknowledgement=?, error_detail=?, attempts=attempts+1, processed_at=SYSUTCDATETIME() WHERE id=?",
            (status, ack or None, error, event_id),
        )
        db.commit()
    return {"status": status, "event_id": event_id, "message_control_id": control_id}


def retry_failed_results(connect=None, limit: int = 20) -> list[dict]:
    """Resend outbound results that were stored but not acknowledged."""
    if connect is None:
        from core.database import connect as connect

    limit = max(1, min(int(limit), 100))
    with connect() as db:
        rows = db.execute(
            f"SELECT TOP ({limit}) id, raw_payload FROM integration_events WHERE direction='outbound' AND status='Failed' ORDER BY id"
        ).fetchall()
    results = []
    host = os.getenv("HL7_RESULT_HOST", "127.0.0.1")
    port = int(os.getenv("HL7_RESULT_PORT", "2576"))
    for row in rows:
        try:
            ack = send_mllp(host, port, row["raw_payload"], timeout=float(os.getenv("HL7_MLLP_TIMEOUT_SECONDS", "15")))
            status = "Processed" if "MSA|AA|" in ack else "Failed"
            error = None if status == "Processed" else ack[:1000]
        except Exception as exc:
            ack = ""
            status = "Failed"
            error = str(exc)[:1000]
        with connect() as db:
            db.execute(
                "UPDATE integration_events SET status=?, acknowledgement=?, error_detail=?, attempts=attempts+1, processed_at=SYSUTCDATETIME() WHERE id=?",
                (status, ack or None, error, row["id"]),
            )
            db.commit()
        results.append({"event_id": row["id"], "status": status})
    return results
