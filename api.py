import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import os
import glob

app = FastAPI(
    title="VoterXcel Downloads API",
    description="Provides API endpoints to download generated Excel files and image manifests."
)

@app.get("/api/download/{job_id}")
async def download_excel(job_id: str):
    """Download the generated Excel file for a completed job."""
    search_pattern = os.path.join("output", f"output_{job_id}_*.xlsx")
    files = glob.glob(search_pattern)
    
    if not files:
        raise HTTPException(status_code=404, detail="Excel file not found")
    
    return FileResponse(
        path=files[0],
        filename=os.path.basename(files[0]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

@app.get("/api/images/{job_id}")
async def get_image_manifest(job_id: str):
    """
    Get the manifest of voter images uploaded to R2 for this job.
    
    Returns a JSON object with:
    - job_id: The job identifier
    - r2_bucket: The R2 bucket used
    - r2_base_path: The base path used in R2
    - image_count: Number of images uploaded
    - images: List of {sr_no, r2_key} entries
    """
    manifest_path = os.path.join("output", f"manifest_{job_id}.json")
    if not os.path.exists(manifest_path):
        raise HTTPException(
            status_code=404,
            detail="Image manifest not found. Either the job hasn't completed, "
                   "no R2 config was provided, or no images were extracted.",
        )
    
    with open(manifest_path) as f:
        return json.load(f)

@app.get("/api/health")
async def health():
    return {"status": "ok"}
