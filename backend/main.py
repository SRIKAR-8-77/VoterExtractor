"""
FastAPI Application — PDF to Excel Voter Data Processor

Provides REST API endpoints for uploading PDFs, tracking processing progress
via Server-Sent Events, downloading generated Excel files, and uploading
extracted voter images directly to Cloudflare R2.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
import psutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# Load .env file from project root
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PDF to Excel — Voter Data Processor",
    description="Upload voter roll PDFs, get structured Excel files back. "
                "Optionally upload extracted voter images directly to Cloudflare R2.",
    version="2.0.0",
)

# ── CORS ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job Queue ────────────────────────────────────────────────
# In-memory store for job tracking. For production, use Redis/DB.

jobs: dict[str, dict] = {}
job_queue = asyncio.Queue()

# Configure how many PDFs to process entirely in parallel. The rest wait in queue.
# Use PDF_PROCESSING_WORKERS from .env if present, otherwise fallback to MAX_CONCURRENT_JOBS or 4.
MAX_CONCURRENT_JOBS = int(os.getenv("PDF_PROCESSING_WORKERS", os.getenv("MAX_CONCURRENT_JOBS", "4")))

async def job_worker():
    """Background worker that processes one job at a time from the queue."""
    while True:
        job_data = await job_queue.get()
        if job_data is None:
            break
        job_id = job_data["job_id"]
        try:
            await _process_job(
                job_id,
                job_data["pdf_paths"],
                max_pages=job_data["max_pages"],
                r2_config=job_data["r2_config"],
                db_params=job_data["db_params"]
            )
        except Exception as e:
            logger.exception("Error processing job %s", job_id)
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["progress"] = 100
        finally:
            job_queue.task_done()

@app.on_event("startup")
async def startup_event():
    # Start multiple background worker processes for parallel execution
    for _ in range(MAX_CONCURRENT_JOBS):
        asyncio.create_task(job_worker())

UPLOAD_DIR = Path(tempfile.gettempdir()) / "pdf_processor" / "uploads"
OUTPUT_DIR = Path(tempfile.gettempdir()) / "pdf_processor" / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── R2 Configuration (from .env) ────────────────────────────
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_BUCKET = os.getenv("R2_BUCKET")


def _get_r2_config(base_path: str) -> Optional[dict]:
    """Build R2 config dict from env vars. Returns None if env vars are not set."""
    if all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT, R2_BUCKET]):
        return {
            "r2_access_key_id": R2_ACCESS_KEY_ID,
            "r2_secret_access_key": R2_SECRET_ACCESS_KEY,
            "r2_endpoint": R2_ENDPOINT,
            "r2_bucket": R2_BUCKET,
            "r2_base_path": base_path,
        }
    return None


def _validate_db_params(
    localBodyId: int,
    prabhagNo: str | None,
    wardNo: str | None,
    gat: str | None,
    gan: str | None,
    boothNo: str | None,
    zpPsSubType: str | None,
):
    """
    Validate DB params with separate inputs for ZP/PS.
    Raises HTTPException on validation failure.
    """
    if not localBodyId:
        raise HTTPException(status_code=400, detail="Local Body ID is required.")

    if zpPsSubType:
        if zpPsSubType not in ("ZP", "PS"):
            raise HTTPException(status_code=400, detail="zpPsSubType must be 'ZP' or 'PS'.")
        if not gat:
            raise HTTPException(status_code=400, detail="Division (गट / gat) is required for ZP/PS.")
        if zpPsSubType == "PS" and (not gan or gan == "-"):
            raise HTTPException(
                status_code=400,
                detail="Electoral College (गण / gan) is required for Panchayat Samiti (PS).",
            )
        if not boothNo:
            raise HTTPException(status_code=400, detail="Booth No. is required for ZP/PS.")
    else:
        if not prabhagNo or not wardNo:
            raise HTTPException(
                status_code=400,
                detail="Prabhag No. and Ward No. are required.",
            )


# ── Helper: R2 Upload ───────────────────────────────────────


def _upload_images_to_r2(
    crop_dir: str,
    records: list[dict],
    r2_config: dict,
    job_id: str,
) -> list[dict]:
    """
    Upload cropped voter images from `crop_dir` to Cloudflare R2.

    Returns a list of dicts: [{"sr_no": "1", "r2_key": "base_path/1.jpg"}, ...]
    """
    s3 = boto3.client(
        "s3",
        endpoint_url=r2_config["r2_endpoint"],
        aws_access_key_id=r2_config["r2_access_key_id"],
        aws_secret_access_key=r2_config["r2_secret_access_key"],
        region_name="auto",
    )

    bucket = r2_config["r2_bucket"]
    base_path = r2_config.get("r2_base_path", f"jobs/{job_id}").rstrip("/")

    manifest = []
    for row in records:
        box_id = row.get("_box_id")
        sr_no = row.get("sr.no", "").strip()
        if not box_id or not sr_no:
            continue

        img_path = os.path.join(crop_dir, f"{box_id}.jpg")
        if not os.path.exists(img_path):
            continue

        r2_key = f"{base_path}/{sr_no}.jpg"
        try:
            s3.upload_file(
                img_path,
                bucket,
                r2_key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )
            manifest.append({"sr_no": sr_no, "r2_key": r2_key})
        except ClientError as e:
            logger.error("Failed to upload %s to R2: %s", r2_key, e)

    return manifest


# ── Endpoints ────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/api/process")
async def process_pdfs(
    files: list[UploadFile] = File(...),
    max_pages: Optional[int] = None,
    r2_base_path: Optional[str] = Form(None),
    localBodyId: Optional[int] = Form(None),
    prabhagNo: Optional[str] = Form(None),
    wardNo: Optional[str] = Form(None),
    gat: Optional[str] = Form(None),
    gan: Optional[str] = Form(None),
    boothNo: Optional[str] = Form(None),
    zpPsSubType: Optional[str] = Form(None),
):
    """
    Accept one or more PDF files, start background processing.
    Returns a job_id for progress tracking and download.

    If `r2_base_path` is provided and R2 credentials are configured in the
    server's .env file, extracted voter images will be uploaded directly to
    Cloudflare R2 instead of being bundled into a ZIP.

    Args:
        files: One or more PDF files to process.
        max_pages: Optional limit on pages to process per PDF (for testing).
        r2_base_path: Path prefix for images in R2 (e.g. \"voters/batch1/images\").
        localBodyId: Required for DB Native Insert (local body database ID).
        prabhagNo: Division / Prabhag Number.
        wardNo: Electoral College / Ward Number.
        boothNo: Booth Number string.
        zpPsSubType: ZP or PS.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' is not a PDF",
            )

    # Build R2 config from env vars if r2_base_path is provided
    r2_config = None
    if r2_base_path:
        r2_config = _get_r2_config(r2_base_path)
        if not r2_config:
            raise HTTPException(
                status_code=500,
                detail="R2 credentials not configured on server. "
                       "Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT, "
                       "and R2_BUCKET in the .env file.",
            )

    # Validate DB params if localBodyId is provided
    if localBodyId:
        _validate_db_params(localBodyId, prabhagNo, wardNo, gat, gan, boothNo, zpPsSubType)

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

    form_data_str = (
        f"r2_base_path={r2_base_path}, "
        f"localBodyId={localBodyId}, "
        f"prabhagNo={prabhagNo}, "
        f"wardNo={wardNo}, "
        f"gat={gat}, "
        f"gan={gan}, "
        f"boothNo={boothNo}, "
        f"zpPsSubType={zpPsSubType}"
    )
    logger.info(f"Received POST /api/process with form data: {form_data_str}")

    jobs[job_id] = {
        "status": "queued",
        "files": [f.filename for f in files],
        "progress": 0,
        "total": 100,
        "stage": "Queued",
        "detail": f"Form Data: {form_data_str}",
        "output_path": None,
        "error": None,
        "excel_ready": False,
        "image_count": 0,
        "db_params": {
            "localBodyId": localBodyId,
            "prabhagNo": prabhagNo,
            "wardNo": wardNo,
            "gat": gat,
            "gan": gan,
            "boothNo": boothNo,
            "zpPsSubType": zpPsSubType,
            "r2_base_path": r2_config["r2_base_path"] if r2_config else None
        } if localBodyId else None,
        "db_records_updated": 0,
        "created_at": datetime.utcnow().isoformat(),
    }

    # Enqueue background processing
    job_queue.put_nowait({
        "job_id": job_id,
        "pdf_paths": saved_paths,
        "max_pages": max_pages,
        "r2_config": r2_config,
        "db_params": jobs[job_id]["db_params"]
    })

    return {"job_id": job_id, "files": [f.filename for f in files]}


