# PDF → Excel: Voter Data Processor

A web application that extracts structured voter data from PDF voter rolls using OCR (Surya-OCR) and generates formatted Excel files. Upload one or more PDFs, track processing progress in real-time, and download the results.

## Features

- 🔍 **OCR-powered extraction** — Uses Surya-OCR with GPU acceleration for high-accuracy Marathi text recognition
- 📊 **Structured output** — Parses voter ID, serial numbers, names, addresses, gender, and age into a clean 12-column Excel format
- 📁 **Multi-file upload** — Process multiple PDFs in a single batch
- 📈 **Real-time progress** — Server-Sent Events (SSE) provide live progress updates during processing
- 🎨 **Modern UI** — Dark-themed React interface with drag-and-drop upload
- 🐳 **Docker ready** — Multi-stage Docker build for easy deployment

## Step-by-Step Deployment

Running the deployment step-by-step is recommended to easily catch any installation errors or dependencies missing on your server.

### 1. Sync Code to Server
From your local machine, use `rsync` to upload the project files (this includes your `.env` configuration file). Change the port and IP to match your server:

```bash
rsync -avz --exclude '.git' --exclude 'output' --exclude 'uploads' --exclude '__pycache__' --exclude 'server.log' -e 'ssh -p 46056' ./ root@76.68.174.145:/root/VoterExtractor/
ssh -p 46056 root@76.68.174.145 -L 8080:localhost:8080
```

### 2. Enter the Server
Connect to your remote server:

```bash
ssh -p 40832 root@135.135.24.114
```

### 3. Install Dependencies
Once inside the server, go to the folder and install all required python packages:

```bash
cd /root/VoterExtractor
# Install heavy ML models and basic libraries
pip install -r backend/requirements.txt
# Ensure server running utilities are present
pip install fastapi uvicorn python-dotenv boto3 psutil streamlit
```

### 4. Start the FastAPI Server (Port 6969)
First, kill any existing instance. Then, start it in the background using `nohup` so it stays alive when you disconnect:

```bash
# Stop old server
pkill -f 'uvicorn backend.main' || true

# Start new server in the background
nohup env CUDA_LAUNCH_BLOCKING=1 python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 6969 < /dev/null > api.log 2>&1 &
```
*Tip: You can view the live server logs by running `tail -f api.log`*

### 5. Start the Streamlit UI (Port 3000) (Optional)
If you want the Streamlit frontend running on that same server:

```bash
# Stop old UI
pkill -f 'streamlit run' || true

# Start new UI in the background
nohup streamlit run app.py --server.port 3000 --server.address 0.0.0.0 < /dev/null > ui.log 2>&1 &
```
*Tip: View the UI logs with `tail -f ui.log`*

### Health Check

Verify the API server is running successfully:

```bash
curl http://123.21.129.10:6969/api/health
# Expected: {"status":"ok","timestamp":"..."}
```

> **Important:** The Streamlit UI and FastAPI API run on **separate ports** (3000 and 6969). If you're using a Cloudflare tunnel, you need a tunnel for each port, or expose only the API port for your Vercel frontend.

## Project Structure

```
swamiproject/
├── backend/
│   ├── __init__.py
│   ├── ocr_engine.py         # Stage 1: PDF rasterization + OCR extraction
│   ├── data_parser.py        # Stage 2: Text cleaning + voter data parsing
│   ├── excel_builder.py      # Formatted Excel file generation
│   ├── main.py               # FastAPI application with SSE progress
│   └── requirements.txt      # Python dependencies
├── frontend/
│   ├── src/
│   │   ├── App.jsx            # Main React component
│   │   ├── App.css            # Application styles
│   │   ├── index.css          # Global styles
│   │   └── main.jsx           # React entry point
│   ├── index.html
│   ├── vite.config.js         # Vite config with API proxy
│   └── package.json
├── testing/
│   └── test.pdf               # Sample voter roll PDF for testing
├── Dockerfile                 # Multi-stage Docker build
├── docker-compose.yml         # Docker Compose with model caching
├── .dockerignore
└── README.md
```

## Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for frontend development)
- **NVIDIA GPU** (recommended for fast OCR processing; CPU fallback supported)
- **CUDA 12.x** (if using GPU)

## Running Locally

### 1. Install Dependencies

```bash
# Install Python packages
pip install -r backend/requirements.txt
pip install streamlit fastapi uvicorn

# For GPU support (recommended), install PyTorch with CUDA:
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
```

