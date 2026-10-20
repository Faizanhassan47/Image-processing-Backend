"""HL7 v2 Message Processing and Sandbox Engine for Digital Pathology.

Handles parsing, segment extraction, semantic validation (MSH, PID, PV1, ORC, OBR, OBX),
database matching/reconciliation, ACK/NACK message generation, and sample message templates
for ADT-A01 (Admit/Register), ORM-O01 (Order Entry), and ORU-R01 (Observation/Result).

Conforms to Section 5.8 (INT-001 through INT-012) of PRS.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import uuid4

logger = logging.getLogger("pathology.hl7")

SAMPLE_MESSAGES = {
    "ADT_A01": (
        "MSH|^~\\&|HOSP_LIS|CENTRAL_LAB|DP_PATHOLOGY|LAB_PATH|20261019153000||ADT^A01|MSG-ADT-2026101901|P|2.5.1\r"
        "EVN|A01|20261019153000\r"
        "PID|1||MRN-984321^^^HOSP||DOE^JANE^ELIZABETH||19840512|F|||123 HEALING WAY^^ISLAMABAD^^44000^PAK||03001234567\r"
        "PV1|1|O|PATH_CLINIC^^^CLINIC_A||||ATT_DOC_01^RAZA^AHMAD^^^^DR\r"
    ),
    "ORM_O01": (
        "MSH|^~\\&|HOSP_LIS|CENTRAL_LAB|DP_PATHOLOGY|LAB_PATH|20261019153500||ORM^O01|MSG-ORM-2026101902|P|2.5.1\r"
        "PID|1||MRN-984321^^^HOSP||DOE^JANE^ELIZABETH||19840512|F\r"
        "ORC|NW|ORD-2026-8812|||||1^R^^20261019153500\r"
        "OBR|1|ORD-2026-8812|ACC-2026-0099|88305^SURGICAL PATHOLOGY LEVEL IV^CPT|||20261019140000|||||||||ATT_DOC_01^RAZA^AHMAD^^^^DR\r"
        "OBX|1|TX|CLINICAL_HX^Clinical History||Right breast upper outer quadrant mass, core biopsy.\r"
    ),
    "ORU_R01": (
        "MSH|^~\\&|HOSP_LIS|CENTRAL_LAB|DP_PATHOLOGY|LAB_PATH|20261019154000||ORU^R01|MSG-ORU-2026101903|P|2.5.1\r"
        "PID|1||MRN-984321^^^HOSP||DOE^JANE^ELIZABETH||19840512|F\r"
        "OBR|1|ORD-2026-8812|ACC-2026-0099|88305^SURGICAL PATHOLOGY LEVEL IV^CPT\r"
        "OBX|1|TX|DIAGNOSIS^Preliminary Diagnosis||Invasive ductal carcinoma, Nottingham grade 2.\r"
        "OBX|2|NM|TUMOR_SIZE^Max Dimension||18|mm^Millimeters||||F\r"
    ),
}


def parse_raw_hl7(raw_payload: str) -> dict[str, Any]:
    """Parses raw pipe-delimited HL7 v2 string into structured segments and fields."""
    normalized = raw_payload.replace("\r\n", "\r").replace("\n", "\r").strip()
    if not normalized:
        return {"error": "Empty message payload", "segments": {}, "segment_list": []}

    lines = [l for l in normalized.split("\r") if l.strip()]
    if not lines:
        return {"error": "No segments detected", "segments": {}, "segment_list": []}

    segment_list = []
    segments_by_name = {}

    for idx, line in enumerate(lines):
        sep = "|"
        fields = line.split(sep)
        if line.startswith("MSH"):
            # In HL7, the field separator itself is MSH-1, and encoding characters is MSH-2.
            # line.split('|') gives: ['MSH', '^~\\&', 'SENDING_APP', ...]
            # To map 1-based MSH index to python list:
            # index 0: 'MSH'
            # index 1: '|' (MSH-1)
            # index 2: fields[1] ('^~\\&', MSH-2)
            # index 3: fields[2] (MSH-3 Sending App)
            # index 4: fields[3] (MSH-4 Sending Facility)
            # index 5: fields[4] (MSH-5 Receiving App)
            # index 6: fields[5] (MSH-6 Receiving Facility)
            # index 7: fields[6] (MSH-7 Date/Time)
            # index 8: fields[7] (MSH-8 Security)
            # index 9: fields[8] (MSH-9 Message Type)
            # index 10: fields[9] (MSH-10 Message Control ID)
            # index 11: fields[10] (MSH-11 Processing ID)
            # index 12: fields[11] (MSH-12 Version ID)
            msh_fields = ["MSH", sep] + fields[1:]
            seg_dict = {
                "segment_name": "MSH",
                "index": idx,
                "fields": msh_fields,
            }
        else:
            seg_dict = {
                "segment_name": fields[0],
                "index": idx,
                "fields": fields,
            }

        segment_list.append(seg_dict)
        seg_name = seg_dict["segment_name"]
        if seg_name not in segments_by_name:
            segments_by_name[seg_name] = []
        segments_by_name[seg_name].append(seg_dict["fields"])

    msh_fields = segments_by_name.get("MSH", [[]])[0]
    msg_type = ""
    trigger_event = ""
    control_id = ""
    version = "2.5.1"
    sending_app = ""
    sending_fac = ""

    if len(msh_fields) > 9 and msh_fields[9]:
        type_parts = msh_fields[9].split("^")
        msg_type = type_parts[0]
        if len(type_parts) > 1:
            trigger_event = type_parts[1]
    if len(msh_fields) > 10 and msh_fields[10]:
        control_id = msh_fields[10]
    if len(msh_fields) > 12 and msh_fields[12]:
        version = msh_fields[12]
    if len(msh_fields) > 3:
        sending_app = msh_fields[3]
    if len(msh_fields) > 4:
        sending_fac = msh_fields[4]

    return {
        "message_type": f"{msg_type}^{trigger_event}".strip("^"),
        "type_code": msg_type,
        "trigger_event": trigger_event,
        "message_control_id": control_id,
        "version": version,
        "sending_application": sending_app,
        "sending_facility": sending_fac,
        "segment_list": segment_list,
        "segments": segments_by_name,
    }


def validate_hl7_structure(parsed: dict[str, Any]) -> list[str]:
    """Validates structural and semantic requirements for standard digital pathology messages."""
    errors = []
    if "error" in parsed:
        return [parsed["error"]]

    segments = parsed.get("segments", {})
    if "MSH" not in segments or not segments["MSH"]:
        errors.append("Missing mandatory MSH (Message Header) segment.")
        return errors

    msh = segments["MSH"][0]
    if len(msh) <= 9 or not msh[9]:
        errors.append("MSH-9 (Message Type) is missing or empty.")
    if len(msh) <= 10 or not msh[10]:
        errors.append("MSH-10 (Message Control ID) is missing or empty.")

    msg_type = parsed.get("type_code", "")

    # Patient Identification is required for clinical messages
    if msg_type in {"ADT", "ORM", "ORU"}:
        if "PID" not in segments or not segments["PID"]:
            errors.append(f"{msg_type} message requires a PID (Patient Identification) segment.")
        else:
            pid = segments["PID"][0]
            # Check for Patient ID / MRN (PID-3 or PID-2)
            has_id = (len(pid) > 3 and pid[3]) or (len(pid) > 2 and pid[2])
            if not has_id:
                errors.append("PID segment lacks patient identifier in PID-3 or PID-2.")

    # Order messages require ORC / OBR
    if msg_type == "ORM":
        if "ORC" not in segments and "OBR" not in segments:
            errors.append("ORM message requires an ORC (Common Order) or OBR (Observation Request) segment.")

    # Observation / Result messages require OBR
    if msg_type == "ORU":
        if "OBR" not in segments:
            errors.append("ORU message requires at least one OBR segment.")

    return errors


def extract_canonical_records(parsed: dict[str, Any]) -> dict[str, Any]:
    """Extracts normalized patient, order, and accession entities from parsed HL7 segments."""
    segments = parsed.get("segments", {})
    extracted = {
        "patient": None,
        "order": None,
        "accession": None,
        "observations": [],
    }

    # Extract Patient
    if "PID" in segments and segments["PID"]:
        pid = segments["PID"][0]
        # PID-3: Patient Identifier List (ID^^^AssigningAuthority)
        raw_id = pid[3] if len(pid) > 3 else ""
        mrn = raw_id.split("^")[0] if raw_id else ""
        if not mrn and len(pid) > 2:
            mrn = pid[2].split("^")[0]

        # PID-5: Patient Name (Family^Given^Middle)
        name_parts = (pid[5].split("^") if len(pid) > 5 and pid[5] else [])
        last_name = name_parts[0] if len(name_parts) > 0 else ""
        first_name = name_parts[1] if len(name_parts) > 1 else ""
        middle_name = name_parts[2] if len(name_parts) > 2 else ""

        # PID-7: Date of Birth (YYYYMMDD)
        dob_raw = pid[7] if len(pid) > 7 else ""
        dob = None
        if len(dob_raw) >= 8:
            dob = f"{dob_raw[0:4]}-{dob_raw[4:6]}-{dob_raw[6:8]}"

        # PID-8: Sex
        sex = pid[8] if len(pid) > 8 else "U"
        if sex not in ("M", "F", "O", "U"):
            sex = "U"

        # PID-11: Address
        address = pid[11].replace("^", ", ").strip(", ") if len(pid) > 11 else ""
        # PID-13: Phone
        phone = pid[13] if len(pid) > 13 else ""

        extracted["patient"] = {
            "medical_record_number": mrn,
            "patient_identifier": mrn or f"HL7-{uuid4().hex[:8]}",
            "first_name": first_name or "Unknown",
            "last_name": last_name or "Patient",
            "middle_name": middle_name,
            "display_label": f"{first_name} {last_name}".strip() or "HL7 Patient",
            "date_of_birth": dob,
            "sex": sex,
            "phone": phone,
            "address": address,
        }

    # Extract Order / Accession from ORC or OBR
    order_id = ""
    priority = "Routine"
    if "ORC" in segments and segments["ORC"]:
        orc = segments["ORC"][0]
        order_id = orc[2].split("^")[0] if len(orc) > 2 and orc[2] else ""
        # ORC-7: Quantity/Timing: Priority in component 6 (e.g. 1^STAT)
        if len(orc) > 7 and "STAT" in orc[7].upper():
            priority = "Stat"
        elif len(orc) > 7 and "URG" in orc[7].upper():
            priority = "Urgent"

    accession_number = ""
    test_code = ""
    test_desc = ""
    if "OBR" in segments and segments["OBR"]:
        obr = segments["OBR"][0]
        if not order_id and len(obr) > 2 and obr[2]:
            order_id = obr[2].split("^")[0]
        if len(obr) > 3 and obr[3]:
            accession_number = obr[3].split("^")[0]
        if len(obr) > 4 and obr[4]:
            tparts = obr[4].split("^")
            test_code = tparts[0]
            test_desc = tparts[1] if len(tparts) > 1 else ""

    if order_id or accession_number:
        extracted["order"] = {
            "order_id": order_id or f"ORD-{uuid4().hex[:6]}",
            "accession_number": accession_number or f"ACC-{uuid4().hex[:6]}",
            "priority": priority,
            "test_code": test_code,
            "test_description": test_desc,
        }
        extracted["accession"] = {
            "accession_number": accession_number or f"ACC-{uuid4().hex[:6]}",
            "order_id": order_id or None,
            "priority": priority,
        }

    # Extract Observations (OBX)
    if "OBX" in segments:
        for obx in segments["OBX"]:
            if len(obx) > 5:
                obx_id = obx[3].split("^")[0] if len(obx) > 3 else ""
                obx_desc = obx[3].split("^")[1] if len(obx) > 3 and "^" in obx[3] else obx_id
                value = obx[5] if len(obx) > 5 else ""
                units = obx[6] if len(obx) > 6 else ""
                extracted["observations"].append({
                    "identifier": obx_id,
                    "description": obx_desc,
                    "value": value,
                    "units": units,
                })

    return extracted


def generate_hl7_ack(parsed: dict[str, Any], accepted: bool = True, error_msg: str = "") -> str:
    """Generates a standard HL7 v2.5.1 ACK message according to RFC specifications."""
    msh = parsed.get("segments", {}).get("MSH", [[]])[0]
    rec_app = msh[2] if len(msh) > 2 else "HOSP_LIS"
    rec_fac = msh[3] if len(msh) > 3 else "CENTRAL_LAB"
    ctrl_id = parsed.get("message_control_id", uuid4().hex[:12])
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    ack_ctrl = f"ACK-{uuid4().hex[:8]}"

    ack_code = "AA" if accepted else "AE"
    lines = [
        f"MSH|^~\\&|DP_PATHOLOGY|LAB_PATH|{rec_app}|{rec_fac}|{now_str}||ACK|{ack_ctrl}|P|2.5.1",
        f"MSA|{ack_code}|{ctrl_id}",
    ]
    if not accepted and error_msg:
        lines.append(f"ERR|^^^100&Segment sequence error&HL70357||E|||||{error_msg.replace('|', '_')}")

    return "\r".join(lines)
