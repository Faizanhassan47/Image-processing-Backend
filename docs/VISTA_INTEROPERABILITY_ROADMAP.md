# VISTA interoperability roadmap

Audit date: 2026-11-06.  
Milestone 1 verification update: 2026-11-06 (isolated LocalDB only; live `Hello` database was not modified).

Readiness labels used here:

- **Implemented:** code exists in this repository.
- **Tested:** an automated or manual verification ran successfully and is recorded below.
- **Interoperable:** exchanged with a named external system. Not claimed.
- **Clinically validated:** not claimed for any AI model or interface.

## Milestone 1 verification status (2026-11-06)

| Item | Status | Evidence |
|---|---|---|
| Migration script additive and reviewed | Implemented | `backend/sql/20261106_slides_dicom_wsi_file_type.sql` — no `DROP TABLE` / `TRUNCATE`; preserves `raster` and `wsi` |
| Migration applied to live `Hello` on `Dell` | Not applied | Read-only probe: constraint is still `([file_type]='wsi' OR [file_type]='raster')`; `dicom-wsi` slide count = 0 |
| Migration applied to isolated development DB | Tested | LocalDB database `Vista_DICOM_Dev`; constraint is `raster` / `wsi` / `dicom-wsi` |
| DICOM import through ingestion API | Tested | `python scripts/verify_dicom_wsi_ingestion.py` → queue `202`, process `200`, one slide |
| Stored `file_type` | Tested | `dicom-wsi`; filename `…/synthetic-wsi.dcm` |
| Case API `dzi_url` | Tested | `/api/slides/1/slide.dzi` |
| DZI descriptor + JPEG tile + thumbnail | Tested | HTTP 200 for descriptor, top-level tile, and thumbnail |
| OpenSeadragon UI contract | Tested at API layer | Existing `WsiViewer` opens `slide.dzi_url`; no frontend change required. Browser login against live `Hello` was not used because that database was not migrated. |
| SVS serializer regression | Tested | `slide_dzi_url(..., "wsi")` still returns `/api/slides/{id}/slide.dzi` |
| Related pytest suite | Tested | 46 passed; 1 failed unrelated (`test_tls_mllp_round_trip` missing `cryptography`) |

### Manual actions still required

1. Review `backend/sql/20261106_slides_dicom_wsi_file_type.sql`.
2. Apply it manually to each target SQL Server database (including `Hello` when approved). Do not run it from application startup.
3. Restart the API after migration so new connections see the updated constraint.
4. Import a real vendor DICOM WSI (not only the synthetic sample) and confirm OpenSlide accepts the transfer syntax.
5. Open that slide in the existing VISTA workspace browser session.

### Remaining limitations

- Live `Hello` cannot store `dicom-wsi` until the migration is applied there.
- Synthetic DICOM only was exercised end-to-end; scanner-produced multi-instance series remain unverified.
- OpenSeadragon was verified through the same DZI/tile endpoints it uses, not through a browser session against `Hello`.
- `registration_service.py` still treats OpenSlide registration as `file_type == "wsi"` only; pairwise registration of DICOM slides is out of this milestone.
- Direct case-upload UI still accepts JPG/PNG/SVS/TIFF only; DICOM enters through the platform ingestion path.

## 1. Architecture

VISTA is one React workspace backed by one FastAPI process and Microsoft SQL Server. Imaging, AI, and hospital messaging are separate paths that meet in the case and slide tables.

```text
React (Vite)
  AppRouter
    Dashboard, Case Library, Create Case, Patient pages
    Pathology workspace  ->  SlideViewer  ->  WsiViewer (OpenSeadragon)
    Platform operations (ingestion, HL7 sandbox, reconciliation)
        |
        v
FastAPI
  cases_api          upload, Deep Zoom descriptor, tiles, thumbnails
  image_validation   extension and DICOM preamble checks
  dicom_reader       VL Whole Slide Microscopy series staging
  slide_cache        OpenSlide handle cache and tile cache
  ai_api / engines   HoVer-Net, KongNet-Det, Camelyon, GrandQC, DIP
  hl7_engine         parse, validate, ACK, ORU
  hl7_mllp           separate TCP process, not started by Uvicorn
  hl7_apply          commit patient, case, accession; send ORU
  dicomweb_client    QIDO / WADO / STOW client, not mounted on a route
  barcode_label      Code 128 and Data Matrix PNG bytes, no route
        |
        v
SQL Server
  patients, cases, accessions, specimens, blocks, slides
  image_versions, annotations, reports, ai_jobs, ai_analyses
  hl7_channels, integration_events
  dicom_studies, dicom_series, dicom_instances  (created in init_tables.py)
```