@app.post("/api/upload-excel-to-db")
async def upload_excel_to_db(
    file: UploadFile = File(...),
    localBodyId: int = Form(...),
    prabhagNo: Optional[str] = Form(None),
    wardNo: Optional[str] = Form(None),
    gat: Optional[str] = Form(None),
    gan: Optional[str] = Form(None),
    boothNo: Optional[str] = Form(None),
    zpPsSubType: Optional[str] = Form(None)
):
    """
    Upload an Excel file to directly update PostgreSQL matching the Next.js logic.
    - localBodyId: Required ID.
    """
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="File must be an Excel file (.xlsx or .xls)")

    # Validate DB params
    _validate_db_params(localBodyId, prabhagNo, wardNo, gat, gan, boothNo, zpPsSubType)
        
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured in environment.")

    from .db_updater import process_voter_excel_to_db

    temp_file_path = UPLOAD_DIR / f"{uuid.uuid4()}_{file.filename}"
    temp_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(temp_file_path, "wb") as out:
        content = await file.read()
        out.write(content)

    db_params = {
        "localBodyId": localBodyId,
        "prabhagNo": prabhagNo,
        "wardNo": wardNo,
        "gat": gat,
        "gan": gan,
        "boothNo": boothNo,
        "zpPsSubType": zpPsSubType
    }

    try:
        records_updated = await asyncio.to_thread(
            process_voter_excel_to_db,
            str(temp_file_path),
            db_params,
            db_url
        )
    except Exception as e:
        logger.exception("Error updating database")
        raise HTTPException(status_code=500, detail=f"Database update failed: {str(e)}")
    finally:
        if temp_file_path.exists():
            os.remove(temp_file_path)

    return {"status": "success", "records_updated": records_updated}


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
                "excel_ready": job.get("excel_ready", False),
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

    output_path = job.get("output_path")
    if not output_path or not os.path.exists(output_path):
        if job.get("status") not in ("completed", "failed"):
            raise HTTPException(status_code=400, detail=f"Excel file not ready yet. Job status: {job.get('status')}")
        raise HTTPException(status_code=404, detail="Output file not found")

    filename = os.path.basename(output_path)
    return FileResponse(
        path=output_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/images/{job_id}")
async def get_image_manifest(job_id: str):
    """
    Get the manifest of voter images uploaded to R2 for this job.

    Returns a JSON object with:
    - job_id: The job identifier
    - r2_base_path: The base path used in R2
    - image_count: Number of images uploaded
    - images: List of {sr_no, r2_key} entries
    """
    manifest_path = OUTPUT_DIR / f"manifest_{job_id}.json"
    if not manifest_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Image manifest not found. Either the job hasn't completed, "
                   "no R2 config was provided, or no images were extracted.",
        )

    with open(manifest_path) as f:
        return json.load(f)


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Get current job status including image upload info."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/api/jobs")
async def get_all_jobs_status():
    """Get the current status of all jobs."""
    return jobs

