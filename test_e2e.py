"""
test_e2e.py
===========
End-to-end verification of every API using FastAPI's in-process TestClient.
No network or background server needed -- this exercises the real app object,
lifespan (dataset load + trie build), all endpoints, batching, trending, the
cache, and consistent-hash routing. Run:  python test_e2e.py
"""
import time
from fastapi.testclient import TestClient
from app.api import app


def main():
    print("Booting app (loads dataset, builds trie)...")
    t0 = time.time()
    with TestClient(app) as c:                       # triggers lifespan startup
        print(f"  ready in {time.time()-t0:.1f}s\n")

        # 1) SUGGEST: basic prefix, mixed case, empty, no-match -------------
        print("1. /suggest")
        r = c.get("/suggest", params={"q": "iph"}).json()
        print("   q=iph ->", r["suggestions"][:3], "| source:", r["source"])
        assert all(s.startswith("iph") for s in r["suggestions"]), "prefix violated"
        assert len(r["suggestions"]) <= 10, "more than 10 suggestions"

        r2 = c.get("/suggest", params={"q": "IPH"}).json()   # mixed case
        assert r2["suggestions"] == r["suggestions"], "mixed-case not normalised"
        print("   mixed-case 'IPH' matches 'iph': OK")

        r3 = c.get("/suggest", params={"q": ""}).json()       # empty
        print("   empty q -> global top:", r3["suggestions"][:2])

        r4 = c.get("/suggest", params={"q": "zzqqxx"}).json() # no match
        assert r4["suggestions"] == [], "no-match should be empty"
        print("   no-match 'zzqqxx' -> [] : OK")

        # 2) CACHE hit on repeat -------------------------------------------
        print("\n2. cache behaviour")
        first = c.get("/suggest", params={"q": "nik"}).json()
        second = c.get("/suggest", params={"q": "nik"}).json()
        print(f"   1st source={first['source']}  2nd source={second['source']}")
        assert first["source"] == "trie" and second["source"] == "cache", \
            "expected trie-miss then cache-hit"
        print("   trie-miss then cache-hit: OK")

        # 3) CACHE DEBUG (consistent hashing routing) ----------------------
        print("\n3. /cache/debug (consistent-hash routing)")
        for p in ["ip", "nik", "python", "java"]:
            d = c.get("/cache/debug", params={"prefix": p}).json()
            print(f"   prefix '{p}' -> {d['owner_node']} [{d['status']}]")

        # 4) SEARCH submit -> dummy response + batch record ----------------
        print("\n4. /search (dummy response + batched write)")
        s = c.post("/search", json={"query": "iphone 15 pro"}).json()
        print("   response:", s)
        assert s == {"message": "Searched"}, "dummy response wrong"

        # Hammer a trending query many times, then flush.
        for _ in range(40):
            c.post("/search", json={"query": "trending demo query"})
        from app.api import state
        state.batch.flush()           # force flush now for the test
        time.sleep(0.1)

        # 5) TRENDING ------------------------------------------------------
        print("\n5. /trending")
        tr = c.get("/trending").json()
        print("   trending:", tr["trending"][:5])
        assert "trending demo query" in tr["trending"], "hot query not trending"
        print("   freshly-spammed query is trending: OK")

        # 6) BASIC vs ENHANCED ranking on the SAME api ---------------------
        print("\n6. ranking modes (same /suggest api)")
        c.post("/admin/ranking", params={"mode": "basic"})
        basic = c.get("/suggest", params={"q": "trend"}).json()["suggestions"]
        c.post("/admin/ranking", params={"mode": "enhanced"})
        # re-spam so recency is fresh under enhanced ranking
        for _ in range(40):
            c.post("/search", json={"query": "trend rocket spike"})
        state.batch.flush(); time.sleep(0.1)
        enhanced = c.get("/suggest", params={"q": "trend"}).json()["suggestions"]
        print("   basic    'trend':", basic[:3])
        print("   enhanced 'trend':", enhanced[:3])

        # 7) STATS ---------------------------------------------------------
        print("\n7. /stats")
        st = c.get("/stats").json()
        print("   dataset:", st["dataset_size"])
        print("   latency ms:", st["latency_ms"])
        print("   cache:", st["cache"]["hits"], "hits /", st["cache"]["misses"],
              "misses  hit_rate=", st["cache"]["hit_rate"])
        print("   batch:", st["batch_writes"])

    print("\nALL CHECKS PASSED ✔")


if __name__ == "__main__":
    main()
