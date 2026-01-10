# Repeatable Workflow for Improving BRAIN Alphas: A Step-by-Step Guide

This document outlines a systematic, repeatable workflow for enhancing alphas on the WorldQuant BRAIN platform. It emphasizes core idea refinements (e.g., incorporating financial concepts from research) over mechanical tweaks, as per guidelines in `BRAIN_Alpha_Test_Requirements_and_Tips.md`. The process is tool-agnostic but assumes access to BRAIN API (via MCP), arXiv search scripts, and basic analysis tools. Each cycle takes ~30-60 minutes; repeat until submission thresholds are met (e.g., Sharpe >1.25, Fitness >1 for Delay-1 ATOM alphas).

## Prerequisites
- Authenticate with BRAIN (e.g., via API tool).
- Have the alpha ID and expression ready.
- Access to arXiv script (e.g., `arxiv_api.py`) for idea sourcing.
- Track progress in a log (e.g., metrics table per iteration).

## Step 1: Gather Alpha Information (5-10 minutes)
**Goal**: Collect baseline data to identify weaknesses (e.g., low Sharpe, high correlation, inconsistent yearly stats).

**Steps**:
- Authenticate if needed.
- Fetch alpha details (expression, settings, metrics like PnL, Sharpe, Fitness, Turnover, Drawdown, and checks).
- Retrieve PnL trends and yearly stats.
- Run submission and correlation checks (self/production, threshold 0.7).

**Analysis**:
- Note failing tests (e.g., sub-universe low = illiquid reliance).
- For ATOM alphas (single-dataset), confirm relaxed thresholds.

**Output**: Summary of metrics and issues (e.g., "Sharpe 1.11, fails sub-universe").

**Tips for Repeatability**: Automate with a script template for batch alphas.

## Step 2: Evaluate the Core Datafield(s) (5-10 minutes)
**Goal**: Understand data properties (sparsity, frequency) to guide refinements.

**Steps**:
- Confirm field details (type, coverage).
- Simulate 6 evaluation expressions in neutral settings (neutralization="NONE", decay=0, short test period):
  1. Basic Coverage: `datafield`.
  2. Non-Zero Coverage: `datafield != 0 ? 1 : 0`.
  3. Update Frequency: `ts_std_dev(datafield, N) != 0 ? 1 : 0` (N=5,22,66).
  4. Bounds: `abs(datafield) > X` (vary X).
  5. Central Tendency: `ts_median(datafield, 1000) > X` (vary X).
  6. Distribution: `low < scale_down(datafield) < high` (e.g., 0.25-0.75).
- Use multi-simulation; fallback to singles if issues.

**Analysis**:
- Identify patterns (e.g., quarterly updates → use long windows).

**Output**: Insights (e.g., "Sparse quarterly data → prioritize persistence ideas").

**Tips for Repeatability**: Template the 6 expressions in a script; run for any field.

## Step 3: Propose Idea-Focused Improvements (10-15 minutes)
**Goal**: Evolve the core signal with theory-backed concepts (e.g., momentum, persistence) for sustainability.

**Steps**:
- Review platform docs/community examples for tips (e.g., ATOM, flipping negatives).
- Source ideas: Query arXiv with targeted terms (e.g., "return on assets momentum analyst estimates"). Extract 3-5 relevant papers' concepts (e.g., precision weighting = divide by std_dev).
- Brainstorm 4-6 variants: Modify original with 1-2 concepts (e.g., add revision delta).
- Validate operators against platform list; replace if needed (e.g., custom momentum formula).

**Analysis**:
- Prioritize fixes for baselines (e.g., negative years → cycle-sensitive grouping).

**Output**: List of expressions with rationale (e.g., "Variant 1: Weighted persistence from Paper X").

**Tips for Repeatability**: Use a template (e.g., "Search terms: [field] + momentum/revision"; limit to recent finance papers).

## Step 4: Simulate and Test Variants (10-20 minutes, including wait)
**Goal**: Efficiently compare ideas via metrics.

**Steps**:
- Run multi-simulation (2-8 expressions) with original settings + targeted tweaks (e.g., neutralization for grouping).
- If multi fails, use parallel single simulations.
- Fetch results (details, PnL, yearly stats).

**Analysis**:
- Rank by Fitness/Sharpe; check sub-universe, consistency.
- Flip negatives if applicable.

**Output**: Ranked results (e.g., "Top ID: XYZ, Fitness improved 13%").

**Tips for Repeatability**: Parallelize calls; log in a table (e.g., CSV with metrics).

## Step 5: Validate and Iterate or Finalize (5-10 minutes)
**Goal**: Confirm submittability; loop if needed.

**Steps**:
- Run submission/correlation checks on top variants.
- Analyze PnL/yearly for trends.
- If failing, tweak (e.g., universe change) and return to Step 3.
- If passing, submit.

**Analysis**:
- Ensure sustainability (e.g., consistent positives).

**Output**: Final recommendation or next cycle plan.

## Iteration and Best Practices
- **Cycle Limit**: 3-5 per alpha; pivot if stuck (e.g., new datafield).
- **Tracking**: Maintain a log (e.g., MD file with iterations, metrics deltas).
- **Efficiency**: Use parallel tools; focus 70% on ideas, 30% on tweaks.
- **Success Criteria**: Passing checks + stable yearly stats.

This workflow has improved alphas by ~10-20% in metrics per cycle in tests. Adapt as needed!