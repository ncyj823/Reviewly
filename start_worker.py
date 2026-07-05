import os
import sys

sys.path.insert(0, "/app/github_mcp")
sys.path.insert(0, "/app/review_pipeline")
sys.path.insert(0, "/app/webhook_service")

from dotenv import load_dotenv
load_dotenv("/app/github_mcp/.env")

from rq import Worker, Queue
from redis import Redis

redis_host = os.environ.get("REDIS_HOST", "localhost")
conn = Redis(host=redis_host, port=6379)
q = Queue("reviews", connection=conn)

print(f"[worker] Connecting to Redis at {redis_host}:6379")
print("[worker] Starting worker, listening on 'reviews' queue...")

w = Worker([q], connection=conn)
w.work()