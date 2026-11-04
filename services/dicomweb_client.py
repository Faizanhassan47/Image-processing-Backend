"""DICOMweb client for a configured archive. This does not start an archive."""

from __future__ import annotations

import os
from pathlib import Path

import httpx


def _base_url() -> str:
    base = os.getenv("DICOMWEB_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("DICOMWEB_BASE_URL is not configured")
    return base


def _auth() -> dict[str, str]:
    token = os.getenv("DICOMWEB_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def search_studies(patient_id: str | None = None, limit: int = 20) -> list[dict]:
    params = {"limit": limit, "includefield": "00081030"}
    if patient_id:
        params["PatientID"] = patient_id
    response = httpx.get(f"{_base_url()}/studies", params=params, headers=_auth(), timeout=30)
    response.raise_for_status()
    return response.json()


def search_series(study_uid: str) -> list[dict]:
    response = httpx.get(f"{_base_url()}/studies/{study_uid}/series", headers=_auth(), timeout=30)
    response.raise_for_status()
    return response.json()


def retrieve_instance(study_uid: str, series_uid: str, instance_uid: str, destination: Path) -> Path:
    url = f"{_base_url()}/studies/{study_uid}/series/{series_uid}/instances/{instance_uid}"
    headers = {**_auth(), "Accept": "application/dicom"}
    response = httpx.get(url, headers=headers, timeout=120)
    response.raise_for_status()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)
    return destination


def store_instance(path: Path) -> httpx.Response:
    payload = Path(path).read_bytes()
    headers = {**_auth(), "Content-Type": "application/dicom"}
    response = httpx.post(f"{_base_url()}/studies", content=payload, headers=headers, timeout=120)
    response.raise_for_status()
    return response
