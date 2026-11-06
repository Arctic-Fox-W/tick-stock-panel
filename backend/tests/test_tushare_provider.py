"""TushareProvider 契约与单位标准化测试。

不依赖真实网络: 用假 TushareClient 返回实测样例行(列式响应已解包), 验证
列式信封解析、字段映射、单位口径(amount 千元→元, daily_basic 股本 万股→股)、
除权因子方向(累计后复权因子 → 单事件比值 f_on/f_prev > 1, 只保留除权日)、
标的维表(ts_code 后缀 → 交易所)、能力声明(未声明数据集回退 tickflow)
与设置页试拉。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.tushare import client as tc
from app.plugins.tushare import provider as tp
from app.plugins.tushare.provider import TushareProvider


class _FakeClient:
    """按 api_name 返回预置行, 记录调用供断言。值可为行列表或 callable(params, fields)。"""

    def __init__(self, responses: dict | None = None, error: Exception | None = None):
        self.responses = responses or {}
        self.error = error
        self.calls: list[dict] = []

    def post(self, api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
        self.calls.append({"api": api_name, "params": dict(params or {}), "fields": fields})
        if self.error:
            raise self.error
        value = self.responses.get(api_name, [])
        if callable(value):
            return list(value(params or {}, fields))
        return [dict(r) for r in value]

    def close(self) -> None:
        pass


def _provider(monkeypatch, responses=None, error=None) -> tuple[TushareProvider, _FakeClient]:
    fake = _FakeClient(responses, error)
    monkeypatch.setattr(tp, "tushare_client", SimpleNamespace(TushareClient=lambda **kw: fake))
    monkeypatch.setattr(tp, "get_api_key", lambda: "test-token")
    return TushareProvider(), fake


def _daily_row(ts_code="000001.SZ", trade_date="20240110", close=9.09, **over):
    row = {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": close + 0.07,
        "high": close + 0.10,
        "low": close,
        "close": close,
        "pre_close": close + 0.09,
        "change": -0.09,
        "pct_chg": -0.9804,
        "vol": 858620.91,
        "amount": 783771.15,
    }
    row.update(over)
    return row


# ---- 客户端: 列式信封解析 ----

def _patch_http(monkeypatch, payload, status_code=200):
    class _Resp:
        def json(self):
            return payload

    _Resp.status_code = status_code

    class _Http:
        def post(self, url, json=None):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(tc.httpx, "Client", lambda **kw: _Http())


def test_client_unpacks_columnar_envelope(monkeypatch):
    """实测信封: data={fields:[...], items:[[...]]} 列式 → 行 dict 列表。"""
    _patch_http(
        monkeypatch,
        {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["000001.SZ", "20240110", 9.09], ["600519.SH", "20240110", 1640.0]],
            },
        },
    )
    rows = tc.TushareClient(token="k").post("daily")
    assert rows == [
        {"ts_code": "000001.SZ", "trade_date": "20240110", "close": 9.09},
        {"ts_code": "600519.SH", "trade_date": "20240110", "close": 1640.0},
    ]


def test_client_empty_fields_returns_empty(monkeypatch):
    _patch_http(monkeypatch, {"code": 0, "msg": "", "data": {"fields": [], "items": []}})
    assert tc.TushareClient(token="k").post("daily") == []


def test_client_raises_on_business_error_code(monkeypatch):
    """实测 40203 = 访问频率超限; 非 0 code 一律抛错, 不静默返回空。"""
    _patch_http(
        monkeypatch,
        {"code": 40203, "msg": "抱歉，您每分钟最多访问该接口xxx次", "data": None},
    )
    with pytest.raises(tc.TushareError, match="40203"):
        tc.TushareClient(token="k").post("stk_mins")


def test_client_raises_on_http_status(monkeypatch):
    _patch_http(monkeypatch, {}, status_code=502)
    with pytest.raises(tc.TushareError, match="502"):
        tc.TushareClient(token="k").post("daily")


def test_client_requires_token():
    with pytest.raises(tc.TushareError):
        tc.TushareClient(token="")


def test_client_sends_token_params_fields(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 0, "data": {"fields": [], "items": []}}

    class _Http:
        def post(self, url, json=None):
            captured["url"], captured["body"] = url, json
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(tc.httpx, "Client", lambda **kw: _Http())
    tc.TushareClient(token="tok").post("adj_factor", {"ts_code": "000001.SZ"}, fields="ts_code")
    assert captured["url"] == "http://api.tushare.pro"
    assert captured["body"] == {
        "api_name": "adj_factor",
        "token": "tok",
        "params": {"ts_code": "000001.SZ"},
        "fields": "ts_code",
    }


# ---- daily: 单位与接口分派 ----

def test_daily_units_amount_thousand_yuan_to_yuan(monkeypatch):
    """核心口径: vol 已是手直接透传; amount 千元 ×1000 → 元(实测 783771.15 → 7.8377e8)。"""
    provider, _ = _provider(monkeypatch, {"daily": [_daily_row()]})
    df = provider.get_daily(["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31))
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df["symbol"].to_list() == ["000001.SZ"]
    assert df["date"].to_list() == [date(2024, 1, 10)]
    assert df["volume"].to_list() == [858620.91]
    assert df["amount"].to_list() == [pytest.approx(783771.15 * 1000)]


def test_daily_uses_fund_and_index_api_by_asset_type(monkeypatch):
    """ETF 走 fund_daily、指数走 index_daily、个股走 daily。"""
    for asset_type, expected in (("stock", "daily"), ("etf", "fund_daily"), ("index", "index_daily")):
        provider, fake = _provider(monkeypatch, {expected: [_daily_row("510300.SH")]})
        df = provider.get_daily(
            ["510300.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31), asset_type=asset_type
        )
        assert [c["api"] for c in fake.calls if c["api"] != "trade_cal"] == [expected]
        assert df.height == 1


def test_daily_unknown_asset_type_returns_empty(monkeypatch):
    provider, fake = _provider(monkeypatch, {"daily": [_daily_row()]})
    df = provider.get_daily(["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31),
                            asset_type="bond")
    assert df.is_empty() and fake.calls == []


def test_daily_soft_fail_keeps_other_symbols(monkeypatch):
    """逐标的失败软跳过: 上游报错不中断整轮。"""
    def _rows(params, fields):
        if params.get("ts_code") == "600519.SH":
            raise tc.TushareError("频率超限")
        return [_daily_row()]

    provider, _ = _provider(monkeypatch, {"daily": _rows})
    df = provider.get_daily(
        ["600519.SH", "000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df["symbol"].unique().to_list() == ["000001.SZ"]


def test_iter_daily_reports_progress_each_task(monkeypatch):
    provider, _ = _provider(monkeypatch, {"daily": [_daily_row()]})
    progress: list[tuple[int, int]] = []
    chunks = list(provider.iter_daily(
        ["000001.SZ", "600519.SH"],
        datetime(2024, 1, 1),
        datetime(2024, 1, 31),
        on_chunk_done=lambda c, t: progress.append((c, t)),
    ))
    assert len(chunks) == 2 and progress == [(1, 2), (2, 2)]


# ---- adj_factor: 方向与变化日 ----

def _adj_rows():
    """实测快照: 后复权累计因子仅在除权日(20230614)上调, 其余交易日恒定。"""
    return [
        {"ts_code": "000001.SZ", "trade_date": "20230613", "adj_factor": 113.936},
        {"ts_code": "000001.SZ", "trade_date": "20230614", "adj_factor": 116.713},
        {"ts_code": "000001.SZ", "trade_date": "20230615", "adj_factor": 116.713},
    ]


def test_adj_factor_ratio_is_current_over_previous(monkeypatch):
    """核心口径: 内部 ex_factor = 除权前收盘/除权参考价 > 1 = 当日因子/前一交易日因子。

    上游仅除权日跳变(113.936 → 116.713) → 只产出一行, 且方向必须 > 1,
    否则 pipeline 会造出 5% 的假跌缺口。
    """
    provider, _ = _provider(monkeypatch, {"adj_factor": _adj_rows()})
    df = provider.get_adj_factors(["000001.SZ"], datetime(2023, 6, 1), datetime(2023, 7, 1))
    assert df.height == 1
    assert df["trade_date"].to_list() == [date(2023, 6, 14)]
    assert df["ex_factor"][0] == pytest.approx(116.713 / 113.936)
    assert df["ex_factor"][0] > 1.0


def test_adj_factor_keeps_event_detail_columns_null(monkeypatch):
    """上游不提供事件明细 → 明细列为 null(不伪造), 列结构对齐 fuyao/tdxquant。"""
    provider, _ = _provider(monkeypatch, {"adj_factor": _adj_rows()})
    df = provider.get_adj_factors(["000001.SZ"], datetime(2023, 6, 1), datetime(2023, 7, 1))
    assert df.columns == [
        "symbol", "trade_date", "ex_factor",
        "dividend", "bonus", "allot", "allot_price", "prev_close",
    ]
    assert df.select(pl.sum_horizontal(pl.all().null_count())).item() == 5


def test_adj_factor_filters_to_requested_window(monkeypatch):
    """窗口外的除权事件不输出; 窗口起点前的因子仍用于推导(向前多拉)。"""
    provider, _ = _provider(monkeypatch, {"adj_factor": _adj_rows()})
    df = provider.get_adj_factors(["000001.SZ"], datetime(2023, 7, 1), datetime(2023, 8, 1))
    assert df.is_empty()
    assert df.columns == [
        "symbol", "trade_date", "ex_factor",
        "dividend", "bonus", "allot", "allot_price", "prev_close",
    ]


def test_adj_factor_empty_symbols_returns_schema(monkeypatch):
    provider, fake = _provider(monkeypatch, {"adj_factor": _adj_rows()})
    df = provider.get_adj_factors([], datetime(2023, 6, 1), datetime(2023, 7, 1))
    assert df.is_empty() and fake.calls == []


# ---- 标的维表 ----

def test_instruments_exchange_from_ts_code_suffix_and_shares_in_ten_thousands(monkeypatch):
    """exchange 由 ts_code 后缀推导(SH/SZ/BJ); daily_basic 股本 万股 → 股。"""
    provider, _ = _provider(monkeypatch, {
        "stock_basic": [
            {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台",
             "exchange": "SSE", "list_date": "20010827"},
            {"ts_code": "920306.BJ", "symbol": "920306", "name": "天元重工",
             "exchange": "BSE", "list_date": "20261029"},
        ],
        "trade_cal": [{"cal_date": "20261030"}, {"cal_date": "20261102"}],
        "daily_basic": [
            {"ts_code": "600519.SH", "total_share": 125619.78, "float_share": 125619.78},
        ],
    })
    rows = provider.get_instruments("stock")
    assert [r["symbol"] for r in rows] == ["600519.SH", "920306.BJ"]
    assert [r["exchange"] for r in rows] == ["SH", "BJ"]
    assert rows[0]["region"] == "CN" and rows[0]["type"] == "stock"
    assert rows[0]["ext"]["listing_date"] == "2001-08-27"
    assert rows[0]["ext"]["total_shares"] == pytest.approx(125619.78 * 10_000)
    assert rows[0]["ext"]["float_shares"] == pytest.approx(125619.78 * 10_000)
    # 股本快照未覆盖的新股留空, 交由 instrument_sync 用历史维表补全
    assert rows[1]["ext"]["total_shares"] is None
    # 上游无此字段 → 不伪造
    assert rows[0]["ext"]["tick_size"] is None
    assert rows[0]["ext"]["limit_up"] is None


def test_instruments_non_stock_returns_empty(monkeypatch):
    provider, fake = _provider(monkeypatch, {"stock_basic": []})
    assert provider.get_instruments("etf") == []
    assert fake.calls == []


def test_instruments_shares_snapshot_survives_daily_basic_failure(monkeypatch):
    """股本快照取数失败不致命, 维表仍返回(股本留空)。"""
    def _boom(params, fields):
        raise tc.TushareError("频率超限")

    provider, _ = _provider(monkeypatch, {
        "stock_basic": [{"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台"}],
        "trade_cal": [{"cal_date": "20261030"}],
        "daily_basic": _boom,
    })
    rows = provider.get_instruments("stock")
    assert len(rows) == 1 and rows[0]["ext"]["total_shares"] is None


# ---- 财务 ----

def _income_row():
    return {
        "ts_code": "600519.SH", "ann_date": "20240403", "end_date": "20231231",
        "revenue": 1.5e11, "oper_cost": 1.2e10, "operate_profit": 1.0e11,
        "n_income": 7.5e10, "n_income_attr_p": 7.4e10,
        "basic_eps": 59.49, "diluted_eps": 59.49,
    }


def test_financial_income_maps_to_canonical_columns(monkeypatch):
    provider, _ = _provider(monkeypatch, {"income": [_income_row()]})
    df = provider.get_financials("income", ["600519.SH"], latest_only=True)
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["period_end"] == "2023-12-31"
    assert row["announce_date"] == "2024-04-03"
    assert row["revenue"] == pytest.approx(1.5e11)
    assert row["operating_cost"] == pytest.approx(1.2e10)
    assert row["operating_profit"] == pytest.approx(1.0e11)
    assert row["net_income"] == pytest.approx(7.5e10)
    assert row["net_income_attributable"] == pytest.approx(7.4e10)
    assert row["basic_eps"] == pytest.approx(59.49)


def test_financial_metrics_maps_percent_columns_as_percentage_points(monkeypatch):
    """fina_indicator 百分比列已是百分点口径, 直接透传(不做 /100)。"""
    provider, _ = _provider(monkeypatch, {"fina_indicator": [{
        "ts_code": "600519.SH", "ann_date": "20240403", "end_date": "20231231",
        "eps": 59.49, "bps": 180.0, "roe": 34.2, "roa": 25.1,
        "grossprofit_margin": 91.96, "netprofit_margin": 52.68,
        "debt_to_assets": 18.5, "or_yoy": 18.04, "netprofit_yoy": 19.16,
        "ocf_to_sales": 45.0,
    }]})
    df = provider.get_financials("metrics", ["600519.SH"], latest_only=True)
    row = df.row(0, named=True)
    assert row["eps_basic"] == pytest.approx(59.49)
    assert row["roe"] == pytest.approx(34.2)
    assert row["gross_margin"] == pytest.approx(91.96)
    assert row["debt_to_asset_ratio"] == pytest.approx(18.5)
    assert row["revenue_yoy"] == pytest.approx(18.04)
    assert row["operating_cash_to_revenue"] == pytest.approx(45.0)
    # 上游无对应字段 → 不映射(不伪造)
    assert "roe_diluted" not in df.columns
    assert "inventory_turnover" not in df.columns


def test_financial_periods_deduped_by_latest_announcement(monkeypatch):
    """同一报告期多条(更正公告)取公告日最新; latest_only 只留 1 期。"""
    rows = [
        {"ts_code": "600519.SH", "ann_date": "20240403", "end_date": "20231231", "revenue": 1.0},
        {"ts_code": "600519.SH", "ann_date": "20240420", "end_date": "20231231", "revenue": 1.5},
        {"ts_code": "600519.SH", "ann_date": "20240430", "end_date": "20240331", "revenue": 2.5},
    ]
    provider, _ = _provider(monkeypatch, {"income": rows})
    latest = provider.get_financials("income", ["600519.SH"], latest_only=True)
    assert latest["revenue"].to_list() == [pytest.approx(2.5)]
    full = provider.get_financials("income", ["600519.SH"], latest_only=False)
    # 报告期降序, 且 20231231 取公告日最新那条(1.5)
    assert full["period_end"].to_list() == ["2024-03-31", "2023-12-31"]
    assert full["revenue"].to_list()[1] == pytest.approx(1.5)


def test_financial_shares_from_balancesheet_in_shares_unit(monkeypatch):
    """balancesheet.total_share 单位已是股 → 直接透传; float_shares 无报告期口径 → null。"""
    provider, _ = _provider(monkeypatch, {"balancesheet": [{
        "ts_code": "600519.SH", "ann_date": "20240403", "end_date": "20231231",
        "total_share": 1_256_197_800.0,
    }]})
    df = provider.get_financials("shares", ["600519.SH"], latest_only=True)
    row = df.row(0, named=True)
    assert row["total_shares"] == pytest.approx(1_256_197_800.0)
    assert row["float_shares"] is None


def test_financial_unknown_table_returns_empty(monkeypatch):
    provider, fake = _provider(monkeypatch, {"income": [_income_row()]})
    assert provider.get_financials("dividend", ["600519.SH"]).is_empty()
    assert fake.calls == []


def test_financial_symbol_failure_skips_without_aborting(monkeypatch):
    def _rows(params, fields):
        if params.get("ts_code") == "000001.SZ":
            raise tc.TushareError("频率超限")
        return [_income_row()]

    provider, _ = _provider(monkeypatch, {"income": _rows})
    df = provider.get_financials("income", ["000001.SZ", "600519.SH"], latest_only=True)
    assert df["symbol"].unique().to_list() == ["600519.SH"]


# ---- 线程/资源 ----

def test_close_is_idempotent_and_clears_client(monkeypatch):
    provider, _ = _provider(monkeypatch, {"daily": [_daily_row()]})
    provider._get_client()
    provider.close()
    provider.close()
    assert provider._client is None


# ---- 能力声明与注册 ----

def test_datasets_declaration_excludes_realtime_and_minute():
    """声明 daily/adj_factor/financial; realtime/minute 未声明 → 回退 TickFlow。

    minute 不接入是实测决定: 上游 stk_mins 限频 1 次/小时(40203), 无法支撑同步节奏。
    """
    datasets = TushareProvider().config.datasets
    assert {"daily", "adj_factor", "financial"} <= set(datasets)
    assert "realtime" not in datasets
    assert "minute" not in datasets
    assert "depth5" not in datasets


def test_manifest_declares_entry_and_key_env():
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("tushare")
    assert manifest is not None
    assert manifest["entry"] == "app.plugins.tushare.provider:TushareProvider"
    assert {"daily", "adj_factor", "financial"} <= set(manifest.get("datasets") or [])
    assert manifest.get("runtime") == "none"
    assert manifest.get("api_key_env") == tp.API_KEY_ENV


def test_available_plugin_registered_as_builtin(monkeypatch):
    """Token 就绪时经 loader 注册为 ManagedProvider, 并标记 builtin。"""
    from app.data_providers.custom import loader

    monkeypatch.setattr(tp, "availability", lambda: (True, "ok"))
    loader._register_one_plugin(loader.plugin_manifest("tushare"))
    assert loader._PLUGIN_STATUS["tushare"]["available"] is True
    assert "tushare" in loader._PROVIDERS
    assert loader.provider_has_dataset("tushare", "daily") is True
    assert loader.provider_has_dataset("tushare", "minute") is False


# ---- API Key 解析 (secrets.json > .env) ----

def test_get_api_key_secrets_store_takes_priority(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {tp.SECRETS_FIELD: "tok-from-ui"})
    assert tp.get_api_key() == "tok-from-ui"


def test_get_api_key_falls_back_to_env(monkeypatch):
    from app import secrets_store

    monkeypatch.setenv(tp.API_KEY_ENV, "tok-from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert tp.get_api_key() == "tok-from-env"


def test_availability_requires_key(monkeypatch):
    from app import secrets_store

    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    ok, reason = tp.availability()
    assert ok is False and tp.API_KEY_ENV in reason


def test_probe_api_key_ok(monkeypatch):
    monkeypatch.setattr(tp, "tushare_client", SimpleNamespace(TushareClient=lambda **kw: _FakeClient()))
    assert tp.probe_api_key("tok") == (True, "ok")


def test_probe_api_key_invalid(monkeypatch):
    fake = _FakeClient(error=tc.TushareError("Tushare 接口错误 code=40001: 无效 token"))
    monkeypatch.setattr(tp, "tushare_client", SimpleNamespace(TushareClient=lambda **kw: fake))
    ok, reason = tp.probe_api_key("bad")
    assert ok is False and "无效" in reason


def test_loader_probe_plugin_key_dispatch(monkeypatch):
    from app.data_providers.custom import loader

    monkeypatch.setattr(tp, "probe_api_key",
                        lambda key: (True, "ok") if key == "good" else (False, "bad"))
    assert loader.probe_plugin_key("tushare", "good") == (True, "ok")
    assert loader.probe_plugin_key("tushare", "bad") == (False, "bad")


# ---- 设置页试拉 ----

def test_test_dataset_daily_preview(monkeypatch):
    """实现窗口为 now-30天..now, 数据日期必须相对今天生成。"""
    day = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    provider, _ = _provider(monkeypatch, {"daily": [_daily_row(trade_date=day)]})
    out = provider.test_dataset("daily", ["000001.SZ"])
    assert out["provider"] == "tushare" and out["dataset"] == "daily"
    assert out["rows"] == 1
    assert out["preview"][0]["date"] == f"{day[:4]}-{day[4:6]}-{day[6:]}"  # date → ISO


def test_test_dataset_financial_preview(monkeypatch):
    provider, _ = _provider(monkeypatch, {"fina_indicator": [{
        "ts_code": "600519.SH", "ann_date": "20240403", "end_date": "20231231", "eps": 59.49,
    }]})
    out = provider.test_dataset("financial", ["600519.SH"])
    assert out["rows"] == 1 and out["preview"][0]["period_end"] == "2023-12-31"


def test_test_dataset_unsupported_reports_fallback(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    out = provider.test_dataset("minute")
    assert out["rows"] == 0 and "回退" in out["error"]


def test_test_dataset_error_returns_error_field(monkeypatch):
    provider, _ = _provider(monkeypatch, {"daily": _raise_on_call})
    out = provider.test_dataset("daily", ["000001.SZ"])
    assert out["rows"] == 0 and "频率超限" in out["error"]


def _raise_on_call(params, fields):
    raise tc.TushareError("频率超限")


# ---- 内部工具 ----

def test_split_window_covers_range_without_overlap():
    windows = tp._split_window(date(2020, 1, 1), date(2026, 1, 1))
    assert windows[0][0] == date(2020, 1, 1)
    assert windows[-1][1] == date(2026, 1, 1)
    for (_, stop), (start, _) in pairwise(windows):
        assert start == stop + timedelta(days=1)
        assert (stop - windows[0][0]).days < tp._WINDOW_DAYS


def test_exchange_of_symbol_prefers_suffix():
    assert tp._exchange_of_symbol("600519.SH", "SSE") == "SH"
    assert tp._exchange_of_symbol("920306.BJ", "BSE") == "BJ"
    # 后缀无法识别时回退上游 exchange 字段
    assert tp._exchange_of_symbol("600519.XX", "SSE") == "SH"


def test_compact_and_iso_roundtrip():
    assert tp._date_of_compact("20240110") == date(2024, 1, 10)
    assert tp._iso_of_compact("20240110") == "2024-01-10"
    assert tp._compact_of_date(date(2024, 1, 10)) == "20240110"
    assert tp._date_of_compact("bad") is None and tp._iso_of_compact(None) is None


def test_adj_frame_is_sorted_and_typed():
    df = pl.DataFrame(
        [{"symbol": "b", "trade_date": date(2024, 1, 2), "ex_factor": 1.1},
         {"symbol": "a", "trade_date": date(2024, 1, 1), "ex_factor": 1.2}],
        schema=tp._ADJ_SCHEMA,
    )
    assert df.schema["trade_date"] == pl.Date and df.schema["ex_factor"] == pl.Float64
