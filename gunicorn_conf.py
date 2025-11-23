# gunicorn_conf.py — production-ready Gunicorn configuration
# Loaded by: gunicorn -c gunicorn_conf.py main:app
# Designed for containerized environments (Docker, Render) with sane defaults.

import multiprocessing
import os
from datetime import timedelta

# -----------------------
# Worker & concurrency
# -----------------------
CPU_COUNT = multiprocessing.cpu_count()
# Classic formula: (2 x $num_cores) + 1 -> sensible default for sync workers
DEFAULT_WORKERS = (CPU_COUNT * 2) + 1
workers = int(os.environ.get("GUNICORN_WORKERS", DEFAULT_WORKERS))

# Choose worker class: 'gthread' is a good general-purpose choice for IO heavy apps.
# If you plan to run pure-async (uvicorn/asgi), use 'uvicorn.workers.UvicornWorker'
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")

# Threads for gthread; ignored if using sync or async worker classes
threads = int(os.environ.get("GUNICORN_THREADS", os.environ.get("GUNICORN_THREAD_COUNT", 4)))

# -----------------------
# Timeouts & graceful shutdowns
# -----------------------
# Hard timeout for workers (seconds)
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
# Graceful timeout for waiting workers to finish on restart/stop
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
# Keep connections alive (seconds)
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))

# -----------------------
# Robustness & memory leak mitigation
# -----------------------
# Restart workers after this many requests — helps mitigate memory leaks
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
# Add jitter so not all workers restart at exactly same time
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 100))

# -----------------------
# Bind / reload / logging
# -----------------------
bind = "0.0.0.0:" + os.environ.get("PORT", "10000")
# In production, reload should remain False. Set to True only in local dev.
reload = os.environ.get("GUNICORN_RELOAD", "false").lower() in ("1", "true", "yes")

# Access & error logs go to stdout/stderr so container logs capture them
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")  # "-" = stdout
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")    # "-" = stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# -----------------------
# Security & limits
# -----------------------
# Protect against very large request lines/headers
limit_request_line = int(os.environ.get("GUNICORN_LIMIT_REQUEST_LINE", 4094))
limit_request_fields = int(os.environ.get("GUNICORN_LIMIT_REQUEST_FIELDS", 100))
limit_request_field_size = int(os.environ.get("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190))

# -----------------------
# Misc / tuning
# -----------------------
# Preload the application code before the worker processes are forked.
# This reduces memory use (on platforms supporting copy-on-write) and speeds boot.
preload_app = os.environ.get("GUNICORN_PRELOAD", "true").lower() in ("1", "true", "yes")

# Enable daemonize? Not recommended in containerized systems
daemon = False

# Worker connection backlog
backlog = int(os.environ.get("GUNICORN_BACKLOG", 2048))

# File descriptor soft/hard limits (optional)
# You can raise these in entrypoint or Dockerfile if needed; leaving unset here.

# -----------------------
# Hooks (optional) — simple logging on worker start/stop
# -----------------------
def on_starting(server):
    server.log.info("Gunicorn starting — workers=%s threads=%s worker_class=%s", workers, threads, worker_class)

def when_ready(server):
    server.log.info("Gunicorn when_ready: server is ready; pid=%s", os.getpid())

def on_exit(server):
    server.log.info("Gunicorn exiting — pid=%s", os.getpid())

def worker_int(worker):
    # called when worker receives INT or QUIT signal
    worker.log.info("Worker %s received INT/QUIT signal", worker.pid)

def worker_abort(worker):
    worker.log.warning("Worker %s aborted", worker.pid)
