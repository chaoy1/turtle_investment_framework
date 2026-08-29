---
name: turtle-analysis
description: Run the full Turtle Investment Framework fundamental analysis for an A-share, HK, or US stock. Use when the user asks for 龟龟策略、穿透回报率、完整基本面分析、定量加估值，or invokes $turtle-analysis. Do not use for a qualitative-only or valuation-only request.
---

# Turtle analysis

Accept one stock code such as `600887`, `00700.HK`, or `AAPL`. If no valid code is available, ask for it.

1. Read `AGENTS.md` and `strategies/turtle/coordinator.md` completely. Treat repository-relative paths as relative to the repository root.
2. Resolve the platform Python executable: use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on Unix; fall back to `python` only when the virtual environment is unavailable.
3. Check `output/{code}_{company}/qualitative_report.md` and `data_pack_market.md`. If either is missing, run the `$business-analysis` workflow first. `data_pack_report.md` is optional.
4. Refresh market data with `scripts/market_collector.py --refresh-market` as specified by the coordinator.
5. Execute Phase 3.1 from `strategies/turtle/phase3_quantitative.md`, then Phase 3.2 from `strategies/turtle/phase3_valuation.md`.
6. Preserve the repository calculation discipline: Python computes; GPT selects judgment parameters and cites generated values with `[src: ...]`.
7. Run the required consistency and audit gates before delivery. Degrade missing inputs explicitly but still produce the final report.

Final output: `output/{code}_{company}/{company}_{code}_分析报告.md`.