> **Note:** On first run, Surya-OCR will download its models (~1.4GB). This is cached in `~/.cache/datalab` for subsequent runs.

### 2. Start the Application Interface (Streamlit)

```bash
streamlit run app.py
```

The Streamlit interface will be available at **http://localhost:8501**

### 3. Start the API Server (For Remote Downloads)

Currently, the downloads API provides endpoints to programmatically download the generated Excel and ZIP files.

In a separate terminal:

```bash
python -m uvicorn api:app --host 0.0.0.0 --port 6969 --reload
```

The API will be available at **http://localhost:6969**

### 4. Test with Sample PDF

Upload `testing/test.pdf` through the web UI, or use curl:

```bash
# Upload PDF (process only first 5 pages for quick testing)
curl -X POST "http://localhost:6969/api/process?max_pages=5" \
  -F "files=@testing/test.pdf"
# Returns: {"job_id": "...", "files": ["test.pdf"]}

# Upload and process ALL pages (no limit)
curl -X POST http://localhost:6969/api/process \
  -F "files=@testing/test.pdf"

# Check progress
curl http://localhost:6969/api/jobs/{job_id}

# Download result (once status is "completed")
curl -O http://localhost:6969/api/download/{job_id}
```

## Running with Docker

### Build and Run

```bash
# Build the Docker image
docker compose build

# Start the application
docker compose up -d
```

The application will be available at **http://localhost:6969**

### GPU Support

To enable GPU acceleration inside Docker, uncomment the `deploy` section in `docker-compose.yml`:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

