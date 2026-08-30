#!/usr/bin/env python3
"""Token-free market data collector for A-share, HK, and US equities.

Provider policy:
* A-share: AKShare first, Yahoo Finance fallback.
* HK/US: Yahoo Finance first.
* Annual-report PDF remains the authoritative source for reported figures.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import validate_stock_code
from format_utils import format_header


SECTION_TITLES = (
    "1. 基本信息", "2. 市场行情", "3. 合并利润表", "4. 合并资产负债表",
    "5. 现金流量表", "6. 分红历史", "7. 股东与治理", "8. 行业与竞争",
    "9. 主营业务构成", "10. 管理层讨论与分析 (MD&A)", "11. 十年周线行情",
    "12. 关键财务指标", "13. 风险警示", "14. 无风险利率",
    "15. 股票回购", "16. 股权质押", "17. 衍生指标（Python 预计算）",
)


class MarketDataClient:
    """Collect public market data without API keys or paid data credits."""

    INCOME_LABELS = {
        "revenue": "营业收入",
        "operate_cost": "营业成本",
        "operate_profit": "营业利润",
        "n_income_attr_p": "归母净利润",
        "net_income": "净利润",
        "basic_eps": "基本EPS",
    }
    BALANCE_LABELS = {
        "money_cap": "货币资金",
        "total_assets": "总资产",
        "total_liab": "总负债",
        "total_hldr_eqy_exc_min_int": "归母所有者权益",
        "total_equity": "所有者权益合计",
        "st_borr": "短期借款",
        "lt_borr": "长期借款",
        "bond_payable": "应付债券",
        "goodwill": "商誉",
    }
    CASHFLOW_LABELS = {
        "n_cashflow_act": "经营活动现金流 (OCF)",
        "capex": "资本开支 (Capex)",
        "free_cash_flow": "自由现金流 (FCF)",
        "n_cashflow_inv_act": "投资活动现金流",
        "n_cash_flows_fnc_act": "融资活动现金流",
    }

    def __init__(self, provider: str = "auto", cache_dir=None):
        if provider not in {"auto", "akshare", "yfinance"}:
            raise ValueError("provider must be auto, akshare, or yfinance")
        self.provider = provider
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "turtle-yfinance-cache"
        self._store: dict[str, pd.DataFrame] = {}
        self._errors: list[str] = []
        self._sources: dict[str, str] = {}
        self._cache_enabled = True
        self._ak = None
        self._yf = None

    @property
    def ak(self):
        if self._ak is None:
            try:
                import akshare as ak
            except ImportError as exc:
                raise RuntimeError("AKShare 未安装，请运行 pip install -r requirements.txt") from exc
            self._ak = ak
        return self._ak

    @property
    def yf(self):
        if self._yf is None:
            try:
                import yfinance as yf
            except ImportError as exc:
                raise RuntimeError("yfinance 未安装，请运行 pip install -r requirements.txt") from exc
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            yf.set_tz_cache_location(str(self.cache_dir))
            self._yf = yf
        return self._yf

    @staticmethod
    def _market(code: str) -> str:
        if code.endswith(".HK"):
            return "HK"
        if code.endswith(".US"):
            return "US"
        return "A"

    @staticmethod
    def _symbol(code: str) -> str:
        return code.split(".", 1)[0]

    @classmethod
    def _yf_ticker(cls, code: str) -> str:
        symbol = cls._symbol(code)
        if code.endswith(".SH"):
            return f"{symbol}.SS"
        if code.endswith(".SZ"):
            return f"{symbol}.SZ"
        if code.endswith(".HK"):
            return f"{int(symbol):04d}.HK"
        return symbol

    @classmethod
    def _ak_report_symbol(cls, code: str) -> str:
        prefix = "SH" if code.endswith(".SH") else "SZ"
        return prefix + cls._symbol(code)

    @classmethod
    def _ak_tx_symbol(cls, code: str) -> str:
        prefix = "sh" if code.endswith(".SH") else "sz"
        return prefix + cls._symbol(code)

    @staticmethod
    def _safe_float(value):
        try:
            result = float(value)
            return None if pd.isna(result) else result
        except (TypeError, ValueError):
            return None

    def _unit_label(self) -> str:
        return {"A": "百万元人民币", "HK": "百万港元", "US": "百万美元"}[self._market(self.code)]

    def _price_unit(self) -> str:
        return {"A": "元", "HK": "港元", "US": "美元"}[self._market(self.code)]

    def _record_error(self, source: str, exc: Exception) -> None:
        message = f"{source}: {type(exc).__name__}: {exc}"
        self._errors.append(message)
        print(f"[market_collector] {message}", file=sys.stderr)

    def _history_ak(self, code: str) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=365 * 11)).strftime("%Y%m%d")
        raw = self.ak.stock_zh_a_hist(
            symbol=self._symbol(code), period="daily", start_date=start,
            end_date=end, adjust="qfq", timeout=15,
        )
        mapping = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                   "最低": "low", "成交量": "volume", "成交额": "amount"}
        return raw.rename(columns=mapping)

    def _history_yf(self, code: str) -> pd.DataFrame:
        raw = self.yf.Ticker(self._yf_ticker(code)).history(period="10y", auto_adjust=True)
        if raw.empty:
            return raw
        raw = raw.reset_index().rename(columns={
            "Date": "date", "Open": "open", "Close": "close", "High": "high",
            "Low": "low", "Volume": "volume",
        })
        return raw

    def _history_tx(self, code: str) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=365 * 11)).strftime("%Y%m%d")
        return self.ak.stock_zh_a_hist_tx(
            symbol=self._ak_tx_symbol(code), start_date=start, end_date=end,
            adjust="qfq", timeout=15,
        )

    def _history(self, code: str) -> pd.DataFrame:
        methods = [
            (self._history_ak, "东方财富（AKShare）"),
            (self._history_tx, "腾讯证券（AKShare）"),
            (self._history_yf, "Yahoo Finance"),
        ]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = [(self._history_yf, "Yahoo Finance")]
        elif self.provider == "akshare":
            methods = methods[:2]
        for method, source in methods:
            try:
                df = method(code)
                if not df.empty:
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
                    self._sources["history"] = source
                    return df.sort_values("date").reset_index(drop=True)
            except Exception as exc:
                self._record_error(method.__name__, exc)
        return pd.DataFrame()

    def _basic_ak(self, code: str) -> dict:
        raw = self.ak.stock_individual_info_em(symbol=self._symbol(code), timeout=15)
        if raw.empty:
            return {}
        values = dict(zip(raw.iloc[:, 0].astype(str), raw.iloc[:, 1]))
        market_cap = self._safe_float(values.get("总市值"))
        shares = self._safe_float(values.get("总股本"))
        return {
            "name": values.get("股票简称") or values.get("简称"),
            "industry": values.get("行业"),
            "list_date": values.get("上市时间"),
            "pe_ttm": self._safe_float(values.get("市盈率(动)")),
            "pb": self._safe_float(values.get("市净率")),
            "market_cap_mm": market_cap / 1e6 if market_cap else None,
            "shares": shares,
        }

    def _basic_yf(self, code: str) -> dict:
        ticker = self.yf.Ticker(self._yf_ticker(code))
        info = ticker.info or {}
        return {
            "name": info.get("longName") or info.get("shortName"),
            "industry": info.get("industry"),
            "market_cap_mm": self._safe_float(info.get("marketCap")) / 1e6
            if self._safe_float(info.get("marketCap")) else None,
            "shares": self._safe_float(info.get("sharesOutstanding")),
            "currency": info.get("currency"),
            "pe_ttm": self._safe_float(info.get("trailingPE")),
            "pb": self._safe_float(info.get("priceToBook")),
        }

    def _basic(self, code: str, history: pd.DataFrame) -> pd.DataFrame:
        methods = [self._basic_ak, self._basic_yf]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = [self._basic_yf]
        elif self.provider == "akshare":
            methods = [self._basic_ak]
        data = {}
        for method in methods:
            try:
                data = method(code)
                if data:
                    source = "东方财富基本信息（AKShare）" if method == self._basic_ak else "Yahoo Finance"
                    data["source"] = source
                    self._sources["basic"] = source
                    break
            except Exception as exc:
                self._record_error(method.__name__, exc)
        data["ts_code"] = code
        if not history.empty:
            data["close"] = self._safe_float(history.iloc[-1].get("close"))
            data["trade_date"] = history.iloc[-1].get("date")
        return pd.DataFrame([data]) if data else pd.DataFrame()

    @staticmethod
    def _pick(raw: pd.DataFrame, mapping: dict[str, tuple[str, ...]]) -> pd.DataFrame:
        if raw.empty:
            return raw
        result = pd.DataFrame(index=raw.index)
        for target, candidates in mapping.items():
            for source in candidates:
                if source in raw.columns:
                    result[target] = raw[source]
                    break
        return result

    def _financials_ak(self, code: str) -> dict[str, pd.DataFrame]:
        symbol = self._ak_report_symbol(code)
        def fetch(label, getter):
            try:
                return getter(symbol=symbol)
            except Exception as exc:
                self._record_error(label, exc)
                return pd.DataFrame()

        income_raw = fetch("AKShare 利润表", self.ak.stock_profit_sheet_by_report_em)
        balance_raw = fetch("AKShare 资产负债表", self.ak.stock_balance_sheet_by_report_em)
        cash_raw = fetch("AKShare 现金流量表", self.ak.stock_cash_flow_sheet_by_report_em)
        income = self._pick(income_raw, {
            "end_date": ("REPORT_DATE",), "revenue": ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
            "operate_cost": ("OPERATE_COST",),
            "operate_profit": ("OPERATE_PROFIT",), "n_income_attr_p": ("PARENT_NETPROFIT",),
            "net_income": ("NETPROFIT",), "basic_eps": ("BASIC_EPS",),
        })
        balance = self._pick(balance_raw, {
            "end_date": ("REPORT_DATE",), "money_cap": ("MONETARYFUNDS",),
            "total_assets": ("TOTAL_ASSETS",), "total_liab": ("TOTAL_LIABILITIES",),
            "total_hldr_eqy_exc_min_int": ("PARENT_EQUITY",),
            "total_equity": ("TOTAL_EQUITY",),
            "st_borr": ("SHORT_LOAN",), "lt_borr": ("LONG_LOAN",),
            "bond_payable": ("BOND_PAYABLE",), "goodwill": ("GOODWILL",),
        })
        cash = self._pick(cash_raw, {
            "end_date": ("REPORT_DATE",), "n_cashflow_act": ("NETCASH_OPERATE",),
            "capex": ("CONSTRUCT_LONG_ASSET",), "n_cashflow_inv_act": ("NETCASH_INVEST",),
            "n_cash_flows_fnc_act": ("NETCASH_FINANCE",),
        })
        for frame in (income, balance, cash):
            for col in frame.columns:
                if col != "end_date" and col != "basic_eps":
                    frame[col] = pd.to_numeric(frame[col], errors="coerce") / 1e6
        return {"income": income, "balancesheet": balance, "cashflow": cash}

    def _financials_yf(self, code: str) -> dict[str, pd.DataFrame]:
        ticker = self.yf.Ticker(self._yf_ticker(code))

        def transpose(raw: pd.DataFrame, mapping: dict[str, tuple[str, ...]]) -> pd.DataFrame:
            if raw is None or raw.empty:
                return pd.DataFrame()
            rows = []
            for date in raw.columns:
                row = {"end_date": pd.Timestamp(date).strftime("%Y%m%d")}
                for target, names in mapping.items():
                    for name in names:
                        if name in raw.index:
                            value = self._safe_float(raw.at[name, date])
                            row[target] = value / 1e6 if value is not None else None
                            break
                rows.append(row)
            return pd.DataFrame(rows)

        income = transpose(ticker.financials, {
            "revenue": ("Total Revenue",), "operate_profit": ("Operating Income",),
            "operate_cost": ("Cost Of Revenue",),
            "n_income_attr_p": ("Net Income Common Stockholders", "Net Income"),
            "net_income": ("Net Income",),
        })
        balance = transpose(ticker.balance_sheet, {
            "money_cap": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
            "total_assets": ("Total Assets",), "total_liab": ("Total Liabilities Net Minority Interest",),
            "total_hldr_eqy_exc_min_int": ("Stockholders Equity",),
            "st_borr": ("Current Debt",), "lt_borr": ("Long Term Debt",), "goodwill": ("Goodwill",),
        })
        cash = transpose(ticker.cashflow, {
            "n_cashflow_act": ("Operating Cash Flow",), "capex": ("Capital Expenditure",),
            "n_cashflow_inv_act": ("Investing Cash Flow",),
            "n_cash_flows_fnc_act": ("Financing Cash Flow",),
        })
        if "capex" in cash:
            cash["capex"] = cash["capex"].abs()
        return {"income": income, "balancesheet": balance, "cashflow": cash}

    def _financials(self, code: str) -> dict[str, pd.DataFrame]:
        methods = [
            (self._financials_ak, "东方财富财报（AKShare）"),
            (self._financials_yf, "Yahoo Finance"),
        ]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = methods[1:]
        elif self.provider == "akshare":
            methods = methods[:1]
        combined = {
            "income": pd.DataFrame(),
            "balancesheet": pd.DataFrame(),
            "cashflow": pd.DataFrame(),
        }
        for method, source in methods:
            try:
                result = method(code)
                for key in combined:
                    frame = result.get(key, pd.DataFrame())
                    if combined[key].empty and frame is not None and not frame.empty:
                        combined[key] = frame
                        self._sources[f"financials.{key}"] = source
                if all(not frame.empty for frame in combined.values()):
                    break
            except Exception as exc:
                self._record_error(method.__name__, exc)
        return combined

    def _dividends_ak(self, code: str) -> pd.DataFrame:
        raw = self.ak.stock_dividend_cninfo(symbol=self._symbol(code))
        if raw is None or raw.empty:
            return pd.DataFrame()
        rows = []
        for _, item in raw.iterrows():
            year_match = re.search(r"\d{4}", str(item.get("报告时间", "")))
            cash_per_ten = self._safe_float(item.get("派息比例"))
            if not year_match or cash_per_ten is None:
                continue
            per_share = round(cash_per_ten / 10.0, 6)
            rows.append({
                "年度": year_match.group(0),
                "每股现金分红(税前)": per_share,
                # CNInfo's endpoint exposes per-10-share cash but not the
                # historical payout base. Current shares would corrupt old
                # totals after splits, placements, or buybacks.
                "总分红 (百万元)": None,
                "登记日": item.get("股权登记日"),
                "除权日": item.get("除权日"),
                "来源": "巨潮资讯（AKShare）",
                "_announcement_date": item.get("实施方案公告日期"),
            })
        result = pd.DataFrame(rows)
        if result.empty:
            return result
        result["_announcement_date"] = pd.to_datetime(result["_announcement_date"], errors="coerce")
        return result.sort_values("_announcement_date", ascending=False).drop(columns="_announcement_date")

    def _dividends_yf(self, code: str) -> pd.DataFrame:
        series = self.yf.Ticker(self._yf_ticker(code)).dividends
        if series is None or series.empty:
            return pd.DataFrame()
        frame = series.rename("每股现金分红(税前)").reset_index()
        date_col = "Date" if "Date" in frame else frame.columns[0]
        frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
        frame["年度"] = "—"
        frame["支付年度"] = frame[date_col].dt.year.astype("Int64").astype(str)
        frame["总分红 (百万元)"] = None
        frame["登记日"] = frame[date_col].dt.date
        frame["除权日"] = frame[date_col].dt.date
        frame["来源"] = "Yahoo Finance"
        frame["口径说明"] = "Yahoo仅提供支付日；所属财年未核定，总额未核定"
        return frame[[
            "年度", "支付年度", "每股现金分红(税前)", "总分红 (百万元)",
            "登记日", "除权日", "来源", "口径说明",
        ]]

    def _dividends(self, code: str) -> pd.DataFrame:
        methods = [(self._dividends_ak, "巨潮资讯（AKShare）"), (self._dividends_yf, "Yahoo Finance")]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = methods[1:]
        elif self.provider == "akshare":
            methods = methods[:1]
        for method, source in methods:
            try:
                result = method(code)
                if not result.empty:
                    self._sources["dividends"] = source
                    return result
            except Exception as exc:
                self._record_error(method.__name__, exc)
        return pd.DataFrame()

    @staticmethod
    def _section(title: str, body: str) -> str:
        return f"{format_header(2, title)}\n\n{body.strip()}\n"

    @staticmethod
    def _table(df: pd.DataFrame, empty_message: str) -> str:
        if df is None or df.empty:
            return empty_message
        display = df.copy().head(12)
        if "end_date" in display:
            display["end_date"] = pd.to_datetime(display["end_date"], errors="coerce").dt.date
            display = display.sort_values("end_date", ascending=False)
        return display.to_markdown(index=False)

    @staticmethod
    def _period_label(value) -> str:
        date = pd.to_datetime(value, errors="coerce")
        if pd.isna(date):
            return str(value)
        suffix = {(3, 31): "Q1", (6, 30): "H1", (9, 30): "Q3", (12, 31): ""}.get(
            (date.month, date.day), date.strftime("-%m-%d")
        )
        return f"{date.year}{suffix}"

    @classmethod
    def _financial_matrix(cls, df: pd.DataFrame, labels: dict[str, str], empty_message: str) -> str:
        """Render the canonical item×period contract consumed by quality_control.py."""
        if df is None or df.empty or "end_date" not in df:
            return empty_message
        display = df.copy()
        display["_date"] = pd.to_datetime(display["end_date"], errors="coerce")
        display = display.dropna(subset=["_date"]).sort_values("_date", ascending=False)
        display = display.drop_duplicates(subset=["_date"], keep="first").head(12)
        periods = [cls._period_label(value) for value in display["_date"]]
        rows = []
        for field, label in labels.items():
            if field not in display:
                continue
            rows.append([label] + [display.iloc[i][field] for i in range(len(display))])
        if not rows:
            return empty_message
        matrix = pd.DataFrame(rows, columns=["项目 (百万元)"] + periods)
        return matrix.to_markdown(index=False)

    @staticmethod
    def _basic_table(basic: pd.DataFrame, empty_message: str) -> str:
        if basic is None or basic.empty:
            return empty_message
        row = basic.iloc[0]
        values = [
            ("股票代码", row.get("ts_code")),
            ("公司名称", row.get("name")),
            ("行业", row.get("industry")),
            ("当前价格", row.get("close")),
            ("交易日", row.get("trade_date")),
            ("上市时间", row.get("list_date")),
            ("币种", row.get("currency")),
            ("PE (TTM)", row.get("pe_ttm")),
            ("PB", row.get("pb")),
            ("总股本（百万股）", row.get("shares") / 1e6 if pd.notna(row.get("shares")) else None),
            ("总市值 (万元)", row.get("market_cap_mm") * 100 if pd.notna(row.get("market_cap_mm")) else None),
            ("来源", row.get("source")),
        ]
        lines = ["| 项目 | 内容 |", "| --- | ---: |"]
        for key, value in values:
            if value is not None and pd.notna(value):
                lines.append(f"| {key} | {value} |")
        return "\n".join(lines)

    @classmethod
    def _key_metrics_matrix(cls, financials: dict[str, pd.DataFrame]) -> str:
        income = financials.get("income", pd.DataFrame()).copy()
        balance = financials.get("balancesheet", pd.DataFrame()).copy()
        if income.empty or balance.empty or "end_date" not in income or "end_date" not in balance:
            return "*关键财务指标数据不足。*"
        income["_date"] = pd.to_datetime(income["end_date"], errors="coerce")
        balance["_date"] = pd.to_datetime(balance["end_date"], errors="coerce")
        merged = income.merge(balance, on="_date", suffixes=("_inc", "_bs"))
        merged = merged.dropna(subset=["_date"]).sort_values("_date", ascending=False)
        merged = merged.drop_duplicates(subset=["_date"], keep="first").head(12)
        if merged.empty:
            return "*关键财务指标数据不足。*"

        def ratio(numerator, denominator):
            n = pd.to_numeric(numerator, errors="coerce")
            d = pd.to_numeric(denominator, errors="coerce")
            return n / d.where(d.ne(0)) * 100.0

        metrics = {}
        if {"revenue", "operate_cost"} <= set(merged.columns):
            metrics["综合毛利率 (%)"] = ratio(merged["revenue"] - merged["operate_cost"], merged["revenue"])
        if {"revenue", "n_income_attr_p"} <= set(merged.columns):
            metrics["归母净利率 (%)"] = ratio(merged["n_income_attr_p"], merged["revenue"])
        if {"total_liab", "total_assets"} <= set(merged.columns):
            metrics["资产负债率 (%)"] = ratio(merged["total_liab"], merged["total_assets"])
        if {"n_income_attr_p", "total_hldr_eqy_exc_min_int"} <= set(merged.columns):
            metrics["ROE (%)"] = ratio(merged["n_income_attr_p"], merged["total_hldr_eqy_exc_min_int"])
        if not metrics:
            return "*关键财务指标数据不足。*"
        periods = [cls._period_label(value) for value in merged["_date"]]
        rows = [[label] + list(values) for label, values in metrics.items()]
        return pd.DataFrame(rows, columns=["指标"] + periods).to_markdown(index=False)

    def _get_annual_df(self, key: str) -> pd.DataFrame:
        df = self._store.get(key, pd.DataFrame()).copy()
        if df.empty or "end_date" not in df:
            return df
        dates = pd.to_datetime(df["end_date"], errors="coerce")
        annual = df[dates.dt.month.eq(12) & dates.dt.day.eq(31)]
        return annual if not annual.empty else df

    def _get_annual_series(self, key: str, col: str) -> list[tuple[str, float | None]]:
        df = self._get_annual_df(key)
        if df.empty or col not in df:
            return []
        return [(str(row.get("end_date")), self._safe_float(row.get(col))) for _, row in df.iterrows()]

    def _get_payout_by_year(self) -> dict[int, float]:
        return {}

    def assemble_data_pack(self, code: str) -> str:
        self.code = validate_stock_code(code)
        self._errors = []
        self._sources = {}
        history = self._history(self.code)
        basic = self._basic(self.code, history)
        financials = self._financials(self.code)
        self._current_shares = (
            self._safe_float(basic.iloc[0].get("shares")) if not basic.empty else None
        )
        dividends = self._dividends(self.code)
        self._store = {
            "basic_info": basic, "weekly_prices": history, "dividends": dividends,
            **financials,
        }

        if not history.empty:
            latest = history.iloc[-1]
            market_body = "\n".join([
                f"- 最新交易日: {pd.Timestamp(latest['date']).date()}",
                f"- 收盘价: {latest.get('close')}",
                f"- 近 52 周最高: {history.tail(252)['high'].max() if 'high' in history else '—'}",
                f"- 近 52 周最低: {history.tail(252)['low'].min() if 'low' in history else '—'}",
            ])
            annual_prices = history.assign(year=history["date"].dt.year).groupby("year", as_index=False).agg(
                年末收盘=("close", "last"), 年内最高=("high", "max"), 年内最低=("low", "min")
            ).tail(10)
        else:
            market_body = "⚠️ 数据不可用：AKShare 与 Yahoo Finance 均未返回行情。"
            annual_prices = pd.DataFrame()

        basic_body = self._basic_table(basic, "⚠️ 数据不可用：公开数据源未返回公司基本信息。")

        cashflow = financials["cashflow"].copy()
        if "n_cashflow_act" in cashflow and "capex" in cashflow:
            cashflow["free_cash_flow"] = cashflow["n_cashflow_act"] - cashflow["capex"].abs()

        source_values = list(dict.fromkeys(self._sources.values()))
        source_label = " + ".join(source_values) if source_values else "AKShare + Yahoo Finance（免费公开数据源）"
        lines = [
            format_header(1, f"数据包 — {self.code}"), "",
            f"*生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}*",
            f"*数据来源: {source_label}*", f"*金额单位: {self._unit_label()}（除特殊标注）*", "", "---", "",
            self._section("1. 基本信息", basic_body), self._section("2. 市场行情", market_body),
            self._section("3. 合并利润表", self._financial_matrix(financials["income"], self.INCOME_LABELS, "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("4. 合并资产负债表", self._financial_matrix(financials["balancesheet"], self.BALANCE_LABELS, "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("5. 现金流量表", self._financial_matrix(cashflow, self.CASHFLOW_LABELS, "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("6. 分红历史", self._table(dividends, "⚠️ 数据不可用，请以年报分红方案补充。")),
            self._section("7. 股东与治理", "*待年报与公告补充。*"),
            self._section("8. 行业与竞争", "*待年报与公开资料补充。*"),
            self._section("9. 主营业务构成", "*待年报分部数据补充。*"),
            self._section("10. 管理层讨论与分析 (MD&A)", "*待年报补充。*"),
            self._section("11. 十年周线行情", self._table(annual_prices, "⚠️ 历史行情不可用。")),
            self._section("12. 关键财务指标", self._key_metrics_matrix(financials)),
            self._section("13. 风险警示", "*请结合年报、交易所公告和公开资料补充。*"),
            self._section("14. 无风险利率", "*估值时从央行/财政部/美国财政部等官方来源获取并标注日期。*"),
            self._section("15. 股票回购", "*待年报与公告补充。*"),
            self._section("16. 股权质押", "*待交易所公告补充。*"),
            self._section("17. 衍生指标（Python 预计算）", "*由 quality_control.py / valuation_engine.py 计算，不由数据源直接提供。*"),
        ]
        if self._errors:
            lines.append(self._section("数据源降级记录", "\n".join(f"- {item}" for item in self._errors)))
        return "\n".join(lines)

    def refresh_market_sections(self, code: str, existing_content: str) -> str:
        refreshed = self.assemble_data_pack(code)
        if "⚠️ 数据不可用：AKShare 与 Yahoo Finance 均未返回行情" in refreshed and existing_content.strip():
            print("[market_collector] 行情刷新失败，保留原数据包", file=sys.stderr)
            return existing_content
        if not existing_content.strip():
            return refreshed

        # Refreshing prices must not erase previously collected statements
        # when a financial endpoint is temporarily unavailable. Replace only
        # the market-sensitive sections and keep the rest byte-for-byte.
        result = existing_content
        for title in ("1. 基本信息", "2. 市场行情", "11. 十年周线行情"):
            pattern = re.compile(
                rf"^##\s+{re.escape(title)}\s*$.*?(?=^##\s|\Z)",
                re.MULTILINE | re.DOTALL,
            )
            source = pattern.search(refreshed)
            if source and pattern.search(result):
                result = pattern.sub(source.group(0), result, count=1)
        generated = re.search(r"\*生成时间:[^\n]*", refreshed)
        if generated:
            result = re.sub(r"\*生成时间:[^\n]*", generated.group(0), result, count=1)
        return result

    @staticmethod
    def _check_staleness(content: str) -> int:
        match = re.search(r"\*生成时间:\s*(\d{4}-\d{2}-\d{2})", content)
        if not match:
            return 999
        return (pd.Timestamp.now().normalize() - pd.Timestamp(match.group(1))).days


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="使用免费公开数据源采集股票市场与财务数据")
    parser.add_argument("--code", required=True, help="600887、00700.HK 或 AAPL")
    parser.add_argument("--output", default="output/data_pack_market.md")
    parser.add_argument("--provider", choices=("auto", "akshare", "yfinance"), default="auto")
    parser.add_argument("--cache-dir", help="yfinance 可写缓存目录（默认使用系统临时目录）")
    parser.add_argument("--refresh-market", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    code = validate_stock_code(args.code)
    if args.dry_run:
        print(f"code={code}\nprovider={args.provider}\noutput={args.output}")
        return 0
    client = MarketDataClient(args.provider, cache_dir=args.cache_dir)
    output_path = Path(args.output)
    if args.refresh_market and output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        data_pack = client.refresh_market_sections(code, existing)
    else:
        data_pack = client.assemble_data_pack(code)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(data_pack, encoding="utf-8")
    print(f"Output written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
