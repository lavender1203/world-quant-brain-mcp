"""Behavioural test for candidate_pool against a fake BRAIN client."""
import asyncio, json, os, sys, tempfile

import pandas as pd

TMP = tempfile.mkdtemp()
os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "candidate_pool.json")
sys.path.insert(0, "/opt/project/world-quant-brain-mcp")

import candidate_pool as cp

# ---- fake client ---------------------------------------------------------- #
# Pairwise correlations we control exactly.
CORR = {
    ("A", "B"): 0.20,
    ("A", "C"): 0.85,   # A and C are near-duplicates -> submitting A kills C
    ("A", "D"): 0.10,
    ("B", "C"): 0.15,
    ("B", "D"): 0.45,   # above mutual(0.40) but below prod(0.70)
    ("C", "D"): 0.05,
}
META = {
    "A": ("analyst", 1.9, 1.80, 0.30),   # pyramid, mult, sharpe, prod_corr
    "B": ("news",    1.9, 1.70, 0.25),
    "C": ("analyst", 1.9, 1.60, 0.40),
    "D": ("macro",   1.9, 1.65, 0.20),
}

def c(a, b):
    if a == b: return 1.0
    return CORR.get((a, b)) or CORR.get((b, a)) or 0.0

class FakeClient:
    def __init__(self): self.prod_calls = 0
    async def get_alpha_details(self, aid):
        pyr, mult, sharpe, _ = META[aid]
        return {"id": aid, "status": "UNSUBMITTED", "code": f"expr({aid})",
                "settings": {"instrumentType": "EQUITY", "region": "GBR",
                             "universe": "TOP700", "delay": 1, "neutralization": "FAST"},
                "metrics": {"sharpe": sharpe, "fitness": 1.2, "turnover": 0.07,
                            "margin": 0.002, "two_year_sharpe": 1.8,
                            "sub_universe_sharpe": 1.1},
                "ra": {"failed_ra_count": 0, "failed_ppa_count": 0, "pyramid_short": pyr},
                "pyramids": {"list": [{"name": f"GBR/D1/{pyr.upper()}", "multiplier": mult}]}}
    async def get_mutual_correlation(self, ids, threshold=0.5, years=4):
        return {"matrix": {a: {b: c(a, b) for b in ids} for a in ids}, "missing_pnl": []}
    async def check_correlation(self, aid, ctype, thr):
        self.prod_calls += 1
        return {"checks": {"production": {"max_correlation": META[aid][3]}}}
    async def check_self_correlation(self, aid, threshold=0.7, correlation_type="self"):
        return {"max_correlation": 0.10}
    async def get_pyramid_alphas(self, s=None, e=None):
        # analyst already has 2 submitted; news/macro have 0.
        return {"pyramids": {"GBR": {"D1": {"analyst": 2, "news": 0, "macro": 0, "model": 4}}}}

FAIL = []
def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"   <-- {extra}"))
    if not cond: FAIL.append(label)

