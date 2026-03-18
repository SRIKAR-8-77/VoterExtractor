import threading
import queue

# Global state to share between the Streamlit UI and the background thread manager
task_queue = queue.Queue()
queue_lock = threading.Lock()
pending_task_count = 0


def enqueue_task(task: dict) -> None:
	"""Add one task and atomically increment pending count."""
	global pending_task_count
	task_queue.put(task)
	with queue_lock:
		pending_task_count += 1


def dequeue_task(timeout: float | None = None) -> dict:
	"""Remove one task and atomically decrement pending count."""
	global pending_task_count
	task = task_queue.get(timeout=timeout)
	with queue_lock:
		pending_task_count = max(0, pending_task_count - 1)
	return task


def clear_pending_tasks() -> int:
	"""Drain all waiting tasks and reset pending count consistently."""
	global pending_task_count
	cleared = 0
	while True:
		try:
			task_queue.get_nowait()
			cleared += 1
		except queue.Empty:
			break

	with queue_lock:
		pending_task_count = max(0, pending_task_count - cleared)
		if task_queue.empty():
			pending_task_count = 0
		return pending_task_count


def get_pending_task_count() -> int:
	with queue_lock:
		return pending_task_count

# Logs is a list of strings that the UI will render
logs = []

# Keep track of concurrently processing jobs: job_id -> {dict of details}
active_jobs = {}

# Keep track of batch progress: batch_id -> {"name": str, "total": int, "completed": int}
batches = {}

# We need a multiprocessing Queue to let isolated OS processes send logs back to this master state
master_mp_queue = None

# Flag to signal worker that all tasks should be aborted immediately
cancel_all_requested = False

# Ensure the background thread only starts once
worker_thread = None
