"""
benchmark.py
============
Generates realistic load against the in-process app and prints the numbers that
go into the performance report (section 12): suggest latency incl. p95, cache
hit rate, and batch write-reduction. Run:  python benchmark.py
"""
import time
import random
from fastapi.testclient import TestClient
from app.api import app

random.seed(7)

# Realistic prefixes a user might type, with repeats (so the cache earns hits).
PREFIXES = ["i", "ip", "iph", "iphone", "n", "ni", "nik", "nike", "p", "py",
            "pyt", "python", "ja", "jav", "java", "sam", "sams", "dell", "hp",
            "lap", "lapt", "head", "head", "iph", "iphone", "python", "nike"]


def main():
    with TestClient(app) as c:
        import app.api as api  # grab the live state object
        st = api.state

        # ---- Warm a realistic typing session: many suggest calls ----
        N = 5000
        print(f"Firing {N} /suggest requests (with realistic repeats)...")
        t0 = time.time()
        for _ in range(N):
            p = random.choice(PREFIXES)
            c.get("/suggest", params={"q": p})
        wall = time.time() - t0
        print(f"  done in {wall:.2f}s ({N/wall:,.0f} req/s)\n")

        # ---- Fire many searches to exercise batching ----
        M = 3000
        hot = ["iphone 15", "python tutorial", "nike air", "java basics",
               "samsung tv", "dell laptop"]
        print(f"Submitting {M} /search requests (mostly repeats)...")
        for _ in range(M):
            c.post("/search", json={"query": random.choice(hot)})
        st.batch.flush()

        # ---- Report ----
        stats = c.get("/stats").json()
        print("\n================ PERFORMANCE REPORT ================")
        print(f"Dataset size           : {stats['dataset_size']:,} queries")
        print(f"Suggest requests       : {stats['suggest_requests']:,}")
        lat = stats["latency_ms"]
        print(f"Latency  p50/p95/p99   : {lat['p50']} / {lat['p95']} / {lat['p99']} ms")
        print(f"Latency  average       : {lat['avg']} ms")
        cache = stats["cache"]
        print(f"Cache hits / misses    : {cache['hits']:,} / {cache['misses']:,}")
        print(f"Cache hit rate         : {cache['hit_rate']*100:.1f}%")
        print(f"Cache per-node sizes   : {cache['per_node_size']}")
        bw = stats["batch_writes"]
        print(f"Naive writes (no batch): {bw['total_submissions_naive_writes']:,}")
        print(f"Actual batched writes  : {bw['aggregated_rows_written']:,}")
        print(f"Write reduction        : {bw['write_reduction_ratio']*100:.1f}%")
        print(f"Flushes performed      : {bw['total_flushes']:,}")
        print("====================================================")
        return stats


if __name__ == "__main__":
    main()
