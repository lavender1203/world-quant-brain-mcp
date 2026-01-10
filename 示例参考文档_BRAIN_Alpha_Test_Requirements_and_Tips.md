# BRAIN Alpha Submission Tests: Requirements and Improvement Tips

This document compiles the key requirements for passing alpha submission tests on the WorldQuant BRAIN platform, based on official documentation and community experiences from the forum. I've focused on the main tests (Fitness, Sharpe, Turnover, Weight, Sub-universe, and Self-Correlation). For each, I'll outline the thresholds, explanations, and strategies to improve or pass them, drawing from doc pages like "Clear these tests before submitting an Alpha" and forum searches on specific topics.

## Overview
## What is an Alpha?
An alpha is a mathematical model or signal designed to predict the future movements of financial instruments (e.g., stocks). On BRAIN, alphas are expressed using the platform's FASTEXPR language and simulated against historical data to evaluate performance. Successful alphas can earn payments and contribute to production strategies.

## What Are Alpha Tests?
Alphas must pass a series of pre-submission checks (e.g., via the `get_submission_check` tool) to ensure they meet quality thresholds. Key tests include:
- **Fitness and Sharpe Ratio**: Measures risk-adjusted returns. Must be above cutoffs (e.g., IS Sharpe > 1.25 for some universes).
- **Correlation Checks**: Against self-alphas and production alphas (threshold ~0.7) to avoid redundancy.
- **Turnover and Drawdown**: Ensures stability (e.g., low turnover < 250%).
- **Regional/Universe-Specific**: Vary by settings like USA TOP3000 (D1) or GLB TOP3000.
- **Other Metrics**: PnL, yearly stats, and risk-neutralized metrics (e.g., RAM, Crowding Risk-Neutralized).

Failing tests result in errors like "Sub-universe Sharpe NaN is not above cutoff" or low fitness.

## General Guidance on Passing Tests
- **Start Simple**: Use basic operators like `ts_rank`, `ts_corr`, or `neutralize` on price-volume data.
- **Optimize Settings**: Choose universes like TOP3000 (USA, D1) for easier testing. Neutralize against MARKET or SUBINDUSTRY to reduce correlation.
- **Improve Metrics**: Apply `ts_decay_linear` for stability, `scale` for normalization, and check with `check_correlation`.
- **Common Pitfalls**: Avoid high correlation (use `check_correlation`), ensure non-NaN data (e.g., via `ts_backfill`), and target high IR/Fitness.
- **Resources**: Review operators (e.g., 102 available like `ts_zscore`), documentation (e.g., "Interpret Results" section), and forum posts.

Alphas must pass these in-sample (IS) performance tests to be submitted for out-of-sample (OS) testing. Only submitted alphas contribute to scoring and payments. Tests are run in sequence, and failure messages guide improvements (e.g., "Improve fitness" or "Reduce max correlation").

## Generating and Improving Alpha Ideas: The Conceptual Foundation
Before diving into metrics and optimizations, strong alphas start with solid ideas rooted in financial theory, market behaviors, or data insights. Improving from an "idea angle" means iterating on the core concept rather than just tweaking parameters—this often leads to more robust alphas that pass tests naturally. Use resources like BRAIN's "Alpha Examples for Beginners" (from Discover BRAIN category) or forum-shared ideas.

### Key Principles
- **Idea Sources**: Draw from academic papers, economic indicators, or datasets (e.g., sentiment, earnings surprises). Validate ideas with backtests to ensure they generalize.
- **Iteration**: Start simple, then refine: Add neutralization for correlation, decay for stability, or grouping for diversification.
- **Avoid Overfitting**: Test ideas across universes/regions; use train/test splits.
- **Tools**: Explore datasets via Data Explorer; use operators like `ts_rank` for signals.

### Using arXiv for Idea Discovery
A powerful way to source fresh ideas is through academic papers on arXiv. Use the provided `arxiv_api.py` script (detailed in `arXiv_API_Tool_Manual.md`) to search and download relevant research.

