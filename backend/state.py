import threading
import queue

# Global state to share between the Streamlit UI and the background thread manager
task_queue = queue.Queue()

# Logs is a list of strings that the UI will render
logs = []

# Keep track of concurrently processing jobs: job_id -> {dict of details}
active_jobs = {}

# We need a multiprocessing Queue to let isolated OS processes send logs back to this master state
master_mp_queue = None

# Flag to signal worker that all tasks should be aborted immediately
cancel_all_requested = False

# Ensure the background thread only starts once
worker_thread = None
