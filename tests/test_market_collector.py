"""Tests for the provider-neutral, token-free market collector."""

import sys
from types import SimpleNamespace

import pandas as pd

from market_collector import MarketDataClient, parse_args
from quality_control import parse_kv, parse_matrix, parse_sections


def _history():
    return pd.DataFrame({
        "date": pd.to_datetime(["2025-12-30", "2025-12-31"]),
        "open": [9.5, 10.0], "close": [10.0, 10.5],
        "high": [10.2, 10.8], "low": [9.4, 9.9], "volume": [100, 120],
    })


def _financials():
    return {
        "income": pd.DataFrame({
            "end_date": ["2025-12-31"], "revenue": [1000.0],
            "operate_cost": [600.0], "n_income_attr_p": [100.0],
        }),
        "balancesheet": pd.DataFrame({
            "end_date": ["2025-12-31"], "total_assets": [2000.0],
            "total_liab": [800.0], "total_hldr_eqy_exc_min_int": [1000.0],
        }),
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


def test_collector_output_matches_quality_control_contract(monkeypatch):
    """Changing the collector back to date×metric tables must break this test."""
    client = MarketDataClient()
    monkeypatch.setattr(client, "_history", lambda code: _history())
    monkeypatch.setattr(client, "_basic", lambda code, history: pd.DataFrame([{
        "ts_code": code,
        "name": "测试公司",
        "industry": "测试行业",
        "close": 10.5,
        "market_cap_mm": 10000.0,
        "shares": 1_000_000_000,
        "trade_date": pd.Timestamp("2025-12-31"),
        "list_date": "1996-03-12",
        "currency": "CNY",
        "pe_ttm": 15.2,
        "pb": 2.1,
    }]))
    monkeypatch.setattr(client, "_financials", lambda code: _financials())
    monkeypatch.setattr(client, "_dividends", lambda code: pd.DataFrame())

    sections = parse_sections(client.assemble_data_pack("600887"))
    basic = parse_kv(sections["1"])
    income_cols, income = parse_matrix(sections["3"])
    cash_cols, cash = parse_matrix(sections["5"])
    metric_cols, metrics = parse_matrix(sections["12"])

    assert basic["股票代码"] == "600887.SH"
    assert basic["当前价格"] == "10.5"
    assert basic["总市值 (万元)"] == "1000000.0"
    assert basic["上市时间"] == "1996-03-12"
    assert basic["币种"] == "CNY"
    assert basic["PE (TTM)"] == "15.2"
    assert basic["PB"] == "2.1"
    assert income_cols == ["2025"]
    assert income["营业收入"]["2025"] == 1000.0
    assert income["归母净利润"]["2025"] == 100.0
    assert cash_cols == ["2025"]
    assert cash["经营活动现金流 (OCF)"]["2025"] == 120.0
    assert cash["自由现金流 (FCF)"]["2025"] == 90.0
    assert metric_cols == ["2025"]
    assert metrics["综合毛利率 (%)"]["2025"] == 40.0
    assert metrics["归母净利率 (%)"]["2025"] == 10.0
    assert metrics["资产负债率 (%)"]["2025"] == 40.0
    assert metrics["ROE (%)"]["2025"] == 10.0


def test_a_share_dividends_do_not_use_current_shares_for_historical_total():
    """Historical totals must stay unresolved when CNInfo omits the payout base."""
    client = MarketDataClient()
    client._ak = SimpleNamespace(stock_dividend_cninfo=lambda symbol: pd.DataFrame([{
        "实施方案公告日期": "2026-01-15",
        "分红类型": "中期分红",
        "派息比例": 2.2,
        "股权登记日": "2026-01-20",
        "除权日": "2026-01-21",
        "报告时间": "2025中报",
    }]))
    client._current_shares = 1_000_000_000

    result = client._dividends("600887.SH")

    assert result.to_dict("records") == [{
        "年度": "2025",
        "每股现金分红(税前)": 0.22,
        "总分红 (百万元)": None,
        "登记日": "2026-01-20",
        "除权日": "2026-01-21",
        "来源": "巨潮资讯（AKShare）",
    }]
    assert client._sources["dividends"] == "巨潮资讯（AKShare）"


def test_yahoo_payment_year_is_not_mislabeled_as_fiscal_year(monkeypatch):
    """A dividend paid in 2025 must not silently become a FY2025 dividend."""
    dates = pd.DatetimeIndex([pd.Timestamp("2025-06-01", tz="UTC")], name="Date")
    dividends = pd.Series([0.5], index=dates)
    fake_yf = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(dividends=dividends))
    client = MarketDataClient(provider="yfinance")
    client._yf = fake_yf
    client._current_shares = 1_000_000_000

    result = client._dividends_yf("AAPL.US")

    assert result.iloc[0]["年度"] == "—"
    assert result.iloc[0]["支付年度"] == "2025"
    assert pd.isna(result.iloc[0]["总分红 (百万元)"])
    assert "财年未核定" in result.iloc[0]["口径说明"]


