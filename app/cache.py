"""
cache.py
========
A DISTRIBUTED cache for prefix -> suggestions, spread across several logical
cache nodes. Which node stores a given prefix is decided by the consistent-hash
ring (consistent_hashing.py).

Assignment requirements covered (section 6):
  * suggestion flow uses cache before falling back to the primary store  -> see api.py
  * cache stores suggestion results for prefixes                          -> CacheNode.get/set
  * cache supports expiry/invalidation so stale data doesn't live forever -> TTL + invalidate()
  * cache is distributed across multiple logical nodes                    -> DistributedCache
  * consistent hashing decides node ownership                             -> ring.get_node()

We also record HITS and MISSES so the performance report can show a cache hit
rate, which the non-functional requirements (section 10) ask for.
"""

from __future__ import annotations
import time
from typing import Any, Dict, List, Optional, Tuple

from .consistent_hashing import ConsistentHashRing


class CacheNode:
    """
    One logical cache node: a plain in-memory dict with per-entry expiry.

    In a real system each node would be a separate Redis/Memcached server.
    Here they are just objects in the same process -- which is perfect for a
    local demo and keeps the consistent-hashing behaviour observable.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        # key -> (value, expires_at_epoch_seconds)
        self._store: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        # Lazy expiry: we only check the TTL when the key is read.
        if time.time() >= expires_at:
            del self._store[key]   # evict the stale entry
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._store[key] = (value, time.time() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def size(self) -> int:
        return len(self._store)


class DistributedCache:
    """
    Front door to the cache. Routes every prefix key to its owning CacheNode
    using the consistent-hash ring, and tracks hit/miss statistics.
    """

    def __init__(self, node_names: List[str], ttl_seconds: float = 30.0) -> None:
        self.ttl_seconds = ttl_seconds
        self.ring = ConsistentHashRing(node_names)
        self.nodes: Dict[str, CacheNode] = {n: CacheNode(n) for n in node_names}
        # Stats for the performance report.
        self.hits = 0
        self.misses = 0

    def _route(self, prefix: str) -> CacheNode:
        """Ask the ring which node owns this prefix, return that CacheNode."""
        node_name = self.ring.get_node(prefix)
        return self.nodes[node_name]

    def get(self, prefix: str) -> Optional[Any]:
        node = self._route(prefix)
        value = node.get(prefix)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def set(self, prefix: str, value: Any) -> None:
        self._route(prefix).set(prefix, value, self.ttl_seconds)

    def invalidate(self, prefix: str) -> None:
        """
        Remove a prefix from whichever node owns it. Called when the underlying
        ranking for that prefix changes (e.g. after a batch flush updates counts),
        so the next read recomputes fresh suggestions instead of serving stale ones.
        """
        self._route(prefix).delete(prefix)

    # ------- introspection helpers for the /cache/debug endpoint ------- #
    def route_debug(self, prefix: str) -> Dict[str, Any]:
        """
        Explain where a prefix routes and whether it's currently cached.
        Backs GET /cache/debug?prefix=... (section 5 API table).
        """
        prefix = (prefix or "").strip().lower()
        node_name = self.ring.get_node(prefix)
        node = self.nodes[node_name]
        is_hit = node.get(prefix) is not None
        return {
            "prefix": prefix,
            "owner_node": node_name,
            "status": "HIT" if is_hit else "MISS",
            "all_nodes": self.ring.nodes(),
        }

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate(), 4),
            "ttl_seconds": self.ttl_seconds,
            "per_node_size": {n: c.size() for n, c in self.nodes.items()},
        }
