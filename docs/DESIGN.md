# Design Decisions & Trade-offs

This document explains the major design choices. The assignment (sections 7, 8,
11) requires you to be able to justify all of these, so each section ends with
the trade-off being made.

---

## 1. Data model: how query-count data is stored

Each query is one row in `QueryStore` (`app/store.py`) holding:

- `count` — all-time popularity (an integer that only grows).
- `recent_score` — a time-decayed signal of *recent* activity.
- `last_updated` — when `recent_score` was last touched (needed for decay).

We keep these in an in-memory dict (`query -> QueryStat`). The assignment
explicitly allows in-memory storage ("maintain query-count data reliably enough
for the assignment demo"). The structure deliberately mirrors a database row, so
moving to SQLite/Postgres/Redis later would be a mechanical change, not a
redesign.

**Trade-off:** in-memory is the fastest and simplest to run and explain, at the
cost of durability — state is lost on restart. For a demo that is the right call;
for production you would back it with a real store and load the dataset on boot.

---

## 2. Serving suggestions fast: the trie with cached top-K

A prefix lookup must be fast and must not depend on dataset size. We use a trie
(`app/trie.py`) where **every node caches its own top-10 completions**. So when
you type "iph", we walk three nodes and read a list that is already computed and
sorted — no subtree traversal at read time.

- Read cost: `O(L + K)` where `L` = prefix length, `K` = 10. Effectively constant.
- Write cost: inserting a query updates the top-K list at each node on its path.

To keep inserts cheap on a 120k dataset, the top-K maintainer has a fast path:
if a node's list is already full and the new score can't beat the current worst
entry, the offer is rejected immediately. Loading the dataset sorted by count
descending makes this fast path fire on almost every later insert, roughly
halving build time.

**Trade-off:** we spend extra memory (a 10-item list per node) and extra write
work to make reads almost free. For typeahead — where reads vastly outnumber
writes — that is the correct direction to trade.

---

## 3. Caching and consistent hashing

### Why a cache

Even a trie lookup costs something. We put a `DistributedCache` (`app/cache.py`)
in front, keyed by prefix → suggestion list. The read path checks the cache
first and only falls back to the trie on a miss, then fills the cache. In the
benchmark this yields a ~99.6% hit rate.

### Why consistent hashing (not `hash % N`)

The cache is split across 4 logical nodes. The naive way to pick a node is
`hash(prefix) % N`, but if you add or remove a node, `N` changes and almost every
key remaps at once — the entire cache goes cold and the database gets hammered
(a cache stampede).

Consistent hashing (`app/consistent_hashing.py`) places nodes on a hash ring. A
key is owned by the first node found walking clockwise from the key's position.
Adding a node only steals the slice of keys between it and its ring neighbour, so
on average only `K/N` keys move. We verified this: adding a 4th node to a 3-node
ring moved only 4 of 12 test keys instead of nearly all.

We use **150 virtual nodes** per physical node so the ring is evenly populated
and no single node accidentally owns a huge arc (the same technique Dynamo and
Cassandra use).

### Expiry / invalidation

Each cache entry has a 30-second TTL (lazy expiry on read), so stale data cannot
live forever. Additionally, when a batch flush changes a query's score, we
explicitly **invalidate** every affected prefix so the next read recomputes fresh
suggestions from the trie.

**Trade-off:** caching adds a consistency lag — a freshly updated count isn't
visible until the cached prefix expires or is invalidated. We accept brief
staleness in exchange for sub-millisecond reads, which is the standard typeahead
bargain.

---

## 4. Trending: recency-aware ranking (the 20% enhancement)

Pure popularity (`count`) means a query that was huge years ago outranks
something hot today, forever. The enhanced ranking blends them:

```
score = count + RECENCY_WEIGHT * recent_score
```

The brief lists five things to explain — here they are:

1. **How recent searches are tracked.** Every search adds a fixed bump to the
   query's `recent_score`.
2. **How recent activity affects ranking.** `recent_score` is added (weighted) to
   `count`, lifting recently-searched queries above equally-popular but stale ones.
3. **How the system avoids permanently over-ranking a short-lived spike.** Before
   each bump, `recent_score` is multiplied by `0.5 ** (elapsed / HALF_LIFE)` —
   exponential decay with a half-life. A burst of activity loses half its weight
   every half-life and fades to nothing on its own. No cleanup job needed; the
   decay is self-correcting. This is the key point.
4. **How the cache is updated when rankings change.** A batch flush rebuilds the
   trie with new scores and invalidates the affected cache prefixes, so the next
   suggestion read reflects the new ranking.
5. **The freshness / latency / complexity trade-off.** A larger `RECENCY_WEIGHT`
   or shorter half-life makes trends jump faster (fresher) but also makes ranking
   noisier and forces more frequent cache invalidation (more recompute, higher
   latency variance). We picked a 5-minute half-life and a moderate weight so the
   effect is visible in a demo without thrashing the cache.

Setting `RECENCY_WEIGHT = 0` recovers the basic count-only ranking — that's how
the same `/suggest` API demonstrates both approaches (`POST /admin/ranking`).

---

## 5. Batch writes (the other 20%)

Writing `count += 1` to the store on every search does not scale. The
`BatchWriter` (`app/batch_writer.py`) instead:

1. **Buffers** each submission in memory (O(1), no store access on the request
   path, so `/search` returns instantly).
2. **Aggregates** duplicates — 10 searches for "iphone" become one `iphone += 10`.
3. **Flushes** every 2 seconds, or sooner if the buffer reaches 50 distinct keys.

In the benchmark, 3,000 submissions became 12 actual writes (99.6% reduction).

**Failure trade-off (the brief asks for this explicitly).** The buffer is in
memory. If the process crashes before a flush, the un-flushed increments are
**lost**. We accept this because search counts are approximate popularity signals,
not financial data — losing a few is harmless, and the throughput win is large.
If durability were required, we'd append each submission to a write-ahead log
before buffering and replay it on restart, trading some latency for safety.

---

## 6. Summary of trade-offs

| Choice | We gained | We gave up |
|--------|-----------|------------|
| In-memory store | Speed, simplicity | Durability on restart |
| Top-K per trie node | O(1) reads | Memory + write work |
| Cache in front of trie | Sub-ms reads | Brief staleness |
| Consistent hashing | Stable cache on resize | A little code complexity |
| Recency decay | Fresh trending | Tuning two parameters |
| Batch writes | 99% fewer writes | Possible loss on crash |
