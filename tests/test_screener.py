"""Tests for the AKShare market screener."""

import pandas as pd

from screener_config import ScreenerConfig
from screener_core import MarketScreener, parse_args


class FakeAK:
    @staticmethod
    def stock_zh_a_spot_em():
        return pd.DataFrame({
            "代码": ["600001", "600002", "600003"],
            "名称": ["正常公司", "ST公司", "高估公司"],
            "最新价": [10.0, 5.0, 20.0],
            "市盈率-动态": [10.0, 8.0, 100.0],
            "市净率": [1.0, 0.8, 12.0],
            "换手率": [1.0, 1.0, 1.0],
            "总市值": [10_000_000_000, 5_000_000_000, 8_000_000_000],
        })


def test_screen_filters_and_scores(monkeypatch, tmp_path):
    config = ScreenerConfig(cache_dir=str(tmp_path), tier2_main_limit=10)
    screener = MarketScreener(config)
    monkeypatch.setattr(screener, "_load_akshare", lambda: FakeAK)
    result = screener.screen(force_refresh=True)
    assert result["code"].tolist() == ["600001"]
    assert "value_score" in result.columns


def test_snapshot_uses_cache(monkeypatch, tmp_path):
    config = ScreenerConfig(cache_dir=str(tmp_path))
    screener = MarketScreener(config)
    cached = FakeAK.stock_zh_a_spot_em().rename(columns=MarketScreener.COLUMN_ALIASES)
    screener.cache.put("akshare_a_share_spot", cached)
    monkeypatch.setattr(screener, "_load_akshare", lambda: (_ for _ in ()).throw(AssertionError("network called")))
    assert len(screener.fetch_snapshot()) == 3


def test_cli_arguments():
    args = parse_args(["--max-pe", "20", "--limit", "50"])
    assert args.max_pe == 20
    assert args.limit == 50