Frontend routes live in `frontend/src/routes/AppRouter.jsx`. The pathology viewer is `frontend/src/components/WsiViewer/WsiViewer.jsx`, opened by `frontend/src/components/SlideViewer/SlideViewer.jsx`.

## 2. What already works

| Area | Status | Where |
|---|---|---|
| Login, roles, JWT, CSRF, audit log | Implemented | `backend/core/security.py`, `backend/main.py` |
| Patients, cases, accessions, specimens, blocks, slides | Implemented | `backend/sql/schema.sql`, `patient_api.py`, `cases_api.py` |
| SVS, TIFF, NDPI, MRXS, SCN, JPEG, PNG upload | Implemented | `backend/services/image_validation.py` |
| OpenSlide cache, Deep Zoom descriptor, JPEG tiles, thumbnail | Implemented | `backend/routes/cases_api.py`, `backend/services/slide_cache.py` |
| OpenSeadragon zoom, pan, annotations, measurements, filters | Implemented | `WsiViewer.jsx` |
| Multi-slide viewing and slide registration | Implemented | `slide_registrations` tables, `RegistrationWorkspace.jsx` |
| Reports and finalization | Implemented | `backend/routes/reports_api.py` |
| HoVer-Net and KongNet-Det jobs | Implemented | `ai_engine.py`, `kongnet_engine.py`, `ai_api.py` |
| GrandQC whole-slide QC | Implemented | `grandqc_engine.py`, `grandqc_api.py`; viewer can select it |
| Camelyon tumor detection | Implemented on the API | `camelyon_engine.py`; viewer Use button still refuses it |
| Digital image processing (Otsu and related) | Implemented | `dip_service.py`, `dip_api.py` |
| AI licensing and subscriptions | Implemented | `ai_subscription_service.py`, admin AI pages |
| HL7 parse, validate, sandbox, replay | Implemented | `hl7_engine.py`, `platform_api.py`, Platform Operations UI |
| MLLP listener and sender | Implemented as a separate process | `python -m services.hl7_mllp` |
| Simulated LIS | Implemented, separate from the API | `backend/integrations/simulated_lis/lis_sim.py` |
| Scanner ingestion and reconciliation records | Implemented | `scanner_adapters.py`, `ingestion_jobs`, `reconciliation_items` |
| HookNet and TIAToolbox | Missing from application code | No references under `backend/` |

## 3. SVS, OpenSlide, and OpenSeadragon

Upload goes through `cases_api.add_slide_record`, which calls `validate_image_file`. Whole-slide files are opened with OpenSlide. Metadata stored for ordinary WSI includes dimensions, level count, checksum, and OpenSlide properties such as `aperio.Barcode` and `hamamatsu.Reference`.

Tile serving is one path for every OpenSlide-readable file:

- `GET /api/slides/{id}/slide.dzi`
- `GET /api/slides/{id}/slide_files/{level}/{col}_{row}.jpeg`
- `GET /api/slides/{id}/thumbnail`

`require_wsi_slide` already accepts `file_type` of `wsi` or `dicom-wsi`. `SlideViewer.jsx` sends both types to `WsiViewer`.

The case serializer does not. `serialize_slide` sets `dzi_url` only when `file_type == "wsi"`. A DICOM slide therefore reaches the viewer with no descriptor URL, so OpenSeadragon cannot open it. That is the concrete viewing gap. The tile functions themselves do not need a second implementation.

## 4. AI models