def test_a_share_history_falls_back_to_tencent_before_yfinance(monkeypatch):
    """Removing Tencent from the A-share fallback chain must break this test."""
    client = MarketDataClient()
    monkeypatch.setattr(client, "_history_ak", lambda code: (_ for _ in ()).throw(RuntimeError("EM down")))
    monkeypatch.setattr(client, "_history_tx", lambda code: _history())
    monkeypatch.setattr(
        client,
        "_history_yf",
        lambda code: (_ for _ in ()).throw(AssertionError("Yahoo should not be reached")),
    )

    result = client._history("600887.SH")

    assert result.iloc[-1]["close"] == 10.5
    assert client._sources["history"] == "腾讯证券（AKShare）"


def test_data_pack_source_label_uses_sources_that_actually_succeeded(monkeypatch):
    """A fixed AKShare+Yahoo label must fail when the real provider was Tencent."""
    client = MarketDataClient()

    def history(code):
        client._sources["history"] = "腾讯证券（AKShare）"
        return _history()

    def financials(code):
        client._sources["financials"] = "东方财富财报（AKShare）"
        return _financials()

    monkeypatch.setattr(client, "_history", history)
    monkeypatch.setattr(client, "_basic", lambda code, history: pd.DataFrame([{"ts_code": code, "close": 10.5}]))
    monkeypatch.setattr(client, "_financials", financials)
    monkeypatch.setattr(client, "_dividends", lambda code: pd.DataFrame())

    output = client.assemble_data_pack("600887")

    assert "*数据来源: 腾讯证券（AKShare） + 东方财富财报（AKShare）*" in output


def test_financials_keep_successful_ak_frames_when_one_endpoint_fails(monkeypatch):
    """A cash-flow endpoint error must not discard successful income and balance data."""
    client = MarketDataClient(provider="akshare")
    client._ak = SimpleNamespace(
        stock_profit_sheet_by_report_em=lambda symbol: pd.DataFrame({
            "REPORT_DATE": ["2025-12-31"], "TOTAL_OPERATE_INCOME": [1_000_000_000],
        }),
        stock_balance_sheet_by_report_em=lambda symbol: pd.DataFrame({
            "REPORT_DATE": ["2025-12-31"], "TOTAL_ASSETS": [2_000_000_000],
        }),
        stock_cash_flow_sheet_by_report_em=lambda symbol: (_ for _ in ()).throw(RuntimeError("cash down")),
    )

    result = client._financials("600887.SH")

    assert result["income"].iloc[0]["revenue"] == 1000.0
    assert result["balancesheet"].iloc[0]["total_assets"] == 2000.0
    assert result["cashflow"].empty


def test_total_equity_is_not_mislabeled_as_parent_equity():
    """Using TOTAL_EQUITY as parent equity must fail because minorities can be material."""
    client = MarketDataClient(provider="akshare")
    client._ak = SimpleNamespace(
        stock_profit_sheet_by_report_em=lambda symbol: pd.DataFrame(),
        stock_balance_sheet_by_report_em=lambda symbol: pd.DataFrame({
            "REPORT_DATE": ["2025-12-31"], "TOTAL_EQUITY": [2_000_000_000],
        }),
        stock_cash_flow_sheet_by_report_em=lambda symbol: pd.DataFrame(),
    )

    balance = client._financials_ak("600887.SH")["balancesheet"]

    assert "total_hldr_eqy_exc_min_int" not in balance
    assert balance.iloc[0]["total_equity"] == 2000.0


def test_financials_fallback_fills_only_missing_frames(monkeypatch):
    """Fallback must fill a missing statement without replacing higher-priority data."""
    client = MarketDataClient()
    ak_result = _financials()
    ak_result["cashflow"] = pd.DataFrame()
    yf_result = _financials()
    yf_result["income"] = pd.DataFrame({"end_date": ["2025-12-31"], "revenue": [9999.0]})
    yf_result["cashflow"] = pd.DataFrame({
        "end_date": ["2025-12-31"], "n_cashflow_act": [999.0], "capex": [9.0],
    })
    monkeypatch.setattr(client, "_financials_ak", lambda code: ak_result)
    monkeypatch.setattr(client, "_financials_yf", lambda code: yf_result)

    result = client._financials("600887.SH")

    assert result["income"].iloc[0]["revenue"] == 1000.0
    assert result["cashflow"].iloc[0]["n_cashflow_act"] == 999.0
    assert client._sources["financials.income"] == "东方财富财报（AKShare）"
    assert client._sources["financials.cashflow"] == "Yahoo Finance"


def test_yfinance_uses_writable_configured_cache(monkeypatch, tmp_path):
    """Removing cache configuration must break this observable initialization contract."""
    configured = []
    fake_yf = SimpleNamespace(set_tz_cache_location=lambda path: configured.append(path))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    client = MarketDataClient(cache_dir=tmp_path / "yf-cache")

    assert client.yf is fake_yf
    assert configured == [str(tmp_path / "yf-cache")]
    assert (tmp_path / "yf-cache").is_dir()


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


def test_parse_args_accepts_cache_dir():
    args = parse_args(["--code", "600887", "--cache-dir", "tmp/yf-cache"])
    assert args.cache_dir == "tmp/yf-cache"
