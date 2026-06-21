"""
store.py
========
The PRIMARY data store: the source of truth for every query's popularity.
In-memory (a dict) per the chosen design, but structured exactly like a real
table would be so the design maps cleanly onto SQL/Redis if scaled up.

This module also implements RECENCY-AWARE RANKING -- the 20% "trending" bonus.

RANKING: BASIC vs ENHANCED
--------------------------
Basic (60% marks):    rank purely by all-time `count`.
Enhanced (20% marks): blend all-time popularity with RECENT activity so that
                      queries trending *right now* surface higher.

THE RECENCY MODEL (exponential time decay)
------------------------------------------
Every query keeps a `recent_score`. On each search we ADD a bump to it. But a
raw running total would let a one-day spike rank forever -- which the doc
explicitly warns against ("avoid permanently over-ranking queries that were
popular only for a short period").

So before adding the bump we DECAY the existing recent_score by how much time
has passed:   recent_score *= 0.5 ** (elapsed_seconds / HALF_LIFE)
This is exponential decay with a HALF_LIFE: after one half-life, an old burst of
recency is worth half as much; after two, a quarter; and so on. A spike thus
fades on its own -- no special cleanup needed. That self-cleaning property is
the key viva point.

FINAL SCORE
-----------
    score = count + RECENCY_WEIGHT * recent_score
`count` carries long-term popularity; the decayed `recent_score` carries the
"what's hot now" signal. RECENCY_WEIGHT tunes how aggressively trends jump.
Setting RECENCY_WEIGHT = 0 recovers the pure-count basic behaviour, which is
how we demonstrate the difference between the two approaches with the same code.
"""

from __future__ import annotations
import time
import math
from typing import Dict, List, Tuple


# Tunables -- all explained above. Exposed so the demo can flip behaviour.
HALF_LIFE_SECONDS = 300.0   # recency "memory": 5 minutes for a snappy demo
RECENCY_WEIGHT = 50.0       # how strongly recent activity boosts ranking
RECENT_BUMP = 100.0         # how much one search adds to recent_score


class QueryStat:
    """One row in the store: a query and everything we know about it."""

    __slots__ = ("query", "count", "recent_score", "last_updated")

    def __init__(self, query: str, count: float = 0.0) -> None:
        self.query = query
        self.count = count                 # all-time popularity
        self.recent_score = 0.0            # decayed "trending" signal
        self.last_updated = time.time()    # when recent_score was last touched

    def _decay(self, now: float) -> None:
        """Apply exponential time decay to recent_score up to `now`."""
        elapsed = now - self.last_updated
        if elapsed > 0 and self.recent_score > 0:
            self.recent_score *= 0.5 ** (elapsed / HALF_LIFE_SECONDS)
        self.last_updated = now

    def bump(self, amount: float, now: float) -> None:
        """Record new activity: decay what's there, then add fresh recency."""
        self._decay(now)
        self.count += amount
        self.recent_score += RECENT_BUMP * amount

    def score(self, now: float, recency_weight: float) -> float:
        """Compute the current ranking score (decaying recency on the fly)."""
        # Decay a COPY's worth without mutating, so reads stay side-effect free.
        if self.recent_score > 0:
            elapsed = now - self.last_updated
            decayed = self.recent_score * (0.5 ** (elapsed / HALF_LIFE_SECONDS))
        else:
            decayed = 0.0
        return self.count + recency_weight * decayed


class QueryStore:
    """The source-of-truth table: query -> QueryStat."""

    def __init__(self) -> None:
        self._rows: Dict[str, QueryStat] = {}

    # ---------------------------- writes ---------------------------- #
    def load(self, query: str, count: float) -> None:
        """Bulk-load a query from the dataset (sets all-time count directly)."""
        query = query.strip().lower()
        if not query:
            return
        self._rows[query] = QueryStat(query, count)

    def apply_increment(self, query: str, amount: float) -> None:
        """
        Apply an aggregated increment (called by the batch writer on flush).
        Inserts the query with an initial count if it didn't exist -- satisfying
        'if the query does not exist, it should be inserted' (section 4.2).
        """
        query = query.strip().lower()
        if not query:
            return
        now = time.time()
        row = self._rows.get(query)
        if row is None:
            row = QueryStat(query, count=0.0)
            self._rows[query] = row
        row.bump(amount, now)

    # ---------------------------- reads ----------------------------- #
    def score_of(self, query: str, recency_weight: float) -> float:
        row = self._rows.get(query.strip().lower())
        return row.score(time.time(), recency_weight) if row else 0.0

    def all_scores(self, recency_weight: float) -> List[Tuple[str, float]]:
        """Return (query, score) for every row -- used to (re)build the trie."""
        now = time.time()
        return [(q, r.score(now, recency_weight)) for q, r in self._rows.items()]

    def trending(self, top_n: int = 10) -> List[Tuple[str, float]]:
        """
        Return the top queries by RECENT activity only (decayed recent_score),
        ignoring all-time count. This powers the 'Trending searches' UI section.
        """
        now = time.time()
        scored = []
        for q, r in self._rows.items():
            if r.recent_score > 0:
                elapsed = now - r.last_updated
                decayed = r.recent_score * (0.5 ** (elapsed / HALF_LIFE_SECONDS))
                if decayed > 0.01:
                    scored.append((q, decayed))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:top_n]

    def size(self) -> int:
        return len(self._rows)
