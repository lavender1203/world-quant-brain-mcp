"""Candidate-pool management for BRAIN regular alphas.

WHY THIS EXISTS
---------------
BRAIN accepts at most a handful of regular-alpha submissions per day (4 for RA),
but a research session can produce many more qualified alphas than that. Those
alphas have to wait somewhere, and while they wait two things are true:

1. A pyramid is "covered" by the alphas you *will* submit, not only by the ones
   already submitted. Planning needs submitted + pooled counted together.
2. Submitting one alpha **changes the production correlation of every other
   candidate**, because the newly submitted alpha joins the pool that production
   correlation is measured against.

Point 2 is the whole reason this module is not just a list. The invariant is:

    projected_prod_corr(B | submit S)
        = max( prod_corr_now(B),  max over A in S of |corr(A, B)| )

and likewise for self correlation (different threshold). So "guarantee that
submitting a candidate never pushes another candidate past 0.7" is equivalent to
"every pair inside the pool is already below 0.7" — an invariant that can be
enforced at admission time, when it is still cheap to act on, rather than
discovered on submission day when it is too late.

The pool therefore enforces, on every ``add``:
  * candidate's own production correlation      < prod_threshold  (default 0.70)
  * candidate's own self correlation            < self_threshold  (default 0.70)
  * |corr(candidate, every pool member)|        < prod_threshold  -- SAFETY
  * |corr(candidate, every pool member)|        < mutual_threshold-- DIVERSITY

Safety is a hard gate (violating it makes the pool self-defeating). Diversity is
a softer, user-chosen basket rule and can be relaxed with ``allow_diversity_fail``.

Correlations among your own alphas are computed locally from PnL, so admission
costs no BRAIN correlation slot. Only the candidate's own production correlation
touches the rate-limited endpoint, and it is cached in the entry.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Pyramid category order used for reporting. Mirrors BRAIN's category ids.
PYRAMID_ORDER: List[str] = [
    "pv", "risk", "earnings", "other", "option", "model", "fundamental",
    "institutions", "analyst", "shortinterest", "insiders", "socialmedia",
    "news", "sentiment", "macro", "imbalance", "broker",
]

PYRAMID_LABELS: Dict[str, str] = {
    "pv": "Price Volume", "risk": "Risk", "earnings": "Earnings",
    "other": "Other", "option": "Option", "model": "Model",
    "fundamental": "Fundamental", "institutions": "Institutions",
    "analyst": "Analyst", "shortinterest": "Short Interest",
    "insiders": "Insiders", "socialmedia": "Social Media", "news": "News",
    "sentiment": "Sentiment", "macro": "Macro", "imbalance": "Imbalance",
    "broker": "Broker",
}

DEFAULT_PROD_THRESHOLD = 0.70
DEFAULT_SELF_THRESHOLD = 0.70
DEFAULT_MUTUAL_THRESHOLD = 0.40
DEFAULT_PYRAMID_TARGET = 3
DEFAULT_DAILY_SUBMIT_CAP = 4

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def pool_path() -> Path:
    """Resolve the on-disk pool file.

    Prefers CANDIDATE_POOL_FILE, then the directory of MCP_CONFIG_FILE (which is
    a docker volume in the shipped compose file, so the pool survives rebuilds),
    then a repo-local fallback.
    """
    explicit = os.environ.get("CANDIDATE_POOL_FILE")
    if explicit:
        return Path(explicit)
    cfg = os.environ.get("MCP_CONFIG_FILE")
    if cfg:
        return Path(cfg).parent / "candidate_pool.json"
    return Path(__file__).parent / "config" / "candidate_pool.json"


def _quarantine_pool(path: Path) -> Optional[str]:
    """Move an unreadable pool file aside, returning where it went.

    Leaving it in place is not an option: ``save_pool`` cannot tell that the
    in-memory pool it is about to write came from a failed read, so the next
    mutation would overwrite the damaged-but-possibly-recoverable bytes with an
    empty pool. Renaming preserves them AND unblocks writes.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, target)
        return str(target)
    except OSError:
        return None


def load_pool() -> Dict[str, Any]:
    """Load the pool, returning an empty structure when absent or corrupt.

    A corrupt file is quarantined (see ``_quarantine_pool``) so it survives the
    next write; the returned pool then carries ``load_error`` and
    ``quarantined_to`` for reporting.
    """
    path = pool_path()
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "entries": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt pool must not take the server down; surface an empty pool
        # and keep the bad bytes under a new name for inspection.
        return {"schema_version": SCHEMA_VERSION, "entries": {},
                "load_error": f"{path}: {exc}",
                "quarantined_to": _quarantine_pool(path)}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": SCHEMA_VERSION, "entries": {},
                "load_error": f"{path}: unexpected structure",
                "quarantined_to": _quarantine_pool(path)}
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def save_pool(pool: Dict[str, Any]) -> None:
    """Atomically persist the pool.

    Refuses to write a pool that came from an unreadable file we could not move
    aside — that write would destroy the only copy of the original data.
    """
    if pool.get("load_error") and not pool.get("quarantined_to"):
        raise RuntimeError(
            f"refusing to overwrite an unreadable pool file ({pool['load_error']}); "
            "move it aside manually first"
        )
    path = pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pool["updated_at"] = _now()
    # Diagnostics describe THIS read, not the pool's contents; never persist them.
    payload = {k: v for k, v in pool.items() if k not in ("load_error", "quarantined_to")}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".candidate_pool.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PoolChangedError(Exception):
    """The pool changed between evaluation and write; re-evaluate and retry."""


def _pool_lock_path() -> Path:
    p = pool_path()
    return p.with_name(f".{p.name}.lock")


