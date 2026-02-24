# PDF → Excel: Voter Data Processor

A web application that extracts structured voter data from PDF voter rolls using OCR (Surya-OCR) and generates formatted Excel files. Upload one or more PDFs, track processing progress in real-time, and download the results.

## Features

- 🔍 **OCR-powered extraction** — Uses Surya-OCR with GPU acceleration for high-accuracy Marathi text recognition
- 📊 **Structured output** — Parses voter ID, serial numbers, names, addresses, gender, and age into a clean 12-column Excel format
- 📁 **Multi-file upload** — Process multiple PDFs in a single batch
- 📈 **Real-time progress** — Server-Sent Events (SSE) provide live progress updates during processing
- 🎨 **Modern UI** — Dark-themed React interface with drag-and-drop upload
- 🐳 **Docker ready** — Multi-stage Docker build for easy deployment

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

### 1. Install Backend Dependencies

```bash
# Install Python packages
pip install -r backend/requirements.txt

# For GPU support (recommended), install PyTorch with CUDA:
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
```

> **Note:** On first run, Surya-OCR will download its models (~1.4GB). This is cached in `~/.cache/datalab` for subsequent runs.

### 2. Start the Backend Server

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at **http://localhost:8000**

### 3. Start the Frontend (Development Mode)

In a separate terminal:

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173** in your browser. The Vite dev server proxies `/api/*` requests to the backend.

### 4. Test with Sample PDF

Upload `testing/test.pdf` through the web UI, or use curl:

```bash
# Upload PDF (process only first 5 pages for quick testing)
curl -X POST "http://localhost:8000/api/process?max_pages=5" \
  -F "files=@testing/test.pdf"
# Returns: {"job_id": "...", "files": ["test.pdf"]}

# Upload and process ALL pages (no limit)
curl -X POST http://localhost:8000/api/process \
  -F "files=@testing/test.pdf"

# Check progress
curl http://localhost:8000/api/jobs/{job_id}

# Download result (once status is "completed")
curl -O http://localhost:8000/api/download/{job_id}
```

## Running with Docker

### Build and Run

```bash
# Build the Docker image
docker compose build

# Start the application
docker compose up -d
```

The application will be available at **http://localhost:8000**

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

If you have a cloud VM (AWS EC2, GCP, Azure, DigitalOcean, etc.) with a **static/elastic IP**, users can access the app directly at `http://<YOUR-STATIC-IP>:8000`.

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

4. **Open port 8000 in your cloud firewall**:
   - **AWS EC2**: Security Group → Add Inbound Rule → TCP port 8000 from 0.0.0.0/0
   - **GCP**: VPC Firewall → Create rule → TCP 8000, target: all instances
   - **Azure**: NSG → Add Inbound Rule → TCP 8000
   - **DigitalOcean**: Networking → Firewalls → Add rule → TCP 8000

5. **Start the server** (binds to all interfaces so external IPs can reach it):
   ```bash
   python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

6. **Users can visit**: `http://<YOUR-STATIC-IP>:8000`

### Running in Background (Production)

Use `nohup` or `tmux` to keep the server running after you disconnect:

```bash
# Option 1: nohup
nohup python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &

# Option 2: tmux (recommended)
tmux new -s pdfapp
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
# Press Ctrl+B then D to detach. Reconnect with: tmux attach -t pdfapp
```

### Using Docker on Cloud

```bash
docker compose up -d --build
# App available at http://<YOUR-STATIC-IP>:8000
```

### Optional: Use Port 80 (No Port Number Needed)

To let users visit `http://<YOUR-STATIC-IP>` without `:8000`:

```bash
# Change the port to 80 (requires root/sudo)
sudo python -m uvicorn backend.main:app --host 0.0.0.0 --port 80

# Or in docker-compose.yml, change ports to:
#   ports:
#     - "80:8000"
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check |
| `POST` | `/api/process?max_pages=N` | Upload PDFs (multipart form, field: `files`). Optional `max_pages` query param limits pages processed per PDF |
| `GET` | `/api/progress/{job_id}` | SSE stream of progress updates |
| `GET` | `/api/jobs/{job_id}` | Get job status (JSON) |
| `GET` | `/api/download/{job_id}` | Download generated Excel file |

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OCR_BATCH_SIZE` | `32` | Batch size for GPU OCR processing |

## Tech Stack

- **Backend:** FastAPI, Surya-OCR, PyMuPDF, pdfplumber, openpyxl
- **Frontend:** React, Vite
- **Container:** Docker, multi-stage build
