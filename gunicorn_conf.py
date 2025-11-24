# Gunicorn configuration for BrittonMethod-auto

bind = "0.0.0.0:8000"
workers = 4
threads = 2
timeout = 120
accesslog = "-"
errorlog = "-"
capture_output = True
graceful_timeout = 120
