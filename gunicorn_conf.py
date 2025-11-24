import multiprocessing
import os

CPU_COUNT = int(os.environ.get("CPU_COUNT", multiprocessing.cpu_count()))
DEFAULT_WORKERS = max(1, (CPU_COUNT * 2) + 1)
_requested_workers = int(os.environ.get("GUNICORN_WORKERS", DEFAULT_WORKERS))
MAX_WORKERS_CAP = int(os.environ.get("MAX_WORKERS_CAP", 8))
workers = min(_requested_workers, MAX_WORKERS_CAP)
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("GUNICORN_THREADS", os.environ.get("GUNICORN_THREAD_COUNT", 2)))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 100))
bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
reload = os.environ.get("GUNICORN_RELOAD", "false").lower() in ("1", "true", "yes")
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = os.environ.get("GUNICORN_ACCESS_FORMAT",
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"')
limit_request_line = int(os.environ.get("GUNICORN_LIMIT_REQUEST_LINE", 4094))
limit_request_fields = int(os.environ.get("GUNICORN_LIMIT_REQUEST_FIELDS", 100))
limit_request_field_size = int(os.environ.get("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190))
preload_app = os.environ.get("GUNICORN_PRELOAD", "false").lower() in ("1", "true", "yes")
daemon = False
backlog = int(os.environ.get("GUNICORN_BACKLOG", 2048))
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
