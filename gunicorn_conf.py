# gunicorn_conf.py — production-ready Gunicorn configuration
# Loaded by: gunicorn -c gunicorn_conf.py main:app
# Designed for containerized environments (Docker, Render) with sane defaults.

import multiprocessing
import os
from datetime import timedelta

# -----------------------
# CPU / workers / concurrency
# -----------------------
# Allow override of CPU count for container environments where cpu_count() is misleading
CPU_COUNT = int(os.environ.get("CPU_COUNT", multiprocessing.cpu_count()))
# Classic formula default: (2 x num_cores) + 1
DEFAULT_WORKERS = max(1, (CPU_COUNT * 2) + 1)
# Read requested workers, fall back to formula
_requested_workers = int(os.environ.get("GUNICORN_WORKERS", DEFAULT_WORKERS))

# Cap workers so small memory hosts don't spawn too many processes
MAX_WORKERS_CAP = int(os.environ.get("MAX_WORKERS_CAP", 8))
workers = min(_requested_workers, MAX_WORKERS_CAP)

# Worker class: gthread is a good general-purpose choice for IO-heavy apps.
# For async frameworks consider 'uvicorn.workers.UvicornWorker'
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")

# Threads per worker (for gthread); keep conservative default for low-memory hosts
threads = int(os.environ.get("GUNICORN_THREADS", os.environ.get("GUNICORN_THREAD_COUNT", 2)))

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
# Prefer using $PORT injected by platform; fallback to 8000 for local dev
bind = "0.0.0.0:" + os.environ.get("PORT", "8000")

# In production, reload should remain False. Set to True only in local dev.
reload = os.environ.get("GUNICORN_RELOAD", "false").lower() in ("1", "true", "yes")

# Access & error logs go to stdout/stderr
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")  # "-" = stdout
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")    # "-" = stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Structured-ish access log format (common fields)
access_log_format = os.environ.get(
    "GUNICORN_ACCESS_FORMAT",
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
)

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
# Set to True only if host supports copy-on-write and you have sufficient memory.
preload_app = os.environ.get("GUNICORN_PRELOAD", "false").lower() in ("1", "true", "yes")

# Daemonize? Not recommended in containerized systems
daemon = False

# Worker connection backlog
backlog = int(os.environ.get("GUNICORN_BACKLOG", 2048))

# -----------------------
# Hooks — lightweight logging
# -----------------------
def on_starting(server):
    server.log.info(
        "Gunicorn starting — workers=%s threads=%s worker_class=%s cpu_count=%s max_workers_cap=%s",
        workers, threads, worker_class, CPU_COUNT, MAX_WORKERS_CAP
    )

def when_ready(server):
    server.log.info("Gunicorn ready — pid=%s", os.getpid())

def on_exit(server):
    server.log.info("Gunicorn exiting — pid=%s", os.getpid())

def worker_int(worker):
    worker.log.info("Worker %s received INT/QUIT signal", worker.pid)

def worker_abort(worker):
    worker.log.warning("Worker %s aborted", worker.pid)
