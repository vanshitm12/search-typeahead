"""
trie.py
=======
The TRIE (prefix tree) is the core read-path data structure for typeahead.

WHY A TRIE?
-----------
The naive way to find suggestions for prefix "ip" is to scan ALL queries and
keep the ones that start with "ip". That is O(N) per keystroke. With 100,000+
queries and a user typing several characters per second, that is far too slow.

A trie stores characters along a path: root -> 'i' -> 'p' -> 'h' -> 'o' ...
To find everything under "ip" we walk just 2 nodes (one per character of the
prefix), then read a PRECOMPUTED list of the top-K most popular completions
stored at that node. That makes a lookup O(L + K) where L = prefix length and
K = number of suggestions (10) -- effectively constant regardless of dataset size.

THE KEY TRICK (top-K caching at each node):
-------------------------------------------
Naively, after reaching the "ip" node we'd still have to traverse the whole
subtree to collect and sort completions. Instead, at insert/update time we keep
a small sorted "top suggestions" list AT EVERY NODE along the path. So the node
for "ip" already knows its 10 best completions. Reading is then trivial.

This is the classic latency/space trade-off: we spend extra memory and a little
extra write work to make reads almost free. That is exactly the kind of design
trade-off the assignment wants you to articulate.
"""

from __future__ import annotations
import heapq
from typing import Dict, List, Tuple


# How many suggestions we keep cached at each node. The assignment asks for 10.
TOP_K = 10


class TrieNode:
    """A single node in the trie. Represents one character position."""

    __slots__ = ("children", "top")  # __slots__ saves memory at large scale

    def __init__(self) -> None:
        # Maps a single character -> child TrieNode
        self.children: Dict[str, "TrieNode"] = {}
        # Precomputed best completions that pass THROUGH this node.
        # Stored as a list of (query, score) kept sorted by score descending.
        # "score" is the value we rank by (count, or a recency-blended score).
        self.top: List[Tuple[str, float]] = []


class Trie:
    """
    A prefix tree whose nodes cache their top-K completions for instant reads.

    Public methods:
      - insert(query, score): add/update a query with its ranking score
      - search(prefix):        return up to TOP_K completions for a prefix
    """

    def __init__(self) -> None:
        self.root = TrieNode()
        # We also keep the canonical score of every query so that when a query's
        # score changes we know its OLD value and can refresh node lists correctly.
        self._scores: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    #  WRITE PATH
    # ------------------------------------------------------------------ #
    def insert(self, query: str, score: float) -> None:
        """
        Insert a NEW query, or update the score of an existing one.

        We normalise to lowercase so "IPhone" and "iphone" collapse together --
        this directly satisfies the 'mixed-case input' requirement.
        """
        query = query.strip().lower()
        if not query:
            return

        self._scores[query] = score

        # Walk down the trie one character at a time, creating nodes as needed.
        node = self.root
        # The root node itself represents the empty prefix "", which is useful
        # for "trending" style global top lists, so we update root too.
        self._offer(node, query, score)
        for ch in query:
            if ch not in node.children:
                node.children[ch] = TrieNode()
            node = node.children[ch]
            # At every prefix boundary, offer this query to that node's top list.
            self._offer(node, query, score)

    @staticmethod
    def _offer(node: TrieNode, query: str, score: float) -> None:
        """
        Maintain a node's cached top-K list when (query, score) is offered.

        FAST PATH: if this query isn't already in the list AND the list is full
        AND the new score can't beat the current worst entry, we skip entirely.
        That avoids touching the vast majority of offers on popular deep nodes,
        which is what made the naive version slow at 120k queries.

        Otherwise we do the simple, obviously-correct thing: drop any old entry
        for this query, append, sort (TOP_K is only 10 so the sort is tiny), and
        truncate. Clarity here matters more than micro-optimisation.
        """
        top = node.top
        # Quick check: is the query already tracked here?
        existing_idx = -1
        for i, (q, _s) in enumerate(top):
            if q == query:
                existing_idx = i
                break

        if existing_idx == -1:
            # New candidate. If the list is full and we can't beat the worst,
            # there is nothing to do -- this is the hot fast path.
            if len(top) >= TOP_K and score <= top[-1][1]:
                return
            top.append((query, score))
        else:
            # Update the existing entry's score in place.
            top[existing_idx] = (query, score)

        # Re-sort the small list (<= TOP_K+1 items) and truncate.
        top.sort(key=lambda qs: (-qs[1], qs[0]))
        if len(top) > TOP_K:
            del top[TOP_K:]

    # ------------------------------------------------------------------ #
    #  READ PATH
    # ------------------------------------------------------------------ #
    def search(self, prefix: str) -> List[Tuple[str, float]]:
        """
        Return up to TOP_K (query, score) pairs that start with `prefix`,
        sorted by score descending. This is the hot path -- it must be fast.

        Handles the required edge cases:
          * empty / missing prefix -> returns global top list (root node)
          * mixed-case             -> normalised to lowercase
          * no matches             -> returns [] (caller renders gracefully)
        """
        if prefix is None:
            prefix = ""
        prefix = prefix.strip().lower()

        node = self.root
        for ch in prefix:
            if ch not in node.children:
                return []  # No query has this prefix -> graceful empty result.
            node = node.children[ch]

        # The node already holds its precomputed best completions. Done.
        return list(node.top)

    def size(self) -> int:
        """Number of distinct queries stored (handy for the perf report)."""
        return len(self._scores)
