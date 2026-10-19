"""Integration test for HoVer-Net AI inference, ROI extraction, and reporting."""

import os
import sys
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import connect, get_slide, get_case, _stored_path
import openslide
from ai_engine import (
    hovernet_manager,
    extract_roi,
    save_roi_image,
    analyze_nuclei,
    summarize_classification_scores,
    calculate_nuclear_density,
    create_overlay_image,
    generate_nucleus_gallery,
    build_color_ai_pdf,
    save_ai_analysis_record,
    AI_REPORT_DIR,
)

def run_test():
    print("=" * 60)
    print("RUNNING HOVER-NET INTEGRATION TEST ON SLIDE 1")
    print("=" * 60)

    slide_row = get_slide(1)
    case_row = get_case(slide_row["case_id"])
    slide_path = _stored_path(slide_row["filename"])

    print(f"Slide: {slide_row['label']} ({slide_path})")
    print(f"Case: {case_row['case_id']}")

    slide = openslide.OpenSlide(str(slide_path))

    # 1. Extract a 256x256 test ROI from slide 1
    # Note: TCGA-A6-2686 has tissue around center or using automatic tissue selection
    print("\n1. Extracting ROI (auto tissue)...")
    x, y, w, h, roi_arr, auto, tissue_pct = extract_roi(slide, width=256, height=256)
    print(f"Extracted ROI at ({x}, {y}) size {w}x{h}, auto={auto}, tissue={tissue_pct:.2f}%")

    test_roi_file = "test_roi_slide1.png"
    roi_path = save_roi_image(roi_arr, test_roi_file)
    print(f"Saved ROI image to: {roi_path}")

    # 2. Run HoVer-Net Inference
    print("\n2. Running HoVer-Net Inference...")
    result = hovernet_manager.run_inference(roi_path)
    nuclei = result.get("nuc", {})
    print(f"Inference completed! Total nuclei detected: {len(nuclei)}")

    assert isinstance(nuclei, dict), "Output nuc is not a dict"
    print(f"Worker is persistent and still running: {hovernet_manager.is_running()}")

    # 3. Morphology & Analysis
    print("\n3. Calculating morphology & stats...")
    mpp_x = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0.25))
    mpp_y = float(slide.properties.get(openslide.PROPERTY_NAME_MPP_Y, 0.25))
    analysis_data = analyze_nuclei(nuclei, mpp_x, mpp_y)
    analysis_data["nuclear_density_per_mm2"] = calculate_nuclear_density(
        analysis_data["total_nuclei"], w, h, mpp_x, mpp_y
    )
    score_stats = summarize_classification_scores(nuclei)

    print("Type Breakdown:")
    for type_name, info in analysis_data["type_summary"].items():
        print(f"  - {type_name}: {info['count']} ({info['percentage']}%)")

    # 4. Generate Overlay Image
    print("\n4. Generating Colored Overlay...")
    overlay_file = "test_overlay_slide1.png"
    overlay_path, valid_cnt, class_counts = create_overlay_image(roi_path, nuclei, overlay_file)
    print(f"Saved overlay to: {overlay_path} (valid contours: {valid_cnt})")

    # 5. Generate Nucleus Gallery
    print("\n5. Generating Representative Nucleus Gallery...")
    gallery_data = generate_nucleus_gallery(
        roi_path, nuclei, "test_job_1", x, y, mpp_x, mpp_y
    )
    print(f"Gallery total crops: {gallery_data['total_gallery_images']}")

    # 6. Build Color AI PDF Report
    print("\n6. Building Full-Color PDF Report...")
    pdf_filename = "test_ai_report_slide1.pdf"
    pdf_path = AI_REPORT_DIR / pdf_filename
    roi_meta = {"x": x, "y": y, "width": w, "height": h, "level": 0}
    build_color_ai_pdf(
        pdf_path,
        slide_id=1,
        case_identifier=case_row["case_id"],
        roi_path=roi_path,
        overlay_path=overlay_path,
        analysis=analysis_data,
        score_stats=score_stats,
        gallery=gallery_data,
        roi_info=roi_meta,
    )
    print(f"Saved PDF report to: {pdf_path} (size: {pdf_path.stat().st_size} bytes)")
    assert pdf_path.is_file() and pdf_path.stat().st_size > 1000, "PDF was not properly created"

    # 7. Persist to SQL Server
    print("\n7. Persisting to SQL Server...")
    analysis_id = save_ai_analysis_record(
        connect,
        slide_row,
        case_row,
        "admin",
        roi_meta,
        analysis_data,
        score_stats,
        nuclei,
        gallery_data,
        test_roi_file,
        overlay_file,
        pdf_filename,
    )
    print(f"Saved to database with Analysis ID: {analysis_id}")

    slide.close()
    print("\nSUCCESS: All AI integration components verified!")

if __name__ == "__main__":
    try:
        run_test()
    finally:
        hovernet_manager.stop_worker()