@app.get("/progress")
async def progress_page():
    """Serve the complete rich HTML progress dashboard."""
    html_path = Path(__file__).parent / "progress.html"
    if html_path.exists():
        with open(html_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>progress.html not found in backend directory</h1>", status_code=404)

@app.get("/api/system")
async def get_system_stats():
    """
    Returns real-time system metrics (CPU, RAM, GPU if available).
    """
    mem = psutil.virtual_memory()
    
    # Try to fetch GPU stats via PyTorch 
    gpu_available = False
    gpu_name = "N/A"
    gpu_memory_used_gb = None
    gpu_memory_total_gb = None
    gpu_memory_percent = None
    
    try:
        import torch
        if torch.cuda.is_available():
            gpu_available = True
            gpu_name = torch.cuda.get_device_name(0)
            gpu_memory_used = torch.cuda.memory_allocated(0)
            gpu_memory_total = torch.cuda.get_device_properties(0).total_memory
            
            gpu_memory_used_gb = round(gpu_memory_used / (1024**3), 2)
            gpu_memory_total_gb = round(gpu_memory_total / (1024**3), 2)
            gpu_memory_percent = round((gpu_memory_used / gpu_memory_total) * 100, 1)
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            # Current PyTorch implementation does not easily expose raw metal memory 
            # limits, but we can verify it's running on Apple Silicon.
            gpu_available = True
            gpu_name = "Apple Neural Engine (MPS)"
    except Exception as e:
        logger.warning(f"Failed to check GPU stats: {e}")

    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": mem.percent,
        "ram_used_gb": round((mem.total - mem.available) / (1024**3), 2),
        "ram_total_gb": round(mem.total / (1024**3), 2),
        "gpu_available": gpu_available,
        "gpu_name": gpu_name,
        "gpu_memory_used_gb": gpu_memory_used_gb,
        "gpu_memory_total_gb": gpu_memory_total_gb,
        "gpu_memory_percent": gpu_memory_percent,
    }

