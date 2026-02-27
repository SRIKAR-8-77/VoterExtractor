"""
OCR Engine Module — Stage 1: PDF Rasterization + OCR Extraction

Uses Surya-OCR for text recognition, pdf2image (poppler) for PDF rasterization,
and pdfplumber for grid/table detection.

Core logic mirrors the original notebook: ocr_final (1).ipynb
"""

import re
import time
import threading
import logging
from typing import Callable, Optional

import numpy as np
import pdfplumber
import fitz
from PIL import Image

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────
# DPI=800 matches the notebook for high-quality rasterization
DPI = 800

# ── Global model holders (lazy-loaded) ───────────────────────
_foundation_predictor = None
_det_predictor = None
_rec_predictor = None


def load_models():
    """Load Surya OCR models. Safe to call multiple times (idempotent)."""
    global _foundation_predictor, _det_predictor, _rec_predictor

    if _rec_predictor is not None:
        return _rec_predictor, _det_predictor

    from surya.foundation import FoundationPredictor
    from surya.detection import DetectionPredictor
    from surya.recognition import RecognitionPredictor

    logger.info("Loading Surya OCR models...")
    t = time.time()
    _foundation_predictor = FoundationPredictor()
    _det_predictor = DetectionPredictor()
    _rec_predictor = RecognitionPredictor(
        foundation_predictor=_foundation_predictor
    )
    logger.info("Models loaded in %.1fs", time.time() - t)
    return _rec_predictor, _det_predictor


# ── Grid Detection ───────────────────────────────────────────


def _get_filtered_coords(lines_subset, coord_key, len_key, tol=2.0, min_len=50):
    """Cluster line coordinates and filter by minimum length."""
    lines_subset.sort(key=lambda x: x[coord_key])
    if not lines_subset:
        return []

    unique_coords = []
    curr_pos = [lines_subset[0][coord_key]]
    curr_len = [abs(lines_subset[0][len_key[1]] - lines_subset[0][len_key[0]])]

    for line in lines_subset[1:]:
        pos = line[coord_key]
        length = abs(line[len_key[1]] - line[len_key[0]])

        if abs(pos - curr_pos[-1]) <= tol:
            curr_pos.append(pos)
            curr_len.append(length)
        else:
            if max(curr_len) > min_len:
                unique_coords.append(sum(curr_pos) / len(curr_pos))
            curr_pos = [pos]
            curr_len = [length]

    if max(curr_len) > min_len:
        unique_coords.append(sum(curr_pos) / len(curr_pos))

    return unique_coords