- **Search Example**: Run `python arxiv_api.py "quantitative finance momentum strategies"` to find papers on momentum ideas. Download top results for detailed study.
- **Integration Tip**: Extract concepts like "earnings surprises" from abstracts, then implement in BRAIN (e.g., using sentiment datasets). This helps generate diverse alphas that pass correlation tests.
- **Why It Helps**: Papers often provide theoretical backing, reducing overfitting risks when adapting to BRAIN simulations.

Refer to the manual for interactive mode and advanced queries to streamline your research workflow.

### Avoid Mixing Datasets: The ATOM Principle
When improving an alpha, prioritize modifications that stay within the same dataset as the original. ATOM (Atomic) alphas are those built from a single dataset (excluding permitted grouping fields like country, sector, etc.), which qualify for relaxed submission criteria—focusing on last 2Y Sharpe instead of full IS Ladder tests.

**Why It's Important**:
- **Robustness**: Mixing datasets can introduce conflicting signals, leading to overfitting and poor out-of-sample performance (forum insights on ATOM alphas).
- **Submission Benefits**: Single-dataset alphas have easier thresholds (e.g., Delay-1: >1 for last 2Y Sharpe in USA) and may align with themes offering multipliers (up to x1.1 for low-utilization pyramids).
- **Correlation Control**: ATOM alphas often have lower self-correlation, helping pass tests and diversify your portfolio.

**How to Apply**:
- Check the alpha's data fields via simulation results or code.
- Search for improvements in the same dataset first (use Data Explorer).
- If mixing is needed, verify it doesn't disqualify ATOM status and retest thoroughly.

This principle, highlighted in BRAIN docs and forums, ensures alphas remain "atomic" and competitive.

### Understanding Datafields Before Improvements
Before optimizing alphas, thoroughly evaluate the datafields involved to address potential issues like unit mismatches or update frequencies. This prevents common pitfalls in tests (e.g., NaN errors, poor sub-universe performance) and ensures appropriate operators are used. Use these 6 methods from the BRAIN exploration guide (adapted for quick simulation in "None" neutralization, decay 0, test_period P0Y0M):

1. **Basic Coverage**: For example, Simulate `datafield` (or `vec_op(datafield)` for vectors). Insight: % coverage = (Long + Short Count) / Universe Size.
2. **Non-Zero Coverage**: For example, Simulate `datafield != 0 ? 1 : 0`. Insight: Actual meaningful data points.
3. **Update Frequency**: For example, Simulate `ts_std_dev(datafield, N) != 0 ? 1 : 0` (vary N=5,22,66). Insight: Daily/weekly/monthly/quarterly updates.
4. **Data Bounds**: For example, Simulate `abs(datafield) > X` (vary X). Insight: Value ranges and normalization.
5. **Central Tendency**: For example, Simulate `ts_median(datafield, 1000) > X` (vary X). Insight: Typical values over time.
6. **Distribution**: Simulate `X < scale_down(datafield) && scale_down(datafield) < Y` (vary X/Y between 0-1). Insight: Data spread patterns.

Apply insights to choose operators (e.g., ts_backfill for sparse data, scale for unit issues) and fix problems before improvements.

### Examples from Community and Docs (From Alpha Template Sharing Post)
These examples are sourced from the forum post on sharing unique alpha ideas and implementations, emphasizing templates that generate robust signals for passing submission tests.

- **Multi-Smoothing Ranking Signal** (User: JB71859): For earnings data, apply double smoothing with ranking and statistical ops. Example: `ts_mean(ts_rank(earnings_field, decay1), decay2)`. First ts_rank normalizes values over time (pre-processing), then ts_mean smooths for stable signals (main signal). Helps improve fitness and reduce turnover by lowering noise; produced 3 ATOM alphas after 2000 simulations.
- **Momentum Divergence Factor** (User: YK49234): Capture divergence between short and long-term momentum on the same field. Example: `ts_delta(ts_zscore(field, short_window), short_window) - ts_delta(ts_zscore(field, long_window), long_window)`. Processes data with z-scoring for normalization, then delta/mean for change detection (main signal). Boosts Sharpe by highlighting momentum shifts; yielded 4 submitable alphas from 20k tests with ~5% signal rate.
- **Network Factor Difference Momentum** (User: JR23144): Compute differences in oth455 PCA factors for 'imbalance' signals, then apply time series ops. Example: `ts_sum(oth455_fact2 - oth455_fact1, 240)`. Math op creates difference (pre-processing), ts op captures persistence (main signal). Enhances correlation passing via unique network insights; effective in EUR for low-fitness but high-margin alphas.

