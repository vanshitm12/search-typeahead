"""
batch_writer.py
===============
Reduces WRITE PRESSURE on the primary store. This is the 20% "batch writes"
component (section 8).

THE PROBLEM
-----------
Every search submission wants to do "count += 1" in the store. Under load that's
thousands of tiny writes per second -- each one a round trip that, in a real DB,
costs a lock, a disk write, etc. That synchronous-write-per-request pattern does
not scale.

THE FIX: BUFFER + AGGREGATE + FLUSH
-----------------------------------
1. BUFFER:    each submission just increments an in-memory counter. Returning to
              the user is instant; we do not touch the store on the request path.
2. AGGREGATE: repeated queries collapse. 10 searches for "iphone" become a single
              "iphone += 10". This is where the write reduction comes from.
3. FLUSH:     a background thread writes the whole buffer to the store either
              every `flush_interval` seconds OR once the buffer hits
              `max_batch_size` -- whichever comes first.

WRITE-REDUCTION MATH (for the perf report)
------------------------------------------
If we receive S submissions across D distinct queries in a flush window, naive
writes = S, batched writes = D. Reduction = 1 - D/S. When the same popular
queries repeat (the realistic case), D << S, so the reduction is large.

FAILURE TRADE-OFF (the doc explicitly asks you to discuss this)
---------------------------------------------------------------
The buffer lives in memory. If the process crashes before a flush, the
un-flushed increments are LOST. We accept this because (a) search COUNTS are
approximate popularity signals, not money -- losing a few is harmless, and
(b) the latency/throughput win is large. If we needed durability we'd write the
buffer to an append-only log (WAL) first and replay it on restart -- at the cost
of latency. That is the freshness/durability/latency trade-off to state aloud.
"""

from __future__ import annotations
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List


class BatchWriter:
    """
    Collects query increments and flushes them in aggregated batches.

    Thread-safe: the API thread calls record(); a background timer thread calls
    flush(). A lock guards the shared buffer.
    """

    def __init__(
        self,
        apply_batch: Callable[[Dict[str, float]], None],
        flush_interval: float = 2.0,
        max_batch_size: int = 50,
    ) -> None:
        # Callback that actually persists a {query: total_increment} batch.
        self._apply_batch = apply_batch
        self.flush_interval = flush_interval
        self.max_batch_size = max_batch_size

        # The in-memory buffer: query -> accumulated increment.
        self._buffer: Dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

        # Metrics for the performance report.
        self.total_submissions = 0     # how many record() calls (naive writes)
        self.total_flushes = 0         # how many times we hit the store
        self.total_rows_written = 0    # aggregated rows actually written

        # Background flusher.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    def record(self, query: str, amount: float = 1.0) -> None:
        """
        Called on the search request path. O(1), no store access -> fast response.
        Triggers an immediate flush if the buffer grew past max_batch_size.
        """
        query = query.strip().lower()
        if not query:
            return
        flush_now = False
        with self._lock:
            self._buffer[query] += amount
            self.total_submissions += 1
            if len(self._buffer) >= self.max_batch_size:
                flush_now = True
        if flush_now:
            self.flush()

    def flush(self) -> Dict[str, float]:
        """
        Swap out the buffer under the lock, then apply it OUTSIDE the lock so the
        request path is never blocked by a slow store write. Returns what we
        wrote (handy for logging/demo).
        """
        with self._lock:
            if not self._buffer:
                return {}
            batch = dict(self._buffer)
            self._buffer.clear()

        self._apply_batch(batch)               # persist aggregated increments
        self.total_flushes += 1
        self.total_rows_written += len(batch)
        return batch

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        """Background loop: flush every flush_interval seconds."""
        while not self._stop.wait(self.flush_interval):
            try:
                self.flush()
            except Exception as exc:  # never let the daemon die silently
                print(f"[BatchWriter] flush error: {exc}")

    def stop(self) -> None:
        """Flush remaining buffer and stop the background thread cleanly."""
        self._stop.set()
        self.flush()

    def stats(self) -> Dict[str, float]:
        naive = self.total_submissions
        batched = self.total_rows_written
        reduction = (1 - batched / naive) if naive else 0.0
        return {
            "total_submissions_naive_writes": naive,
            "total_flushes": self.total_flushes,
            "aggregated_rows_written": batched,
            "write_reduction_ratio": round(reduction, 4),
            "buffer_currently_pending": len(self._buffer),
        }