async def main():
    cl = FakeClient()

    print("\n[1] admission")
    r = await cp.add_candidate(cl, "A")
    check("A admitted into empty pool", r["added"], r)

    r = await cp.add_candidate(cl, "B")
    check("B admitted (corr 0.20 vs A)", r["added"], r)

    r = await cp.add_candidate(cl, "C")
    check("C REJECTED: 0.85 vs A breaks production safety", not r["added"], r)
    check("C rejection names the safety reason",
          any("push the other past the production gate" in x for x in r["reasons"]), r["reasons"])

    r = await cp.add_candidate(cl, "D")
    check("D REJECTED by diversity (0.45 vs B >= 0.40)", not r["added"], r)
    r = await cp.add_candidate(cl, "D", allow_diversity_fail=True)
    check("D admitted once diversity waived", r["added"], r)

    r = await cp.add_candidate(cl, "C", force=True)
    check("C admitted under force", r["added"], r)
    check("forced entry records why", bool(r["forced_reasons"]), r)

    print("\n[2] pyramid coverage (target=3)")
    cov = await cp.pyramid_coverage(cl, region="GBR", delay=1, target=3)
    rows = {x["pyramid"]: x for x in cov["rows"]}
    check("analyst: 2 submitted + 2 pooled = 4",
          rows["analyst"]["submitted"] == 2 and rows["analyst"]["pool"] == 2
          and rows["analyst"]["total"] == 4, rows.get("analyst"))
    check("analyst needs 1 more submission", rows["analyst"]["needed_submissions"] == 1,
          rows.get("analyst"))
    check("analyst reachable from pool",
          rows["analyst"]["status"] == "NEEDS_1_SUBMISSIONS_FROM_POOL", rows.get("analyst"))
    check("model already lit (4 submitted, 0 pooled)",
          rows["model"]["status"] == "OS_SUFFICIENT", rows.get("model"))
    check("news short by 2 (0 submitted, 1 pooled, needs 3)",
          rows["news"]["status"] == "SHORT_BY_2_CANDIDATES", rows.get("news"))

    print("\n[3] submission plan — the collateral-damage rule")
    plan = await cp.submission_plan(cl, max_submissions=4, region="GBR", delay=1, target=3)
    picked = [p["alpha_id"] for p in plan["plan"]]
    check("A and C are never in the same batch (0.85 apart)",
          not ("A" in picked and "C" in picked), picked)
    skipped_ids = [s["alpha_id"] for s in plan["skipped"]]
    check("the A/C conflict is reported as skipped",
          "C" in skipped_ids or "A" in skipped_ids, plan["skipped"])
    check("every candidate left behind stays under the gates",
          plan["all_remaining_safe"], plan["remaining_pool_after_batch"])

    proj = {r["alpha_id"]: r for r in plan["remaining_pool_after_batch"]}
    if "A" in picked and "C" in proj:
        check("C's projected prod corr reflects A's submission (0.85)",
              abs(proj["C"]["projected_prod_corr"] - 0.85) < 1e-6, proj["C"])
        check("C is flagged unsafe rather than silently left",
              proj["C"]["still_safe"] is False, proj["C"])

    print("\n[3b] deadlock resolution")
    check("conflicts reported for the A/C pair", len(plan["conflicts"]) >= 1, plan["conflicts"])
    plan3 = await cp.submission_plan(cl, max_submissions=4, region="GBR", delay=1,
                                     target=3, resolve_conflicts=True)
    p3 = [x["alpha_id"] for x in plan3["plan"]]
    check("with resolve_conflicts, the better of A/C IS submitted",
          ("A" in p3) or ("C" in p3), p3)
    check("the sacrificed one is named", bool(plan3["sacrificed"]), plan3["sacrificed"])
    check("sacrifice is deliberate, so the batch still reads as safe",
          plan3["all_remaining_safe"], plan3["remaining_pool_after_batch"])
    check("A (higher Sharpe) is kept over C", "A" in p3 and plan3["sacrificed"] == ["C"],
          (p3, plan3["sacrificed"]))

    print("\n[4] daily cap")
    plan2 = await cp.submission_plan(cl, max_submissions=1, region="GBR", delay=1)
    check("cap of 1 yields at most 1", len(plan2["plan"]) <= 1, plan2["plan"])

    print("\n[4b] raw (unslimmed) BRAIN payload shapes")
    class RawClient(FakeClient):
        async def get_pyramid_alphas(self, st=None, en=None):
            # shape returned by brain_client (flat list), not the slimmed nested dict
            return {"pyramids": [
                {"category": {"id": "analyst", "name": "Analyst"}, "region": "GBR", "delay": 1, "alphaCount": 2},
                {"category": {"id": "model",   "name": "Model"},   "region": "GBR", "delay": 1, "alphaCount": 4},
                {"category": {"id": "news",    "name": "News"},    "region": "GBR", "delay": 1, "alphaCount": 0},
                {"category": {"id": "analyst", "name": "Analyst"}, "region": "USA", "delay": 1, "alphaCount": 9},
            ]}
    covr = await cp.pyramid_coverage(RawClient(), region="GBR", delay=1, target=3)
    rr = {x["pyramid"]: x for x in covr["rows"]}
    check("raw list shape parsed", rr["analyst"]["submitted"] == 2, rr.get("analyst"))
    check("raw shape: model reads 4 submitted", rr["model"]["submitted"] == 4, rr.get("model"))
    check("raw shape: other regions excluded", rr["analyst"]["submitted"] != 11, rr.get("analyst"))

    print("\n[5] persistence + sync")
    listing = cp.list_pool(region="GBR")
    check("pool persisted to disk", listing["pool_size"] == 4, listing["pool_size"])
    check("pool file really written", os.path.exists(os.environ["CANDIDATE_POOL_FILE"]))

    class SubmittedClient(FakeClient):
        async def get_alpha_details(self, aid):
            d = await FakeClient.get_alpha_details(self, aid)
            if aid == "A": d["status"] = "ACTIVE"
            return d

    # Default path: one submitted-alpha listing, no per-entry record fetches.
    class ListingClient(SubmittedClient):
        def __init__(self):
            super().__init__(); self.detail_calls = 0; self.list_calls = 0
        async def get_alpha_details(self, aid):
            self.detail_calls += 1
            return await SubmittedClient.get_alpha_details(self, aid)
        async def get_submitted_ids_since(self, since=None):
            self.list_calls += 1
            return {"ids": ["A"], "rows": {}, "since": since, "pages": 1}

    lc = ListingClient()
    s = await cp.sync_pool(lc)
    check("sync drops the now-submitted A", s["promoted_to_submitted"] == ["A"], s)
    check("pool shrank to 3", s["pool_size"] == 3, s)
    check("cheap path used the submitted listing", s.get("mode") == "submitted-list", s)
    check("cheap path made 1 listing call", lc.list_calls == 1, lc.list_calls)
    check("cheap path fetched no per-entry records", lc.detail_calls == 0, lc.detail_calls)

    # Opt-in full refresh still reads every entry, and a client without the
    # listing method degrades to that path instead of failing.
    s2 = await cp.sync_pool(SubmittedClient(), refresh_details=True)
    check("refresh_details path still reports a mode", s2.get("mode") == "full-refresh", s2)

    # --------------------------------------------------------------------- #
    # Regressions for the review findings (A1-A3, B1-B2, C, D).
    # Each block reproduces the old wrong behaviour and pins the new one.
    # --------------------------------------------------------------------- #

    print("\n[6] A1: a sacrificed candidate must never ride along in the same batch")
    # P and Q are 0.55 apart: fine for the production gate (0.70), over the self
    # gate (0.50). Submitting both lifts each other's self correlation to 0.55.
    class PairClient(FakeClient):
        PAIR = 0.55
        async def get_alpha_details(self, aid):
            pyr = {"P": "news", "Q": "macro"}[aid]
            sh = {"P": 2.0, "Q": 1.9}[aid]
            return {"id": aid, "status": "UNSUBMITTED",
                    "settings": {"instrumentType": "EQUITY", "region": "GBR",
                                 "universe": "TOP700", "delay": 1},
                    "metrics": {"sharpe": sh},
                    "ra": {"pyramid_short": pyr, "failed_ra_count": 0},
                    "pyramids": {"list": [{"name": f"GBR/D1/{pyr.upper()}", "multiplier": 1.5}]}}
        async def get_mutual_correlation(self, ids, threshold=0.5, years=4):
            def v(a, b): return 1.0 if a == b else self.PAIR
            return {"matrix": {a: {b: v(a, b) for b in ids} for a in ids}, "missing_pnl": []}
        async def check_correlation(self, aid, ctype, thr, **kw):
            return {"checks": {"production": {"max_correlation": 0.20}}}
        async def get_pyramid_alphas(self, s=None, e=None):
            return {"pyramids": {"GBR": {"D1": {"news": 0, "macro": 0}}}}

    pool_file = os.environ["CANDIDATE_POOL_FILE"]
    os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "pool_a1.json")
    pc = PairClient()
    for aid in ("P", "Q"):
        r = await cp.add_candidate(pc, aid, allow_diversity_fail=True, self_threshold=0.50)
        check(f"{aid} admitted (0.55 < prod 0.70, self gate is about the OTHER one)", r["added"], r)
    plan = await cp.submission_plan(pc, max_submissions=4, region="GBR", delay=1,
                                    prod_threshold=0.70, self_threshold=0.50,
                                    resolve_conflicts=True)
    picked = [x["alpha_id"] for x in plan["plan"]]
    check("resolve_conflicts submits exactly one of the pair", picked == ["P"], picked)
    check("the sacrificed one is named", plan["sacrificed"] == ["Q"], plan["sacrificed"])
    check("the sacrificed one is NOT also in the plan",
          not set(plan["sacrificed"]) & set(picked), (plan["sacrificed"], picked))
    reasons = " ".join(x.get("reason", "") for x in plan["skipped"])
    check("and it is reported as skipped, saying why", "sacrific" in reasons, plan["skipped"])
    # Positive control: the same pair IS shippable together once the self gate is
    # loose enough that 0.55 clears both thresholds.
    plan_ok = await cp.submission_plan(pc, max_submissions=4, region="GBR", delay=1,
                                       prod_threshold=0.70, self_threshold=0.70)
    check("with self_threshold 0.70 the 0.55 pair ships together",
          sorted(x["alpha_id"] for x in plan_ok["plan"]) == ["P", "Q"], plan_ok["plan"])

    print("\n[7] A2: no correlation data must not read as 'safe'")
    class BlindClient(PairClient):
        async def get_mutual_correlation(self, ids, threshold=0.5, years=4):
            return {"error": "Fewer than 2 alphas had usable PnL.", "missing_pnl": list(ids)}

    os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "pool_a2.json")
    bc = BlindClient()
    r = await cp.add_candidate(bc, "P")
    check("first candidate needs no pairwise check (empty pool)", r["added"], r)
    r = await cp.add_candidate(bc, "Q")
    check("A3: unverifiable pairwise BLOCKS admission", not r["added"], r)
    check("the block names the unverifiable pair",
          any("unverifiable" in x for x in r["reasons"]), r["reasons"])
    r = await cp.add_candidate(bc, "Q", force=True)
    check("force still admits it, recording why", r["added"] and r["forced_reasons"], r)
    plan = await cp.submission_plan(bc, max_submissions=1, region="GBR", delay=1,
                                    prod_threshold=0.70, self_threshold=0.70)
    check("all_remaining_safe is null, not true, with zero correlation data",
          plan["all_remaining_safe"] is None, plan["all_remaining_safe"])
    check("the unprovable candidate is named", plan["unprovable_for"], plan)
    left = {x["alpha_id"]: x for x in plan["remaining_pool_after_batch"]}
    check("its still_safe is null (not True)",
          all(v["still_safe"] is None for v in left.values()), left)
    check("and the missing pair is attributed",
          all(v["unknown_pairwise_vs"] for v in left.values()), left)

    print("\n[8] A3: an unknown own-correlation is not zero")
    class BusyClient(PairClient):
        PAIR = 0.10
        async def check_correlation(self, aid, ctype, thr, **kw):
            if aid == "Q":      # slot taken -> no number at all
                return {"status": "correlation_busy", "retry_after": 42,
                        "checks": {"production": {"max_correlation": None,
                                                  "status": "correlation_busy",
                                                  "retry_after": 42}}}
            return {"checks": {"production": {"max_correlation": 0.20}}}

    os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "pool_a3.json")
    busy = BusyClient()
    r = await cp.add_candidate(busy, "P")
    check("P admitted normally", r["added"], r)
    r = await cp.add_candidate(busy, "Q")
    check("Q blocked while its production correlation is unknown", not r["added"], r)
    r = await cp.add_candidate(busy, "Q", force=True)
    check("forced Q stores prod_corr as null, not 0", r["entry"]["prod_corr"] is None, r["entry"])
    plan = await cp.submission_plan(busy, max_submissions=1, region="GBR", delay=1,
                                    prod_threshold=0.70, self_threshold=0.70)
    leftover = {x["alpha_id"]: x for x in plan["remaining_pool_after_batch"]}
    check("the unknown-corr candidate is not declared safe",
          leftover.get("Q", {}).get("still_safe") is None, leftover)
    check("so the batch verdict is null too", plan["all_remaining_safe"] is None, plan["all_remaining_safe"])

    print("\n[9] B1: concurrent adds must not lose each other")
    class SlowClient(PairClient):
        PAIR = 0.10
        async def check_correlation(self, aid, ctype, thr, **kw):
            await asyncio.sleep(0.05)      # yields the event loop mid-evaluation
            return {"checks": {"production": {"max_correlation": 0.20}}}

    os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "pool_b1.json")
    sc2 = SlowClient()
    await asyncio.gather(cp.add_candidate(sc2, "P", allow_diversity_fail=True),
                         cp.add_candidate(sc2, "Q", allow_diversity_fail=True))
    got = sorted(cp.load_pool()["entries"])
    check("both concurrent adds survived", got == ["P", "Q"], got)

    print("\n[10] B2: a corrupt pool file must survive the next write")
    corrupt = os.path.join(TMP, "pool_corrupt.json")
    os.environ["CANDIDATE_POOL_FILE"] = corrupt
    with open(corrupt, "w", encoding="utf-8") as fh:
        fh.write("{ not json at all")
    loaded = cp.load_pool()
    check("corrupt load reports the error", bool(loaded.get("load_error")), loaded)
    backup = loaded.get("quarantined_to")
    check("and moves the bad file aside", backup and os.path.exists(backup), loaded)
    check("the original bytes are intact",
          open(backup, encoding="utf-8").read() == "{ not json at all", backup)
    cp.save_pool(loaded)
    check("the write went to a fresh file, not over the evidence",
          json.load(open(corrupt, encoding="utf-8"))["entries"] == {}, corrupt)
    check("diagnostics are not persisted",
          "load_error" not in json.load(open(corrupt, encoding="utf-8")), corrupt)
    try:
        cp.save_pool({"entries": {}, "load_error": "unreadable", "schema_version": 1})
        check("an unquarantined corrupt pool refuses to be overwritten", False, "no raise")
    except RuntimeError:
        check("an unquarantined corrupt pool refuses to be overwritten", True)

    print("\n[11] C: one self-correlation default everywhere")
    check("module default is the platform gate 0.70", cp.DEFAULT_SELF_THRESHOLD == 0.70,
          cp.DEFAULT_SELF_THRESHOLD)

    print("\n[12] D: refresh_prod reports what it could not refresh")
    class SlotClient(PairClient):
        """One correlation slot per ~3 minutes: only the first refresh succeeds."""
        PAIR = 0.10
        PYR = {"P": "news", "Q": "macro", "R": "pv"}
        def __init__(self):
            super().__init__(); self.sync = False; self.sync_calls = []; self.forced = []
        async def get_alpha_details(self, aid):
            pyr = self.PYR[aid]
            return {"id": aid, "status": "UNSUBMITTED",
                    "settings": {"instrumentType": "EQUITY", "region": "GBR",
                                 "universe": "TOP700", "delay": 1},
                    "metrics": {"sharpe": 1.5},
                    "ra": {"pyramid_short": pyr, "failed_ra_count": 0},
                    "pyramids": {"list": [{"name": f"GBR/D1/{pyr.upper()}", "multiplier": 1.5}]}}
        async def check_correlation(self, aid, ctype, thr, force_refresh=False):
            if not self.sync:
                return {"checks": {"production": {"max_correlation": 0.20}}}
            self.sync_calls.append(aid); self.forced.append(force_refresh)
            if len(self.sync_calls) == 1:
                return {"checks": {"production": {"max_correlation": 0.33}}}
            return {"status": "correlation_busy", "retry_after": 180,
                    "checks": {"production": {"max_correlation": None,
                                              "status": "correlation_busy",
                                              "retry_after": 180}}}
        async def get_pyramid_alphas(self, s=None, e=None):
            return {"pyramids": {"GBR": {"D1": {"news": 0, "macro": 0, "pv": 0}}}}
        async def get_submitted_ids_since(self, since=None):
            return {"ids": [], "rows": {}, "since": since, "pages": 1}

    os.environ["CANDIDATE_POOL_FILE"] = os.path.join(TMP, "pool_d.json")
    slot = SlotClient()
    for aid in ("P", "Q", "R"):
        r = await cp.add_candidate(slot, aid, allow_diversity_fail=True)
        check(f"{aid} pooled for the sync test", r["added"], r)
    slot.sync = True
    s3 = await cp.sync_pool(slot, refresh_prod=True)
    pr = s3.get("prod_refresh") or {}
    check("refresh_prod bypasses the 5-minute cache", all(slot.forced), slot.forced)
    check("exactly one entry refreshed (one slot)", pr.get("updated") == ["P"], pr)
    check("the busy one is reported, not silently skipped",
          [b["alpha_id"] for b in pr.get("busy") or []] == ["Q"], pr)
    check("the untried ones are named", pr.get("not_attempted") == ["R"], pr)
    check("no request was wasted on them", slot.sync_calls == ["P", "Q"], slot.sync_calls)
    check("a retry_after is surfaced", pr.get("retry_after") == 180, pr)
    check("the note says what happened", "not attempted" in (pr.get("note") or ""), pr)
    entries = cp.load_pool()["entries"]
    check("only the refreshed entry's number moved",
          entries["P"]["prod_corr"] == 0.33 and entries["Q"]["prod_corr"] == 0.20, entries)
    os.environ["CANDIDATE_POOL_FILE"] = pool_file

    print("\n[13] A3 (client): an uncomputable correlation is None, never 0")
    try:
        sys.path.insert(0, "/app")
        import main as _main
    except Exception as exc:                                    # pragma: no cover
        print(f"  SKIP  main.py not importable here ({exc})")
    else:
        import inspect as _inspect

        class _Client(_main.BrainApiClient):
            def __init__(self):
                self.log = lambda *a, **k: None
            async def ensure_authenticated(self): pass
            async def get_alpha_pnl(self, aid, force_refresh=False):
                # X and Y overlap; Z lives in a disjoint, later window, so no pair
                # involving Z has a computable correlation.
                spans = {"X": ("2020-01-01", 0), "Y": ("2020-01-01", 1), "Z": ("2023-01-01", 2)}
                start, seed = spans[aid]
                dates = pd.date_range(start, periods=40, freq="D")
                cum, acc = [], 0.0
                for i in range(len(dates)):
                    acc += ((i * 7 + seed * 13) % 11) - 5
                    cum.append(acc)
                return {"schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
                        "records": [[d.strftime("%Y-%m-%d"), v] for d, v in zip(dates, cum)]}

        res = await _Client().get_mutual_correlation(["X", "Y", "Z"], threshold=0.5)
        check("a computable pair is a number", isinstance(res["matrix"]["X"]["Y"], float),
              res["matrix"]["X"]["Y"])
        check("an uncomputable pair is None, not 0.0", res["matrix"]["X"]["Z"] is None,
              res["matrix"]["X"]["Z"])
        check("uncomputable pairs are listed", bool(res["unknown_pairs"]), res["unknown_pairs"])
        check("all_below_threshold is not claimed while a pair is unknown",
              res["all_below_threshold"] is False, res["all_below_threshold"])
        # And the pool reads those Nones as "unknown", not "safe".
        row = res["matrix"]["X"]
        check("None survives into the pool's pairwise view", row["Z"] is None, row)
        check("C: pool tool defaults agree on the self gate",
              _inspect.signature(_main.pool_add).parameters["self_threshold"].default
              == _inspect.signature(_main.pool_check).parameters["self_threshold"].default
              == _inspect.signature(_main.pool_submission_plan).parameters["self_threshold"].default
              == cp.DEFAULT_SELF_THRESHOLD,
              {n: _inspect.signature(getattr(_main, n)).parameters["self_threshold"].default
               for n in ("pool_add", "pool_check", "pool_submission_plan")})
        check("D: force_refresh is exposed on the correlation tools",
              "force_refresh" in _inspect.signature(_main.check_correlation).parameters
              and "force_refresh" in _inspect.signature(_main.check_power_pool_correlation).parameters)

        class _CacheClient(_main.BrainApiClient):
            """Only the correlation cache path; no auth, no lock, no network."""
            def __init__(self):
                self.log = lambda *a, **k: None
                self.auth_credentials = {"email": "t@example.com"}
                self.redis_client = None
                self._brain_correlation_cache_ttl_seconds = 300
                self._brain_correlation_min_interval_seconds = 180
                self._brain_correlation_busy_retry_after_seconds = 180
                self._corr_result_cache = {}
                self.polls = 0
            async def ensure_authenticated(self): pass
            async def _try_acquire_brain_correlation_lock(self, op):
                return {"acquired": True, "token": "t", "layers": []}
            async def _release_brain_correlation_lock(self, info, op): pass
            async def _poll_platform_correlation(self, aid, endpoint="prod", label="production"):
                self.polls += 1
                return {"max": 0.4 + 0.1 * self.polls, "records": [[0.4, 0.5, 2]], "schema": {}}

        cc = _CacheClient()
        first = await cc.get_production_correlation("A1")
        again = await cc.get_production_correlation("A1")
        check("D: a repeat check is served from the cache",
              cc.polls == 1 and again["cached"] is True, (cc.polls, again))
        forced = await cc.get_production_correlation("A1", force_refresh=True)
        check("D: force_refresh really re-queries",
              cc.polls == 2 and not forced.get("cached"), (cc.polls, forced))
        check("D: and the fresh answer replaces the cached one",
              (await cc.get_production_correlation("A1"))["max"] == forced["max"],
              forced)
        check("D: prod and power-pool caches stay separate",
              (await cc.get_power_pool_correlation("A1"))["max"] != first["max"], cc.polls)

    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}"))
    return 1 if FAIL else 0

sys.exit(asyncio.run(main()))
