import os
import time
import uuid
import logging
import traceback
import multiprocessing
import zipfile
import shutil
import queue

from backend import state

# -- Top Level isolated function (Runs in separate OS process) --
def process_pdf_isolated(task, progress_queue):
    """
    This function runs in a totally isolated Python process spawned by ProcessPoolExecutor.
    It has its own memory space and therefore its own initialized PyTorch/CUDA context.
    It communicates with the Streamlit main process ONLY by pushing messages to the `progress_queue`.
    """
    job_id = task["job_id"]
    filename = task["filename"]
    pdf_path = task["pdf_path"]
    
    # We must import these locally so the child process loads the ML models into its own isolated VRAM
    from backend.ocr_engine import extract_pdf
    from backend.data_parser import parse_raw_text
    from backend.excel_builder import build_excel
    
    def emit(level, msg, progress=None, total=None, stage=None):
        progress_queue.put({
            "job_id": job_id,
            "filename": filename,
            "level": level,
            "msg": msg,
            "progress": progress,
            "total": total,
            "stage": stage
        })
        
    emit("info", f"--- Started processing {filename} ---", progress=0, total=0, stage="Starting extraction...")
    
    try:
        # Check if cancelled before starting
        if state.cancel_all_requested:
            emit("warning", f"[{filename}] Cancelled before extraction started.", stage="Aborted")
            return
            
        # Progress callback specifically for this isolated OCR engine
        def progress_cb(current, total, stage_name):
            if state.cancel_all_requested:
                raise Exception("SYSTEM_ABORT")
            emit("info", f"[{filename}] {stage_name} ({current}/{total})", progress=current, total=total, stage=stage_name)
            
        # 1. OCR Extraction (Bottleneck - fully isolated per CPU core / GPU stream)
        raw_lines = extract_pdf(pdf_path, progress_callback=progress_cb, job_id=job_id)
        
        if not raw_lines:
            emit("warning", f"No valid voter data extracted from {filename}.", stage="Finished (No Data)")
            return
            
        if state.cancel_all_requested: raise Exception("SYSTEM_ABORT")
            
        emit("info", f"[{filename}] Stage 2: Parsing {len(raw_lines)} lines of text...", stage="Parsing extracted text...")
        
        # 2. Parse Text
        results = parse_raw_text(raw_lines)
        
        if state.cancel_all_requested: raise Exception("SYSTEM_ABORT")
        
        emit("info", f"[{filename}] Stage 3: Formatting {len(results)} records into Excel...", stage="Building Excel file...")
        
        # 3. Build Excel
        os.makedirs("output", exist_ok=True)
        output_filename = f"output_{job_id}_{filename}.xlsx"
        output_path = os.path.join("output", output_filename)
        build_excel(results, output_path)
        
        emit("info", f"[{filename}] Stage 4: Bundling images into ZIP...", stage="Zipping voter images...")
        
        # 4. Build ZIP file of crop images
        zip_filename = f"output_{job_id}_{filename}.zip"
        zip_path = os.path.join("output", zip_filename)
        crop_dir = os.path.join("output", f"{job_id}_crops")
        
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for row in results:
                box_id = row.get("_box_id")
                sr_no = row.get("sr.no", "").strip()
                if box_id and sr_no:
                    img_path = os.path.join(crop_dir, f"{box_id}.jpg")
                    if os.path.exists(img_path):
                        zf.write(img_path, arcname=f"{sr_no}.jpg")
                        
        shutil.rmtree(crop_dir, ignore_errors=True)
        
        emit("info", f"--- Successfully finished {filename} -> {output_filename} and {zip_filename} ---", stage="Completed")
    except Exception as e:
        if str(e) == "SYSTEM_ABORT":
            emit("warning", f"[{filename}] Processing aborted by user.", stage="Aborted")
        else:
            err_trace = traceback.format_exc()
            emit("error", f"Error processing {filename}: {e}\n{err_trace}", stage="Failed")
    finally:
        # Final cleanup
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except:
                pass
        
        # Let Streamlit know this worker finished
        progress_queue.put({"job_id": job_id, "done": True})


