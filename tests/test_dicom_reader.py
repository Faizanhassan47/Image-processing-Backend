"""DICOM WSI identification. These files are headers only and are not viewable slides."""

from pathlib import Path

import pytest

pydicom = pytest.importorskip("pydicom")

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from services.dicom_reader import WSI_SOP_CLASS_UID, discover_series
from services.image_validation import ImageValidationError, validate_image_file


def _write(path: Path, sop_class: str, series: str, study: str, instance: str, rows: int, columns: int):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = instance
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = sop_class
    dataset.SOPInstanceUID = instance
    dataset.SeriesInstanceUID = series
    dataset.StudyInstanceUID = study
    dataset.Modality = "SM" if sop_class == WSI_SOP_CLASS_UID else "CT"
    dataset.Rows = rows
    dataset.Columns = columns
    dataset.TotalPixelMatrixRows = rows
    dataset.TotalPixelMatrixColumns = columns
    dataset.PixelSpacing = [0.25, 0.25]
    dataset.Manufacturer = "TestScanner"
    dataset.SpecimenIdentifier = "SP-100"
    dataset.save_as(path, enforce_file_format=True)


def test_wsi_series_is_accepted_and_ct_is_ignored(tmp_path: Path):
    study = generate_uid()
    series = generate_uid()
    other = generate_uid()
    _write(tmp_path / "a.dcm", WSI_SOP_CLASS_UID, series, study, generate_uid(), 1024, 2048)
    _write(tmp_path / "b.dcm", WSI_SOP_CLASS_UID, series, study, generate_uid(), 256, 512)
    _write(tmp_path / "ct.dcm", "1.2.840.10008.5.1.4.1.1.2", other, study, generate_uid(), 512, 512)

    series_info = discover_series(tmp_path / "a.dcm")

    assert series_info["file_type"] == "dicom-wsi"
    assert series_info["width"] == 2048
    assert series_info["height"] == 1024
    assert len(series_info["instances"]) == 2
    assert series_info["physical_calibration"]["pixel_size_microns_x"] == 250.0
    assert series_info["normalized_metadata"]["barcode"] == "SP-100"


def test_ct_only_file_is_rejected(tmp_path: Path):
    path = tmp_path / "ct.dcm"
    _write(path, "1.2.840.10008.5.1.4.1.1.2", generate_uid(), generate_uid(), generate_uid(), 64, 64)
    with pytest.raises(ImageValidationError, match="Unsupported DICOM"):
        validate_image_file(path)


def test_duplicate_sop_instance_is_rejected(tmp_path: Path):
    study = generate_uid()
    series = generate_uid()
    instance = generate_uid()
    _write(tmp_path / "a.dcm", WSI_SOP_CLASS_UID, series, study, instance, 100, 100)
    _write(tmp_path / "b.dcm", WSI_SOP_CLASS_UID, series, study, instance, 100, 100)
    with pytest.raises(ImageValidationError, match="Duplicate SOP"):
        discover_series(tmp_path / "a.dcm")
