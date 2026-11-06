"""Tushare 内置数据源 provider (https://tushare.pro)。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily        个股(tushare daily) / ETF(fund_daily) / 指数(index_daily) 日K, 原始价
  - adj_factor   除权因子(上游为累积因子, 此处推导为单事件比值)
  - financial    财务五表(metrics / income / balance_sheet / cash_flow / shares)
未声明 realtime / depth5 / minute → provider_has_dataset 为 False, 自动回退 TickFlow。
分钟数据不接入: 上游 stk_mins 限频 1 次/小时(实测 40203), 无法支撑同步节奏。

单位与口径(实测确认, 不可凭字段名推断):
  - daily / fund_daily / index_daily 的 vol 单位为手, 与内部契约一致, 直接透传;
    amount 单位为千元 → ×1000 转为元。
  - daily_basic 的 total_share / float_share 单位为万股 → ×10000 转股;
    balancesheet.total_share 单位已是股, 直接透传。
  - adj_factor 为后复权累积因子, 仅在除权日跳变且方向为上调(实测 000001.SZ
    2023-06-14 除权日 113.936 → 116.713); 内部契约 ex_factor 为单事件
    「除权日前收盘 / 除权参考价」比值(> 1) → ex_factor = 当日因子 / 前一交易日因子,
    只保留变化日(变化日即除权日)。
  - 单次请求硬上限 6000 行(实测) → 长区间按日期窗口分片。
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.tushare import client as tushare_client
from app.plugins.tushare.client import TushareClient, TushareError

logger = logging.getLogger(__name__)

# 只声明真实提供的数据集; 其余数据集 provider_has_dataset 返回 False → 回退 tickflow
_DATASETS = ("daily", "adj_factor", "financial")

API_KEY_ENV = "TUSHARE_API_KEY"
SECRETS_FIELD = "tushare_api_key"  # UI 配置的 Token 存 secrets.json, 优先级高于 .env

# 单只标的单次请求的日期窗口跨度(≈5.5 年, 约 1350 个交易日, 远低于上游 6000 行硬上限)
_WINDOW_DAYS = 2000
# 逐请求节流, 降低触发上游频率限制 (40203) 的概率
_REQUEST_INTERVAL_S = 0.15
# 除权因子向前多拉的日历天数: 保证窗口内首个除权日能拿到「前一交易日累积因子」算比值
_ADJ_LOOKBACK_DAYS = 40
# 财务历史回看窗口: latest_only=False 时取近 3 年报告期(约 12 期季报)
_FINANCIAL_LOOKBACK_DAYS = 1095

# 资产类型 → 上游日K接口
_ASSET_DAILY_API = {"stock": "daily", "etf": "fund_daily", "index": "index_daily"}
# 资产类型 → 内部财务表名 → 上游报表接口
_STATEMENT_API = {"income": "income", "balance_sheet": "balancesheet", "cash_flow": "cashflow"}

# stock_basic.exchange 取值(SSE/SZSE/BSE) → 内部 exchange 代码(SH/SZ/BJ)
_EXCHANGE_CODE = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
# ts_code 后缀 → 内部 exchange 代码(优先按后缀推导, 不依赖上游字段是否返回)
_SUFFIX_EXCHANGE = {".SH": "SH", ".SZ": "SZ", ".BJ": "BJ"}

# 财务字段映射: Tushare 原始字段 → 项目 canonical 列名(TickFlow 口径, 前端财务页与
# 回测 FUNDAMENTAL_FACTORS 按此消费)。上游没有对应字段的 canonical 列不映射,
# 页面显示为 —(不伪造)。金额单位: 元; 百分比列: 百分点, 与上游 fina_indicator 一致。
_INCOME_FIELD_MAP = {
    "revenue": "revenue",
    "oper_cost": "operating_cost",
    "operate_profit": "operating_profit",
    "sell_exp": "selling_expense",
    "admin_exp": "admin_expense",
    "rd_exp": "rd_expense",
    "fin_exp": "financial_expense",
    "non_oper_income": "non_operating_income",
    "non_oper_exp": "non_operating_expense",
    "total_profit": "total_profit",
    "income_tax": "income_tax",
    "n_income": "net_income",
    "n_income_attr_p": "net_income_attributable",
    "basic_eps": "basic_eps",
    "diluted_eps": "diluted_eps",
}
_BALANCE_FIELD_MAP = {
    "total_assets": "total_assets",
    "total_cur_assets": "total_current_assets",
    "total_nca": "total_non_current_assets",
    "money_cap": "cash_and_equivalents",
    "accounts_receiv": "accounts_receivable",
    "inventories": "inventory",
    "fix_assets": "fixed_assets",
    "intan_assets": "intangible_assets",
    "goodwill": "goodwill",
    "total_liab": "total_liabilities",
    "total_cur_liab": "total_current_liabilities",
    "total_ncl": "total_non_current_liabilities",
    "st_borr": "short_term_borrowing",
    "lt_borr": "long_term_borrowing",
    "acct_payable": "accounts_payable",
    "total_hldr_eqy_inc_min_int": "total_equity",
    "total_hldr_eqy_exc_min_int": "equity_attributable",
    "undistr_porfit": "retained_earnings",
    "minority_int": "minority_interest",
}
_CASHFLOW_FIELD_MAP = {
    "n_cashflow_act": "net_operating_cash_flow",
    "n_cashflow_inv_act": "net_investing_cash_flow",
    "n_cash_flows_fnc_act": "net_financing_cash_flow",
    "c_pay_acq_const_fiolta": "capex",
    "n_incr_cash_cash_equ": "net_cash_change",
}
# 上游 fina_indicator 无「摊薄 ROE」「存货周转率」对应字段 → roe_diluted /
# inventory_turnover 不映射, 页面显示为 —。
_METRICS_FIELD_MAP = {
    "eps": "eps_basic",
    "dt_eps": "eps_diluted",
    "bps": "bps",
    "ocfps": "ocfps",
    "roe": "roe",
    "roa": "roa",
    "grossprofit_margin": "gross_margin",
    "netprofit_margin": "net_margin",
    "debt_to_assets": "debt_to_asset_ratio",
    "or_yoy": "revenue_yoy",
    "netprofit_yoy": "net_income_yoy",
    "ocf_to_sales": "operating_cash_to_revenue",
}

_STATEMENT_FIELD_MAPS = {
    "income": _INCOME_FIELD_MAP,
    "balance_sheet": _BALANCE_FIELD_MAP,
    "cash_flow": _CASHFLOW_FIELD_MAP,
}

_ADJ_SCHEMA = {
    "symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64,
    "dividend": pl.Float64, "bonus": pl.Float64, "allot": pl.Float64,
    "allot_price": pl.Float64, "prev_close": pl.Float64,
}


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: Token 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    return False, f"缺少 Token(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Token 实探一次轻量接口(先探后存, 对齐 /plugin-key 语义)。不落盘。"""
    client = None
    try:
        client = tushare_client.TushareClient(token=api_key, timeout=10.0)
        client.post(
            "trade_cal",
            {"exchange": "SSE", "start_date": "20240102", "end_date": "20240105"},
        )
        return True, "ok"
    except TushareError as e:
        return False, f"Token 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "Tushare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_of_compact(value) -> date | None:
    """上游 'YYYYMMDD' → date。非法值返回 None。"""
    s = str(value or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _compact_of_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _iso_of_compact(value) -> str | None:
    d = _date_of_compact(value)
    return d.isoformat() if d is not None else None


def _exchange_of_symbol(symbol: str, fallback: str = "") -> str | None:
    """ts_code 后缀 → 内部 exchange 代码; 无法识别时回退上游 exchange 字段。"""
    suffix = symbol[-3:].upper()
    return _SUFFIX_EXCHANGE.get(suffix) or _EXCHANGE_CODE.get(fallback.upper())


def _split_window(start_d: date, end_d: date) -> list[tuple[date, date]]:
    """把 [start_d, end_d] 切成不超过 _WINDOW_DAYS 的日历窗口, 绕开单次 6000 行上限。"""
    windows: list[tuple[date, date]] = []
    cur = start_d
    while cur <= end_d:
        stop = min(cur + timedelta(days=_WINDOW_DAYS - 1), end_d)
        windows.append((cur, stop))
        cur = stop + timedelta(days=1)
    return windows


class TushareProvider:
    """Tushare 数据源。只提供 daily / adj_factor / financial, 其余数据集回退 TickFlow。"""

    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self._client: TushareClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TushareClient:
        if self._client is None:
            self._client = tushare_client.TushareClient(token=get_api_key())
        return self._client

    def _post(self, api_name: str, params: dict, fields: str = "") -> list[dict]:
        return self._get_client().post(api_name, params, fields)

    # ---- 交易日历 ----
    def _trade_days(self, start_d: date, end_d: date) -> list[date]:
        """A 股交易日(以上交所日历为准)。按年分片调用, 避开单次 6000 行上限。"""
        days: list[date] = []
        cur = start_d
        while cur <= end_d:
            stop = min(date(cur.year, 12, 31), end_d)
            rows = self._post("trade_cal", {
                "exchange": "SSE",
                "start_date": _compact_of_date(cur),
                "end_date": _compact_of_date(stop),
                "is_open": "1",
            })
            days.extend(d for d in (_date_of_compact(r.get("cal_date")) for r in rows) if d)
            cur = date(cur.year + 1, 1, 1)
        return sorted(set(days))

    # ---- 标的维表 ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """标的维表(仅 stock, 与 instrument_sync 的调用一致)。

        股本字段中 listing_date 取自 stock_basic.list_date; total_shares /
        float_shares 取自最新交易日 daily_basic(万股 → 股), 上游未覆盖的新股留空,
        由 instrument_sync._preserve_share_fields 用历史维表补全; tick_size 上游无此
        字段恒为 None; 涨跌停随交易日变化, 不由维表提供。
        """
        if asset_type != "stock":
            return []
        rows = self._post(
            "stock_basic",
            {"list_status": "L"},
            fields="ts_code,symbol,name,exchange,list_date,list_status",
        )
        shares = self._latest_shares()
        out: list[dict] = []
        for r in rows:
            symbol = str(r.get("ts_code") or "").strip().upper()
            if not symbol:
                continue
            sh = shares.get(symbol) or {}
            out.append({
                "symbol": symbol,
                "name": r.get("name") or symbol,
                "code": str(r.get("symbol") or symbol.split(".")[0]),
                "exchange": _exchange_of_symbol(symbol, str(r.get("exchange") or "")),
                "region": "CN",
                "type": asset_type,
                "ext": {
                    "listing_date": _iso_of_compact(r.get("list_date")),
                    "total_shares": sh.get("total_shares"),
                    "float_shares": sh.get("float_shares"),
                    "tick_size": None,
                    "limit_up": None,
                    "limit_down": None,
                },
            })
        return out

    def _latest_shares(self) -> dict[str, dict]:
        """最新交易日的股本(股)。daily_basic 按交易日查询, 最新一日尚未发布时回退前一日。"""
        today = date.today()
        try:
            days = self._trade_days(today - timedelta(days=30), today)
        except TushareError as e:
            logger.warning("Tushare 交易日历获取失败, 股本字段留空: %s", e)
            return {}
        for d in sorted(days, reverse=True)[:3]:
            try:
                rows = self._post(
                    "daily_basic",
                    {"trade_date": _compact_of_date(d)},
                    fields="ts_code,total_share,float_share",
                )
            except TushareError as e:
                # 股本仅为维表富化字段, 取数失败不应让整张维表同步失败
                logger.warning("Tushare 股本快照 %s 取数失败: %s", d, e)
                continue
            if not rows:
                continue
            logger.info("Tushare 股本快照取自 %s: %d 只", d, len(rows))
            return {
                str(r["ts_code"]).strip().upper(): {
                    "total_shares": self._to_shares(r.get("total_share")),
                    "float_shares": self._to_shares(r.get("float_share")),
                }
                for r in rows
                if r.get("ts_code")
            }
        logger.warning("Tushare 股本快照为空, instruments 股本字段留空待历史维表补全")
        return {}

    @staticmethod
    def _to_shares(value) -> float | None:
        """daily_basic 的股本单位为万股 → 股。"""
        v = _to_float(value)
        return None if v is None else v * 10_000.0

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日K → 内部契约。原始价, vol 直接透传(单位已是手), amount 千元 → 元。"""
        chunks = [
            df
            for df in self.iter_daily(
                symbols,
                start_time=start_time,
                end_time=end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """分批产出日K, 供历史同步逐批落盘。

        取数按请求数择优选路(两者结果一致):
        - 标签少/区间长 → 逐标的请求 + 日期窗口分片;
        - 标签多/区间短(全市场回填) → 逐交易日请求全市场后本地按 symset 过滤,
          避免「5500 标的 × 250 天」退化成数千次请求。
        """
        api_name = _ASSET_DAILY_API.get(asset_type)
        if not symbols or api_name is None:
            return
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        if start_d > end_d:
            return
        symset = {str(s).strip().upper() for s in symbols if str(s).strip()}
        if not symset:
            return

        mode, tasks = self._plan(api_name, symset, start_d, end_d)
        total = len(tasks)
        for i, (key, win_start, win_end) in enumerate(tasks):
            if i:
                time.sleep(_REQUEST_INTERVAL_S)
            keep = symset if mode == "date" else None
            try:
                rows = self._post(api_name, self._daily_params(mode, key, win_start, win_end))
            except TushareError as e:
                logger.warning("Tushare 日K %s 取数失败(%s %s~%s): %s",
                               api_name, key, win_start, win_end, e)
                rows = []
            df = self._daily_frame(rows, keep)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            if not df.is_empty():
                yield df

    @staticmethod
    def _daily_params(mode: str, key: str, win_start: date, win_end: date) -> dict:
        if mode == "date":
            return {"trade_date": _compact_of_date(win_start)}
        return {
            "ts_code": key,
            "start_date": _compact_of_date(win_start),
            "end_date": _compact_of_date(win_end),
        }

    def _plan(
        self, api_name: str, symset: set[str], start_d: date, end_d: date
    ) -> tuple[str, list[tuple[str, date, date]]]:
        """按预计请求数择优选路, 返回 (mode, [(key, win_start, win_end)])。"""
        windows = _split_window(start_d, end_d)
        symbol_tasks = [(s, ws, we) for s in sorted(symset) for ws, we in windows]
        try:
            trade_days = self._trade_days(start_d, end_d)
        except TushareError as e:
            logger.warning("Tushare 交易日历获取失败, 改用逐标的取数: %s", e)
            trade_days = []
        if trade_days and len(trade_days) < len(symbol_tasks):
            return "date", [(d.isoformat(), d, d) for d in trade_days]
        return "symbol", symbol_tasks

    def _daily_frame(self, rows: list[dict], keep: set[str] | None) -> pl.DataFrame:
        out: list[dict] = []
        for r in rows:
            symbol = str(r.get("ts_code") or "").strip().upper()
            if not symbol or (keep is not None and symbol not in keep):
                continue
            iso = _iso_of_compact(r.get("trade_date"))
            if iso is None:
                continue
            amount = _to_float(r.get("amount"))
            out.append({
                "ts_code": symbol,
                "trade_date": iso,
                "open": _to_float(r.get("open")),
                "high": _to_float(r.get("high")),
                "low": _to_float(r.get("low")),
                "close": _to_float(r.get("close")),
                "vol": _to_float(r.get("vol")),  # 单位: 手, 与内部契约一致
                "amount": amount * 1000.0 if amount is not None else None,  # 千元 → 元
            })
        return normalize_daily(out, source=self.name)

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """除权因子 → 内部契约(symbol/trade_date/ex_factor 单事件比值, 非累积)。

        Tushare adj_factor 为后复权累积因子, 仅在除权日跳变且方向为上调; 复权连续性
        raw_prev × f_prev = ref × f_on → ref = raw_prev × f_prev / f_on, 故内部契约
        ex_factor = raw_prev / ref = f_on / f_prev(> 1), 即 ex_factor = 当日累积因子 /
        前一交易日累积因子, 只保留变化日(变化日即除权日)。事件明细列
        (dividend/bonus/allot/allot_price/prev_close)上游不提供, 置 null(不影响本地
        复权链与受影响判定)。
        窗口向前多拉 _ADJ_LOOKBACK_DAYS, 保证窗口内首个除权日能拿到前一日因子。
        """
        if not symbols:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=3650))
        start_d, end_d = start_dt.date(), end_dt.date()
        if start_d > end_d:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        symset = {str(s).strip().upper() for s in symbols if str(s).strip()}
        if not symset:
            return pl.DataFrame(schema=_ADJ_SCHEMA)

        fetch_start = start_d - timedelta(days=_ADJ_LOOKBACK_DAYS)
        mode, tasks = self._plan("adj_factor", symset, fetch_start, end_d)
        total = len(tasks)
        prev_factor: dict[str, float] = {}
        out: list[dict] = []
        for i, (key, win_start, win_end) in enumerate(tasks):
            if i:
                time.sleep(_REQUEST_INTERVAL_S)
            try:
                rows = self._post("adj_factor", self._daily_params(mode, key, win_start, win_end),
                                  fields="ts_code,trade_date,adj_factor")
            except TushareError as e:
                logger.warning("Tushare 除权因子取数失败(%s %s~%s): %s", key, win_start, win_end, e)
                rows = []
            self._collect_adj(rows, symset if mode == "date" else None,
                              start_d, end_d, prev_factor, out)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
        if not out:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return (
            pl.DataFrame(out, schema=_ADJ_SCHEMA)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    @staticmethod
    def _collect_adj(
        rows: list[dict],
        keep: set[str] | None,
        lo: date,
        hi: date,
        prev_factor: dict[str, float],
        out: list[dict],
    ) -> None:
        """累积因子 → 单事件比值。同一标的的日期必须按升序经过(逐标的/逐交易日皆满足)。"""
        parsed = []
        for r in rows:
            symbol = str(r.get("ts_code") or "").strip().upper()
            if not symbol or (keep is not None and symbol not in keep):
                continue
            d = _date_of_compact(r.get("trade_date"))
            f = _to_float(r.get("adj_factor"))
            if d is None or f is None or f <= 0:
                continue
            parsed.append((d, symbol, f))
        for d, symbol, f in sorted(parsed):
            last = prev_factor.get(symbol)
            prev_factor[symbol] = f
            if last is None or last <= 0 or abs(f - last) <= 1e-12:
                continue
            if not (lo <= d <= hi):
                continue
            out.append({
                "symbol": symbol, "trade_date": d, "ex_factor": f / last,
                "dividend": None, "bonus": None, "allot": None,
                "allot_price": None, "prev_close": None,
            })

    # ---- financial ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据, 映射为 canonical 列(symbol/period_end/announce_date/指标)。

        全部按标的逐只取数(上游财务接口必须传 ts_code)。report_period 合并写入由
        financial_sync 负责, 多源字段并集共存互不覆盖。
        """
        syms = [str(s).strip().upper() for s in symbols if str(s).strip()]
        if not syms:
            return pl.DataFrame()
        if table == "metrics":
            return self._financial_rows("fina_indicator", _METRICS_FIELD_MAP, syms, latest_only)
        if table in _STATEMENT_API:
            return self._financial_rows(
                _STATEMENT_API[table], _STATEMENT_FIELD_MAPS[table], syms, latest_only
            )
        if table == "shares":
            return self._financial_shares(syms, latest_only)
        return pl.DataFrame()

    def _financial_rows(
        self,
        api_name: str,
        field_map: dict[str, str],
        symbols: list[str],
        latest_only: bool,
    ) -> pl.DataFrame:
        client = self._get_client()
        rows_out: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_REQUEST_INTERVAL_S)
            try:
                rows = self._periods(client, api_name, sym, latest_only)
            except TushareError as e:
                logger.warning("Tushare 财务 %s %s 失败: %s", api_name, sym, e)
                continue
            for r in rows:
                row: dict = {
                    "symbol": sym,
                    "period_end": _iso_of_compact(r.get("end_date")),
                    "announce_date": _iso_of_compact(r.get("ann_date")),
                }
                for src, dst in field_map.items():
                    row[dst] = _to_float(r.get(src))
                rows_out.append(row)
        # 稀疏列(如仅个别行业有值的科目)前 100 行可能全空, 默认采样会推断成 Null 列,
        # 后续数值 append 失败 → 全量扫描推断
        return pl.DataFrame(rows_out, infer_schema_length=None) if rows_out else pl.DataFrame()

    def _financial_shares(self, symbols: list[str], latest_only: bool) -> pl.DataFrame:
        """股本表: total_shares 取自 balancesheet.total_share(单位已是股, 按报告期)。

        float_shares 上游只按交易日提供(daily_basic, 无报告期口径), 无法对应到
        报告期 → 置 null; 流通股本由 instruments 维表提供, 供 enriched 换手率使用。
        """
        client = self._get_client()
        rows_out: list[dict] = []
        for i, sym in enumerate(symbols):
            if i:
                time.sleep(_REQUEST_INTERVAL_S)
            try:
                rows = self._periods(
                    client, "balancesheet", sym, latest_only,
                    fields="ts_code,ann_date,end_date,total_share",
                )
            except TushareError as e:
                logger.warning("Tushare 财务 balancesheet %s 失败: %s", sym, e)
                continue
            for r in rows:
                rows_out.append({
                    "symbol": sym,
                    "period_end": _iso_of_compact(r.get("end_date")),
                    "announce_date": _iso_of_compact(r.get("ann_date")),
                    "total_shares": _to_float(r.get("total_share")),
                    "float_shares": None,
                })
        return pl.DataFrame(rows_out, infer_schema_length=None) if rows_out else pl.DataFrame()

    @staticmethod
    def _periods(
        client: TushareClient,
        api_name: str,
        symbol: str,
        latest_only: bool,
        fields: str = "",
    ) -> list[dict]:
        """取一只标的的报告期数据。同一报告期可能有更正/多报告类型 → 按公告日取最后一条,
        并统一按报告期降序返回; latest_only 时只留最新一期。"""
        end = date.today()
        rows = client.post(api_name, {
            "ts_code": symbol,
            "start_date": _compact_of_date(end - timedelta(days=_FINANCIAL_LOOKBACK_DAYS)),
            "end_date": _compact_of_date(end),
        }, fields)
        latest: dict[str, dict] = {}
        for r in rows:
            key = str(r.get("end_date") or "")
            if not key:
                continue
            cur = latest.get(key)
            if cur is None or str(r.get("ann_date") or "") >= str(cur.get("ann_date") or ""):
                latest[key] = r
        ordered = [latest[k] for k in sorted(latest, reverse=True)]
        return ordered[:1] if latest_only else ordered

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」: 直连上游单标的取数。

        刻意不走 get_daily / get_adj_factors 的批量软失败路径 —— 批量同步遇上游
        报错逐块跳过来保证长跑不中断, 但试拉是诊断入口, 需要把错误如实回给用户,
        否则失败会伪装成「0 行」。
        """
        syms = [s for s in (symbols or []) if s]
        try:
            if dataset == "daily":
                df = self._probe_daily(syms[:3] or ["000001.SZ"])
            elif dataset == "adj_factor":
                df = self._probe_adj_factor(syms[:3] or ["000001.SZ"])
            elif dataset == "financial":
                df = self.get_financials("metrics", syms[:1] or ["600519.SH"], latest_only=True)
            else:
                return {
                    "provider": self.name, "dataset": dataset, "rows": 0,
                    "error": f"Tushare 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
                }
        except TushareError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": head,
        }

    def _probe_daily(self, symbols: list[str]) -> pl.DataFrame:
        """试拉日K(个股口径), 近 30 天。"""
        end_d = date.today()
        start_d = end_d - timedelta(days=30)
        frames = []
        for sym in symbols:
            rows = self._post(_ASSET_DAILY_API["stock"], {
                "ts_code": sym,
                "start_date": _compact_of_date(start_d),
                "end_date": _compact_of_date(end_d),
            })
            frame = self._daily_frame(rows, None)
            if not frame.is_empty():
                frames.append(frame)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def _probe_adj_factor(self, symbols: list[str]) -> pl.DataFrame:
        """试拉除权因子, 近 1 年(同样向前多拉, 保证首个除权日能算比值)。"""
        end_d = date.today()
        start_d = end_d - timedelta(days=365)
        prev_factor: dict[str, float] = {}
        out: list[dict] = []
        for sym in symbols:
            rows = self._post("adj_factor", {
                "ts_code": sym,
                "start_date": _compact_of_date(start_d - timedelta(days=_ADJ_LOOKBACK_DAYS)),
                "end_date": _compact_of_date(end_d),
            }, fields="ts_code,trade_date,adj_factor")
            self._collect_adj(rows, None, start_d, end_d, prev_factor, out)
        if not out:
            return pl.DataFrame(schema=_ADJ_SCHEMA)
        return (
            pl.DataFrame(out, schema=_ADJ_SCHEMA)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )
