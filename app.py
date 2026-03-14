import os
import time
import uuid
import threading
import streamlit as st
import pandas as pd

from backend import state, worker

# Configure Streamlit page
st.set_page_config(page_title="VoterXcel - PDF to Excel", page_icon="📊", layout="wide")

# Ensure directories exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("output", exist_ok=True)

# ─── Initialization ───────────────────────────────────────────────
# Streamlit occasionally forgets global threads during heavy session state wipes.
# Ensure the background worker thread is always actively draining the queue.
if getattr(state, "worker_thread", None) is None or not state.worker_thread.is_alive():
    state.worker_thread = threading.Thread(target=worker.background_worker_loop, daemon=True)
    state.worker_thread.start()

# ─── Sidebar: Config & Environment ──────────────────────────────────
with st.sidebar:
    st.title("⚙️ Environment & Config")
    st.info(f"**CPU Threads (OMP):** {os.environ.get('OMP_NUM_THREADS', 'Not set')}")
    st.info(f"**OCR Batch Size:** {os.environ.get('OCR_BATCH_SIZE', 'Not set')}")
    st.info(f"**Worker Thread Active:** {'✅ Yes' if state.worker_thread.is_alive() else '❌ No'}")
    
    st.markdown("---")
    st.markdown("""
    ### About
        App by MrImmortal09
    """)

# ─── Main Interface ───────────────────────────────────────────────
st.title("📄 PDF to Excel: Voter Data Processor")

tabs = st.tabs(["📤 Upload PDF", "📋 Processing Logs", "📥 Download Results", "🗄️ Upload Excel to DB"])

# ─── Tab 1: Upload ───
with tabs[0]:
    st.header("Add PDFs to Job Queue")
    
    with st.form("upload_form", clear_on_submit=True):
        uploaded_files = st.file_uploader("Upload Voter Roll PDFs", type="pdf", accept_multiple_files=True)
        submitted = st.form_submit_button("Start Processing Queue")
        
        if submitted and uploaded_files:
            for uf in uploaded_files:
                job_id = str(uuid.uuid4())[:8]
                file_path = os.path.join("uploads", f"{job_id}_{uf.name}")
                
                # Save to disk
                with open(file_path, "wb") as f:
                    f.write(uf.getbuffer())
                    
                # Add to global queue
                state.task_queue.put({
                    "job_id": job_id,
                    "filename": uf.name,
                    "pdf_path": file_path
                })
            st.success(f"Added {len(uploaded_files)} file(s) to the backend queue!")

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
    q_size = state.task_queue.qsize()
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
    
    # Read files from 'output/' directory
    if os.path.exists("output"):
        output_files = [f for f in os.listdir("output") if f.endswith(".xlsx")]
    else:
        output_files = []
        
    if not output_files:
        st.info("No generated Excel files found yet. Process a PDF first!")
    else:
        # Sort by creation time (newest first)
        output_files.sort(key=lambda x: os.path.getctime(os.path.join("output", x)), reverse=True)
        
        if st.button("Delete ALL Output Data", type="primary", use_container_width=True):
            for file in os.listdir("output"):
                path = os.path.join("output", file)
                try: os.remove(path)
                except: pass
            st.success("All outputs deleted.")
            st.rerun()
            
        st.divider()
        
        for f in output_files:
            excel_path = os.path.join("output", f)
            zip_f = f.replace(".xlsx", ".zip")
            zip_path = os.path.join("output", zip_f)
            
            col1, col2, col3, col4 = st.columns([5, 2, 2, 1])
            
            size_kb = os.path.getsize(excel_path) / 1024
            
            with col1:
                st.write(f"📄 **{f}** ({size_kb:.1f} KB)")
                
            with col2:
                with open(excel_path, "rb") as file_data:
                    st.download_button(
                        label="⬇️ Excel",
                        data=file_data,
                        file_name=f,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_ex_{f}"
                    )
                    
            with col3:
                if os.path.exists(zip_path):
                    with open(zip_path, "rb") as zip_data:
                        st.download_button(
                            label="⬇️ Images (ZIP)",
                            data=zip_data,
                            file_name=zip_f,
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
