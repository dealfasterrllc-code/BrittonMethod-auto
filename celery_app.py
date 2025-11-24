from celery import Celery

# Use Redis as broker and backend (or configure a DB backend if you prefer)
app = Celery(
    "britton_method",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

# Example task
@app.task
def add(x, y):
    return x + y

# You can put your real “deal-closing” job here
# def analyze_and_close_deal(deal_id): ...
