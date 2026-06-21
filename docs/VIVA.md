# Viva Prep — Explaining This System

Read this before your viva. Section 11 of the brief is blunt: if you can't
explain your design choices and core code, it's treated as plagiarism *even if
the code runs*. So this isn't optional. The good news: the whole system is four
ideas. Get these four and you can answer anything they throw at you.

The format below is: the question they'll likely ask → the short answer you say
out loud → the deeper detail if they push.

---

## Idea 1 — The Trie (prefix search)

**Q: "How do you find suggestions for a prefix quickly?"**

Say this: *"I use a trie — a prefix tree. Each character is a node, so to find
everything starting with 'iph' I just walk three nodes. And I cache the top-10
most popular completions at every node, so once I reach the 'iph' node the
answer is already computed and sorted. The lookup is basically constant time, it
doesn't matter if I have 100 queries or a million."*

**If they push — "Why not just scan a list and filter by prefix?"**
Because that's O(N) per keystroke. With 120k queries and someone typing fast,
you'd re-scan everything several times a second. The trie turns that into walking
a handful of nodes.

**If they push — "How do you keep the top-10 at each node updated?"**
When I insert or update a query, I walk its path and at each node I offer the
query to that node's top-10 list. The list is tiny (10 items) so I just re-sort
it. There's a fast path: if a node's list is already full and the new score can't
beat the worst entry, I skip it — that's what keeps inserts cheap at scale.

**Where:** `app/trie.py`, the `_offer` and `search` methods.

---

## Idea 2 — Consistent Hashing (the one they'll dig into most)

**Q: "Why consistent hashing? Why not `hash(key) % number_of_nodes`?"**

Say this: *"Because `% N` breaks the moment you add or remove a cache node. If I
go from 3 nodes to 4, the modulus changes and almost every key suddenly maps to a
different node — the whole cache goes cold at once and the database gets
stampeded. Consistent hashing puts nodes on a ring; a key is owned by the next
node clockwise. Adding a node only moves the small slice of keys between it and
its neighbour, so the cache stays mostly warm."*

**If they push — "Show me it actually moves fewer keys."**
I tested it: adding a 4th node to a 3-node ring moved only 4 of 12 keys. With
`% N` nearly all 12 would move. On average consistent hashing moves about K/N
keys when you change the node count.

**If they push — "What are virtual nodes?"**
If each physical node sits at just one point on the ring, by bad luck one node
can own a huge arc and get overloaded. So I place each physical node at 150
points ("virtual nodes"). More points → smoother, more even distribution. It's
the same trick Amazon Dynamo and Cassandra use.

**If they push — "How does the lookup actually work?"**
I hash the key to a position on a circle (0 to 2^32). The ring positions are kept
in a sorted list, so I binary-search for the first position greater than or equal
to the key's, and that's the owner. If I run off the end, I wrap to the start —
it's a circle.

**Where:** `app/consistent_hashing.py`, `get_node`. The ring is a sorted list
plus a position→node map; lookup is a `bisect`.

---

## Idea 3 — Recency / Trending (the 20% bonus)

**Q: "How does trending work? How do you stop an old spike ranking forever?"**

Say this: *"Each query has two numbers: an all-time count, and a recent_score.
Every search bumps the recent_score. But before each bump I decay the existing
recent_score using exponential decay — I multiply it by 0.5 to the power of
(time elapsed / half-life). So a burst of activity loses half its weight every
half-life and fades to nothing on its own. That's what stops a one-day spike from
ranking forever — the decay is self-cleaning, I don't need a cleanup job. The
final ranking score is count plus a weighted recent_score."*

**If they push — "Why exponential decay specifically?"**
It's smooth and cheap to compute (one multiply), and 'half-life' is an intuitive
knob: a 5-minute half-life means recency from 5 minutes ago is worth half as
much now. Linear decay would need clamping at zero and feels arbitrary; a sliding
time window needs you to store timestamps for every event. Exponential decay
needs just one number per query.

**If they push — "How do basic and enhanced ranking differ in your code?"**
Same formula: `score = count + recency_weight * recent_score`. If
`recency_weight = 0`, it's pure count — the basic 60% version. Set it positive
and recent activity lifts trending queries — the enhanced version. The same
`/suggest` API serves both; I flip it at runtime with `/admin/ranking`. In my
test, under enhanced ranking a freshly-spammed query jumped above a
historically-bigger one. That's the demo of "the difference between the two
approaches" the brief asks for.

**Where:** `app/store.py`, the `_decay`, `bump`, and `score` methods, and
`trending`.

---

## Idea 4 — Batch Writes (the other 20%)

**Q: "Why batch writes? What happens on a crash?"**

Say this: *"Writing count+=1 to the store on every single search doesn't scale —
that's a database round trip per keystroke-submit. Instead I buffer submissions
in memory and aggregate them: ten searches for 'iphone' become one 'iphone +=
10'. A background thread flushes every 2 seconds, or sooner if the buffer fills.
In my benchmark, 3,000 submissions became 12 actual writes — a 99.6% reduction."*

**The crash question is the one they want — answer it head-on:**
*"The buffer is in memory, so if the process crashes before a flush, those
un-flushed increments are lost. I accept that because search counts are
approximate popularity signals, not money — losing a handful doesn't matter, and
the throughput win is huge. If I needed durability I'd write each submission to
an append-only log first and replay it on restart, trading a bit of latency for
safety. That's the freshness-vs-durability-vs-latency trade-off."*

**If they push — "How do batched updates reach the suggestions?"**
On each flush I apply the aggregated increments to the store, rebuild the trie
with the new scores, and invalidate the affected cache prefixes. So the next
suggestion read picks up the change instead of serving a stale cached list.

**Where:** `app/batch_writer.py` (`record`, `flush`), and `apply_batch` in
`app/api.py`.

---

## The read path and write path (asked together a lot)

**Q: "Walk me through what happens when I type, and when I hit search."**

*Typing (`GET /suggest`):* check the distributed cache first (the right node is
chosen by consistent hashing). On a hit, return immediately — sub-millisecond. On
a miss, read the trie, store the result in the cache, return it. The UI shows
whether each result came from `cache` or `trie`, plus the latency.

*Searching (`POST /search`):* return `{"message": "Searched"}` instantly and hand
the query to the batch writer. The actual count update happens later on a flush.
This keeps the request fast and the database calm.

---

## Likely "gotcha" questions

- **"What if two requests update the same query at once?"** The batch writer
  guards its buffer with a lock, and flushing swaps the buffer out under the lock
  then writes outside it, so the request path is never blocked by a slow write.

- **"How do you handle weird input?"** Empty/missing prefix returns the global
  top list (the root node). Mixed case is normalised to lowercase on both insert
  and search, so 'IPhone' and 'iphone' collapse. A prefix with no matches returns
  an empty list and the UI shows a friendly "no matches" message.

- **"Why is your p95 latency so low?"** Almost every read is a cache hit served
  from memory; the trie only runs on a miss, and even then it's an O(prefix-length)
  walk to a precomputed list.

- **"How would this scale to real production?"** Swap the in-memory store for a
  database, make each cache node a real Redis server (the consistent-hash routing
  already supports that), and replace the in-process batch buffer with a durable
  queue like Kafka. The architecture doesn't change — only the backends do.

---

## One-line summary to anchor everything

> *Reads must be fast and frequent, writes must be frequent without crushing the
> database. The trie + cache make reads fast; consistent hashing keeps the cache
> stable as it scales; recency decay keeps suggestions fresh; batching keeps
> writes cheap.*

If you can say that sentence and then expand any clause on demand, you're ready.
