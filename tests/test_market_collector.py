"""Tests for the provider-neutral, token-free market collector."""

import pandas as pd

from market_collector import MarketDataClient, parse_args


def _history():
    return pd.DataFrame({
        "date": pd.to_datetime(["2025-12-30", "2025-12-31"]),
        "open": [9.5, 10.0], "close": [10.0, 10.5],
        "high": [10.2, 10.8], "low": [9.4, 9.9], "volume": [100, 120],
    })


def _financials():
    return {
        "income": pd.DataFrame({"end_date": ["2025-12-31"], "revenue": [1000.0], "n_income_attr_p": [100.0]}),
        "balancesheet": pd.DataFrame({"end_date": ["2025-12-31"], "total_assets": [2000.0]}),
        "cashflow": pd.DataFrame({"end_date": ["2025-12-31"], "n_cashflow_act": [120.0], "capex": [30.0]}),
    }


def test_symbol_conversion():
    assert MarketDataClient._yf_ticker("600887.SH") == "600887.SS"
    assert MarketDataClient._yf_ticker("00700.HK") == "0700.HK"
    assert MarketDataClient._yf_ticker("AAPL.US") == "AAPL"


def test_assemble_data_pack_without_network(monkeypatch):
    client = MarketDataClient()
    monkeypatch.setattr(client, "_history", lambda code: _history())
    monkeypatch.setattr(client, "_basic", lambda code, history: pd.DataFrame([{
        "ts_code": code, "name": "测试公司", "close": 10.5,
        "market_cap_mm": 10000.0, "shares": 1_000_000_000,
    }]))
    monkeypatch.setattr(client, "_financials", lambda code: _financials())
    monkeypatch.setattr(client, "_dividends", lambda code: pd.DataFrame({"date": ["2025-06-01"], "cash_div": [0.5]}))
    output = client.assemble_data_pack("600887")
    assert "数据包 — 600887.SH" in output
    assert "AKShare + Yahoo Finance" in output
    for number in range(1, 18):
        assert f"## {number}." in output
    assert client._store["income"].iloc[0]["revenue"] == 1000.0


def test_refresh_preserves_existing_when_market_is_down(monkeypatch):
    client = MarketDataClient()
    monkeypatch.setattr(
        client, "assemble_data_pack",
        lambda code: "⚠️ 数据不可用：AKShare 与 Yahoo Finance 均未返回行情。",
    )
    assert client.refresh_market_sections("600887.SH", "old pack") == "old pack"


def test_refresh_only_replaces_market_sections(monkeypatch):
    client = MarketDataClient()
    old = """*生成时间: 2025-01-01 00:00:00*
## 1. 基本信息

old basic
## 2. 市场行情

old market
## 3. 合并利润表

valuable financial statement
## 11. 十年周线行情

old prices
## 12. 关键财务指标

old metrics
"""
    new = """*生成时间: 2026-01-01 00:00:00*
## 1. 基本信息

new basic
## 2. 市场行情

new market
## 3. 合并利润表

missing financial statement
## 11. 十年周线行情

new prices
## 12. 关键财务指标

new metrics
"""
    monkeypatch.setattr(client, "assemble_data_pack", lambda code: new)
    result = client.refresh_market_sections("600887.SH", old)
    assert "new market" in result
    assert "new prices" in result
    assert "valuable financial statement" in result
    assert "missing financial statement" not in result


def test_annual_helpers():
    client = MarketDataClient()
    client._store["income"] = pd.DataFrame({
        "end_date": ["2025-12-31", "2025-09-30"], "revenue": [100.0, 70.0],
    })
    assert len(client._get_annual_df("income")) == 1
    assert client._get_annual_series("income", "revenue")[0][1] == 100.0


def test_parse_args_has_no_token_option():
    args = parse_args(["--code", "600887", "--provider", "akshare"])
    assert args.provider == "akshare"
    assert not hasattr(args, "token")
