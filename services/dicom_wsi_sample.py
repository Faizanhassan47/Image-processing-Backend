"""Write a small uncompressed DICOM VL Whole Slide Microscopy image.

OpenSlide 4 can open this when the tiled pyramid metadata is present.
It is a synthetic slide for the viewer path, not a clinical specimen.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from services.dicom_reader import WSI_SOP_CLASS_UID


def write_synthetic_wsi(directory: Path, tile: int = 256, matrix: int = 512) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "synthetic-wsi.dcm"
    pixels = np.zeros((matrix, matrix, 3), dtype=np.uint8)
    pixels[:, :, 0] = 180
    pixels[40:200, 40:220] = (120, 60, 140)
    frames = []
    for top in range(0, matrix, tile):
        for left in range(0, matrix, tile):
            frames.append(pixels[top:top + tile, left:left + tile].tobytes())
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = WSI_SOP_CLASS_UID
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = WSI_SOP_CLASS_UID
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.StudyInstanceUID = generate_uid()
    dataset.SeriesInstanceUID = generate_uid()
    dataset.Modality = "SM"
    dataset.ImageType = ["ORIGINAL", "PRIMARY", "VOLUME", "NONE"]
    dataset.Manufacturer = "VISTA"
    dataset.SpecimenIdentifier = "ACC-DEMO03"
    dataset.ContainerIdentifier = "ACC-DEMO03"
    dataset.SamplesPerPixel = 3
    dataset.PhotometricInterpretation = "RGB"
    dataset.PlanarConfiguration = 0
    dataset.Rows = tile
    dataset.Columns = tile
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.NumberOfFrames = len(frames)
    dataset.TotalPixelMatrixRows = matrix
    dataset.TotalPixelMatrixColumns = matrix
    dataset.TotalPixelMatrixFocalPlanes = 1
    dataset.ImagedVolumeWidth = 1.0
    dataset.ImagedVolumeHeight = 1.0
    dataset.ImagedVolumeDepth = 0.001
    # DICOM PixelSpacing is millimeters. 0.001953125 mm is 1.953125 microns.
    dataset.PixelSpacing = [1.953125 / 1000.0, 1.953125 / 1000.0]
    dataset.PixelData = b"".join(frames)
    dataset.save_as(path, enforce_file_format=True)
    return path