| Model | Code | Engine | Viewer | DICOM |
|---|---|---|---|---|
| HoVer-Net | `HOVERNET` | `ai_engine.py` | Selectable | OpenSlide read; calibrated run blocked when `image_versions.pixel_size_microns` is missing |
| KongNet-Det | `MITOTIC_DETECTION` | `kongnet_engine.py` | Selectable | Same calibration gate |
| GrandQC | `SLIDE_QUALITY_CONTROL` | `grandqc_engine.py` | Selectable | Own worker and Python; not proven against DICOM WSI |
| Camelyon | `CAMELYON_TUMOR_DETECTION` | `camelyon_engine.py` | Use button opens the “cannot run” dialog | Whole-slide Camelyon accepts only `.svs`, `.tif`, `.tiff` |
| DIP | not an AI feature code | `dip_service.py` | Viewer DIP panel | OpenSlide region read |

`slide_patches.read_rgb_patch` is the shared RGB read for SVS and DICOM. It uses OpenSlide and can derive microns-per-pixel from `dicom.PixelSpacing` when `openslide.mpp-x` is absent. AI workers still open slides themselves. There is no paired SVS-versus-DICOM accuracy comparison in the repository.

Model environments on this machine are not all runnable. GrandQC’s virtual environment was pointed at a local Python 3.10.11 install. HoVer-Net, KongNet, and Camelyon `.env` paths still point at `E:/Pathology-Integrated/...`, which is not present, and their copied virtual environments still reference another user’s Python. That is a deployment gap, not a missing API.

None of these models are clinically validated. Outputs remain research-use or pathologist-review results.

## 5. HL7 and MLLP

`hl7_engine.py` parses ER7, checks MSH, PID, and order or result segments, builds ACK and NACK, and builds `ORU^R01`. Sample messages cover `ADT^A01`, `ORM^O01`, and `ORU^R01`. Validation also accepts `ADT`, `ORM`, `OML`, and `ORU` as message types. The sandbox UI calls `/hl7/sandbox/validate` and `/hl7/sandbox/simulate`.

`hl7_mllp.py` frames messages with the MLLP start byte and end block, limits frame size, supports a timeout, and can wrap the socket in TLS when certificate paths are set. The client TLS context does not verify the peer certificate. The listener handles one connection at a time. It is started only by `python -m services.hl7_mllp`. Uvicorn does not start it, which avoids a duplicate listener on reload.

`hl7_apply.py` commits before the ACK is returned. A repeated processed message control ID returns the stored acknowledgement and does not insert another patient or accession. Matching is by medical record number and accession number, not by name. `ADT^A08` updates an existing patient. A new order creates a case and accession when both MRN and accession are present. Finalizing a report calls `deliver_final_result`, which queues `ORU^R01` and sends it to `HL7_RESULT_HOST:HL7_RESULT_PORT` (default `127.0.0.1:2576`). Failures stay on `integration_events` and can be resent by `retry_failed_results`.

`integrations/simulated_lis/lis_sim.py` sends synthetic `ADT^A04` and `ORM^O01` and listens for an ORU. It does not import a slide, run AI, or finalize a report by itself.

Not present:

- Specimen (`SPM`) and container (`SAC`) mapping into `specimens`, `blocks`, and `slides`.
- Assigning-authority namespaces. Two hospitals with the same MRN would collide.
- A conflict queue when identifiers disagree. The current rule updates or inserts.
- A supervised service definition (Windows service or process manager) for the listener.
- Peer-verified TLS.

Tests present, not re-run here: `backend/tests/test_hl7_mllp.py`, `backend/tests/test_hl7_sandbox.py`, the TLS case in `backend/tests/test_remaining_interop.py`.

## 6. DICOM

`dicom_reader.py` does not trust the file extension alone. It requires the `DICM` preamble, then reads the dataset with `pydicom` without loading pixels. The only accepted SOP Class is VL Whole Slide Microscopy Image (`1.2.840.10008.5.1.4.1.1.77.1.6`). Other modalities, including CT and MRI, are rejected. A folder is grouped by Series Instance UID. Duplicate SOP Instance UIDs in that folder are rejected. Original files are copied by `stage_series`. Pixel decoding is left to OpenSlide.