@contextmanager
def _pool_file_lock():
    """Exclusive cross-process lock around one load->modify->save cycle.

    Held for file operations only, never across a network await: a correlation
    fetch takes minutes and must not block another process's pool write.
    """
    path = _pool_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _mutate_pool(mutate) -> Tuple[Dict[str, Any], Any]:
    """Load, apply ``mutate(pool)``, and persist — atomically.

    Every writer here does network I/O before it knows what to write, and a bare
    load -> await -> save is exactly how one caller's entry gets overwritten by
    another's stale copy (reproduced: two concurrent ``add_candidate`` calls left
    only one entry behind). So the read and the write happen together, inside the
    lock, and ``mutate`` MUST be synchronous. It may raise
    ``PoolChangedError`` to abort without writing.
    """
    with _pool_file_lock():
        pool = load_pool()
        result = mutate(pool)
        if result is not False:
            save_pool(pool)
        return pool, result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 stamp (ours or BRAIN's) into an aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Alpha metadata extraction
# --------------------------------------------------------------------------- #

def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _normalize_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either a raw BRAIN alpha object or main.py's slimmed form.

    ``brain_client.get_alpha_details`` returns the RAW object, where the metrics
    live under ``is`` and pyramid / two-year-Sharpe / sub-universe-Sharpe are
    buried inside ``is.checks``. main.py's ``_slim_alpha`` already knows how to
    dig those out, so reuse it rather than duplicating (and drifting from) that
    logic. The import is deferred because main.py imports this module.
    """
    if not isinstance(details, dict):
        return {}
    if "is" not in details and "regular" not in details:
        return details  # already slim
    try:
        from main import _slim_alpha  # noqa: PLC0415 - deferred to break the cycle
    except Exception:
        return details
    try:
        return _slim_alpha(details)
    except Exception:
        return details


def summarize_alpha(details: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a get_alpha_details payload to the fields the pool reasons about."""
    details = _normalize_details(details)
    settings = details.get("settings") or {}
    metrics = details.get("metrics") or details.get("is") or {}
    ra = details.get("ra") or {}
    pyr = details.get("pyramids") or {}
    pyr_list = pyr.get("list") or []

    pyramid = ra.get("pyramid_short")
    if not pyramid and pyr_list:
        # Names look like "GBR/D1/ANALYST".
        tail = str(pyr_list[0].get("name", "")).rsplit("/", 1)[-1]
        pyramid = tail.lower() or None

    multiplier = None
    if pyr_list:
        multiplier = _num(pyr_list[0].get("multiplier"))

    return {
        "alpha_id": details.get("id") or details.get("alpha_id"),
        "code": details.get("code"),
        "status": details.get("status"),
        "instrument_type": settings.get("instrumentType"),
        "region": settings.get("region"),
        "universe": settings.get("universe"),
        "delay": settings.get("delay"),
        "neutralization": settings.get("neutralization"),
        "decay": settings.get("decay"),
        "truncation": settings.get("truncation"),
        "max_trade": settings.get("maxTrade"),
        "pyramid": pyramid,
        "pyramid_multiplier": multiplier,
        "metrics": {
            "sharpe": _num(metrics.get("sharpe")),
            "fitness": _num(metrics.get("fitness")),
            "turnover": _num(metrics.get("turnover")),
            "returns": _num(metrics.get("returns")),
            "margin": _num(metrics.get("margin")),
            "drawdown": _num(metrics.get("drawdown")),
            "two_year_sharpe": _num(metrics.get("two_year_sharpe")),
            "sub_universe_sharpe": _num(metrics.get("sub_universe_sharpe")),
        },
        "failed_ra_count": ra.get("failed_ra_count"),
        "failed_ppa_count": ra.get("failed_ppa_count"),
    }


# --------------------------------------------------------------------------- #
# Correlation helpers
# --------------------------------------------------------------------------- #

async def pairwise_against_pool(
    client: Any,
    alpha_id: str,
    other_ids: Sequence[str],
    years: int = 4,
) -> Tuple[Dict[str, float], List[str]]:
    """|corr| of ``alpha_id`` against each id in ``other_ids``.

    Local PnL correlation; consumes no BRAIN correlation slot. Returns
    (mapping other_id -> abs correlation, list of ids whose PnL was unusable).
    """
    others = [o for o in dict.fromkeys(other_ids) if o and o != alpha_id]
    if not others:
        return {}, []

    result = await client.get_mutual_correlation(
        [alpha_id] + others, threshold=1.1, years=years
    )
    if result.get("error"):
        # Propagate as "unknown" rather than silently treating it as zero:
        # a missing correlation must never be read as "safe".
        return {}, list(others)

    matrix = result.get("matrix") or {}
    row = matrix.get(alpha_id) or {}
    out: Dict[str, float] = {}
    missing: List[str] = []
    for oid in others:
        val = row.get(oid)
        if val is None:
            missing.append(oid)
        else:
            out[oid] = abs(float(val))
    missing.extend([m for m in (result.get("missing_pnl") or []) if m in others and m not in missing])
    return out, missing


def _accepts_kwarg(fn: Any, name: str) -> bool:
    """Whether ``fn`` takes a ``name=`` keyword (test fakes often do not)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def fetch_production_correlation(
    client: Any, alpha_id: str, *, force_refresh: bool = False
) -> Dict[str, Any]:
    """Production correlation for one alpha, normalised to {value, status, retry_after}.

    ``force_refresh`` bypasses the client's 5-minute result cache. Needed right
    after a submission, which is precisely when the cached number is wrong:
    the submitted alpha has joined the pool everyone else is measured against.
    """
    kwargs = {}
    if force_refresh and _accepts_kwarg(client.check_correlation, "force_refresh"):
        kwargs["force_refresh"] = True
    data = await client.check_correlation(
        alpha_id, "production", DEFAULT_PROD_THRESHOLD, **kwargs)
    checks = (data or {}).get("checks") or {}
    prod = checks.get("production") or {}
    status = prod.get("status") or (data or {}).get("status")
    return {
        "value": _num(prod.get("max_correlation")),
        "status": status or ("ok" if prod.get("max_correlation") is not None else "unavailable"),
        "message": prod.get("message") or (data or {}).get("message"),
        "retry_after": prod.get("retry_after") or (data or {}).get("retry_after"),
        "cached": bool((prod.get("correlation_data") or {}).get("cached")),
    }


async def fetch_self_correlation(client: Any, alpha_id: str) -> Dict[str, Any]:
    """Local self correlation against the whole submitted-OS pool (PPACs included)."""
    data = await client.check_self_correlation(
        alpha_id, threshold=DEFAULT_SELF_THRESHOLD, correlation_type="self"
    )
    checks = (data or {}).get("checks") or {}
    node = checks.get("self") or data or {}
    return {
        "value": _num(node.get("max_correlation")),
        "status": "ok" if node.get("max_correlation") is not None else "unavailable",
    }


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #

async def evaluate_candidate(
    client: Any,
    alpha_id: str,
    pool: Dict[str, Any],
    *,
    prod_threshold: float = DEFAULT_PROD_THRESHOLD,
    self_threshold: float = DEFAULT_SELF_THRESHOLD,
    mutual_threshold: float = DEFAULT_MUTUAL_THRESHOLD,
    refresh_prod: bool = True,
    years: int = 4,
) -> Dict[str, Any]:
    """Assess whether ``alpha_id`` may join the pool. Never mutates the pool."""
    entries: Dict[str, Any] = pool.get("entries", {})
    details = await client.get_alpha_details(alpha_id)
    summary = summarize_alpha(details or {})
    summary["alpha_id"] = summary.get("alpha_id") or alpha_id

    blockers: List[str] = []
    warnings: List[str] = []

    if summary.get("status") == "ACTIVE":
        blockers.append("alpha is already submitted (status ACTIVE)")

    # --- own production correlation ---------------------------------------- #
    existing = entries.get(alpha_id) or {}
    if refresh_prod or existing.get("prod_corr") is None:
        prod = await fetch_production_correlation(client, alpha_id)
    else:
        prod = {"value": existing.get("prod_corr"), "status": "cached"}
    if prod["value"] is None:
        warnings.append(
            f"production correlation unavailable ({prod.get('status')}); "
            "candidate admitted only with force=True"
        )
        blockers.append("production correlation unknown")
    elif prod["value"] >= prod_threshold:
        blockers.append(
            f"production correlation {prod['value']:.4f} >= {prod_threshold}"
        )

    # --- own self correlation ---------------------------------------------- #
    slf = await fetch_self_correlation(client, alpha_id)
    if slf["value"] is not None and slf["value"] >= self_threshold:
        blockers.append(f"self correlation {slf['value']:.4f} >= {self_threshold}")

    # --- against everything already pooled --------------------------------- #
    pool_ids = [pid for pid in entries if pid != alpha_id]
    pair_corr, missing = await pairwise_against_pool(client, alpha_id, pool_ids, years=years)

    safety_violations = [
        {"alpha_id": oid, "correlation": round(c, 4)}
        for oid, c in sorted(pair_corr.items(), key=lambda kv: -kv[1])
        if c >= prod_threshold
    ]
    diversity_violations = [
        {"alpha_id": oid, "correlation": round(c, 4)}
        for oid, c in sorted(pair_corr.items(), key=lambda kv: -kv[1])
        if mutual_threshold <= c < prod_threshold
    ]
    if safety_violations:
        blockers.append(
            "pairwise correlation >= prod threshold with "
            + ", ".join(f"{v['alpha_id']}({v['correlation']})" for v in safety_violations)
            + " — submitting either would push the other past the production gate"
        )
    if diversity_violations:
        warnings.append(
            "pairwise correlation >= mutual threshold with "
            + ", ".join(f"{v['alpha_id']}({v['correlation']})" for v in diversity_violations)
        )
    if missing:
        # An unknown correlation must never be read as safe — that is the whole
        # premise of the pool invariant, so it blocks rather than warns.
        blockers.append(
            "pairwise correlation unverifiable (PnL unavailable) against: "
            + ", ".join(missing)
            + " — admit with force=True only if you accept an unproven pair"
        )

    max_pair = max(pair_corr.values()) if pair_corr else 0.0

    return {
        "alpha_id": alpha_id,
        "summary": summary,
        "prod_corr": prod["value"],
        "prod_corr_status": prod.get("status"),
        "self_corr": slf["value"],
        "max_pairwise_vs_pool": round(max_pair, 4),
        "pairwise": {k: round(v, 4) for k, v in sorted(pair_corr.items(), key=lambda kv: -kv[1])},
        "safety_violations": safety_violations,
        "diversity_violations": diversity_violations,
        "missing_pnl": missing,
        "blockers": blockers,
        "warnings": warnings,
        "admissible": not blockers,
        "thresholds": {
            "prod": prod_threshold,
            "self": self_threshold,
            "mutual": mutual_threshold,
        },
    }


async def add_candidate(
    client: Any,
    alpha_id: str,
    *,
    note: Optional[str] = None,
    force: bool = False,
    allow_diversity_fail: bool = False,
    prod_threshold: float = DEFAULT_PROD_THRESHOLD,
    self_threshold: float = DEFAULT_SELF_THRESHOLD,
    mutual_threshold: float = DEFAULT_MUTUAL_THRESHOLD,
    refresh_prod: bool = True,
) -> Dict[str, Any]:
    """Evaluate and, if it passes, persist the candidate.

    The gates are evaluated against a snapshot, then written under the pool lock
    only if the pool's membership has not changed meanwhile — the pairwise gate
    is a statement about the pool that was read, so a pool that grew during the
    (slow) evaluation must be re-evaluated rather than written over.
    """
    for attempt in range(3):
        pool = load_pool()
        snapshot_ids = frozenset(pool.get("entries", {})) - {alpha_id}
        report = await evaluate_candidate(
            client, alpha_id, pool,
            prod_threshold=prod_threshold,
            self_threshold=self_threshold,
            mutual_threshold=mutual_threshold,
            refresh_prod=refresh_prod,
        )

        rejected_for = list(report["blockers"])
        if report["diversity_violations"] and not allow_diversity_fail:
            rejected_for.append(
                f"diversity: pairwise >= {mutual_threshold} with "
                + ", ".join(v["alpha_id"] for v in report["diversity_violations"])
            )

        if rejected_for and not force:
            return {
                "action": "add",
                "added": False,
                "alpha_id": alpha_id,
                "reasons": rejected_for,
                "report": report,
                "hint": "Pass force=true to admit anyway (records forced_reasons on the entry).",
            }

        previous = (pool.get("entries") or {}).get(alpha_id) or {}
        entry = dict(report["summary"])
        entry.update({
            "prod_corr": report["prod_corr"],
            # A value carried over from the entry keeps the timestamp that value
            # was actually measured at; overwriting it with None would erase the
            # only evidence of how stale the number is.
            "prod_corr_checked_at": (
                previous.get("prod_corr_checked_at")
                if report.get("prod_corr_status") == "cached"
                else _now()
            ),
            "self_corr": report["self_corr"],
            "max_pairwise_vs_pool": report["max_pairwise_vs_pool"],
            "note": note if note is not None else previous.get("note"),
            "added_at": previous.get("added_at") or _now(),
            "forced": bool(rejected_for),
            "forced_reasons": rejected_for or None,
        })

        def _apply(fresh: Dict[str, Any]) -> bool:
            entries = fresh.setdefault("entries", {})
            if frozenset(entries) - {alpha_id} != snapshot_ids:
                raise PoolChangedError()
            entries[alpha_id] = entry
            return True

        try:
            pool, _ = _mutate_pool(_apply)
        except PoolChangedError:
            continue        # someone else wrote to the pool; re-evaluate against it
        break
    else:
        return {
            "action": "add",
            "added": False,
            "alpha_id": alpha_id,
            "reasons": ["the pool kept changing under this evaluation (3 attempts)"],
            "hint": "Retry when concurrent pool writes have settled.",
        }

    return {
        "action": "add",
        "added": True,
        "alpha_id": alpha_id,
        "entry": entry,
        "warnings": report["warnings"],
        "forced": bool(rejected_for),
        "forced_reasons": rejected_for or None,
        "pool_size": len(pool["entries"]),
    }


def remove_candidates(alpha_ids: Iterable[str]) -> Dict[str, Any]:
    ids = list(alpha_ids)
    removed: List[str] = []
    absent: List[str] = []

    def _apply(pool: Dict[str, Any]) -> bool:
        entries = pool.setdefault("entries", {})
        for aid in ids:
            if entries.pop(aid, None) is not None:
                removed.append(aid)
            else:
                absent.append(aid)
        return bool(removed)

    pool, _ = _mutate_pool(_apply)
    return {"action": "remove", "removed": removed, "not_found": absent,
            "pool_size": len(pool.get("entries") or {})}


def list_pool(
    region: Optional[str] = None,
    pyramid: Optional[str] = None,
) -> Dict[str, Any]:
    """Pooled candidates, grouped region -> pyramid, best Sharpe first inside each."""
    pool = load_pool()
    entries = pool.get("entries", {})
    rows = []
    for aid, e in entries.items():
        if region and str(e.get("region", "")).upper() != region.upper():
            continue
        if pyramid and str(e.get("pyramid", "")).lower() != pyramid.lower():
            continue
        rows.append(e)
    rows.sort(key=lambda e: (
        str(e.get("region") or ""),
        str(e.get("pyramid") or ""),
        -( (e.get("metrics") or {}).get("sharpe") or 0.0),
    ))
    return {
        "pool_size": len(entries),
        "returned": len(rows),
        "filters": {"region": region, "pyramid": pyramid},
        "entries": rows,
        "path": str(pool_path()),
    }


# --------------------------------------------------------------------------- #
# Pyramid coverage: submitted + pooled
# --------------------------------------------------------------------------- #

def _flatten_submitted(
    pyramid_alphas: Dict[str, Any],
    region: Optional[str],
    delay: Optional[int],
) -> Dict[str, int]:
    """Collapse a pyramid-alphas payload into {category: submitted_count}.

    Handles both shapes this codebase produces:
      * RAW  (brain_client): {"pyramids": [{category:{id,..}, region, delay, alphaCount}, ...]}
      * SLIM (main._slim_pyramids): {"pyramids": {region: {"D1": {cat: n}}}}
    """
    counts: Dict[str, int] = {}
    root = (pyramid_alphas or {}).get("pyramids")

    def bump(cat: Any, n: Any) -> None:
        if not cat:
            return
        try:
            counts[str(cat)] = counts.get(str(cat), 0) + int(n or 0)
        except (TypeError, ValueError):
            pass

    if isinstance(root, list):  # raw
        for p in root:
            if not isinstance(p, dict):
                continue
            if region and str(p.get("region", "")).upper() != region.upper():
                continue
            if delay is not None and p.get("delay") != delay:
                continue
            cat = p.get("category")
            bump(cat.get("id") if isinstance(cat, dict) else cat, p.get("alphaCount"))
        return counts

    if isinstance(root, dict):  # slim
        for reg, by_delay in root.items():
            if region and str(reg).upper() != region.upper():
                continue
            if not isinstance(by_delay, dict):
                continue
            for dkey, cats in by_delay.items():
                if delay is not None and str(dkey).upper() != f"D{delay}":
                    continue
                if not isinstance(cats, dict):
                    continue
                for cat, n in cats.items():
                    bump(cat, n)
    return counts


async def pyramid_coverage(
    client: Any,
    *,
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = DEFAULT_PYRAMID_TARGET,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """The 'true coverage' table: submitted + pool, per pyramid.

    ``target`` is how many SUBMITTED alphas a pyramid needs before it counts as
    lit. Pool entries do not light a pyramid on their own — they are the queue
    that can get it there, which is exactly why they are reported alongside.
    """
    pyramid_alphas = await client.get_pyramid_alphas(start_date, end_date)
    submitted = _flatten_submitted(pyramid_alphas, region, delay)

    pool = load_pool()
    pooled: Dict[str, int] = {}
    for e in pool.get("entries", {}).values():
        if region and str(e.get("region", "")).upper() != region.upper():
            continue
        if delay is not None and e.get("delay") != delay:
            continue
        cat = (e.get("pyramid") or "unknown").lower()
        pooled[cat] = pooled.get(cat, 0) + 1

    known = [c for c in PYRAMID_ORDER if c in submitted or c in pooled]
    extra = sorted((set(submitted) | set(pooled)) - set(PYRAMID_ORDER))
    cats = list(dict.fromkeys(known + extra))

    rows = []
    lit = 0
    reachable = 0
    counted = 0
    for cat in cats:
        sub = submitted.get(cat, 0)
        poo = pooled.get(cat, 0)
        need = max(0, target - sub)
        if cat == "unknown":
            # Pooled entries whose record carried no pyramid. They are NOT a
            # pyramid, so they must not be counted as one (they used to show up
            # as a permanently short 18th tower) — just reported so the missing
            # metadata is visible.
            rows.append({
                "pyramid": cat,
                "label": "Unclassified (no pyramid on the record)",
                "submitted": sub,
                "pool": poo,
                "total": sub + poo,
                "needed_submissions": None,
                "status": "UNCLASSIFIED",
                "counts_as_pyramid": False,
            })
            continue
        counted += 1
        if need == 0:
            status = "OS_SUFFICIENT"
            lit += 1
            reachable += 1
        elif poo >= need:
            status = f"NEEDS_{need}_SUBMISSIONS_FROM_POOL"
            reachable += 1
        else:
            status = f"SHORT_BY_{need - poo}_CANDIDATES"
        rows.append({
            "pyramid": cat,
            "label": PYRAMID_LABELS.get(cat, cat),
            "submitted": sub,
            "pool": poo,
            "total": sub + poo,
            "needed_submissions": need,
            "status": status,
        })

    return {
        "scope": {"region": region, "delay": delay, "target_per_pyramid": target},
        "rows": rows,
        "totals": {
            "pyramids": counted,
            "lit_by_submitted": lit,
            "reachable_with_pool": reachable,
            "not_reachable": counted - reachable,
            "submitted_alphas": sum(submitted.values()),
            "pooled_alphas": sum(pooled.values()),
            "unclassified_pool_entries": pooled.get("unknown", 0),
        },
        "note": (
            "A pyramid lights on SUBMITTED alphas only. 'reachable_with_pool' counts "
            "pyramids that would light if the listed pool candidates were submitted."
        ),
    }


# --------------------------------------------------------------------------- #
# Submission planning
# --------------------------------------------------------------------------- #

async def submission_plan(
    client: Any,
    *,
    max_submissions: int = DEFAULT_DAILY_SUBMIT_CAP,
    region: Optional[str] = None,
    delay: Optional[int] = None,
    target: int = DEFAULT_PYRAMID_TARGET,
    prod_threshold: float = DEFAULT_PROD_THRESHOLD,
    self_threshold: float = DEFAULT_SELF_THRESHOLD,
    resolve_conflicts: bool = False,
    respect_daily_cap: bool = True,
    years: int = 4,
) -> Dict[str, Any]:
    """Choose today's submission batch and prove it is safe for the rest of the pool.

    Safety proof, for every candidate B left in the pool after submitting S:
        projected_prod_corr(B) = max(prod_corr(B), max_{A in S} |corr(A,B)|)
        projected_self_corr(B) = max(self_corr(B), max_{A in S} |corr(A,B)|)
    Both must stay under their thresholds; a candidate that would be pushed over
    is reported in ``collateral_damage`` and the batch is trimmed to avoid it.
    A candidate whose projection cannot be computed (missing pairwise PnL, or its
    own prod/self correlation never recorded) is reported with
    ``still_safe: null`` and makes ``all_remaining_safe`` null rather than true —
    an unknown correlation is never treated as a zero.

    A pool that was forced to accept a mutually-exclusive pair would otherwise
    deadlock — neither member can be submitted without destroying the other, so
    a purely protective planner submits neither, forever. Such pairs are surfaced
    in ``conflicts`` with a recommended keep/drop. ``resolve_conflicts=True`` acts
    on that recommendation: it submits the higher-priority member and marks the
    loser ``sacrificed`` (it stays in the pool; removing it is your call).

    Ranking asks "does this batch FINISH a pyramid", re-evaluated before every
    pick — see ``priority``. ``max_submissions`` is treated as the day's budget:
    with ``respect_daily_cap`` the alphas already submitted today are subtracted
    from it (``submission_budget`` reports the arithmetic). Entries whose
    ``prod_corr`` was measured before the newest submission are listed in
    ``stale_prod_corr`` — that number can only have gone up since.
    """
    pool = load_pool()
    entries: Dict[str, Any] = pool.get("entries", {})

    def in_scope(e: Dict[str, Any]) -> bool:
        if region and str(e.get("region", "")).upper() != region.upper():
            return False
        if delay is not None and e.get("delay") != delay:
            return False
        return True

    scoped = {aid: e for aid, e in entries.items() if in_scope(e)}
    if not scoped:
        return {"plan": [], "reason": "no pool candidates in scope",
                "scope": {"region": region, "delay": delay}}

    # The daily cap is account-wide, so today's submissions are counted across
    # every region — not just the scope being planned.
    context = await _submission_context(client, entries, max_submissions)
    stale_prod_corr = context.pop("stale_prod_corr", [])
    slots_left_today = context.get("slots_left_today")
    if respect_daily_cap and isinstance(slots_left_today, int):
        budget = min(max_submissions, slots_left_today)
    else:
        budget = max_submissions
    submission_budget = dict(context, max_submissions=max_submissions,
                             applied=budget, respect_daily_cap=respect_daily_cap)
    if budget <= 0:
        return {
            "scope": {"region": region, "delay": delay, "max_submissions": max_submissions,
                      "target_per_pyramid": target},
            "plan": [],
            "plan_size": 0,
            "reason": (f"today's submission budget is used up "
                       f"({context.get('submitted_today')} of {max_submissions} submitted)"),
            "submission_budget": submission_budget,
            "stale_prod_corr": stale_prod_corr or None,
            "note": "Nothing to plan until the daily cap resets. "
                    "Pass respect_daily_cap=false to plan anyway.",
        }

    # Full pairwise matrix across the WHOLE pool (out-of-scope entries can still
    # be damaged by an in-scope submission, so they must be considered too).
    all_ids = list(entries.keys())
    corr: Dict[str, Dict[str, float]] = {}
    missing_pairs: List[str] = []
    if len(all_ids) >= 2:
        res = await client.get_mutual_correlation(all_ids, threshold=1.1, years=years)
        if res.get("error"):
            missing_pairs = all_ids
        else:
            matrix = res.get("matrix") or {}
            for a in all_ids:
                corr[a] = {b: abs(float(v)) for b, v in (matrix.get(a) or {}).items()
                           if b != a and v is not None}
            missing_pairs = list(res.get("missing_pnl") or [])

    def pair(a: str, b: str) -> Optional[float]:
        return (corr.get(a) or {}).get(b)

    # Coverage need drives priority: a submission that lights a pyramid beats one
    # that adds a fourth alpha to an already-lit pyramid.
    coverage = await pyramid_coverage(
        client, region=region, delay=delay, target=target
    )
    need_by_pyramid = {r["pyramid"]: r["needed_submissions"] for r in coverage["rows"]}

    def _cat(aid: str) -> str:
        return (scoped[aid].get("pyramid") or "unknown").lower()

    def priority(aid: str, slots_left: int, remaining: Dict[str, int],
                 available: Dict[str, int]) -> Tuple[int, int, float, float]:
        """Rank a candidate for the NEXT slot. Higher is better.

        A pyramid lights only when ``need`` more of its alphas are submitted, so
        the value of a submission is 'does this batch finish a pyramid', not 'is
        this pyramid far from lit'. Ranking by need descending (the previous rule)
        did the opposite and burned a whole batch on one distant pyramid: with 4
        slots, analyst needing 3 and news needing 1, all four went to analyst and
        lit ONE pyramid where 3+1 would have lit two.

        Tiers: 3 = can be finished inside this batch (cheapest need first),
        2 = progress on an unlit pyramid, 1 = pyramid already lit.
        """
        e = scoped[aid]
        cat = _cat(aid)
        need = remaining.get(cat, 0)
        pooled = available.get(cat, 0)
        if need <= 0:
            tier = 1
        elif need <= slots_left and pooled >= need:
            tier = 3
        else:
            tier = 2
        sharpe = (e.get("metrics") or {}).get("sharpe") or 0.0
        mult = e.get("pyramid_multiplier") or 1.0
        # -need so the cheapest lighting comes first within a tier.
        return (tier, -need, mult, sharpe)

    def availability(pending: Iterable[str]) -> Dict[str, int]:
        """How many still-unprocessed candidates each pyramid has left."""
        out: Dict[str, int] = {}
        for aid in pending:
            cat = _cat(aid)
            out[cat] = out.get(cat, 0) + 1
        return out

    # Static order at full budget: the priority a candidate has before anything
    # is picked. Used for the conflict keep/drop recommendation, which must be a
    # stable statement about two candidates rather than an artefact of ordering.
    ordered = sorted(
        scoped.keys(),
        key=lambda a: priority(a, budget, need_by_pyramid, availability(scoped)),
        reverse=True,
    )
    rank = {aid: i for i, aid in enumerate(ordered)}

    selected: List[str] = []
    skipped: List[Dict[str, Any]] = []
    sacrificed: List[str] = []
    sacrificed_for: Dict[str, str] = {}
    conflicts: List[Dict[str, Any]] = []
    unverifiable: List[Dict[str, Any]] = []
    remaining_need = dict(need_by_pyramid)

    def damage_from(
        aid: str, ignore: Sequence[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """(damaged, unverifiable) pool candidates for submitting ``aid``.

        A missing number is NOT zero: an entry whose pairwise correlation or own
        prod/self correlation is unknown cannot be proven safe, so it is reported
        separately instead of silently counting as harmless.
        """
        out: List[Dict[str, Any]] = []
        unknown: List[Dict[str, Any]] = []
        for other, oe in entries.items():
            if other == aid or other in selected or other in ignore:
                continue
            c = pair(aid, other)
            if c is None:
                unknown.append({"alpha_id": other, "victim_of": aid,
                                "reason": "pairwise correlation unavailable"})
                continue
            prod_now = _num(oe.get("prod_corr"))
            self_now = _num(oe.get("self_corr"))
            proj_prod = max(prod_now, c) if prod_now is not None else c
            proj_self = max(self_now, c) if self_now is not None else c
            if proj_prod >= prod_threshold or proj_self >= self_threshold:
                out.append({
                    "alpha_id": other,
                    "pairwise": round(c, 4),
                    "projected_prod_corr": round(proj_prod, 4),
                    "projected_self_corr": round(proj_self, 4),
                })
            elif prod_now is None or self_now is None:
                unknown.append({"alpha_id": other, "victim_of": aid,
                                "pairwise": round(c, 4),
                                "reason": "candidate's own prod/self correlation unknown"})
        return out, unknown

    # Batch members land in the production AND self pools together, so a pair
    # inside the batch has to clear BOTH gates: at pairwise >= self_threshold
    # each member lifts the other's self correlation over the line even though
    # the production gate is still satisfied.
    batch_gate = min(prod_threshold, self_threshold)

    # Re-rank before every pick: a slot spent on a pyramid lowers that pyramid's
    # remaining need, which is exactly the input the ranking depends on. Walking a
    # single up-front ordering left `remaining_need` computed but never read.
    pending = list(ordered)
    while pending:
        if len(selected) >= budget:
            break
        slots_left = budget - len(selected)
        avail = availability(pending)
        pending.sort(key=lambda a: priority(a, slots_left, remaining_need, avail), reverse=True)
        aid = pending.pop(0)
        if aid in sacrificed:
            # Already given up on to make room for a higher-priority candidate in
            # this very batch — submitting it anyway would recreate exactly the
            # collision the sacrifice was meant to avoid.
            skipped.append({"alpha_id": aid,
                            "reason": f"sacrificed in favour of {sacrificed_for.get(aid, 'a higher-priority candidate')}",
                            "sacrificed": True})
            continue
        e = scoped[aid]

        clash = next(
            ((s, pair(aid, s)) for s in selected
             if (pair(aid, s) is not None and pair(aid, s) >= batch_gate)),
            None,
        )
        if clash:
            gate = "prod" if clash[1] >= prod_threshold else "self"
            skipped.append({"alpha_id": aid,
                            "reason": (f"pairwise {clash[1]:.4f} >= {gate}_threshold "
                                       f"with selected {clash[0]}"),
                            "correlation": round(clash[1], 4)})
            continue

        damage, damage_unknown = damage_from(aid, ignore=sacrificed)
        unverifiable.extend(damage_unknown)
        if damage:
            # A victim that ranks BELOW aid is a genuine either/or: the pool can
            # ship aid or that victim, never both. Record the trade-off, and take
            # it only when the caller asked us to.
            losers = [d["alpha_id"] for d in damage if rank.get(d["alpha_id"], -1) > rank[aid]]
            blockers = [d for d in damage if d["alpha_id"] not in losers]
            for d in damage:
                if d["alpha_id"] in losers:
                    conflicts.append({
                        "keep": aid, "drop": d["alpha_id"],
                        "correlation": d["pairwise"],
                        "reason": (
                            f"mutually exclusive: submitting {aid} lifts {d['alpha_id']} "
                            f"to prod {d['projected_prod_corr']} / self {d['projected_self_corr']}"
                        ),
                        "recommendation": (
                            f"keep {aid} (higher pyramid need / multiplier / Sharpe), "
                            f"remove {d['alpha_id']} from the pool"
                        ),
                    })
            if blockers or not resolve_conflicts:
                skipped.append({
                    "alpha_id": aid,
                    "reason": "would push other candidates past a gate",
                    "collateral_damage": damage,
                    "resolvable": bool(losers) and not blockers,
                })
                continue
            sacrificed.extend(losers)
            for loser in losers:
                sacrificed_for[loser] = aid

        selected.append(aid)
        remaining_need[_cat(aid)] = max(0, remaining_need.get(_cat(aid), 0) - 1)

    # Report the post-submission state of everything left behind. ``still_safe``
    # is deliberately three-valued: True (proven under both gates), False (proven
    # over one), None (cannot be computed — a missing correlation is not a zero).
    projected = []
    for other, oe in entries.items():
        if other in selected:
            continue
        vals = [(s, pair(s, other)) for s in selected]
        unknown_vs = [s for s, v in vals if v is None]
        known = [v for _, v in vals if v is not None]
        top = max(known) if known else None
        prod_now = _num(oe.get("prod_corr"))
        self_now = _num(oe.get("self_corr"))
        proj_prod = max([v for v in (prod_now, top) if v is not None], default=None)
        proj_self = max([v for v in (self_now, top) if v is not None], default=None)
        breached = (
            (proj_prod is not None and proj_prod >= prod_threshold)
            or (proj_self is not None and proj_self >= self_threshold)
        )
        if breached:
            still_safe: Optional[bool] = False
        elif unknown_vs or prod_now is None or self_now is None:
            still_safe = None
        else:
            still_safe = True
        projected.append({
            "alpha_id": other,
            "pyramid": oe.get("pyramid"),
            "prod_corr_now": oe.get("prod_corr"),
            "prod_corr_checked_at": oe.get("prod_corr_checked_at"),
            "max_pairwise_vs_batch": round(top, 4) if top is not None else None,
            "projected_prod_corr": round(proj_prod, 4) if proj_prod is not None else None,
            "projected_self_corr": round(proj_self, 4) if proj_self is not None else None,
            "still_safe": still_safe,
            "unknown_pairwise_vs": unknown_vs or None,
            "sacrificed": other in sacrificed,
        })
    projected.sort(key=lambda r: -(r["projected_prod_corr"] or 0.0))

    live = [r for r in projected if not r["sacrificed"]]
    unprovable_for = [r["alpha_id"] for r in live if r["still_safe"] is None]
    if any(r["still_safe"] is False for r in live):
        all_remaining_safe: Optional[bool] = False
    elif unprovable_for:
        all_remaining_safe = None
    else:
        all_remaining_safe = True

    plan = []
    for i, aid in enumerate(selected, 1):
        e = scoped[aid]
        cat = _cat(aid)
        plan.append({
            "order": i,
            "alpha_id": aid,
            "pyramid": e.get("pyramid"),
            "region": e.get("region"),
            "delay": e.get("delay"),
            "sharpe": (e.get("metrics") or {}).get("sharpe"),
            "prod_corr": e.get("prod_corr"),
            "prod_corr_checked_at": e.get("prod_corr_checked_at"),
            "self_corr": e.get("self_corr"),
            "pyramid_need_before": need_by_pyramid.get(cat, 0),
            "lights_pyramid": (
                cat != "unknown"
                and need_by_pyramid.get(cat, 0) > 0
                and remaining_need.get(cat, 0) == 0
            ),
        })

    return {
        "scope": {"region": region, "delay": delay, "max_submissions": max_submissions,
                  "target_per_pyramid": target},
        "thresholds": {"prod": prod_threshold, "self": self_threshold},
        "submission_budget": submission_budget,
        "stale_prod_corr": stale_prod_corr or None,
        "plan": plan,
        "plan_size": len(plan),
        "skipped": skipped,
        "conflicts": conflicts,
        "sacrificed": sacrificed,
        "remaining_pool_after_batch": projected,
        # Deliberately sacrificed candidates are excluded: they were given up on
        # purpose, so counting them as breakage would hide real breakage.
        # None means "not provable" — with no correlation data at all the honest
        # answer is 'unknown', never the True that an all() over zero facts gives.
        "all_remaining_safe": all_remaining_safe,
        "unprovable_for": unprovable_for or None,
        "unverifiable": unverifiable or None,
        "pairwise_unavailable_for": missing_pairs,
        "coverage_before": coverage["rows"],
        "note": (
            "The agent does NOT submit. This is a plan; submit manually. "
            "projected_prod_corr = max(current prod corr, |corr| vs the submitted batch). "
            + (
                f"{len(stale_prod_corr)} candidate(s) carry a prod_corr measured before the "
                f"newest submission ({context.get('newest_submission_at')}) — it can only have "
                "risen; run pool_sync refresh_prod=true to re-measure. "
                if stale_prod_corr else ""
            )
            + (
                f"all_remaining_safe is null: {len(unprovable_for)} candidate(s) cannot be "
                "proven safe because a correlation is missing (see unprovable_for / "
                "pairwise_unavailable_for) — treat this as NOT safe until refreshed. "
                if all_remaining_safe is None else ""
            )
            + (
                f"{len(conflicts)} mutually-exclusive pair(s) found — rerun with "
                "resolve_conflicts=true to submit the recommended member and give up the other."
                if conflicts and not resolve_conflicts else ""
            )
        ).strip(),
    }


async def _submission_context(
    client: Any, entries: Dict[str, Any], daily_cap: int
) -> Dict[str, Any]:
    """Today's remaining submission slots + which stored prod_corr values are stale.

    Both answers come from ONE submitted-alpha listing (the same 1-2 pages
    ``sync_pool`` uses), because both are questions about what has been submitted
    recently:

    * the daily cap is a budget, and alphas already submitted today have spent
      part of it — planning 4 when 2 are gone plans two submissions that cannot
      happen;
    * an entry's production correlation was measured against the production pool
      as it stood at ``prod_corr_checked_at``; any submission after that moment
      can only have raised it, so the stored number is a lower bound, not a fact.
    """
    out: Dict[str, Any] = {
        "submitted_today": None,
        "slots_left_today": None,
        "newest_submission_at": None,
        "stale_prod_corr": [],
        "source": "unavailable",
    }
    if not hasattr(client, "get_submitted_ids_since"):
        return out

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    checks = [_parse_ts(e.get("prod_corr_checked_at")) for e in entries.values()]
    known_checks = [t for t in checks if t]
    since = min([today_start] + known_checks)
    try:
        listing = await client.get_submitted_ids_since(since.isoformat())
    except Exception as exc:  # noqa: BLE001 - planning must not fail over this
        out["error"] = str(exc)
        return out

    rows = listing.get("rows") or {}
    stamps = [_parse_ts((rows.get(aid) or {}).get("dateSubmitted"))
              for aid in (listing.get("ids") or [])]
    stamps = [t for t in stamps if t]
    out["source"] = "submitted-list"

    if stamps:
        out["newest_submission_at"] = max(stamps).isoformat()
        out["submitted_today"] = sum(1 for t in stamps if t >= today_start)
        out["slots_left_today"] = max(0, daily_cap - out["submitted_today"])
    elif not (listing.get("ids") or []):
        # Nothing submitted in the window at all — the cap is untouched.
        out["submitted_today"] = 0
        out["slots_left_today"] = daily_cap
    else:
        # Ids without usable timestamps: refuse to guess how many were today.
        out["note"] = "submission timestamps unavailable; daily cap not adjusted"

    newest = max(stamps) if stamps else None
    for aid, e in entries.items():
        if e.get("prod_corr") is None:
            continue                      # already reported as unknown, not stale
        checked = _parse_ts(e.get("prod_corr_checked_at"))
        if newest is not None and (checked is None or checked < newest):
            out["stale_prod_corr"].append({
                "alpha_id": aid,
                "prod_corr": e.get("prod_corr"),
                "prod_corr_checked_at": e.get("prod_corr_checked_at"),
                "submissions_since": sum(
                    1 for t in stamps if checked is None or t > checked),
            })
    return out


async def _refresh_prod_corrs(client: Any, aids: Sequence[str]) -> Dict[str, Any]:
    """Re-query production correlation for ``aids``, honestly.

    The platform admits ONE correlation check per account per ~3 minutes, so a
    loop over N entries cannot refresh N entries: everything after the first
    comes back ``correlation_busy``. Silently skipping those (the previous
    behaviour) reported a refresh that never happened, so the first busy answer
    stops the loop and the untried entries are named instead.

    ``force_refresh`` is used because the caller's reason for refreshing —
    something got submitted — is exactly what makes the client's 5-minute
    cached value wrong.
    """
    values: Dict[str, float] = {}
    updated: List[str] = []
    busy: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []
    not_attempted: List[str] = []
    retry_after: Optional[int] = None

    pending = list(aids)
    while pending:
        aid = pending.pop(0)
        prod = await fetch_production_correlation(client, aid, force_refresh=True)
        if prod["value"] is not None:
            values[aid] = prod["value"]
            updated.append(aid)
            continue
        if prod.get("status") in ("correlation_busy", "pending"):
            busy.append({"alpha_id": aid, "status": prod.get("status"),
                         "retry_after": prod.get("retry_after")})
            try:
                retry_after = int(prod.get("retry_after") or 0) or None
            except (TypeError, ValueError):
                retry_after = None
            not_attempted = pending           # the slot is gone for retry_after seconds
            break
        unavailable.append({"alpha_id": aid, "status": prod.get("status"),
                            "message": prod.get("message")})

    return {
        "values": values,
        "report": {
            "updated": updated,
            "busy": busy,
            "unavailable": unavailable,
            "not_attempted": not_attempted,
            "retry_after": retry_after,
            "note": (
                f"{len(updated)} refreshed; stopped at the per-account correlation slot "
                f"({len(busy)} busy, {len(not_attempted)} not attempted). "
                f"Retry in {retry_after}s." if busy else f"{len(updated)} refreshed."
            ),
        },
    }


async def sync_pool(
    client: Any,
    *,
    refresh_prod: bool = False,
    refresh_details: bool = False,
) -> Dict[str, Any]:
    """Refresh entries: drop alphas that are now submitted.

    ``refresh_details`` re-reads every entry's record (one request per entry).
    It is off by default because a pooled alpha's record is frozen apart from
    being submitted, which is detected far more cheaply from the submitted-alpha
    list. Turn it on to also pick up manual edits or platform-side deletions.

    ``refresh_prod`` re-queries production correlation, bypassing the client's
    5-minute cache. That endpoint admits one check per account per ~3 minutes, so
    a multi-entry pool CANNOT be fully refreshed in one call: the sweep stops at
    the first busy answer and reports what it did and did not touch in
    ``prod_refresh`` (``updated`` / ``busy`` / ``not_attempted`` / ``retry_after``).

    Writes land under the pool lock, so a concurrent ``pool_add`` is not lost.
    """
    pool = load_pool()
    entries: Dict[str, Any] = pool.get("entries", {})
    promoted, refreshed, errors = [], [], []
    aids = list(entries.keys())
    prod_refresh: Optional[Dict[str, Any]] = None

    def _commit(drop: Sequence[str], updates: Dict[str, Dict[str, Any]],
                prod_values: Dict[str, float]) -> Dict[str, Any]:
        """Apply this sweep's findings to the CURRENT pool, under the lock."""
        def _apply(fresh: Dict[str, Any]) -> bool:
            fresh_entries = fresh.setdefault("entries", {})
            for aid in drop:
                fresh_entries.pop(aid, None)
            for aid, fields in updates.items():
                if aid in fresh_entries:
                    fresh_entries[aid].update(fields)
            stamp = _now()
            for aid, value in prod_values.items():
                if aid in fresh_entries:
                    fresh_entries[aid]["prod_corr"] = value
                    fresh_entries[aid]["prod_corr_checked_at"] = stamp
            return True

        fresh, _ = _mutate_pool(_apply)
        return fresh

    # A pooled alpha is in-sample, and an in-sample alpha's record is produced by
    # its simulation: re-simulating yields a NEW id, so code, settings, pyramid
    # and every IS metric are frozen for the life of the entry (verified against
    # live records: 0 of the sampled entries had drifted). The single thing that
    # can change is that it got submitted.
    #
    # So the sweep asks the cheap question directly — "what was submitted since
    # this pool's oldest entry was added?" — which is 1-2 list pages no matter
    # how large the pool is, instead of one record fetch per entry.
    cheap_path = aids and not refresh_details and hasattr(client, "get_submitted_ids_since")
    if cheap_path:
        added = [e.get("added_at") for e in entries.values() if e.get("added_at")]
        try:
            submitted = await client.get_submitted_ids_since(min(added) if added else None)
            submitted_ids = set(submitted.get("ids") or [])
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "sync",
                "error": f"could not list submitted alphas: {exc}",
                "hint": "retry, or call with refresh_details=True to read every entry individually",
                "promoted_to_submitted": [],
                "refreshed": [],
                "errors": [{"error": str(exc)}],
                "pool_size": len(entries),
            }
        for aid in aids:
            if aid in submitted_ids:
                promoted.append(aid)
            else:
                refreshed.append(aid)
        prod_values: Dict[str, float] = {}
        if refresh_prod and refreshed:
            outcome = await _refresh_prod_corrs(client, refreshed)
            prod_values = outcome["values"]
            prod_refresh = outcome["report"]
        pool = _commit(promoted, {}, prod_values)
        return {
            "action": "sync",
            "mode": "submitted-list",
            "promoted_to_submitted": promoted,
            "refreshed": refreshed,
            "errors": errors,
            "pool_size": len(pool.get("entries") or {}),
            "refresh_prod": refresh_prod,
            "prod_refresh": prod_refresh,
            "requests_used": submitted.get("pages"),
            "note": ("Detected promotions from the submitted-alpha list "
                     f"({len(submitted_ids)} submitted since {submitted.get('since')}). "
                     "Pass refresh_details=True to re-read every entry's record instead."),
        }

    # Full re-read: every entry's record in one concurrent batch. Independent GETs
    # on a rate-limited endpoint, so the client paces them; doing them one at a
    # time made a sweep cost (pool size x round trip) of pure latency.
    async def _details(aid: str):
        try:
            return aid, await client.get_alpha_details(aid, force_refresh=True), None
        except Exception as exc:  # noqa: BLE001 - report, don't abort the sweep
            return aid, None, exc

    fetched = await asyncio.gather(*[_details(aid) for aid in aids])

    updates: Dict[str, Dict[str, Any]] = {}
    for aid, details, exc in fetched:
        if exc is not None:
            errors.append({"alpha_id": aid, "error": str(exc)})
            continue
        summary = summarize_alpha(details or {})
        if summary.get("status") == "ACTIVE":
            promoted.append(aid)
            continue
        updates[aid] = {k: v for k, v in summary.items() if v is not None}
        refreshed.append(aid)

    prod_values = {}
    if refresh_prod and refreshed:
        outcome = await _refresh_prod_corrs(client, refreshed)
        prod_values = outcome["values"]
        prod_refresh = outcome["report"]

    pool = _commit(promoted, updates, prod_values)
    return {
        "action": "sync",
        "mode": "full-refresh",
        "promoted_to_submitted": promoted,
        "refreshed": refreshed,
        "errors": errors,
        "pool_size": len(pool.get("entries") or {}),
        "refresh_prod": refresh_prod,
        "prod_refresh": prod_refresh,
    }
