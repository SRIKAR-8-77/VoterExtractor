import os
import io
import time
import uuid
import threading
import zipfile
import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from backend import state, worker

load_dotenv()

# Configure Streamlit page
st.set_page_config(page_title="VoterXcel - PDF to Excel", page_icon="📊", layout="wide")

# Ensure directories exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs("server_uploads", exist_ok=True)


def _download_name_from_output_file(output_file: str, extension: str) -> str:
    """Map internal output filename to user-facing download name."""
    base_name = output_file
    if output_file.startswith("output_"):
        parts = output_file.split("_", 2)
        if len(parts) == 3:
            base_name = parts[2]

    stem, _ = os.path.splitext(base_name)
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    return f"{stem}{extension}"


def _build_batch_zip_bundle(batch_id: str) -> bytes | None:
    """Create an in-memory ZIP of a specific processed batch directory."""
    batch_dir = os.path.join("output", batch_id)
    if not os.path.isdir(batch_dir):
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for root, _, files in os.walk(batch_dir):
            for file in files:
                if file == ".batch_name":
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, batch_dir)
                bundle.write(file_path, arcname=arcname)
    return buffer.getvalue()


def _build_bulk_zip_excel_bundle() -> tuple[bytes | None, int, int]:
    """Create an in-memory ZIP containing all ZIP and Excel files in output/."""
    source_dirs = ["output"]
    entries: list[tuple[str, str]] = []

    for base_dir in source_dirs:
        if not os.path.isdir(base_dir):
            continue

        for name in os.listdir(base_dir):
            if not name.lower().endswith((".zip", ".xlsx")):
                continue
            file_path = os.path.join(base_dir, name)
            if os.path.isfile(file_path):
                entries.append((file_path, base_dir))

    if not entries:
        return None, 0, 0

    buffer = io.BytesIO()
    zip_count = 0
    excel_count = 0

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file_path, base_dir in sorted(entries, key=lambda item: os.path.getctime(item[0]), reverse=True):
            file_name = os.path.basename(file_path)
            archive_name = os.path.join(base_dir, file_name)
            bundle.write(file_path, arcname=archive_name)
            if file_name.lower().endswith(".zip"):
                zip_count += 1
            elif file_name.lower().endswith(".xlsx"):
                excel_count += 1

    return buffer.getvalue(), zip_count, excel_count