Stored identifiers: Study, Series, and SOP Instance UID, SOP Class UID, transfer syntax, manufacturer, specimen and container identifiers, matrix size, and a pixel-spacing value when `PixelSpacing` is numeric. There is no `dicom_slide_metadata` table. Optical path, focal plane, orientation, and image-volume spacing are only partly read and are not stored as their own columns.

Scanner ingestion in `platform_api.py` inserts `dicom_studies`, `dicom_series`, and `dicom_instances` after a DICOM import. Those tables are created in `backend/sql/init_tables.py`. They are not in `backend/sql/schema.sql`.

Two blockers previously stopped this from being a working viewer path. Code fixes are in place; the SQL migration is prepared but not auto-applied:

1. `slides.file_type` check: additive script `backend/sql/20261106_slides_dicom_wsi_file_type.sql` permits `dicom-wsi`. `schema.sql` and `init_tables.py` CREATE paths are updated. Apply the migration to each existing database manually before inserting DICOM rows.
2. `serialize_slide` now returns `dzi_url` for both `wsi` and `dicom-wsi` through `slide_dzi_url()`.

OpenSlide DICOM support depends on the installed OpenSlide build and the transfer syntax of the file. A synthetic open-and-patch test exists in `test_remaining_interop.py` and was not run in this audit. Vendor DICOM WSI from a scanner has not been validated here.

## 7. Database tables that matter

Clinical and imaging, from `backend/sql/schema.sql` unless noted:

- `users`, `audit_log`
- `patients`, `cases`, `accessions`, `specimens`, `blocks`, `case_links`
- `slides`, `slide_qc_history`, `image_versions`, `image_metadata_revisions`
- `annotations`, `annotation_revisions`
- `reports`, `report_reviews`, `report_findings`
- `ingestion_jobs`, `reconciliation_items`, `scanner_compatibility`
- `viewer_bookmarks`, `viewer_preferences`
- `slide_viewing_trails`, `slide_registrations`, `slide_registration_history` (`init_tables.py`)
- `ai_models`, `ai_jobs`, `ai_analyses`, plus subscription tables in `init_tables.py` and `sql/ai_subscriptions_migration.sql`
- `hl7_channels`, `integration_events`, `system_settings`
- `dicom_studies`, `dicom_series`, `dicom_instances` (`init_tables.py` only)

No migration has added `dicom-wsi` to the `slides.file_type` check. That change has to be additive. Do not rebuild `slides`.

## 8. Gaps against this specification

| Capability | Status | Extend |
|---|---|---|
| SVS and current OpenSlide formats in one viewer | Implemented | Keep `cases_api.py`, `WsiViewer.jsx` |
| DICOM WSI identification and series staging | Implemented, not wired through a successful slide insert | `dicom_reader.py`, `image_validation.py`, `platform_api.py` |
| DICOM slide row and viewer URL | Missing because of the check constraint and `dzi_url` | `schema.sql` or a new additive migration, `init_tables.py`, `cases_api.serialize_slide` |
| Shared tile API | Implemented for any OpenSlide slide the API can store | `cases_api.py` tile routes |
| DICOM metadata beyond UIDs and basic spacing | Partial | `dicom_reader.py`; add columns only where the current JSON metadata is not enough |
| AI on DICOM through OpenSlide | Partial | `ai_api.py`, `slide_patches.py`, GrandQC and Camelyon workers |
| Calibration required before AI | Implemented for `dicom-wsi` on the shared AI route | `ai_api.py` |
| HL7 sandbox | Implemented | `platform_api.py` |
| Real MLLP | Implemented, must be run as its own process | `hl7_mllp.py` |
| ADT A04/A08, ORM, OML acceptance, ORU outbound | Implemented | `hl7_apply.py`, `reports_api.py` |
| SPM/SAC, namespace-safe identity, conflict queue | Missing | `hl7_apply.py`, `patients`, `specimens` |
| Full simulated hospital path including slide, AI, and report | Partial. LIS sends ADT and ORM and waits for ORU | `lis_sim.py` plus existing case, slide, and report APIs |
| Code 128 and Data Matrix images | Implemented, no API or Create Case control | `barcode_label.py`; add a small endpoint and a control on the existing add-slide form |
| Barcode match against accession | Implemented | `slide_identity.py` |
| DICOMweb QIDO, WADO, STOW client | Implemented, no route, no archive in this repo | `dicomweb_client.py`; call `dicom_reader` after retrieve |
| Orthanc or another archive | Missing. External software, not an application rewrite | Configuration only after a licensing check |
| HookNet, TIAToolbox | Missing | Do not add unless a later milestone confirms a real integration |
| Clinical certification | Missing and out of scope for these milestones | — |

