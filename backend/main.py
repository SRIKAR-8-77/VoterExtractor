"""
FastAPI Application — PDF to Excel Voter Data Processor

Provides REST API endpoints for uploading PDFs, tracking processing progress
via Server-Sent Events, and downloading generated Excel files.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PDF to Excel — Voter Data Processor",
    description="Upload voter roll PDFs, get structured Excel files back.",
    version="1.0.0",
)

# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job Store ────────────────────────────────────────────────
# In-memory store for job tracking. For production, use Redis/DB.

jobs: dict[str, dict] = {}

UPLOAD_DIR = Path(tempfile.gettempdir()) / "pdf_processor" / "uploads"
OUTPUT_DIR = Path(tempfile.gettempdir()) / "pdf_processor" / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Endpoints ────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/api/process")
async def process_pdfs(
    files: list[UploadFile] = File(...),
    max_pages: Optional[int] = None,
):
    """
    Accept one or more PDF files, start background processing.
    Returns a job_id for progress tracking and download.

    Args:
        max_pages: Optional limit on pages to process per PDF (for testing).
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' is not a PDF",
            )

    job_id = str(uuid.uuid4())
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded files
    saved_paths = []
    for f in files:
        dest = job_dir / f.filename
        with open(dest, "wb") as out:
            content = await f.read()
            out.write(content)
        saved_paths.append(str(dest))

    jobs[job_id] = {
        "status": "queued",
        "files": [f.filename for f in files],
        "progress": 0,
        "total": 100,
        "stage": "Queued",
        "detail": "",
        "output_path": None,
        "error": None,
        "created_at": datetime.utcnow().isoformat(),
    }

    # Launch background processing
    asyncio.create_task(_process_job(job_id, saved_paths, max_pages=max_pages))

    return {"job_id": job_id, "files": [f.filename for f in files]}


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    """
    Server-Sent Events endpoint for real-time progress updates.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        while True:
            job = jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break

            payload = json.dumps({
                "status": job["status"],
                "progress": job["progress"],
                "total": job["total"],
                "stage": job["stage"],
                "detail": job["detail"],
                "error": job["error"],
            })
            yield f"data: {payload}\n\n"

            if job["status"] in ("completed", "failed"):
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/download/{job_id}")
async def download_result(job_id: str):
    """Download the generated Excel file for a completed job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job status: {job['status']}")

    output_path = job.get("output_path")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output file not found")

    filename = os.path.basename(output_path)
    return FileResponse(
        path=output_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Get current job status."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ── Background Processing ───────────────────────────────────


async def _process_job(job_id: str, pdf_paths: list[str], max_pages: Optional[int] = None):
    """Run the full OCR + parsing pipeline in the background."""
    job = jobs[job_id]
    job["status"] = "processing"

    try:
        # Import here to avoid slow startup if models not needed yet
        from .ocr_engine import extract_pdf
        from .data_parser import parse_raw_text
        from .excel_builder import build_excel

        all_voter_records = []
        total_files = len(pdf_paths)

        for file_idx, pdf_path in enumerate(pdf_paths):
            filename = os.path.basename(pdf_path)
            job["detail"] = f"Processing {filename} ({file_idx + 1}/{total_files})"

            def progress_cb(current, total, stage):
                # Scale progress: each file gets equal share of 0-90%
                file_base = (file_idx / total_files) * 90
                file_share = 90 / total_files
                if total > 0:
                    file_progress = (current / total) * file_share
                else:
                    file_progress = 0
                job["progress"] = int(file_base + file_progress)
                job["total"] = 100
                job["stage"] = f"{stage} — {filename}"

            # Stage 1: OCR Extraction (run in thread to avoid blocking)
            raw_lines = await asyncio.to_thread(
                extract_pdf, pdf_path, progress_cb, max_pages
            )

            if not raw_lines:
                logger.warning("No data extracted from %s", filename)
                continue

            # Stage 2: Parse raw text
            job["stage"] = f"Parsing data — {filename}"
            records = await asyncio.to_thread(parse_raw_text, raw_lines)
            all_voter_records.extend(records)

        if not all_voter_records:
            job["status"] = "failed"
            job["error"] = "No voter data found in any of the uploaded PDFs"
            job["progress"] = 100
            return

        # Stage 3: Build Excel
        job["progress"] = 92
        job["stage"] = "Building Excel file"

        output_path = str(OUTPUT_DIR / f"{job_id}_voters.xlsx")
        await asyncio.to_thread(build_excel, all_voter_records, output_path)

        job["status"] = "completed"
        job["progress"] = 100
        job["stage"] = "Complete"
        job["output_path"] = output_path
        job["detail"] = f"Processed {len(all_voter_records)} voter records"

        logger.info(
            "Job %s completed: %d records from %d files",
            job_id, len(all_voter_records), total_files,
        )

    except Exception as e:
        logger.exception("Job %s failed", job_id)
        job["status"] = "failed"
        job["error"] = str(e)
        job["progress"] = 100
        job["stage"] = "Failed"

    finally:
        # Cleanup uploaded files
        job_dir = UPLOAD_DIR / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


# ── Static Files (Frontend) ─────────────────────────────────
# Serve React build if it exists
_frontend_dir = Path(__file__).parent.parent / "frontend" / "dist"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