These community-shared templates promote diverse, ATOM-friendly ideas that align with test requirements like low correlation and high robustness.

### Official BRAIN Examples
Draw from BRAIN's structured tutorials for foundational ideas:

- **Beginner Level** ([19 Alpha Examples](https://platform.worldquantbrain.com/learn/documentation/create-alphas/19-alpha-examples)): Start with simple price-based signals. Example: `ts_rank(close, 20)` – Ranks closing prices over 20 days to capture momentum. Improve by adding neutralization: `neutralize(ts_rank(close, 20), "MARKET")` to reduce market bias and pass correlation tests.
  
- **Bronze Level** ([Sample Alpha Concepts](https://platform.worldquantbrain.com/learn/documentation/create-alphas/sample-alpha-concepts)): Incorporate multiple data fields. Example: `ts_corr(close, volume, 10)` – Correlation between price and volume over 10 days. Enhance fitness by decaying: `ts_decay_linear(ts_corr(close, volume, 10), 5)` for smoother signals.

- **Silver Level** ([Example Expression Alphas](https://platform.worldquantbrain.com/learn/documentation/create-alphas/example-expression-alphas)): Advanced combinations. Example: `scale(ts_rank(ts_delay(vwap, 1) / vwap, 252))` – Normalized 1-year price change. Iterate by adding groups: `group_zscore(scale(ts_rank(ts_delay(vwap, 1) / vwap, 252)), "INDUSTRY")` to improve sub-universe robustness.

These examples show how starting with a core idea (e.g., momentum) and layering improvements (e.g., neutralization, decay) can help pass tests like fitness and sub-universe.

## 1. Fitness
### Requirements
- At least "Average": Greater than 1.3 for Delay-0 or Greater than 1 for Delay-1.
- Fitness = Sharpe * sqrt(abs(Returns) / max(Turnover, 0.125)).
- Ratings: Spectacular (>2.5 Delay-1 or >3.25 Delay-0), Excellent (>2 or >2.6), etc.

### Explanation
Fitness balances Sharpe, Returns, and Turnover. High fitness indicates a robust alpha. It's a key metric for alpha quality.

### Tips to Improve
- **From Docs**: Increase Sharpe/Returns and reduce Turnover. Optimize by balancing these—improving one may hurt another. Aim for upward PnL trends with minimal drawdown.
- **Forum Experiences** (from searches on "increase fitness alpha"):
  - Use group operators (e.g., with pv13) to boost fitness without overcomplicating expressions.
  - Screen alphas with author_fitness >=2 or similar in competitions like Super Alpha.
  - Manage alphas via databases or tags; query for high-fitness ones (e.g., via API with fitness filters).
  - In hand-crafting alphas, iteratively add operators like left_tail and group to push fitness over thresholds, but watch for overfitting.
  - Community shares: High-fitness alphas (e.g., >2) often come from multi-factor fusions or careful data field selection.

## 2. Sharpe Ratio
### Requirements
- Greater than 2 for Delay-0 or Greater than 1.25 for Delay-1.
- Sharpe = sqrt(252) * IR, where IR = mean(PnL) / stdev(PnL).

### Explanation
Measures risk-adjusted returns. Higher Sharpe means more consistent performance. For GLB alphas, additional sub-geography Sharpes (>=1 for AMER, APAC, EMEA).

### Tips to Improve
- **From Docs**: Focus on consistent PnL with low volatility. Use visualization to ensure upward trends. For sub-geography, incorporate region-specific signals (e.g., earnings for AMER, microstructure for APAC).
- **Forum Experiences** (from searches on "improve Sharpe ratio alpha"):
  - Decay signals separately for liquid/non-liquid stocks (e.g., ts_decay_linear with rank(volume*close)).
  - Avoid size-related multipliers (e.g., rank(-assets)) that shift weights to illiquid stocks.
  - Check yearly Sharpe data via API and store in databases for analysis.
  - In templates like CCI-based, combine with z-score and delay to stabilize Sharpe.
  - Community tip: Prune low-Sharpe alphas in pools using weighted methods to retain high-Sharpe ones.
  - **Flipping Negative Sharpe**: For non-CHN regions, if an alpha shows negative Sharpe (e.g., -1 to -2), add a minus sign to the expression (e.g., `-original_expression`) to flip it positive. This preserves the signal while improving metrics; verify it doesn't introduce correlation issues.

## 3. Turnover
### Requirements
- 1% < Turnover < 70%.
- Turnover = Dollar trading volume / Book size.

### Explanation
Indicates trading frequency. Low turnover reduces costs; extremes fail submission.

### Tips to Improve
- **From Docs**: Aim for balanced trading—too low means inactive, too high means over-trading.
- **Forum Experiences**: (Note: Specific turnover searches weren't direct, but tied to fitness/Sharpe improvements)
  - Use decay functions to smooth signals, reducing unnecessary trades.
  - In multi-alpha simulations, filter by turnover thresholds in code to pre-select candidates.

## 4. Weight Test
### Requirements
- Max weight in any stock <10%.
- Sufficient instruments assigned weight (varies by universe, e.g., TOP3000).

### Explanation
Ensures diversification; fails if concentrated or too few stocks weighted.

### Tips to Improve
- **From Docs**: Avoid expressions that overly concentrate weights. Assign weights broadly after simulation start.
- **Forum Experiences**: (Limited direct posts; inferred from general submission tips)
  - Use neutralization (e.g., market) to distribute weights evenly.
  - Check via simulation stats; adjust with rank or scale operators.

## 5. Sub-universe Test
### Requirements
- Sub-universe Sharpe >= 0.75 * sqrt(subuniverse_size / alpha_universe_size) * alpha_sharpe.
- Ensures robustness in more liquid sub-universes (e.g., TOP1000 for TOP3000).

### Explanation
Tests if alpha performs in liquid stocks, avoiding over-reliance on illiquid ones.

### Tips to Improve
- **From Docs**: Avoid size-related multipliers. Decay liquid/non-liquid parts separately (e.g., ts_decay_linear(signal,5)*rank(volume*close) + ts_decay_linear(signal,10)*(1-rank(volume*close))). From this example, we can see that the signal can be inflated by different weights for different parts of an datafield.
  - Step-by-step improvements; discard non-robust signals.
- **Forum Experiences**: (From "how to pass submission tests")
  - Improve overall Sharpe first, as it scales the threshold.
  - Use pasteurize to handle NaNs and ensure even distribution.

## 6. Self-Correlation
### Requirements
- <0.7 PnL correlation with own submitted alphas.
- Or Sharpe at least 10% greater than correlated alphas.

### Explanation
Promotes diversity; based on 4-year PnL window. Allows improvements if new alpha is significantly better.

### Tips to Improve
- **From Docs**: Submit diverse ideas. Use correlation table in results to identify issues.
- **Forum Experiences** (from searches on "reduce correlation self alphas"):
  - Local computation of self-correlation (e.g., via PnL matrices) to pre-filter before submission.
  - Code optimizations: Prune high-correlation alphas, use clustering or weighted pruning (e.g., Sharpe-weighted) to retain diverse sets.
  - Handle negatives: Transform negatively correlated alphas (e.g., in China market) by inversion or adjustments.
  - Scripts for batch checking: Use machine_lib modifications to print correlations and pyramid info.
  - Community shares: Differences between local and platform calculations (e.g., due to NaN handling); align by using full PnL data.

### Evaluating Whole Alpha Quality
Before final submission, perform these checks on simulation results:

- **Yearly Stats Quality Check**: Review yearly statistics. If records are missing for >5 years, it indicates low data quality (e.g., sparse coverage). Fix with ts_backfill, data selection, or alternative fields to ensure robust performance across tests.

This complements per-test improvements by validating overall alpha reliability.

## General Advice
- Start with broad simulations, narrow based on stats.
- Use tools like check_submission API for pre-checks.
- Forum consensus: Automate with Python scripts for efficiency (e.g., threading for simulates, databases for alpha management).
- Risks: Overfitting in manual tweaks; validate with train/test splits.

This guide is based on tool-gathered data. For updates, check BRAIN docs or forum.