def get_boxes_and_header(pdf_path: str, page_num: int, img_w: int, img_h: int):
    """
    Detect voter boxes and header region on a page using pdfplumber line analysis.

    Returns:
        (boxes, header_rect) where boxes is a list of (x, y, w, h) tuples
        and header_rect is (x1, y1, x2, y2) or None.
    """
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]
        lines = page.lines

        v_raw = [l for l in lines if abs(l["x1"] - l["x0"]) < 1]
        h_raw = [l for l in lines if abs(l["bottom"] - l["top"]) < 1]

        unique_vs = _get_filtered_coords(v_raw, "x0", ("top", "bottom"), min_len=50)
        unique_hs = _get_filtered_coords(h_raw, "top", ("x0", "x1"), min_len=50)

        if not unique_vs or not unique_hs:
            return [], None

        scale_x = img_w / page.width
        scale_y = img_h / page.height

        header_bottom = int(unique_hs[0] * scale_y)
        header_rect = (0, 0, img_w, max(50, header_bottom - 5))

        boxes = []
        for i in range(len(unique_hs) - 1):
            y_top = unique_hs[i]
            y_bottom = unique_hs[i + 1]
            if y_bottom - y_top < 40:
                continue
            for j in range(len(unique_vs) - 1):
                x_left = unique_vs[j]
                x_right = unique_vs[j + 1]
                if x_right - x_left < 100:
                    continue
                x = int(x_left * scale_x)
                y = int(y_top * scale_y)
                w = int((x_right - x_left) * scale_x)
                h = int((y_bottom - y_top) * scale_y)
                boxes.append((x, y, w, h))

        boxes.sort(key=lambda b: (int(b[1] // 50), b[0]))
        return boxes, header_rect


# ── Main Extraction Pipeline ─────────────────────────────────


def extract_pdf(
    pdf_path: str,
    progress_callback: Optional[Callable] = None,
    max_pages: Optional[int] = None,
    job_id: str = "temp"
) -> list[str]:
    """
    Full Stage 1 pipeline: Rasterize PDF → Detect grids → OCR → Return raw text lines.

    Core logic matches the notebook (ocr_final (1).ipynb):
    - Pages are processed one at a time (not pre-rasterized in bulk)
    - pdf2image with DPI=800 is used for high-quality rasterization
    - Auto-detection: skips intro pages, stops immediately when the voter
      section ends (first non-voter page after start triggers a break)
    - OCR is called per-crop with a threading lock (matches notebook behaviour)
    - Saves crop images to disk temporarily if a job_id is provided.

    Args:
        pdf_path: Absolute path to the PDF file.
        progress_callback: Optional callback(current, total, stage_name).
        max_pages: Optional limit on number of pages to process (for testing).
        job_id: Unique identifier for the job, used for temp crop storage.

    Returns:
        List of raw text lines (same format as the _RAW.txt files from the notebook).
    """
    import os
    rec_predictor, det_predictor = load_models()
    
    crop_dir = os.path.join("output", f"{job_id}_crops")
    os.makedirs(crop_dir, exist_ok=True)

    def _progress(current, total, stage):
        if progress_callback:
            progress_callback(current, total, stage)

    _progress(0, 1, "Opening PDF")

    with pdfplumber.open(pdf_path) as p:
        total_pages = len(p.pages)

    if max_pages and max_pages > 0:
        total_pages = min(total_pages, max_pages)
        logger.info("Limiting processing to %d pages", total_pages)

    logger.info("Processing %d pages at DPI=%d", total_pages, DPI)

    output_lines = []

    for page_num in range(total_pages):
        _progress(page_num + 1, total_pages, f"Processing page {page_num + 1}")

        try:
            # Rasterize single page at high DPI using PyMuPDF (fitz)
            with fitz.open(pdf_path) as doc:
                page = doc[page_num]
                mat = fitz.Matrix(DPI / 72.0, DPI / 72.0)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            boxes, header_rect = get_boxes_and_header(
                pdf_path, page_num, img.width, img.height
            )

            if not boxes:
                continue

            # Simply process the page and let data_parser.py scrub empty boxes later.
            logger.info("Processing Page %d...", page_num + 1)

            # Extract header text
            header_text = ""
            if header_rect:
                hc = img.crop(header_rect)
                h_preds = rec_predictor([hc], det_predictor=det_predictor)
                header_text = " ".join([ln.text for ln in h_preds[0].text_lines])

            output_lines.append(f"\n=== PAGE {page_num + 1} ===")
            output_lines.append(f"HEADER: {header_text}")
            output_lines.append("")

            # OCR all valid voter boxes in a single batch for maximum speed
            valid_crops = []
            valid_indices = []
            
            for i, (x, y, w, h) in enumerate(boxes):
                crop = img.crop((x, y, x + w, y + h))
                # Skip blank / all-white boxes
                if np.mean(np.array(crop.convert("L"))) <= 250:
                    valid_crops.append(crop)
                    valid_indices.append(i)

            box_count = 0
            if valid_crops:
                try:
                    preds = rec_predictor(valid_crops, det_predictor=det_predictor)
                    
                    for crop_idx, pred in zip(valid_indices, preds):
                        crop_img = valid_crops[valid_indices.index(crop_idx)]
                        
                        # Save the crop image for the zip bundle later
                        crop_filename = f"{page_num}_{crop_idx}.jpg"
                        crop_img.save(os.path.join(crop_dir, crop_filename), "JPEG")
                        
                        raw_text = " | ".join([ln.text for ln in pred.text_lines])
                        output_lines.append(f"BOX {page_num}_{crop_idx}: {raw_text}")
                        
                    box_count = len(valid_crops)
                except Exception as e:
                    logger.warning("Error processing batch on page %d: %s", page_num + 1, e)

            logger.info("Extracted %d boxes from page %d", box_count, page_num + 1)

        except Exception as e:
            logger.error("Error on page %d: %s", page_num + 1, e)
            continue

    if not output_lines:
        logger.warning("No voter pages detected in %s", pdf_path)
        return []

    return output_lines
