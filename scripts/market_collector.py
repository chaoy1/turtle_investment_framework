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

    def __init__(self, provider: str = "auto"):
        if provider not in {"auto", "akshare", "yfinance"}:
            raise ValueError("provider must be auto, akshare, or yfinance")
        self.provider = provider
        self._store: dict[str, pd.DataFrame] = {}
        self._errors: list[str] = []
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

    def _history(self, code: str) -> pd.DataFrame:
        methods = [self._history_ak, self._history_yf]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = [self._history_yf]
        elif self.provider == "akshare":
            methods = [self._history_ak]
        for method in methods:
            try:
                df = method(code)
                if not df.empty:
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
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
                    data["source"] = "AKShare" if method == self._basic_ak else "Yahoo Finance"
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
        income_raw = self.ak.stock_profit_sheet_by_report_em(symbol=symbol)
        balance_raw = self.ak.stock_balance_sheet_by_report_em(symbol=symbol)
        cash_raw = self.ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
        income = self._pick(income_raw, {
            "end_date": ("REPORT_DATE",), "revenue": ("TOTAL_OPERATE_INCOME", "OPERATE_INCOME"),
            "operate_profit": ("OPERATE_PROFIT",), "n_income_attr_p": ("PARENT_NETPROFIT",),
            "net_income": ("NETPROFIT",), "basic_eps": ("BASIC_EPS",),
        })
        balance = self._pick(balance_raw, {
            "end_date": ("REPORT_DATE",), "money_cap": ("MONETARYFUNDS",),
            "total_assets": ("TOTAL_ASSETS",), "total_liab": ("TOTAL_LIABILITIES",),
            "total_hldr_eqy_exc_min_int": ("PARENT_EQUITY", "TOTAL_EQUITY"),
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
        methods = [self._financials_ak, self._financials_yf]
        if self.provider == "yfinance" or self._market(code) != "A":
            methods = [self._financials_yf]
        elif self.provider == "akshare":
            methods = [self._financials_ak]
        for method in methods:
            try:
                result = method(code)
                if any(not frame.empty for frame in result.values()):
                    return result
            except Exception as exc:
                self._record_error(method.__name__, exc)
        return {"income": pd.DataFrame(), "balancesheet": pd.DataFrame(), "cashflow": pd.DataFrame()}

    def _dividends(self, code: str) -> pd.DataFrame:
        try:
            series = self.yf.Ticker(self._yf_ticker(code)).dividends
            if series is None or series.empty:
                return pd.DataFrame()
            return series.rename("cash_div").reset_index().rename(columns={"Date": "date"})
        except Exception as exc:
            self._record_error("yfinance dividends", exc)
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
        history = self._history(self.code)
        basic = self._basic(self.code, history)
        financials = self._financials(self.code)
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

        if not basic.empty:
            row = basic.iloc[0]
            basic_body = "\n".join(f"- {key}: {value}" for key, value in row.items() if pd.notna(value))
        else:
            basic_body = "⚠️ 数据不可用：公开数据源未返回公司基本信息。"

        source_label = "AKShare + Yahoo Finance（免费公开数据源）"
        lines = [
            format_header(1, f"数据包 — {self.code}"), "",
            f"*生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}*",
            f"*数据来源: {source_label}*", f"*金额单位: {self._unit_label()}（除特殊标注）*", "", "---", "",
            self._section("1. 基本信息", basic_body), self._section("2. 市场行情", market_body),
            self._section("3. 合并利润表", self._table(financials["income"], "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("4. 合并资产负债表", self._table(financials["balancesheet"], "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("5. 现金流量表", self._table(financials["cashflow"], "⚠️ 数据不可用，请以年报 PDF 补充。")),
            self._section("6. 分红历史", self._table(dividends, "⚠️ 数据不可用，请以年报分红方案补充。")),
            self._section("7. 股东与治理", "*待年报与公告补充。*"),
            self._section("8. 行业与竞争", "*待年报与公开资料补充。*"),
            self._section("9. 主营业务构成", "*待年报分部数据补充。*"),
            self._section("10. 管理层讨论与分析 (MD&A)", "*待年报补充。*"),
            self._section("11. 十年周线行情", self._table(annual_prices, "⚠️ 历史行情不可用。")),
            self._section("12. 关键财务指标", "*由质量控制与估值脚本基于标准化报表计算。*"),
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
    parser.add_argument("--refresh-market", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    code = validate_stock_code(args.code)
    if args.dry_run:
        print(f"code={code}\nprovider={args.provider}\noutput={args.output}")
        return 0
    client = MarketDataClient(args.provider)
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
