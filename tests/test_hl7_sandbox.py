import sys
from pathlib import Path
from uuid import uuid4
import pytest

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient
from main import app
from core.security import create_access_token
from services.hl7_engine import SAMPLE_MESSAGES, parse_raw_hl7, validate_hl7_structure

@pytest.fixture
def auth_headers():
    token = create_access_token(data={"sub": "ETDDev", "role": "System Administrator"})
    return {"Authorization": f"Bearer {token}"}

@pytest.fixture
def client():
    return TestClient(app)

def test_hl7_samples_endpoint(client, auth_headers):
    res = client.get("/api/platform/hl7/sandbox/samples", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "ADT_A01" in data
    assert "ORM_O01" in data
    assert "ORU_R01" in data

def test_hl7_sandbox_validation_success(client, auth_headers):
    orm_msg = SAMPLE_MESSAGES["ORM_O01"]
    res = client.post("/api/platform/hl7/sandbox/validate", json={"raw_payload": orm_msg}, headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["is_valid"] is True
    assert len(data["errors"]) == 0
    assert data["parsed"]["type_code"] == "ORM"
    assert "MSA|AA" in data["simulated_ack"]
    assert data["canonical_entities"]["patient"]["first_name"] == "JANE"
    assert data["canonical_entities"]["accession"]["accession_number"] == "ACC-2026-0099"

def test_hl7_sandbox_validation_invalid(client, auth_headers):
    bad_msg = "INVALID|MESSAGE|WITHOUT|MSH"
    res = client.post("/api/platform/hl7/sandbox/validate", json={"raw_payload": bad_msg}, headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["is_valid"] is False
    assert len(data["errors"]) > 0
    assert "MSA|AE" in data["simulated_ack"]

def test_hl7_sandbox_simulation_creates_entities(client, auth_headers):
    uid = uuid4().hex[:6]
    unique_ctrl = f"MSG-SIM-{uid}"
    raw = SAMPLE_MESSAGES["ORM_O01"].replace("MSG-ORM-2026101902", unique_ctrl).replace("ACC-2026-0099", f"ACC-TEST-{uid}")
    res = client.post("/api/platform/hl7/sandbox/simulate", json={"raw_payload": raw}, headers=auth_headers)
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "Processed"
    assert "MSA|AA" in data["acknowledgement"]
    assert "patient" in data["created_records"]
    assert "accession" in data["created_records"]