Requires [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

### Useful Docker Commands

```bash
# View logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

## Deploying on a Cloud Instance (Static IP)

If you have a cloud VM (AWS EC2, GCP, Azure, DigitalOcean, etc.) with a **static/elastic IP**, users can access the app directly at `http://<YOUR-STATIC-IP>:6969`.

### Step-by-Step

1. **SSH into your cloud instance**:
   ```bash
   ssh user@<YOUR-STATIC-IP>
   ```

2. **Clone or upload the project**:
   ```bash
   git clone <your-repo-url>
   cd swamiproject
   ```

3. **Install dependencies**:
   ```bash
   pip install -r backend/requirements.txt
   cd frontend && npm install && npm run build && cd ..
   ```

4. **Open ports in your cloud firewall**:
   - Port **8501** for Streamlit UI
   - Port **6969** for API Downloads

5. **Start the application and API server**:
   ```bash
   # Run Streamlit (UI)
   tmux new -s pdfapp_ui
   streamlit run app.py
   # Detach with Ctrl+B then D

   # Run API (Downloads)
   tmux new -s pdfapp_api
   python -m uvicorn api:app --host 0.0.0.0 --port 6969
   # Detach with Ctrl+B then D
   ```

6. **Users can visit**: `http://<YOUR-STATIC-IP>:8501`

### Running in Background (Production)

Use `nohup` or `tmux` to keep the servers running after you disconnect:

```bash
# Option 2: tmux (recommended)
tmux new -s pdfapp_ui
streamlit run app.py
# Press Ctrl+B then D to detach. Reconnect with: tmux attach -t pdfapp_ui

tmux new -s pdfapp_api
python -m uvicorn api:app --host 0.0.0.0 --port 6969
# Press Ctrl+B then D to detach. Reconnect with: tmux attach -t pdfapp_api
```

### Using Docker on Cloud

```bash
docker compose up -d --build
# App available at http://<YOUR-STATIC-IP>:6969
```

### Optional: Use Port 80 (No Port Number Needed)

To let users visit `http://<YOUR-STATIC-IP>` without `:6969`:

```bash
# Change the port to 80 (requires root/sudo)
sudo python -m uvicorn backend.main:app --host 0.0.0.0 --port 80

# Or in docker-compose.yml, change ports to:
#   ports:
#     - "80:6969"
```

## API Reference

### Health Check

```
GET /api/health
```

**Response:**
```json
{ "status": "ok", "timestamp": "2026-03-13T00:00:00" }
```

---

### Process PDFs

```
POST /api/process
Content-Type: multipart/form-data
```

Upload one or more PDF files for OCR processing. If `r2_base_path` is provided and R2 credentials are configured in the server's `.env` file, extracted voter images are uploaded directly to Cloudflare R2.

**Form Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `files` | File(s) | ✅ | One or more PDF files |
| `max_pages` | integer | ❌ | Limit pages processed per PDF (for testing) |
| `r2_base_path` | string | ❌ | Path prefix for images in R2 (e.g. `voters/batch1/images`). If provided, images are uploaded to R2. |
| `localBodyId` | integer | ❌ | Local Body ID for DB insert. If provided, extracted data is saved to the database. |
| `prabhagNo` | string | ❌ | Prabhag number (used for standard local body DB scoping). |
| `wardNo` | string | ❌ | Ward number (used for standard local body DB scoping). |
| `gat` | string | ❌ | Division / Gat number (used for ZP/PS types). |
| `gan` | string | ❌ | Electoral College / Gan number (used for PS type). |
| `boothNo` | string | ❌ | Booth number (for ZP/PS types). |
| `zpPsSubType` | string | ❌ | `"ZP"` or `"PS"` — targets the `zp_ps_voters` table instead of `voters`. |

> **Note:** R2 and DB credentials are read from the server's `.env` file, not sent in the request. See [Configuration](#configuration) below.

**Response:**
```json
{ "job_id": "a1b2c3d4-...", "files": ["voters_part1.pdf"] }
```

**Example — Without R2 (images saved as ZIP locally):**
```bash
curl -X POST http://localhost:6969/api/process \
  -F "files=@testing/test.pdf"
```

**Example — Full pipeline (R2 + DB):**
```bash
curl -X POST http://localhost:6969/api/process \
  -F "files=@testing/test.pdf" \
  -F "r2_base_path=voters/batch_001/images" \
  -F "localBodyId=1" \
  -F "prabhagNo=1" \
  -F "wardNo=1"
```

**Example — Limit to 5 pages (testing):**
```bash
curl -X POST "http://localhost:6969/api/process?max_pages=5" \
  -F "files=@testing/test.pdf" \
  -F "r2_base_path=testing/images"
```

---

### Track Job Progress (SSE)

```
GET /api/progress/{job_id}
```

Server-Sent Events stream for real-time progress updates. Connect and listen for `data:` events.

**Event payload:**
```json
{
  "status": "processing",
  "progress": 45,
  "total": 100,
  "stage": "OCR Extraction — voters_part1.pdf",
  "detail": "Processing voters_part1.pdf (1/1)",
  "error": null
}
```

**Status values:** `queued` → `processing` → `completed` | `failed`

**Example:**
```bash
curl -N http://localhost:6969/api/progress/{job_id}
```

---

### Get Job Status

```
GET /api/jobs/{job_id}
```

**Response (completed with R2):**
```json
{
  "status": "completed",
  "files": ["voters_part1.pdf"],
  "progress": 100,
  "total": 100,
  "stage": "Complete",
  "detail": "Processed 450 voter records, uploaded 450 images to R2, updated 450 DB records",
  "output_path": "/tmp/pdf_processor/outputs/a1b2c3d4_voters.xlsx",
  "error": null,
  "image_count": 450,
  "db_params": {
    "localBodyId": 1,
    "prabhagNo": "1",
    "wardNo": "1",
    "boothNo": null,
    "zpPsSubType": null
  },
  "db_records_updated": 450,
  "created_at": "2026-03-13T00:00:00"
}
```

---

### Download Excel

```
GET /api/download/{job_id}
```

Downloads the generated `.xlsx` Excel file for a completed job.

**Example:**
```bash
curl -O http://localhost:6969/api/download/{job_id}
```

---

### Get Image Manifest

```
GET /api/images/{job_id}
```

Returns the manifest of voter images that were uploaded to R2. Only available if `r2_base_path` was provided during processing.

**Response:**
```json
{
  "job_id": "a1b2c3d4-...",
  "r2_bucket": "voter-images",
  "r2_base_path": "voters/batch_001/images",
  "image_count": 450,
  "images": [
    { "sr_no": "1", "r2_key": "voters/batch_001/images/1.jpg" },
    { "sr_no": "2", "r2_key": "voters/batch_001/images/2.jpg" }
  ]
}
```

**Example:**
```bash
curl http://localhost:6969/api/images/{job_id}
```

---

### Upload Excel to Database (Standalone)

```
POST /api/upload-excel-to-db
Content-Type: multipart/form-data
```

Upload a pre-existing Excel file to the PostgreSQL database (same logic as the integrated pipeline).

**Form Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | File | ✅ | The `.xlsx` or `.xls` Excel file to upload |
| `localBodyId` | integer | ✅ | Local Body ID in the database |
| `prabhagNo` | string | ❌ | Prabhag number (for standard types) |
| `wardNo` | string | ❌ | Ward number (for standard types) |
| `gat` | string | ❌ | Division / Gat number (for ZP/PS types) |
| `gan` | string | ❌ | Electoral College / Gan number (for PS type) |
| `boothNo` | string | ❌ | Booth number (for ZP/PS types) |
| `zpPsSubType` | string | ❌ | `"ZP"` or `"PS"` — targets `zp_ps_voters` table |

> **Note:** Requires `DATABASE_URL` to be properly configured in the `.env` file.

**Response:**
```json
{
  "status": "success",
  "records_updated": 450
}
```

**Example:**
```bash
curl -X POST http://localhost:6969/api/upload-excel-to-db \
  -F "file=@voters.xlsx" \
  -F "localBodyId=1" \
  -F "prabhagNo=1" \
  -F "wardNo=1"
```

---

## Cloudflare R2 Integration

This architecture is designed for **Vercel frontends** that cannot handle large ZIP files (200MB+). Instead of downloading and unzipping images on the edge, the backend uploads them directly to your R2 bucket.

### How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Vercel App  │────▶│  Backend (FastAPI)│────▶│ Cloudflare R2│
│  (Frontend)  │     │  (GPU Server)     │     │  (Storage)   │
└─────────────┘     └──────────────────┘     └──────────────┘
       │                     │                       ▲
       │  1. POST /process   │                       │
       │  (with r2_base_path)│                       │
       │────────────────────▶│                       │
       │  ◀── { job_id }     │  2. OCR + Extract     │
       │                     │  3. Upload images ────┘
       │  4. GET /jobs/{id}  │     directly to R2
       │────────────────────▶│     (creds from .env)
       │  ◀── { status,      │
       │    image_count }    │
       │                     │
       │  5. GET /download   │
       │────────────────────▶│
       │  ◀── Excel file     │
       │                     │
       │  6. GET /images     │
       │────────────────────▶│
       │  ◀── manifest.json  │ (list of R2 keys)
       └─────────────────────┘
```

### Frontend Integration (JavaScript / Vercel)

```javascript
// 1. Submit PDF with R2 base path (creds are on the server's .env)
const form = new FormData();
form.append('files', pdfFile);
form.append('r2_base_path', `the folder path`);

const res = await fetch(`${BACKEND_URL}/api/process`, {
  method: 'POST',
  body: form,
});
const { job_id } = await res.json();

// 2. Poll for completion
let status;
do {
  const jobRes = await fetch(`${BACKEND_URL}/api/jobs/${job_id}`);
  status = await jobRes.json();
  await new Promise(r => setTimeout(r, 2000));
} while (status.status === 'processing' || status.status === 'queued');

// 3. Download Excel (small file, ~1-5MB)
const excelBlob = await fetch(`${BACKEND_URL}/api/download/${job_id}`).then(r => r.blob());

// 4. Get image manifest (images are already in your R2 bucket!)
const manifest = await fetch(`${BACKEND_URL}/api/images/${job_id}`).then(r => r.json());
console.log(`${manifest.image_count} images uploaded to R2`);
// Access images via: https://your-r2-domain.com/{r2_key}
```

### R2 Bucket Setup

1. Create an R2 bucket in your [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Generate an API token with **Object Read & Write** permissions
3. Note your **Account ID** (used in the endpoint URL)
4. Your endpoint URL format: `https://<account_id>.r2.cloudflarestorage.com`
5. Copy `.env.example` to `.env` and fill in your R2 credentials:
   ```bash
   cp .env.example .env
   # Edit .env with your R2 credentials
   ```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OCR_BATCH_SIZE` | `32` | Batch size for GPU OCR processing |
| `R2_ACCESS_KEY_ID` | — | Cloudflare R2 access key ID |
| `R2_SECRET_ACCESS_KEY` | — | Cloudflare R2 secret access key |
| `R2_ENDPOINT` | — | R2 endpoint URL (e.g. `https://<account_id>.r2.cloudflarestorage.com`) |
| `R2_BUCKET` | — | R2 bucket name |
| `DATABASE_URL` | — | PostgreSQL connection URL (e.g. `postgresql://user:pass@host/db?sslmode=require`) |

## Tech Stack

- **Backend Logic:** Surya-OCR, PyMuPDF, pdfplumber, openpyxl, multiprocessing architecture
- **Frontend / UI:** Streamlit
- **API:** FastAPI
- **Database:** PostgreSQL (Neon) via SQLAlchemy + psycopg2
- **Image Storage:** Cloudflare R2 (S3-compatible) via boto3
- **Container:** Docker, multi-stage build