@app.get("/system")
async def system_page():
    """Serve the system dashboard HTML page."""
    html_path = Path(__file__).parent / "system.html"
    if html_path.exists():
        with open(html_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>system.html not found in backend directory</h1>", status_code=404)

@app.get("/api/logs")
async def get_logs(lines: int = 1000):
    """Return the last N lines of the api.log file."""
    # Assuming api.log is in the root where the uvicorn command was executed
    log_path = Path("api.log")
    if not log_path.exists():
        # Try finding it in the parent directory just in case
        log_path = Path(__file__).parent.parent / "api.log"
        
    if not log_path.exists():
        return {"logs": ["api.log not found."]}
    
    try:
        from collections import deque
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            last_lines = deque(f, lines)
            return {"logs": list(last_lines)}
    except Exception as e:
        return {"logs": [f"Error reading logs: {str(e)}"]}

@app.get("/logs")
async def logs_page():
    """Serve the API logs dashboard HTML page."""
    html_path = Path(__file__).parent / "logs.html"
    if html_path.exists():
        with open(html_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>logs.html not found in backend directory</h1>", status_code=404)


# ── Background Processing ───────────────────────────────────


async def _process_job(
    job_id: str,
    pdf_paths: list[str],
    max_pages: Optional[int] = None,
    r2_config: Optional[dict] = None,
    db_params: Optional[dict] = None,
):
    """Run the full OCR + parsing + R2 upload pipeline in the background."""
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
                # Scale progress: each file gets equal share of 0-80%
                file_base = (file_idx / total_files) * 80
                file_share = 80 / total_files
                if total > 0:
                    file_progress = (current / total) * file_share
                else:
                    file_progress = 0
                job["progress"] = int(file_base + file_progress)
                job["total"] = 100
                job["stage"] = f"{stage} — {filename}"

            # Stage 1: OCR Extraction (run in thread to avoid blocking)
            raw_lines = await asyncio.to_thread(
                extract_pdf, pdf_path, progress_cb, max_pages, job_id=job_id
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
        job["progress"] = 82
        job["stage"] = "Building Excel file"

        output_path = str(OUTPUT_DIR / f"{job_id}_voters.xlsx")
        await asyncio.to_thread(build_excel, all_voter_records, output_path)
        
        # Make Excel available immediately for download
        job["output_path"] = output_path
        job["excel_ready"] = True

        # Stage 4: Upload images to R2 (if config provided)
        image_count = 0
        if r2_config:
            job["progress"] = 90
            job["stage"] = "Uploading images to Cloudflare R2"

            # Look for the crop directory
            crop_dir = str(OUTPUT_DIR / f"{job_id}_crops")
            # Also check fallback location used by worker.py
            if not os.path.exists(crop_dir):
                crop_dir = os.path.join("output", f"{job_id}_crops")

            if os.path.exists(crop_dir):
                manifest_entries = await asyncio.to_thread(
                    _upload_images_to_r2,
                    crop_dir,
                    all_voter_records,
                    r2_config,
                    job_id,
                )

                image_count = len(manifest_entries)

                # Save manifest to disk
                manifest_data = {
                    "job_id": job_id,
                    "r2_bucket": r2_config["r2_bucket"],
                    "r2_base_path": r2_config.get("r2_base_path", ""),
                    "image_count": image_count,
                    "images": manifest_entries,
                }
                manifest_path = str(OUTPUT_DIR / f"manifest_{job_id}.json")
                with open(manifest_path, "w") as f:
                    json.dump(manifest_data, f, indent=2)

                # Cleanup crop directory after upload
                shutil.rmtree(crop_dir, ignore_errors=True)

                logger.info(
                    "Job %s: uploaded %d images to R2 bucket '%s'",
                    job_id, image_count, r2_config["r2_bucket"],
                )
            else:
                logger.warning("Job %s: crop directory not found, no images to upload", job_id)

        # Stage 5: Upload to Database (if configured)
        records_updated = 0
        if db_params and db_params.get("localBodyId"):
            job["progress"] = 95
            job["stage"] = "Uploading data to PostgreSQL Database"
            
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                logger.error("Job %s: DATABASE_URL not set but db_params provided", job_id)
            else:
                from .db_updater import process_voter_excel_to_db
                
                records_updated = await asyncio.to_thread(
                    process_voter_excel_to_db,
                    output_path,
                    db_params,
                    db_url
                )
                
                logger.info(
                    "Job %s: Processed and inserted %d records to DB",
                    job_id, records_updated
                )


        job["status"] = "completed"
        job["progress"] = 100
        job["stage"] = "Complete"
        job["output_path"] = output_path
        job["image_count"] = image_count
        job["db_records_updated"] = records_updated
        job["detail"] = (
            f"Processed {len(all_voter_records)} voter records"
            + (f", uploaded {image_count} images to R2" if image_count else "")
            + (f", updated {records_updated} DB records" if db_params else "")
        )

        logger.info(
            "Job %s completed: %d records from %d files, %d images uploaded, %d db records updated",
            job_id, len(all_voter_records), total_files, image_count, records_updated
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