def _detect_and_rename_file(file_path: str, file_name: str) -> tuple[str, str]:
    """If file has missing/garbage extension, analyze its bytes to fix it."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
            new_name = file_name
            if header.startswith(b"%PDF") and not file_name.lower().endswith(".pdf"):
                new_name = f"{file_name}.pdf"
            elif (header.startswith(b"PK\x03\x04") or header.startswith(b"PK\x05\x06")) and not file_name.lower().endswith(".zip"):
                new_name = f"{file_name}.zip"
            elif header.startswith(b"Rar!\x1a\x07") and not file_name.lower().endswith(".rar"):
                new_name = f"{file_name}.rar"
            
            if new_name != file_name:
                dirname = os.path.dirname(file_path)
                new_path = os.path.join(dirname, new_name)
                import shutil
                shutil.move(file_path, new_path)
                return new_path, new_name
    except Exception:
        pass
    return file_path, file_name

def _process_local_file_to_queue(file_path: str, file_name: str) -> None:
    import shutil
    # Pre-process via signature detection to fix Google Drive missing extensions
    file_path, file_name = _detect_and_rename_file(file_path, file_name)

    if file_name.lower().endswith((".zip", ".rar")):
        batch_id = str(uuid.uuid4())[:8]
        batch_dir = os.path.join("uploads", "batches", batch_id)
        os.makedirs(batch_dir, exist_ok=True)
        
        archive_path = os.path.join(batch_dir, file_name)
        shutil.move(file_path, archive_path)
        
        if file_name.lower().endswith(".zip"):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(batch_dir)
        elif file_name.lower().endswith(".rar"):
            try:
                import patoolib
                patoolib.extract_archive(archive_path, outdir=batch_dir, verbosity=-1)
            except Exception as e:
                st.error(f"Failed to extract {file_name}: {e}")
                return
                
        pdf_count = 0
        for root, _, files in os.walk(batch_dir):
            for pf in files:
                if pf.lower().endswith(".pdf") and "__MACOSX" not in root and not pf.startswith("._"):
                    pdf_path = os.path.join(root, pf)
                    rel_path = os.path.relpath(pdf_path, batch_dir)
                    rel_dir = os.path.dirname(rel_path)
                    
                    job_id = str(uuid.uuid4())[:8]
                    state.enqueue_task({
                        "job_id": job_id,
                        "filename": pf,
                        "pdf_path": pdf_path,
                        "batch_id": batch_id,
                        "batch_name": file_name,
                        "rel_dir": rel_dir
                    })
                    pdf_count += 1
                    
        out_batch_dir = os.path.join("output", batch_id)
        os.makedirs(out_batch_dir, exist_ok=True)
        with open(os.path.join(out_batch_dir, ".batch_name"), "w") as f:
            f.write(file_name)
            
        state.batches[batch_id] = {
            "name": file_name,
            "total": pdf_count,
            "completed": 0
        }
            
        st.success(f"Added {pdf_count} PDF(s) from {file_name} to the backend queue!")
    else:
        job_id = str(uuid.uuid4())[:8]
        dest_path = os.path.join("uploads", f"{job_id}_{file_name}")
        shutil.move(file_path, dest_path)
        
        state.enqueue_task({
            "job_id": job_id,
            "filename": file_name,
            "pdf_path": dest_path
        })
        st.success(f"Added {file_name} to the backend queue!")

# ─── Initialization ───────────────────────────────────────────────
# Streamlit occasionally forgets global threads during heavy session state wipes.
# Ensure the background worker thread is always actively draining the queue.
if getattr(state, "worker_thread", None) is None or not state.worker_thread.is_alive():
    from streamlit.runtime.scriptrunner import add_script_run_ctx
    state.worker_thread = threading.Thread(target=worker.background_worker_loop, daemon=True)
    add_script_run_ctx(state.worker_thread)
    state.worker_thread.start()

# ─── Sidebar: Config & Environment ──────────────────────────────────
with st.sidebar:
    st.title("⚙️ Environment & Config")
    st.info(f"**CPU Threads (OMP):** {os.environ.get('OMP_NUM_THREADS', 'Not set')}")
    st.info(f"**PDF Workers:** {os.environ.get('PDF_PROCESSING_WORKERS', 'Not set')}")
    st.info(f"**OCR Batch Size:** {os.environ.get('OCR_BATCH_SIZE', 'Not set')}")
    st.info(f"**Worker Thread Active:** {'✅ Yes' if state.worker_thread.is_alive() else '❌ No'}")
    
    st.markdown("---")
    st.markdown("""
    ### About
        App by MrImmortal09
    """)

# ─── Main Interface ───────────────────────────────────────────────
st.title("📄 PDF to Excel: Voter Data Processor")

tabs = st.tabs(["📤 Upload PDF", "📋 Processing Logs", "📥 Download Results"])

# ─── Tab 1: Upload ───
with tabs[0]:
    st.header("Add PDFs, ZIPs, or RARs to Job Queue")
    
    with st.form("upload_form", clear_on_submit=True):
        uploaded_files = st.file_uploader("Upload Voter Roll PDFs or ZIP/RAR archives", type=None, accept_multiple_files=True)
        submitted = st.form_submit_button("Start Processing Queue")
        
        if submitted and uploaded_files:
            for uf in uploaded_files:
                if not uf.name.lower().endswith((".pdf", ".zip", ".rar")):
                    st.warning(f"Skipping {uf.name} (only PDF, ZIP, and RAR are supported).")
                    continue

                if uf.name.lower().endswith((".zip", ".rar")):
                    batch_id = str(uuid.uuid4())[:8]
                    batch_dir = os.path.join("uploads", "batches", batch_id)
                    os.makedirs(batch_dir, exist_ok=True)
                    
                    archive_path = os.path.join(batch_dir, uf.name)
                    with open(archive_path, "wb") as f:
                        f.write(uf.getbuffer())
                        
                    if uf.name.lower().endswith(".zip"):
                        with zipfile.ZipFile(archive_path, 'r') as zf:
                            zf.extractall(batch_dir)
                    elif uf.name.lower().endswith(".rar"):
                        try:
                            import patoolib
                            patoolib.extract_archive(archive_path, outdir=batch_dir, verbosity=-1)
                        except ImportError:
                            st.error("Please install patool to support RAR files: `pip install patool`")
                            continue
                        except Exception as e:
                            st.error(f"Failed to extract RAR (ensure 'unrar' or '7z' is installed on your OS): {e}")
                            continue
                        
                    pdf_count = 0
                    for root, _, files in os.walk(batch_dir):
                        for file in files:
                            if file.lower().endswith(".pdf") and "__MACOSX" not in root and not file.startswith("._"):
                                pdf_path = os.path.join(root, file)
                                rel_path = os.path.relpath(pdf_path, batch_dir)
                                rel_dir = os.path.dirname(rel_path)
                                
                                job_id = str(uuid.uuid4())[:8]
                                state.enqueue_task({
                                    "job_id": job_id,
                                    "filename": file,
                                    "pdf_path": pdf_path,
                                    "batch_id": batch_id,
                                    "batch_name": uf.name,
                                    "rel_dir": rel_dir
                                })
                                pdf_count += 1
                                
                    out_batch_dir = os.path.join("output", batch_id)
                    os.makedirs(out_batch_dir, exist_ok=True)
                    with open(os.path.join(out_batch_dir, ".batch_name"), "w") as f:
                        f.write(uf.name)
                        
                    state.batches[batch_id] = {
                        "name": uf.name,
                        "total": pdf_count,
                        "completed": 0
                    }
                        
                    st.success(f"Added {pdf_count} PDF(s) from {uf.name} to the backend queue!")
                else:
                    job_id = str(uuid.uuid4())[:8]
                    file_path = os.path.join("uploads", f"{job_id}_{uf.name}")
                    
                    # Save to disk
                    with open(file_path, "wb") as f:
                        f.write(uf.getbuffer())
                        
                    # Add to global queue
                    state.enqueue_task({
                        "job_id": job_id,
                        "filename": uf.name,
                        "pdf_path": file_path
                    })
                    st.success(f"Added {uf.name} to the backend queue!")

    st.divider()
    st.subheader("📁 Option 2: Huge File Bypass (Terminal Upload)")
    st.write("Open your terminal, navigate to the folder with your files, and paste this command to securely upload them straight to the queue:")
    
    # Dynamically fetch server IP
    try:
        import urllib.request
        server_ip = urllib.request.urlopen("https://api.ipify.org", timeout=2).read().decode('utf-8').strip()
    except Exception:
        server_ip = os.environ.get("SERVER_PUBLIC_IP", "74.48.140.178")
        
    ssh_port = os.environ.get("SSH_PORT", "27351")
    ssh_conn = os.environ.get("SSH_CONNECTION")
    if ssh_conn:
        try:
            ssh_port = ssh_conn.split()[-1]
        except Exception:
            pass
            
    ssh_user = os.environ.get("SSH_USER", "root")
    
    upload_cmd = f"rsync -avP -e 'ssh -p {ssh_port}' ./*.{{pdf,zip,rar}} {ssh_user}@{server_ip}:/root/VoterExtractor/server_uploads/"
    st.code(upload_cmd, language="bash")
    
    if st.button("Scan 'server_uploads' & Process Files Now", type="primary"):
        import shutil
        found = False
        for file_name in os.listdir("server_uploads"):
            if not file_name.lower().endswith((".pdf", ".zip", ".rar")):
                continue
            found = True
            file_path = os.path.join("server_uploads", file_name)
            _process_local_file_to_queue(file_path, file_name)
                
        if not found:
            st.info("⚠️ Ensure the files are placed directly in the `server_uploads` directory, and that they end in .zip, .rar, or .pdf.")

    st.divider()
    st.subheader("🌐 Option 3: Download from Cloud URL (File or Folder)")
    st.write("Provide a public link to bypass browser uploads entirely. Good for Google Drive files OR folders.")
    
    with st.form("url_upload_form", clear_on_submit=True):
        cloud_url = st.text_input("Public Download URL (Google Drive File/Folder link):", placeholder="https://drive.google.com/drive/folders/...")
        fetch_btn = st.form_submit_button("Fetch & Process")
        
        if fetch_btn and cloud_url.strip():
            with st.spinner("Downloading directly to server... This may take a while for large folders."):
                try:
                    import gdown
                    import shutil
                    temp_dir = "server_uploads"
                    os.makedirs(temp_dir, exist_ok=True)
                    
                    is_folder = "folder" in cloud_url or "drive/folders" in cloud_url
                    
                    if is_folder:
                        # gdown.download_folder creates a target subdirectory, we need a unique one
                        folder_dest = os.path.join(temp_dir, f"gdrive_folder_{uuid.uuid4().hex[:8]}")
                        
                        # Fix: explicitly provide the true download target for folder handling
                        if not os.path.exists(folder_dest):
                            os.makedirs(folder_dest)
                            
                        # Set current working directory locally right before calling to force it into folder_dest cleanly just in case
                        old_cwd = os.getcwd()
                        try:
                            os.chdir(folder_dest)
                            # gdown download returns paths relative to current dir when doing folders
                            downloaded = gdown.download_folder(cloud_url.strip(), quiet=False, use_cookies=False)
                        finally:
                            os.chdir(old_cwd)
                        
                        if downloaded and os.path.exists(folder_dest):
                            # Recursively find all supported files inside the downloaded folder
                            found_any = False
                            for root, _, files in os.walk(folder_dest):
                                for f_name in files:
                                    if f_name.lower().endswith((".pdf", ".zip", ".rar")) and not f_name.startswith("._"):
                                        found_any = True
                                        full_path = os.path.join(root, f_name)
                                        _process_local_file_to_queue(full_path, f_name)
                            if not found_any:
                                st.warning("Folder downloaded, but no valid .pdf, .zip, or .rar files were found inside.")
                            shutil.rmtree(folder_dest, ignore_errors=True)
                        else:
                            st.error("Failed to download Google Drive folder. Ensure the link is public ('Anyone with link').")
                            
                    else:
                        # Standard single file download
                        downloaded_path = gdown.download(cloud_url.strip(), output=f"{temp_dir}/", quiet=False, fuzzy=True)
                        
                        if not downloaded_path:
                            # Fallback to direct requests if gdown fails/skips
                            import requests
                            from urllib.parse import urlparse
                            parsed = urlparse(cloud_url.strip())
                            fname = os.path.basename(parsed.path) or f"downloaded_{uuid.uuid4().hex[:8]}.zip"
                            downloaded_path = os.path.join(temp_dir, fname)
                            try:
                                with requests.get(cloud_url.strip(), stream=True) as r:
                                    r.raise_for_status()
                                    with open(downloaded_path, 'wb') as f:
                                        for chunk in r.iter_content(chunk_size=8192):
                                            f.write(chunk)
                            except Exception as req_e:
                                st.error(f"Fallback request failed: {req_e}")
                                downloaded_path = None
                                        
                        if downloaded_path and os.path.exists(downloaded_path):
                            _process_local_file_to_queue(downloaded_path, os.path.basename(downloaded_path))
                        else:
                            st.error("Failed to download file from URL.")
                except Exception as e:
                    st.error(f"Download Error: {e}")

# ─── Tab 2: Logs & Status ───
with tabs[1]:
    st.header("Live Processing Status")
    
    # Auto-refresh helper using a frontend script injected via st.components (or just a manual refresh button)
    col1, col2 = st.columns([8, 2])
    with col1:
        st.write("This status pulls directly from the background thread running independently.")
    with col2:
        if st.button("🔄 Refresh Logs"):
            pass # Streamlit automatically reruns the script on button press
    
    # Active Jobs Section
    st.subheader("Active Jobs (Multiprocessing)")
    if not state.active_jobs:
        st.info("Zzz... Worker Pool is idle. Queue is empty.")
    else:
        # Loop over every actively processing PDF and create an isolated progress card
        st.session_state["active_jobs_snapshot"] = dict(state.active_jobs)
        for job_id, job in st.session_state["active_jobs_snapshot"].items():
            st.warning(f"🔄 **Processing:** {job['filename']}")
            st.text(f"Status: {job['stage']}")
            
            # Explicit per-PDF Logs
            if job["total"] > 0:
                current = job["progress"]
                total = job["total"]
                
                st.write(f"**Progress:** Processed {current} / {total} pages")
                
                progress_pct = current / total
                # Clamp progress to 0.0 - 1.0
                progress_pct = max(0.0, min(1.0, progress_pct))
                st.progress(progress_pct)
            
            st.divider()
    
    # Queue Length & Controls
    q_size = state.get_pending_task_count()
    st.write(f"**Voter PDFs in Queue:** {q_size}")
    
    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("🔄 Refresh Logs", key="refresh_bottom"):
            pass
    with col_b:
        if st.button("Cancel All & Clear Jobs", type="primary"):
            state.cancel_all_requested = True
            st.success("Sent Abort Signal to Worker Pool.")
            st.rerun()

    # Server Logs
    st.subheader("Server Logs")
    # Show last 20 logs reversed (newest first)
    log_text = "\n".join(reversed(state.logs[-20:]))
    if not log_text:
        log_text = "No logs yet..."
    st.code(log_text, language="bash")

# ─── Tab 3: Download & Manage ───
with tabs[2]:
    st.header("Completed Excel Files")

    bundle_bytes, bundle_zip_count, bundle_excel_count = _build_bulk_zip_excel_bundle()
    bundle_name = f"voterxcel_bundle_{time.strftime('%Y%m%d_%H%M%S')}.zip"

    if bundle_bytes:
        st.download_button(
            label=f"⬇️ Download ALL ZIPs + Excel ({bundle_zip_count} ZIP, {bundle_excel_count} Excel)",
            data=bundle_bytes,
            file_name=bundle_name,
            mime="application/zip",
            use_container_width=True,
            key="dl_bundle_zip_pdf",
        )
    else:
        st.info("No ZIP/Excel files available yet for a combined download.")

    st.divider()
    
    # Read files from 'output/' directory
    if os.path.exists("output"):
        output_items = os.listdir("output")
        output_files = [f for f in output_items if f.endswith(".xlsx") and os.path.isfile(os.path.join("output", f))]
        output_batches = [d for d in output_items if os.path.isdir(os.path.join("output", d)) and os.path.exists(os.path.join("output", d, ".batch_name"))]
    else:
        output_files = []
        output_batches = []
        
    if not output_files and not output_batches:
        st.info("No generated files found yet. Process a PDF first!")
    else:
        # Sort by creation time (newest first)
        output_files.sort(key=lambda x: os.path.getctime(os.path.join("output", x)), reverse=True)
        
        if st.button("Delete ALL Output Data", type="primary", use_container_width=True):
            for file in os.listdir("output"):
                path = os.path.join("output", file)
                try: 
                    if os.path.isdir(path):
                        import shutil
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except: pass
            st.success("All outputs deleted.")
            st.rerun()
            
        st.divider()
        
        if output_batches:
            active_batches = []
            completed_batches = []
            
            for b in output_batches:
                if hasattr(state, "batches") and b in state.batches and state.batches[b]["completed"] < state.batches[b]["total"]:
                    active_batches.append(b)
                else:
                    completed_batches.append(b)
                    
            if active_batches:
                st.subheader("⚙️ Processing Archive Batches...")
                for b in active_batches:
                    batch_dir = os.path.join("output", b)
                    try:
                        with open(os.path.join(batch_dir, ".batch_name"), "r") as f:
                            b_name = f.read().strip()
                    except:
                        b_name = b
                        
                    b_info = state.batches[b]
                    completedCount = b_info["completed"]
                    totalCount = b_info["total"]
                    pct = completedCount / max(1, totalCount)
                    
                    st.write(f"🗂️ **{b_name}** - {completedCount} / {totalCount} PDFs Completed...")
                    st.progress(pct)
                st.divider()
                
            if completed_batches:
                st.subheader("📦 Processed Archive Batches")
                for b in completed_batches:
                    batch_dir = os.path.join("output", b)
                    try:
                        with open(os.path.join(batch_dir, ".batch_name"), "r") as f:
                            b_name = f.read().strip()
                    except:
                        b_name = b
                        
                    col_b1, col_b2, col_b3 = st.columns([5, 3, 1])
                    with col_b1:
                        st.write(f"🗂️ **{b_name}** (Completed)")
                    with col_b2:
                        batch_bytes = _build_batch_zip_bundle(b)
                        if batch_bytes:
                            dl_name = "processed_" + os.path.splitext(b_name)[0] + ".zip"
                            st.download_button(
                                label=f"⬇️ Download {b_name} as ZIP",
                                data=batch_bytes,
                                file_name=dl_name,
                                mime="application/zip",
                                key=f"dl_batch_{b}"
                            )
                    with col_b3:
                        if st.button("🗑️", key=f"del_batch_{b}"):
                            import shutil
                            shutil.rmtree(batch_dir, ignore_errors=True)
                            st.rerun()
                st.divider()
            
        if output_files:
            st.subheader("📄 Standalone Processed Files")
        for f in output_files:
            excel_path = os.path.join("output", f)
            zip_f = f.replace(".xlsx", ".zip")
            zip_path = os.path.join("output", zip_f)
            download_excel_name = _download_name_from_output_file(f, ".xlsx")
            download_zip_name = _download_name_from_output_file(f, ".zip")
            
            col1, col2, col3, col4 = st.columns([5, 2, 2, 1])
            
            size_kb = os.path.getsize(excel_path) / 1024
            
            with col1:
                st.write(f"📄 **{f}** ({size_kb:.1f} KB)")
                
            with col2:
                with open(excel_path, "rb") as file_data:
                    st.download_button(
                        label="⬇️ Excel",
                        data=file_data,
                        file_name=download_excel_name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_ex_{f}"
                    )
                    
            with col3:
                if os.path.exists(zip_path):
                    with open(zip_path, "rb") as zip_data:
                        st.download_button(
                            label="⬇️ Images (ZIP)",
                            data=zip_data,
                            file_name=download_zip_name,
                            mime="application/zip",
                            key=f"dl_zip_{f}"
                        )
                else:
                    st.write("No images")
                    
            with col4:
                # Need a unique delete key
                if st.button("🗑️", key=f"del_{f}"):
                    try:
                        os.remove(excel_path)
                        if os.path.exists(zip_path):
                            os.remove(zip_path)
                    except:
                        pass
                    st.rerun()
