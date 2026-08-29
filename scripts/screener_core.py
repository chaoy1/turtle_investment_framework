#!/usr/bin/env python3
"""Token-free A-share screener backed by AKShare public market data."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from cache_utils import ScreenerCache
from screener_config import ScreenerConfig


class MarketScreener:
    """Fast first-pass screen for the A-share universe.

    Deep fundamental analysis remains PDF-first because free bulk APIs do not
    offer sufficiently stable audited-statement coverage.
    """

    COLUMN_ALIASES = {
        "代码": "code", "名称": "name", "最新价": "close", "市盈率-动态": "pe",
        "市净率": "pb", "换手率": "turnover", "总市值": "market_cap",
        "年初至今涨跌幅": "ytd_change", "涨跌幅": "change_pct",
    }

    def __init__(self, config: ScreenerConfig | None = None):
        self.config = config or ScreenerConfig()
        errors = self.config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.cache = ScreenerCache(self.config.cache_dir)

    @staticmethod
    def _load_akshare():
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("AKShare 未安装，请运行 pip install -r requirements.txt") from exc
        return ak

    def fetch_snapshot(self, force_refresh: bool = False) -> pd.DataFrame:
        key = "akshare_a_share_spot"
        if not force_refresh:
            cached = self.cache.get(key, 24 * 3600)
            if cached is not None:
                return cached
        raw = self._load_akshare().stock_zh_a_spot_em()
        if raw is None or raw.empty:
            raise RuntimeError("AKShare 未返回 A 股行情快照")
        frame = raw.rename(columns=self.COLUMN_ALIASES)
        required = {"code", "name", "close", "pe", "pb", "turnover", "market_cap"}
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(f"AKShare 行情字段缺失: {', '.join(sorted(missing))}")
        for col in ("close", "pe", "pb", "turnover", "market_cap"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        self.cache.put(key, frame)
        return frame

    def screen(self, force_refresh: bool = False) -> pd.DataFrame:
        df = self.fetch_snapshot(force_refresh).copy()
        cfg = self.config
        df = df[~df["name"].astype(str).str.contains(r"ST|退", case=False, regex=True, na=False)]
        df = df[
            (df["market_cap"] >= cfg.min_market_cap_yi * 1e8)
            & (df["turnover"] >= cfg.min_turnover_pct)
            & (df["pb"] > 0) & (df["pb"] <= cfg.max_pb)
            & (df["pe"] > 0) & (df["pe"] <= cfg.max_pe)
        ]
        df["market_cap_yi"] = df["market_cap"] / 1e8
        df["value_score"] = (
            cfg.pe_weight / df["pe"].clip(lower=0.01)
            + cfg.pb_weight / df["pb"].clip(lower=0.01)
            + cfg.dv_weight * df["market_cap_yi"].rank(pct=True)
        )
        columns = ["code", "name", "close", "pe", "pb", "turnover", "market_cap_yi", "value_score"]
        return df.sort_values("value_score", ascending=False)[columns].head(cfg.tier2_main_limit).reset_index(drop=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="AKShare 免费 A 股筛选器")
    parser.add_argument("--max-pe", type=float, default=50.0)
    parser.add_argument("--max-pb", type=float, default=10.0)
    parser.add_argument("--min-market-cap", type=float, default=5.0, help="亿元")
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--cache-refresh", action="store_true")
    parser.add_argument("--csv", default="output/screener.csv")
    parser.add_argument("--html", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = ScreenerConfig(
        max_pe=args.max_pe, max_pb=args.max_pb,
        min_market_cap_yi=args.min_market_cap, tier2_main_limit=args.limit,
    )
    result = MarketScreener(config).screen(force_refresh=args.cache_refresh)
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    if args.html:
        html_path = Path(args.html)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_html(html_path, index=False)
    print(result.to_string(index=False))
    print(f"\n结果已写入 {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
