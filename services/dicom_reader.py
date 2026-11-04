"""DICOM VL Whole Slide Microscopy inspection and series staging.

Pixel decoding stays with OpenSlide. This module only identifies a series,
rejects non-pathology DICOM, and copies the original instances unchanged.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from shutil import copy2
from typing import Any
from uuid import uuid4

from services.image_validation import ImageValidationError

WSI_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.77.1.6"


def is_dicom_file(path: Path) -> bool:
    try:
        with Path(path).open("rb") as handle:
            preamble = handle.read(132)
    except OSError:
        return False
    return len(preamble) >= 132 and preamble[128:132] == b"DICM"


def _pydicom():
    try:
        import pydicom
    except ImportError as exc:
        raise ImageValidationError("DICOM inspection requires pydicom") from exc
    return pydicom


def _text(dataset, name: str) -> str | None:
    value = getattr(dataset, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(dataset, name: str) -> float | None:
    value = getattr(dataset, name, None)
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        try:
            value = value[0]
        except (TypeError, IndexError, KeyError):
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _dataset(path: Path):
    pydicom = _pydicom()
    if not is_dicom_file(path):
        raise ImageValidationError(f"{path.name} is not a DICOM file")
    try:
        dataset = pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
    except Exception as exc:
        raise ImageValidationError(f"{path.name} could not be read as DICOM") from exc
    return dataset


def read_instance(path: Path) -> dict[str, Any]:
    dataset = _dataset(path)
    sop_class = _text(dataset, "SOPClassUID")
    if sop_class != WSI_SOP_CLASS_UID:
        modality = _text(dataset, "Modality") or "unknown"
        raise ImageValidationError(
            f"Unsupported DICOM SOP Class {sop_class or 'missing'} (modality {modality}). "
            "Only VL Whole Slide Microscopy Image is accepted."
        )
    sop_instance = _text(dataset, "SOPInstanceUID")
    series_uid = _text(dataset, "SeriesInstanceUID")
    study_uid = _text(dataset, "StudyInstanceUID")
    if not sop_instance or not series_uid or not study_uid:
        raise ImageValidationError(f"{path.name} is missing Study, Series, or SOP Instance UID")
    rows = int(getattr(dataset, "TotalPixelMatrixRows", 0) or getattr(dataset, "Rows", 0) or 0)
    columns = int(getattr(dataset, "TotalPixelMatrixColumns", 0) or getattr(dataset, "Columns", 0) or 0)
    if rows < 1 or columns < 1:
        raise ImageValidationError(f"{path.name} has no image dimensions")
    return {
        "path": str(path),
        "sop_class_uid": sop_class,
        "sop_instance_uid": sop_instance,
        "series_instance_uid": series_uid,
        "study_instance_uid": study_uid,
        "transfer_syntax_uid": _text(dataset.file_meta, "TransferSyntaxUID") if getattr(dataset, "file_meta", None) else None,
        "specimen_id": _text(dataset, "SpecimenIdentifier") or _text(dataset, "ContainerIdentifier"),
        "container_id": _text(dataset, "ContainerIdentifier"),
        "manufacturer": _text(dataset, "Manufacturer"),
        "manufacturer_model": _text(dataset, "ManufacturerModelName"),
        "rows": rows,
        "columns": columns,
        "number_of_frames": int(getattr(dataset, "NumberOfFrames", 1) or 1),
        "pixel_spacing_x": _float(dataset, "ImagedVolumeWidth"),
        "pixel_spacing_row": (_float(dataset, "PixelSpacing") or 0) * 1000 or None,
        "optical_path_id": _text(dataset, "OpticalPathIdentifier"),
        "instance_number": int(getattr(dataset, "InstanceNumber", 0) or 0),
        "file_size": path.stat().st_size,
    }


def discover_series(anchor: Path) -> dict[str, Any]:
    """Read the WSI series that contains anchor. Sibling files of another series are ignored."""
    anchor = Path(anchor)
    if anchor.is_dir():
        candidates = sorted(path for path in anchor.iterdir() if path.is_file() and is_dicom_file(path))
        if not candidates:
            raise ImageValidationError("Directory contains no DICOM files")
        anchor = candidates[0]
    if not anchor.is_file():
        raise ImageValidationError("DICOM source does not exist")

    seed = read_instance(anchor)
    folder = anchor.parent
    instances = []
    seen: set[str] = set()
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not is_dicom_file(path):
            continue
        try:
            instance = read_instance(path)
        except ImageValidationError:
            if path.resolve() == anchor.resolve():
                raise
            continue
        if instance["series_instance_uid"] != seed["series_instance_uid"]:
            continue
        if instance["sop_instance_uid"] in seen:
            raise ImageValidationError(f"Duplicate SOP Instance UID {instance['sop_instance_uid']}")
        seen.add(instance["sop_instance_uid"])
        instances.append(instance)
    if not instances:
        raise ImageValidationError("DICOM series contains no readable WSI instances")

    digest = sha256()
    total_size = 0
    for instance in instances:
        digest.update(instance["sop_instance_uid"].encode("utf-8"))
        digest.update(str(instance["file_size"]).encode("utf-8"))
        total_size += instance["file_size"]
    largest = max(instances, key=lambda item: item["rows"] * item["columns"])
    spacing = largest["pixel_spacing_row"]
    return {
        "file_type": "dicom-wsi",
        "format": "DICOM WSI",
        "width": largest["columns"],
        "height": largest["rows"],
        "level_count": len(instances),
        "study_instance_uid": seed["study_instance_uid"],
        "series_instance_uid": seed["series_instance_uid"],
        "sop_class_uid": WSI_SOP_CLASS_UID,
        "anchor": largest["path"],
        "instances": instances,
        "file_size": total_size,
        "checksum_sha256": digest.hexdigest(),
        "normalized_metadata": {
            "slide_label": largest["specimen_id"],
            "barcode": largest["container_id"] or largest["specimen_id"],
            "scanner_vendor": largest["manufacturer"],
            "scanner_model": largest["manufacturer_model"],
            "magnification": None,
            "acquisition_time": None,
        },
        "physical_calibration": {
            "available": bool(spacing),
            "pixel_size_microns_x": spacing,
            "pixel_size_microns_y": spacing,
            "source": "PixelSpacing" if spacing else None,
        },
        "dicom": {
            "study_instance_uid": seed["study_instance_uid"],
            "series_instance_uid": seed["series_instance_uid"],
            "sop_instance_uids": [item["sop_instance_uid"] for item in instances],
            "transfer_syntax_uid": largest["transfer_syntax_uid"],
        },
    }


def stage_series(series: dict[str, Any], uploads_dir: Path) -> str:
    """Copy original instances into uploads/<id>/ and return the stored anchor path."""
    folder_name = uuid4().hex
    destination = Path(uploads_dir) / folder_name
    destination.mkdir(parents=True, exist_ok=False)
    anchor_name = None
    anchor_source = str(Path(series["anchor"]).resolve())
    for instance in series["instances"]:
        source = Path(instance["path"])
        target_name = source.name
        copy2(source, destination / target_name)
        if str(source.resolve()) == anchor_source:
            anchor_name = target_name
    if anchor_name is None:
        raise ImageValidationError("DICOM series anchor was not copied")
    return f"{folder_name}/{anchor_name}"