# -- Main Thread Loop (Runs in Streamlit App background to coordinate the OS processes) --
def background_worker_loop():
    """
    Infinite loop running in a single background thread attached to Streamlit.
    Watches state.task_queue, dispatches them to the ProcessPool, and constantly
    drains the `state.master_mp_queue` to keep Streamlit's UI updated.
    """
    logger = logging.getLogger("worker_manager")
    logger.setLevel(logging.INFO)
    
    logger.info("Background Process Manager initialized.")
    os.makedirs("output", exist_ok=True)
    
    # Needs to be "spawn" for PyTorch CUDA safety
    ctx = multiprocessing.get_context('spawn')
    state.master_mp_queue = ctx.Queue()
    
    import concurrent.futures
    # Allow exactly 3 PDFs to burst the hardware simultaneously
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=3, mp_context=ctx)

    def drain_queue():
        """Helper to instantly process all messages waiting from child processes."""
        if state.master_mp_queue is None: return
        while not state.master_mp_queue.empty():
            try:
                msg = state.master_mp_queue.get_nowait()
                job_id = msg.get("job_id")
                if not job_id: continue
                
                if msg.get("done"):
                    # Process completed natively
                    if job_id in state.active_jobs:
                        del state.active_jobs[job_id]
                    continue
                
                # Update UI state dictionary
                if job_id not in state.active_jobs:
                    state.active_jobs[job_id] = {
                        "filename": msg.get("filename", "Unknown"),
                        "progress": 0,
                        "total": 0,
                        "stage": "Initializing..."
                    }
                    
                if msg.get("progress") is not None:
                    state.active_jobs[job_id]["progress"] = msg["progress"]
                if msg.get("total") is not None:
                    state.active_jobs[job_id]["total"] = msg["total"]
                if msg.get("stage") is not None:
                    state.active_jobs[job_id]["stage"] = msg["stage"]
                    
                # Store the log text
                log_text = msg.get("msg")
                if log_text:
                    lvl = msg.get("level", "info")
                    prefix = "[ERROR]" if lvl == "error" else "[INFO]"
                    
                    state.logs.append(f"{time.strftime('%H:%M:%S')} {prefix} - {log_text}")
                    if len(state.logs) > 1000:
                        state.logs.pop(0)
                        
            except queue.Empty:
                break

    while True:
        # Handle global cancellation completely wiping the worker system
        if state.cancel_all_requested:
            logger.warning("Global cancellation triggered! Shutting down pool and purging queue...")
            
            # Instantly purge the queue
            while not state.task_queue.empty():
                try: state.task_queue.get_nowait()
                except: pass
                
            # Abandon running tasks and recreate the pool
            pool.shutdown(wait=False, cancel_futures=True)
            pool = concurrent.futures.ProcessPoolExecutor(max_workers=3, mp_context=ctx)
            
            # Wipe local dict
            state.active_jobs = {}
            state.cancel_all_requested = False
            state.logs.append(f"{time.strftime('%H:%M:%S')} [WARNING] - ALL TASKS ABORTED AND QUEUE CLEARED.")
            
        # Drain UI updates constantly
        drain_queue()
        
        try:
            # Check for new PDFs in Streamlit's task queue without blocking forever
            task = state.task_queue.get(timeout=0.5)
            
            # Submitting to pool doesn't block; it immediately queues it into the 3-OS-process pool
            try:
                pool.submit(process_pdf_isolated, task, state.master_mp_queue)
                state.logs.append(f"{time.strftime('%H:%M:%S')} [INFO] - Successfully dispatched {task['filename']} to active Worker Pool.")
            except Exception as pool_err:
                state.logs.append(f"{time.strftime('%H:%M:%S')} [ERROR] - ProcessPool Dead: {pool_err}")
                # Try to recycle task
                state.task_queue.put(task)
            
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Worker Loop Warning: {e}")
            time.sleep(1)
