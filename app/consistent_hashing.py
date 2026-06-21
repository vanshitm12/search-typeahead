"""
consistent_hashing.py
=====================
Decides WHICH cache node owns a given prefix key. This is the concept the
assignment cares about most (section 6: "Consistent hashing must be used to
decide which cache node owns a prefix key").

THE PROBLEM WITH NAIVE HASHING (hash % N)
-----------------------------------------
Suppose you have 3 cache nodes and route a key with:  node = hash(key) % 3
Now you add a 4th node. The formula becomes hash(key) % 4. For almost EVERY
key the result changes, so nearly the entire cache is suddenly pointing at the
wrong node -> a mass cache miss ("cache stampede") that hammers your database.
Removing a node is just as bad.

THE FIX: A HASH RING
--------------------
Imagine a circle of positions 0 .. 2^32-1. We hash each *node* to a position on
the ring. To find the node for a *key*, we hash the key to a position and then
walk CLOCKWISE to the first node we meet. That node owns the key.

Why this is better: when we add a node, it only steals the slice of keys that
sit between it and the previous node on the ring. Every other key keeps its
owner. On average only K/N keys move when you add/remove a node (K keys, N
nodes), instead of nearly all of them. So the cache stays mostly warm.

VIRTUAL NODES (replicas)
------------------------
A single position per node gives uneven load -- by luck one node might own a
huge arc of the ring. So we place each physical node at MANY positions
(virtual nodes / "vnodes"), e.g. 150 each. More points -> smoother, more even
distribution. This is exactly how Amazon Dynamo and Cassandra do it.
"""

from __future__ import annotations
import hashlib
import bisect
from typing import Dict, List, Optional


class ConsistentHashRing:
    """
    A consistent-hash ring mapping keys -> node names, with virtual nodes.
    """

    def __init__(self, nodes: Optional[List[str]] = None, vnodes: int = 150) -> None:
        # How many virtual positions each physical node occupies on the ring.
        self.vnodes = vnodes
        # Sorted list of ring positions (integers). Kept sorted so we can do a
        # fast binary search ("walk clockwise") to find the owning node.
        self._ring_positions: List[int] = []
        # Maps a ring position -> the physical node name that lives there.
        self._position_to_node: Dict[int, str] = {}
        # Track which physical nodes exist.
        self._nodes: set[str] = set()

        for node in (nodes or []):
            self.add_node(node)

    # ------------------------------------------------------------------ #
    #  Hashing helper
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hash(key: str) -> int:
        """
        Map a string to a 32-bit position on the ring.
        We use md5 purely as a fast, well-distributed hash (NOT for security).
        Taking the first 8 hex chars gives us a 32-bit integer.
        """
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    # ------------------------------------------------------------------ #
    #  Membership changes
    # ------------------------------------------------------------------ #
    def add_node(self, node: str) -> None:
        """Add a physical node by scattering `vnodes` virtual points on the ring."""
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self.vnodes):
            # Each virtual node gets a distinct key like "cache-1#37".
            pos = self._hash(f"{node}#{i}")
            # Insert keeping the positions list sorted (bisect.insort).
            if pos not in self._position_to_node:
                bisect.insort(self._ring_positions, pos)
                self._position_to_node[pos] = node

    def remove_node(self, node: str) -> None:
        """Remove a physical node and all of its virtual points."""
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        for i in range(self.vnodes):
            pos = self._hash(f"{node}#{i}")
            if self._position_to_node.get(pos) == node:
                idx = bisect.bisect_left(self._ring_positions, pos)
                if idx < len(self._ring_positions) and self._ring_positions[idx] == pos:
                    self._ring_positions.pop(idx)
                del self._position_to_node[pos]

    # ------------------------------------------------------------------ #
    #  The core lookup
    # ------------------------------------------------------------------ #
    def get_node(self, key: str) -> Optional[str]:
        """
        Return the physical node responsible for `key`.

        Algorithm: hash the key to a ring position, then binary-search for the
        first ring position >= it (walking clockwise). If we fall off the end of
        the ring, wrap around to the first position (the ring is circular).
        """
        if not self._ring_positions:
            return None
        pos = self._hash(key)
        idx = bisect.bisect_right(self._ring_positions, pos)
        if idx == len(self._ring_positions):
            idx = 0  # wrap around the circle
        owner_position = self._ring_positions[idx]
        return self._position_to_node[owner_position]

    def nodes(self) -> List[str]:
        return sorted(self._nodes)
