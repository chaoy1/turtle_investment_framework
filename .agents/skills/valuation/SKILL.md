---
name: valuation
description: Run the standalone Turtle Investment Framework valuation workflow for an A-share, HK, or US stock. Use for 估值、DCF、DDM、PE Band、PEG、PS、安全边际，or $valuation. Do not use for a full qualitative business analysis.
---

# Valuation

Accept one stock code such as `600887`, `00700.HK`, or `AAPL`. If no valid code is available, ask for it.

1. Read `AGENTS.md` and `strategies/valuation/coordinator.md` completely.
2. Resolve the platform Python executable: use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on Unix.
3. Verify the prerequisite qualitative report and market data. If missing, run the `$business-analysis` workflow first.
4. Follow `strategies/valuation/phase2_valuation.md` and its routed references. Select assumptions with GPT; send every arithmetic operation through `scripts/valuation_engine.py`.
5. Cite script outputs, state unavailable data explicitly, and complete the coordinator's validation gates before delivery.

Write outputs to the company directory under `output/` as specified by the coordinator.

