"""
api.py
======
The FastAPI application. Wires together every component and exposes the APIs
from the assignment's section 5 table, plus a few helpers for the perf report.

REQUEST FLOWS
-------------
GET /suggest?q=<prefix>   (the hot read path)
    1. Look in the DISTRIBUTED CACHE (routed by consistent hashing).
    2. On a HIT  -> return immediately (fast).
    3. On a MISS -> read from the TRIE, store the result in the cache, return it.

POST /search   (the write path)
    1. Return {"message": "Searched"} immediately (dummy response).
    2. Hand the query to the BATCH WRITER (buffered, not written synchronously).
    -> Later, a flush aggregates increments into the STORE, rebuilds the trie,
       and invalidates affected cache entries so suggestions refresh.

GET /cache/debug?prefix=<p>   -> shows which cache node owns the prefix + hit/miss
GET /trending                  -> top recently-active queries
GET /stats                     -> latency / cache / write-reduction metrics
"""

from __future__ import annotations
import csv
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, List

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .trie import Trie
from .store import QueryStore, RECENCY_WEIGHT
from .cache import DistributedCache
from .batch_writer import BatchWriter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_FILE = os.path.join(ROOT, "data", "queries.csv")
STATIC_DIR = os.path.join(ROOT, "static")

# Cache nodes -- "logical" nodes living in-process. Consistent hashing spreads
# prefixes across them. The names appear in /cache/debug output.
CACHE_NODES = ["cache-node-1", "cache-node-2", "cache-node-3", "cache-node-4"]


class AppState:
    """Holds the live system objects. One instance for the whole process."""

    def __init__(self) -> None:
        self.store = QueryStore()
        self.trie = Trie()
        self.cache = DistributedCache(CACHE_NODES, ttl_seconds=30.0)
        # recency_weight starts at 0 -> BASIC (count-only) ranking. Flip it to
        # RECENCY_WEIGHT to enable ENHANCED (trending) ranking at runtime.
        self.recency_weight = 0.0
        # Track which prefixes a query touches so we can invalidate them on flush.
        self.batch = BatchWriter(
            apply_batch=self.apply_batch,
            flush_interval=2.0,
            max_batch_size=50,
        )
        # Simple in-memory latency log (milliseconds) for the perf report.
        self.suggest_latencies_ms: List[float] = []

    # ---------------- dataset loading + trie build ---------------- #
    def load_dataset(self) -> None:
        rows = []
        with open(DATA_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append((row["query"], float(row["count"])))
        # Sort by count descending before loading. This isn't just tidiness:
        # the trie's top-K fast path rejects a candidate cheaply when it can't
        # beat a node's current worst entry. Feeding the most popular queries
        # first means almost every later insert is rejected instantly, which
        # roughly halves build time on a 120k dataset.
        rows.sort(key=lambda r: -r[1])
        for query, count in rows:
            self.store.load(query, count)
        self.rebuild_trie()

    def rebuild_trie(self) -> None:
        """(Re)build the trie from current store scores. Called on load & flush.
        Scores are sorted descending first so the trie's top-K fast path is
        maximally effective (see load_dataset for why)."""
        new_trie = Trie()
        scored = self.store.all_scores(self.recency_weight)
        scored.sort(key=lambda qs: -qs[1])
        for q, score in scored:
            new_trie.insert(q, score)
        self.trie = new_trie

    # ---------------- batch flush callback ---------------- #
    def apply_batch(self, batch: Dict[str, float]) -> None:
        """
        Called by the BatchWriter on every flush. Applies aggregated increments
        to the store, refreshes the trie, and invalidates the cache for the
        prefixes of every changed query so stale suggestions don't linger.
        """
        if not batch:
            return
        for query, amount in batch.items():
            self.store.apply_increment(query, amount)
        # Cheapest correct approach for the demo: rebuild the trie once per flush
        # (not once per query). Then invalidate affected prefixes in the cache.
        self.rebuild_trie()
        for query in batch:
            q = query.strip().lower()
            for i in range(1, len(q) + 1):
                self.cache.invalidate(q[:i])
            self.cache.invalidate("")  # global/trending prefix


state: AppState  # populated in lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = AppState()
    print("Loading dataset (this builds the trie over 120k queries)...")
    t0 = time.time()
    state.load_dataset()
    print(f"Loaded {state.store.size():,} queries in {time.time()-t0:.2f}s")
    yield
    state.batch.stop()  # flush + stop background thread on shutdown


app = FastAPI(title="Search Typeahead System", lifespan=lifespan)


# ----------------------------- MODELS ----------------------------- #
class SearchBody(BaseModel):
    query: str


# ----------------------------- ROUTES ----------------------------- #
@app.get("/suggest")
def suggest(q: str = Query(default="")):
    """
    Fetch up to 10 prefix-matching suggestions, sorted by score descending.
    Cache-first; falls back to the trie on a miss.
    """
    t0 = time.perf_counter()
    prefix = (q or "").strip().lower()

    cached = state.cache.get(prefix)
    if cached is not None:
        results = cached
        source = "cache"
    else:
        pairs = state.trie.search(prefix)           # [(query, score), ...]
        results = [p[0] for p in pairs]             # UI only needs the strings
        state.cache.set(prefix, results)
        source = "trie"

    elapsed_ms = (time.perf_counter() - t0) * 1000
    state.suggest_latencies_ms.append(elapsed_ms)
    return {
        "prefix": prefix,
        "suggestions": results,
        "source": source,                # 'cache' or 'trie' -- visible in UI
        "latency_ms": round(elapsed_ms, 3),
    }


@app.post("/search")
def search(body: SearchBody):
    """
    Submit a search. Returns the dummy response immediately and records the
    query via the batch writer (no synchronous store write).
    """
    state.batch.record(body.query, amount=1.0)
    return {"message": "Searched"}


@app.get("/trending")
def trending():
    """Top queries by recent (decayed) activity -- powers the trending UI."""
    return {"trending": [q for q, _ in state.store.trending(top_n=10)]}


@app.get("/cache/debug")
def cache_debug(prefix: str = Query(default="")):
    """Show which cache node owns a prefix and whether it's a hit or miss."""
    return state.cache.route_debug(prefix)


@app.post("/admin/ranking")
def set_ranking(mode: str = Query(...)):
    """
    Switch ranking mode at runtime so the demo can show BOTH approaches with the
    SAME suggestion API:
      mode=basic    -> rank by all-time count only
      mode=enhanced -> blend in decayed recency (trending-aware)
    """
    if mode == "enhanced":
        state.recency_weight = RECENCY_WEIGHT
    else:
        state.recency_weight = 0.0
    state.rebuild_trie()
    # Clear cache so the new ranking is visible immediately.
    for node in state.cache.nodes.values():
        node._store.clear()
    return {"ranking_mode": mode, "recency_weight": state.recency_weight}


@app.get("/stats")
def stats():
    """Performance + behaviour metrics for the report and viva."""
    lat = sorted(state.suggest_latencies_ms)
    def pct(p):
        if not lat:
            return 0.0
        idx = min(len(lat) - 1, int(round((p / 100) * (len(lat) - 1))))
        return round(lat[idx], 3)
    return {
        "dataset_size": state.store.size(),
        "suggest_requests": len(lat),
        "latency_ms": {
            "p50": pct(50), "p95": pct(95), "p99": pct(99),
            "avg": round(sum(lat) / len(lat), 3) if lat else 0.0,
        },
        "cache": state.cache.stats(),
        "batch_writes": state.batch.stats(),
        "ranking_mode": "enhanced" if state.recency_weight else "basic",
    }


# Serve the frontend (index.html) at the root.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