Final validation of a scanner, a hospital LIS, and a PACS requires those systems or an agreed simulator. Software tests cannot replace that.

## 9. Files to extend or create

Do not add a second viewer, a second tile service, or a second HL7 parser.

| Change | Files |
|---|---|
| Allow `dicom-wsi` without rebuilding `slides` | New additive SQL migration under `backend/sql/`. Update the check in `init_tables.py` for new databases. Leave existing rows untouched. |
| Give the existing viewer a descriptor | `backend/routes/cases_api.py` (`serialize_slide`) |
| Store series folder name within the current filename limits | `platform_api.py` ingestion insert and `cases_api.py` upload path |
| Keep DICOM metadata that staging already extracts | `dicom_instances` or `image_versions.metadata_json` before adding `dicom_slide_metadata` |
| Run MLLP under a process supervisor | `hl7_mllp.py` and deployment notes. Do not start it inside `main.py` while `--reload` is used. |
| Map specimen segments and conflicting identifiers | `hl7_engine.py`, `hl7_apply.py` |
| Finish the synthetic LIS scenario | `integrations/simulated_lis/lis_sim.py` and existing slide and report routes |
| Print and scan a slide identifier on the current add-slide form | `barcode_label.py`, `cases_api.py`, the existing create-case or add-slide component |
| Retrieve from an archive into the current ingestion path | `dicomweb_client.py`, then `dicom_reader.stage_series` |
| DICOM regression tests | `backend/tests/test_dicom_reader.py`, `test_remaining_interop.py`, `test_hl7_mllp.py` |

## 10. Implementation order

Milestone 0 is this document.

**Milestone 1. Make a stored DICOM WSI open in the current viewer.** Code and isolated LocalDB verification are complete (see Milestone 1 verification status). Remaining: apply the migration to approved environments and test a real scanner DICOM series in the browser. Risk: the installed OpenSlide build may reject a real scanner transfer syntax even when `pydicom` accepts the headers. Do not claim all DICOM WSI works.

**Milestone 2. Exercise the MLLP process that already exists.** Run `python -m services.hl7_mllp` and `python -m integrations.simulated_lis.lis_sim` against a development database. Run `test_hl7_mllp.py`. Then add assigning-authority checks and SPM/SAC only where the current MRN match is unsafe. Risk: duplicate MRNs across hospitals if namespaces are skipped.

**Milestone 3. Close the simulated hospital loop.** The LIS already sends ADT and ORM and listens for ORU. Add the slide association and report step with synthetic files, using the existing upload and finalize APIs. Risk: report finalization already sends ORU; a test must not point that at a real hospital address.

**Milestone 4. Barcode on the existing add-slide form.** Reuse `barcode_label.py` and `slide_identity.py`. Do not redesign Create Case. Risk: treating an OCR or DICOM label as identity without the reconciliation row that already exists.

**Milestone 5. AI on a DICOM slide that OpenSlide can open.** Reuse `slide_patches.py` and the calibration gate in `ai_api.py`. GrandQC and Camelyon need an explicit decision per model. Do not retrain models. Risk: missing microns-per-pixel must keep blocking calibrated analysis.

**Milestone 6. DICOMweb.** Configure `DICOMWEB_BASE_URL` for a local archive and pass retrieved files into `stage_series`. Do not add a second decoder. Orthanc is optional and needs a license check before it is introduced.

**Milestone 7. Production review.** Listener supervision, TLS peer verification, log redaction of HL7 payloads, backup of original DICOM files, and a written limit on which transfer syntaxes were actually opened.

Each milestone should stop after its tests. Existing SVS viewing, annotations, AI licensing, and the current workspace layout stay in place.
