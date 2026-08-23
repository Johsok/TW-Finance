# -*- coding: utf-8 -*-
"""
台股財報爬蟲核心：對齊 US-Finance 的 JSON 欄位與計算方式。

資料來源（由穩到細）：
1. Yahoo Finance 財報日曆 + yfinance earnings_dates（預估 EPS、真實 EPS、Surprise）
2. 公開資訊觀測站法說會（官方公布日／法說日）
3. FinMind 綜合損益表（歷史 EPS、毛利率）
4. 證交所／櫃買 OpenAPI（本益比、淨值比）
5. yfinance 日 K（股價、跳空）
"""

from __future__ import annotations

import io
import json
import os
import random
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError as exc:
    print(f"缺少必要套件: {exc}")
    raise SystemExit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "earnings_data.json")
LOG_FILE = os.path.join(BASE_DIR, "DailyLog.txt")
UNIVERSE_FILE = os.path.join(BASE_DIR, "stock_universe.json")

SEARCH_DAYS = 7
KEEP_DAYS = 30
PAST_REPORT_DAYS = 20
MAX_WORKERS = 5
CALENDAR_WORKERS = 6
GAP_LOOKAHEAD_DAYS = 10
GAP_LOOKBACK_BARS = 3
GAP_THRESHOLD_PCT = 2.0

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_PE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_PE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
TWSE_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_LIST_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MOPS_IR_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t100sb02_1"
YAHOO_CAL_URL = "https://finance.yahoo.com/calendar/earnings"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REVENUE_ROW_NAMES = (
    "Total Revenue", "Operating Revenue", "Revenue", "TotalRevenue", "OperatingRevenue",
)
GROSS_PROFIT_ROW_NAMES = ("Gross Profit", "GrossProfit", "Gross Profit Combined")
COGS_ROW_NAMES = (
    "Cost Of Revenue", "Cost of Revenue", "Reconciled Cost Of Revenue", "CostOfRevenue",
)
NII_ROW_NAMES = ("Net Interest Income", "NetInterestIncome")


def parse_num(val):
    """
    將各站數值欄位轉成浮點數。
    @param {*} val - 原始欄位值
    @returns {number|null} 解析後數值；缺值則為 None
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if pd.isna(val):
            return None
        return round(float(val), 2)
    s = str(val).strip().replace(",", "").replace("%", "").replace("+", "")
    if s in ("", "-", "—", "–", "--", "nan", "None", "N/A"):
        return None
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


def is_missing_num(val):
    """
    判斷數值欄位是否為空缺。
    @param {*} val - 欄位值
    @returns {boolean} 是否缺值
    """
    return parse_num(val) is None


def _to_finite_float(val):
    """
    將欄位值轉成有限浮點數。
    @param {*} val - 原始值
    @returns {number|null} 轉換結果；缺值則為 None
    """
    if val is None:
        return None
    if isinstance(val, (pd.Series, pd.DataFrame)):
        try:
            cleaned = val.dropna()
            if cleaned.empty:
                return None
            val = cleaned.iloc[0]
        except Exception:
            return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def _series_cell(series, col):
    """
    取出損益表某一列、某一期的數值。
    @param {pandas.Series|null} series - 列資料
    @param {*} col - 期別欄位
    @returns {number|null} 數值
    """
    if series is None:
        return None
    try:
        return _to_finite_float(series[col])
    except Exception:
        return None


def _find_statement_row(df, names):
    """
    以多組欄位名稱（不分大小寫）找出損益表列。
    @param {pandas.DataFrame} df - 損益表
    @param {Array<string>} names - 候選列名
    @returns {pandas.Series|null} 對應列
    """
    if df is None or getattr(df, "empty", True):
        return None
    index_map = {str(idx).strip().lower(): idx for idx in df.index}
    for name in names:
        key = name.strip().lower()
        if key in index_map:
            return df.loc[index_map[key]]
    return None


def _margin_pct(numer, denom):
    """
    計算百分比毛利率。
    @param {number|null} numer - 毛利或淨利息收入
    @param {number|null} denom - 營收
    @returns {number|null} 百分比（兩位小數）
    """
    numer = _to_finite_float(numer)
    denom = _to_finite_float(denom)
    if numer is None or denom is None or denom == 0:
        return None
    return round(numer / denom * 100, 2)


def growth_pct(curr, base):
    """
    計算年增／季增百分比。
    @param {number|null} curr - 本期（預估或真實）
    @param {number|null} base - 基期
    @returns {number|null} 百分比（一位小數）
    """
    curr = _to_finite_float(curr)
    base = _to_finite_float(base)
    if curr is None or base is None or base == 0:
        return None
    return round((curr - base) / abs(base) * 100, 1)


def record_key(item):
    """
    以股票代碼與財報日期組成唯一鍵。
    @param {object} item - 單筆財報紀錄
    @returns {tuple} (symbol, date)
    """
    return (item.get("symbol"), item.get("date"))


def roc_year(ad_year):
    """
    西元年轉民國年。
    @param {number} ad_year - 西元年
    @returns {number} 民國年
    """
    return int(ad_year) - 1911


def parse_tw_date(val):
    """
    解析民國或西元日期字串。
    @param {*} val - 例如 115/08/24 或 2026-08-24
    @returns {datetime.date|null} 日期
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    s = str(val).strip().replace(".", "/").replace("-", "/")
    if not s or s in ("nan", "None", "-"):
        return None
    parts = [p for p in s.replace("年", "/").replace("月", "/").replace("日", "").split("/") if p]
    if len(parts) < 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        if y < 1911:
            y += 1911
        return date(y, m, d)
    except Exception:
        return None


def to_code(symbol):
    """
    將 Yahoo 代碼轉成台股四碼（或 KY 碼）。
    @param {string} symbol - 2330.TW / 2330
    @returns {string} 市場代碼
    """
    return str(symbol or "").strip().upper().split(".")[0]


def is_tw_yf_symbol(symbol):
    """
    判斷是否為台股 Yahoo 代碼。
    @param {string} symbol - 股票代碼
    @returns {boolean} 是否為 .TW / .TWO
    """
    s = str(symbol or "").strip().upper()
    return s.endswith(".TW") or s.endswith(".TWO")


def format_market_cap(val):
    """
    將市值格式化為兆／億。
    @param {number|string} val - 原始市值（新台幣）
    @returns {string} 例如 12.34兆、456.7億
    """
    num = _to_finite_float(val)
    if num is None:
        return str(val) if val not in (None, "") else "-"
    if num >= 1e12:
        return f" {num / 1e12:.2f}兆".strip()
    if num >= 1e8:
        return f"{num / 1e8:.2f}億"
    if num >= 1e4:
        return f" {num / 1e4:.2f}萬".strip()
    return str(int(round(num)))


def default_headers(referer=None):
    """
    組出 HTTP 標頭。
    @param {string|null} referer - Referer
    @returns {object} headers
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_get(url, params=None, timeout=20, referer=None):
    """
    GET 請求；遇 SSL 或連線中斷時重試，必要時關閉憑證驗證。
    @param {string} url - 網址
    @param {object|null} params - 查詢參數
    @param {number} timeout - 秒
    @param {string|null} referer - Referer
    @returns {requests.Response} 回應
    """
    headers = default_headers(referer)
    last_error = None
    for verify in (True, False):
        for _attempt in range(2):
            try:
                return requests.get(
                    url, params=params, headers=headers, timeout=timeout, verify=verify,
                )
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                last_error = exc
                time.sleep(0.6)
    raise last_error


def http_post(url, data=None, timeout=20, referer=None):
    """
    POST 請求；遇 SSL 或連線中斷時重試，必要時關閉憑證驗證。
    @param {string} url - 網址
    @param {object|null} data - 表單
    @param {number} timeout - 秒
    @param {string|null} referer - Referer
    @returns {requests.Response} 回應
    """
    headers = default_headers(referer)
    last_error = None
    for verify in (True, False):
        for _attempt in range(2):
            try:
                return requests.post(
                    url, data=data, headers=headers, timeout=timeout, verify=verify,
                )
            except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                last_error = exc
                time.sleep(0.6)
    raise last_error


class Universe:
    """上市／上櫃代碼對照 Yahoo 後綴、中文名、產業。"""

    def __init__(self):
        self.by_code = {}

    def yf_symbol(self, code):
        """
        取得 Yahoo 代碼。
        @param {string} code - 2330
        @returns {string} 2330.TW 或 2330.TWO
        """
        code = to_code(code)
        info = self.by_code.get(code) or {}
        suffix = info.get("suffix") or "TW"
        return f"{code}.{suffix}"

    def name_of(self, code):
        """
        取得中文簡稱。
        @param {string} code - 股票代碼
        @returns {string} 名稱或空字串
        """
        info = self.by_code.get(to_code(code)) or {}
        return info.get("name") or ""

    def load(self):
        """
        載入上市櫃清單（先讀快取再合併 OpenAPI，避免上櫃失敗覆寫）。
        @returns {number} 檔數
        """
        if os.path.exists(UNIVERSE_FILE):
            try:
                with open(UNIVERSE_FILE, "r", encoding="utf-8") as handle:
                    cached = json.load(handle)
                if isinstance(cached, dict) and cached:
                    self.by_code = cached
                    print(f"📦 代碼快取 {len(self.by_code)} 檔")
            except Exception:
                pass
        self._load_openapi()
        if len(self.by_code) < 500:
            self._load_finmind()
        if self.by_code:
            self._save()
        else:
            print("⚠️ 無法載入上市櫃清單，僅能處理 Yahoo 回傳的 .TW/.TWO")
        return len(self.by_code)

    def _save(self):
        try:
            with open(UNIVERSE_FILE, "w", encoding="utf-8") as handle:
                json.dump(self.by_code, handle, ensure_ascii=False)
        except Exception:
            pass

    def _put(self, code, name, suffix, market):
        code = to_code(code)
        if not code or not code.isalnum():
            return
        if len(code) > 6:
            return
        self.by_code[code] = {
            "name": str(name or "").strip(),
            "suffix": suffix,
            "market": market,
        }

    def _load_openapi(self):
        ok = False
        try:
            rows = http_get(TWSE_LIST_URL, timeout=25).json()
            for row in rows:
                code = row.get("公司代號") or row.get("Code") or row.get("公司代碼") or row.get("SecuritiesCompanyCode")
                name = (
                    row.get("公司簡稱")
                    or row.get("CompanyAbbreviation")
                    or row.get("Name")
                    or row.get("公司名稱")
                    or row.get("CompanyName")
                )
                self._put(code, name, "TW", "twse")
            ok = True
        except Exception as exc:
            print(f"⚠️ 上市清單失敗: {exc}")
        try:
            rows = http_get(TPEX_LIST_URL, timeout=25).json()
            for row in rows:
                code = row.get("公司代號") or row.get("Code") or row.get("SecuritiesCompanyCode")
                name = (
                    row.get("公司簡稱")
                    or row.get("CompanyAbbreviation")
                    or row.get("Name")
                    or row.get("公司名稱")
                    or row.get("CompanyName")
                )
                self._put(code, name, "TWO", "tpex")
            ok = True
        except Exception as exc:
            print(f"⚠️ 上櫃清單失敗: {exc}")
        if ok and self.by_code:
            print(f"📋 上市櫃清單 {len(self.by_code)} 檔")
        return ok and bool(self.by_code)

    def _load_finmind(self):
        try:
            token = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_TOKEN") or ""
            params = {"dataset": "TaiwanStockInfo"}
            if token:
                params["token"] = token
            payload = http_get(FINMIND_URL, params=params, timeout=30).json()
            rows = payload.get("data") or []
            latest = {}
            for row in rows:
                code = to_code(row.get("stock_id"))
                if not code:
                    continue
                prev = latest.get(code)
                if prev is None or str(row.get("date") or "") >= str(prev.get("date") or ""):
                    latest[code] = row
            for code, row in latest.items():
                market = str(row.get("type") or "").lower()
                suffix = "TWO" if market == "tpex" else "TW"
                if market == "emerging":
                    continue
                self._put(code, row.get("stock_name"), suffix, market or "twse")
            if self.by_code:
                self._save()
                print(f"📋 FinMind 代碼表 {len(self.by_code)} 檔")
                return True
        except Exception as exc:
            print(f"⚠️ FinMind 代碼表失敗: {exc}")
        return False


class RatioCache:
    """證交所／櫃買本益比、淨值比快取。"""

    def __init__(self):
        self.pe = {}
        self.pb = {}

    def load(self):
        """
        一次抓上市＋上櫃本益比／淨值比。
        @returns {number} 有資料檔數
        """
        self._load_twse()
        self._load_tpex()
        print(f"📐 本益比/淨值比 {len(self.pe)} 檔")
        return len(self.pe)

    def _load_twse(self):
        try:
            rows = http_get(TWSE_PE_URL, timeout=25).json()
            for row in rows:
                code = to_code(row.get("Code") or row.get("證券代號"))
                pe = parse_num(row.get("PEratio") or row.get("本益比"))
                pb = parse_num(row.get("PBratio") or row.get("股價淨值比"))
                if code and pe is not None:
                    self.pe[code] = pe
                if code and pb is not None:
                    self.pb[code] = pb
        except Exception as exc:
            print(f"⚠️ 上市本益比失敗: {exc}")

    def _load_tpex(self):
        try:
            rows = http_get(TPEX_PE_URL, timeout=25).json()
            for row in rows:
                code = to_code(
                    row.get("SecuritiesCompanyCode")
                    or row.get("公司代號")
                    or row.get("Code")
                )
                pe = parse_num(row.get("PriceEarningRatio") or row.get("本益比"))
                pb = parse_num(row.get("PriceBookRatio") or row.get("股價淨值比"))
                if code and pe is not None:
                    self.pe[code] = pe
                if code and pb is not None:
                    self.pb[code] = pb
        except Exception as exc:
            print(f"⚠️ 上櫃本益比失敗: {exc}")


class FinMindClient:
    """FinMind 綜合損益表：歷史 EPS 與毛利率。"""

    def __init__(self):
        self.token = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_TOKEN") or ""
        self._stmt = {}

    def statement(self, code):
        """
        取得個股近幾年損益科目。
        @param {string} code - 股票代碼
        @returns {pandas.DataFrame} 長表
        """
        code = to_code(code)
        if code in self._stmt:
            return self._stmt[code]
        start = (datetime.now().date() - timedelta(days=800)).strftime("%Y-%m-%d")
        params = {
            "dataset": "TaiwanStockFinancialStatements",
            "data_id": code,
            "start_date": start,
        }
        if self.token:
            params["token"] = self.token
        try:
            payload = http_get(FINMIND_URL, params=params, timeout=25).json()
            df = pd.DataFrame(payload.get("data") or [])
        except Exception:
            df = pd.DataFrame()
        self._stmt[code] = df
        time.sleep(0.15)
        return df

    def _typed(self, code, type_name):
        df = self.statement(code)
        if df is None or df.empty:
            return pd.DataFrame()
        sub = df[df["type"] == type_name].copy()
        if sub.empty:
            return sub
        sub["date"] = pd.to_datetime(sub["date"], errors="coerce")
        sub = sub.dropna(subset=["date"]).sort_values("date")
        return sub

    def eps_series(self, code):
        """
        單季 EPS 序列（期底日）。
        @param {string} code - 股票代碼
        @returns {pandas.Series} 日期 → EPS
        """
        sub = self._typed(code, "EPS")
        if sub.empty:
            return pd.Series(dtype=float)
        return pd.Series(sub["value"].astype(float).values, index=sub["date"])

    def nearest_eps(self, code, target, window_days=50):
        """
        找最接近目標日的單季 EPS。
        @param {string} code - 股票代碼
        @param {datetime.date} target - 目標期底或公布日前推估日
        @param {number} window_days - 容許天數
        @returns {number|null} EPS
        """
        series = self.eps_series(code)
        if series.empty or target is None:
            return None
        target_ts = pd.Timestamp(target)
        best = None
        best_diff = 10**9
        for idx, val in series.items():
            diff = abs((idx - target_ts).days)
            if diff < best_diff and diff <= window_days:
                best_diff = diff
                best = val
        return _to_finite_float(best)

    def last_two_eps(self, code, announce_date):
        """
        以公布日推上季、去年同期 EPS。
        未公布：上季＝最新一期、去年同期＝往前第 4 季。
        已公布：上季＝上一期、去年同期＝往前第 5 季（略過本期）。
        @param {string} code - 股票代碼
        @param {datetime.date} announce_date - 財報／法說日
        @returns {tuple} (last_q, last_y)
        """
        series = self.eps_series(code)
        if series.empty:
            return None, None
        cutoff = pd.Timestamp(announce_date)
        past = series[series.index <= cutoff + pd.Timedelta(days=5)]
        if past.empty:
            past = series
        past = past.sort_index()
        upcoming = announce_date >= datetime.now().date()
        last_q = None
        last_y = None
        if upcoming:
            if len(past) >= 1:
                last_q = _to_finite_float(past.iloc[-1])
            if len(past) >= 4:
                last_y = _to_finite_float(past.iloc[-4])
        else:
            if len(past) >= 2:
                last_q = _to_finite_float(past.iloc[-2])
            if len(past) >= 5:
                last_y = _to_finite_float(past.iloc[-5])
            elif len(past) >= 4:
                last_y = _to_finite_float(past.iloc[-4])
        if last_y is None:
            last_y = self.nearest_eps(code, announce_date - timedelta(days=365), 50)
        if last_q is None:
            last_q = self.nearest_eps(code, announce_date - timedelta(days=90), 50)
        return last_q, last_y

    def gross_margin(self, code):
        """
        最新一季毛利率：毛利／營收。
        @param {string} code - 股票代碼
        @returns {number|string} 百分比或 "-"
        """
        gp = self._typed(code, "GrossProfit")
        rev = self._typed(code, "Revenue")
        if gp.empty or rev.empty:
            return "-"
        merged = pd.merge(gp[["date", "value"]], rev[["date", "value"]], on="date", suffixes=("_gp", "_rev"))
        merged = merged.sort_values("date")
        for _, row in merged.iloc[::-1].iterrows():
            val = _margin_pct(row["value_gp"], row["value_rev"])
            if val is not None:
                return val
        return "-"


def _margin_from_info(info):
    """
    從 yfinance info 取 TTM 毛利率。
    @param {object|null} info - Ticker.info
    @returns {number|null} 毛利率百分比
    """
    if not info:
        return None
    gm = _to_finite_float(info.get("grossMargins"))
    gp = _to_finite_float(info.get("grossProfits"))
    rev = _to_finite_float(info.get("totalRevenue"))
    gp_equals_rev = (
        gp is not None and rev is not None and rev != 0 and abs(gp - rev) / abs(rev) < 1e-6
    )
    if gp is not None and rev is not None and rev > 0 and not gp_equals_rev:
        computed = _margin_pct(gp, rev)
        if computed is not None:
            return computed
    if gm is not None:
        bank_placeholder = gm == 0 and (rev is None or gp is None or gp_equals_rev)
        if not bank_placeholder:
            return round(gm * 100, 2)
    return None


def _margin_from_statement(df):
    """
    從單張損益表計算毛利率。
    @param {pandas.DataFrame|null} df - 年／季損益表
    @returns {number|null} 毛利率百分比
    """
    if df is None or getattr(df, "empty", True):
        return None
    rev_row = _find_statement_row(df, REVENUE_ROW_NAMES)
    if rev_row is None:
        return None
    gp_row = _find_statement_row(df, GROSS_PROFIT_ROW_NAMES)
    cogs_row = _find_statement_row(df, COGS_ROW_NAMES)
    nii_row = _find_statement_row(df, NII_ROW_NAMES)
    for col in df.columns:
        rev = _series_cell(rev_row, col)
        if rev is None or rev == 0:
            continue
        gp = _series_cell(gp_row, col)
        if gp is not None:
            return _margin_pct(gp, rev)
        cogs = _series_cell(cogs_row, col)
        if cogs is not None:
            return _margin_pct(rev - cogs, rev)
    for col in df.columns:
        rev = _series_cell(rev_row, col)
        nii = _series_cell(nii_row, col)
        if rev is None or rev == 0 or nii is None:
            continue
        return _margin_pct(nii, rev)
    return None


def get_gross_margin_pct(stock, info=None, finmind=None, code=None):
    """
    取得最新毛利率（%）。優先 FinMind，其次 yfinance。
    @param {yfinance.Ticker|null} stock - 個股物件
    @param {object|null} info - Ticker.info
    @param {FinMindClient|null} finmind - FinMind 客戶端
    @param {string|null} code - 台股代碼
    @returns {number|string} 毛利率百分比；失敗則為 "-"
    """
    if finmind and code:
        val = finmind.gross_margin(code)
        if val != "-" and not is_missing_num(val):
            return val
    try:
        if info is None and stock is not None:
            try:
                info = stock.info or {}
            except Exception:
                info = {}
        val = _margin_from_info(info)
        if val is not None:
            return val
        if stock is not None:
            for attr in ("income_stmt", "quarterly_income_stmt", "financials", "quarterly_financials"):
                try:
                    df = getattr(stock, attr)
                except Exception:
                    continue
                val = _margin_from_statement(df)
                if val is not None:
                    return val
        op = _to_finite_float((info or {}).get("operatingMargins"))
        if op is not None and op != 0:
            return round(op * 100, 2)
        return "-"
    except Exception:
        return "-"


def _extract_price_series(df, symbol, field):
    """
    從 yfinance download 結果取出指定價位序列。
    @param {pandas.DataFrame} df - download 回傳表
    @param {string} symbol - Yahoo 代碼
    @param {string} field - Open / Close
    @returns {pandas.Series|null} 價位序列
    """
    if df is None or df.empty:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            level0 = list(df.columns.get_level_values(0))
            level1 = list(df.columns.get_level_values(1))
            if symbol in level0:
                sub = df[symbol]
                if field in sub.columns:
                    return sub[field]
            if field in level0 and symbol in level1:
                return df[field][symbol]
            return None
        if field in df.columns:
            return df[field]
    except Exception:
        return None
    return None


def _gaps_from_open_close(opens, closes):
    """
    以 ((今開 - 昨收) / 昨收) * 100 計算近幾個交易日的跳空。
    @param {pandas.Series|null} opens - 開盤價
    @param {pandas.Series|null} closes - 收盤價
    @returns {string} 例如「8/3_3%」；無跳空則為 "-"
    """
    if opens is None or closes is None:
        return "-"
    try:
        frame = pd.concat({"Open": opens, "Close": closes}, axis=1).dropna()
    except Exception:
        return "-"
    if len(frame) < 2:
        return "-"
    frame = frame.tail(GAP_LOOKBACK_BARS + 1)
    labels = []
    for i in range(1, len(frame)):
        prev_close = float(frame["Close"].iloc[i - 1])
        curr_open = float(frame["Open"].iloc[i])
        if prev_close == 0:
            continue
        pct = (curr_open - prev_close) / prev_close * 100
        if abs(pct) >= GAP_THRESHOLD_PCT:
            idx = frame.index[i]
            bar_date = idx.date() if hasattr(idx, "date") else pd.Timestamp(idx).date()
            labels.append(f"{bar_date.month}/{bar_date.day}_{int(round(pct))}%")
    return " ".join(labels) if labels else "-"


def fetch_gap_map(yf_symbols):
    """
    批次抓近 3 日跳空（今開對昨收）。
    @param {Array<string>} yf_symbols - Yahoo 代碼
    @returns {object} Yahoo 代碼 → 跳空字串
    """
    gap_map = {s: "-" for s in yf_symbols}
    if not yf_symbols:
        return gap_map
    chunk_size = 30
    for i in range(0, len(yf_symbols), chunk_size):
        chunk = yf_symbols[i:i + chunk_size]
        try:
            df = yf.download(
                tickers=chunk,
                period="10d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
            for symbol in chunk:
                gap_map[symbol] = _gaps_from_open_close(
                    _extract_price_series(df, symbol, "Open"),
                    _extract_price_series(df, symbol, "Close"),
                )
        except Exception as exc:
            print(f"⚠️ 批次抓取跳空失敗，改為逐檔補抓: {exc}")
            for symbol in chunk:
                try:
                    hist = yf.Ticker(symbol).history(period="10d", auto_adjust=False)
                    if hist is None or hist.empty:
                        gap_map[symbol] = "-"
                        continue
                    opens = hist["Open"] if "Open" in hist.columns else None
                    closes = hist["Close"] if "Close" in hist.columns else None
                    gap_map[symbol] = _gaps_from_open_close(opens, closes)
                except Exception:
                    gap_map[symbol] = "-"
        time.sleep(0.3)
    return gap_map


def update_gap_fields(records):
    """
    為今日至後 10 日的各股寫入跳空欄。
    @param {Array<object>} records - 紀錄清單
    @returns {number} 變更筆數
    """
    today = datetime.now().date()
    end_date = today + timedelta(days=GAP_LOOKAHEAD_DAYS)
    targets = []
    for item in records:
        try:
            item_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= item_date <= end_date:
            targets.append(item)
    if not targets:
        return 0
    yf_symbols = list(dict.fromkeys(item.get("yf_symbol") or f"{item['symbol']}.TW" for item in targets))
    print(f"📈 開始抓取今日至後 {GAP_LOOKAHEAD_DAYS} 日共 {len(yf_symbols)} 檔近 {GAP_LOOKBACK_BARS} 日跳空")
    gap_map = fetch_gap_map(yf_symbols)
    changed = 0
    hit = 0
    for item in targets:
        key = item.get("yf_symbol") or f"{item.get('symbol')}.TW"
        new_gap = gap_map.get(key, "-")
        if new_gap != "-":
            hit += 1
        if item.get("gap") != new_gap:
            item["gap"] = new_gap
            changed += 1
    print(f"   🔍 發現跳空 {hit} 檔，更新 {changed} 筆")
    return changed


def update_gross_margin_fields(records, finmind):
    """
    補齊缺毛利率的紀錄。
    @param {Array<object>} records - 紀錄清單
    @param {FinMindClient} finmind - FinMind 客戶端
    @returns {number} 變更筆數
    """
    today = datetime.now().date()
    start_date = today - timedelta(days=PAST_REPORT_DAYS)
    end_date = today + timedelta(days=GAP_LOOKAHEAD_DAYS)
    missing = []
    seen = set()
    for item in records:
        code = item.get("symbol")
        if not code or not is_missing_num(item.get("gross_margin")):
            continue
        if code in seen:
            continue
        seen.add(code)
        try:
            item_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
            in_window = start_date <= item_date <= end_date
        except Exception:
            in_window = False
        missing.append((code, item.get("yf_symbol"), in_window))
    missing.sort(key=lambda x: (not x[2], x[0]))
    if not missing:
        return 0
    print(f"📊 開始抓取毛利率，共 {len(missing)} 檔")
    margin_map = {}

    def _one(row):
        code, yf_symbol, _in_window = row
        val = finmind.gross_margin(code) if finmind else "-"
        if val == "-" or is_missing_num(val):
            try:
                val = get_gross_margin_pct(yf.Ticker(yf_symbol or f"{code}.TW"), code=code, finmind=None)
            except Exception:
                val = "-"
        return code, val

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_one, row) for row in missing]
        for future in as_completed(futures):
            try:
                code, val = future.result()
                margin_map[code] = val
            except Exception:
                pass
    changed = 0
    hit = 0
    for item in records:
        code = item.get("symbol")
        if code not in margin_map:
            continue
        new_val = margin_map[code]
        if new_val == "-" or is_missing_num(new_val):
            continue
        hit += 1
        if item.get("gross_margin") != new_val:
            item["gross_margin"] = new_val
            changed += 1
    print(f"   🔍 取得毛利率 {hit} 檔，更新 {changed} 筆")
    return changed


def merge_reported_fields(existing, incoming):
    """
    只合併 Reported EPS 與 Surprise (%)。
    @param {object} existing - 既有紀錄
    @param {object} incoming - 新欄位
    @returns {boolean} 是否更新
    """
    changed = False
    for key in ("eps_reported", "surprise_pct"):
        new_val = incoming.get(key)
        if new_val not in (None, "-", "", "nan"):
            if existing.get(key) != new_val:
                existing[key] = new_val
                changed = True
        elif key not in existing:
            existing[key] = "-"
            changed = True
    return changed


class StockScraper:
    """台股財報日曆＋個股細節爬蟲。"""

    def __init__(self, universe, ratios, finmind):
        self.universe = universe
        self.ratios = ratios
        self.finmind = finmind
        self.headers = default_headers("https://finance.yahoo.com/")
        self._mops_cache = {}

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def normalize_calendar_columns(self, df):
        """
        將 Yahoo 財報日曆欄位名稱正規化。
        @param {pandas.DataFrame} df - 原始表格
        @returns {pandas.DataFrame} 重新命名後表格
        """
        rename = {}
        for col in df.columns:
            if isinstance(col, tuple):
                name = " ".join(str(x) for x in col if str(x) != "nan")
            else:
                name = str(col)
            key = name.strip().lower().replace(" ", "").replace("_", "")
            if key == "symbol" or key.endswith("symbol"):
                rename[col] = "Symbol"
            elif "earningscalltime" in key:
                rename[col] = "Earnings Call Time"
            elif "marketcap" in key:
                rename[col] = "Market Cap"
            elif "reportedeps" in key or "epsactual" in key or "actualeps" in key:
                rename[col] = "Reported EPS"
            elif "surprise" in key:
                rename[col] = "Surprise (%)"
            elif "company" in key:
                rename[col] = "Company"
        return df.rename(columns=rename)

    def get_yahoo_calendar(self, target_date):
        """
        抓 Yahoo 國際版當日財報日曆，只留台股。
        @param {datetime.date} target_date - 目標日
        @returns {pandas.DataFrame} 台股列
        """
        days_to_subtract = (target_date.weekday() + 1) % 7
        start_date = target_date - timedelta(days=days_to_subtract)
        end_date = start_date + timedelta(days=6)
        url_date_str = target_date.strftime("%Y-%m-%d")
        from_str = start_date.strftime("%Y-%m-%d")
        to_str = end_date.strftime("%Y-%m-%d")
        all_dfs = []
        offset = 0
        size = 100
        wanted = ["Symbol", "Earnings Call Time", "Market Cap", "Reported EPS", "Surprise (%)", "Company"]
        session = requests.Session()
        session.headers.update(self.headers)
        try:
            while True:
                url = (
                    f"{YAHOO_CAL_URL}?from={from_str}&to={to_str}"
                    f"&day={url_date_str}&offset={offset}&size={size}"
                )
                try:
                    resp = session.get(url, timeout=15)
                    resp.raise_for_status()
                    if "<table" not in resp.text:
                        break
                    dfs = pd.read_html(io.StringIO(resp.text))
                    if not dfs:
                        break
                    df = None
                    for table in dfs:
                        nd = self.normalize_calendar_columns(table)
                        if "Symbol" in nd.columns:
                            df = nd
                            break
                    if df is None or df.empty:
                        break
                    present = [c for c in wanted if c in df.columns]
                    all_dfs.append(df[present])
                    if len(df) < size:
                        break
                    offset += size
                    time.sleep(random.uniform(0.2, 0.5))
                except Exception as exc:
                    self.log(f"⚠️ Yahoo 日曆失敗: {exc}")
                    break
        finally:
            session.close()
        if not all_dfs:
            return pd.DataFrame(columns=wanted)
        result = pd.concat(all_dfs, ignore_index=True)
        result = result[result["Symbol"].map(is_tw_yf_symbol)]
        for col in wanted:
            if col not in result.columns:
                result[col] = None
        return result.reset_index(drop=True)

    def get_mops_ir_calendar(self, year, month):
        """
        抓公開資訊觀測站法說會一覽（上市＋上櫃，整年一次後依日期篩選）。
        單月查詢在財報季常回「查無資料」，改抓該年全部再過濾。
        @param {number} year - 西元年
        @param {number} month - 月（保留參數；實際整年抓取）
        @returns {Array<object>} {symbol, date, time, name, source}
        """
        events = []
        roc = roc_year(year)
        cache_key = (year, "all")
        if cache_key in self._mops_cache:
            return self._mops_cache[cache_key]
        for typek in ("sii", "otc"):
            payload = {
                "encodeURIComponent": "1",
                "step": "1",
                "firstin": "1",
                "off": "1",
                "TYPEK": typek,
                "year": str(roc),
                "month": "",
                "co_id": "",
            }
            try:
                resp = http_post(
                    MOPS_IR_URL,
                    data=payload,
                    timeout=60,
                    referer="https://mopsov.twse.com.tw/mops/web/t100sb02_1",
                )
                resp.encoding = "utf-8"
                if "<table" not in resp.text.lower():
                    continue
                tables = pd.read_html(io.StringIO(resp.text))
            except Exception as exc:
                self.log(f"⚠️ MOPS 法說會 {typek} {year}-{month:02d} 失敗: {exc}")
                continue
            for table in tables:
                cols = [str(c) for c in table.columns]
                code_col = next((c for c in table.columns if "代號" in str(c) or "代碼" in str(c)), None)
                date_col = next((c for c in table.columns if "日期" in str(c)), None)
                name_col = next((c for c in table.columns if "名稱" in str(c) or "簡稱" in str(c)), None)
                time_col = next((c for c in table.columns if "時間" in str(c)), None)
                if code_col is None or date_col is None:
                    joined = "".join(cols)
                    if "代號" not in joined:
                        continue
                    continue
                for _, row in table.iterrows():
                    code = to_code(row.get(code_col))
                    event_date = parse_tw_date(row.get(date_col))
                    if not code or event_date is None:
                        continue
                    if not code.isdigit() and not code.replace("A", "").isalnum():
                        continue
                    raw_time = str(row.get(time_col) or "")
                    time_str = "盤後"
                    if any(x in raw_time for x in ("09", "10", "11", "12", "13:0", "13:1", "上午", "盤中")):
                        time_str = "盤中"
                    if any(x in raw_time for x in ("08:", "盤前")):
                        time_str = "盤前"
                    events.append({
                        "symbol": code,
                        "date": event_date,
                        "time": time_str,
                        "name": str(row.get(name_col) or "").strip() if name_col else "",
                        "source": "mops",
                    })
            time.sleep(0.4)
        self._mops_cache[cache_key] = events
        return events

    def collect_day_events(self, target_date):
        """
        合併 Yahoo 日曆與 MOPS 法說會，得到當日台股事件。
        @param {datetime.date} target_date - 目標日
        @returns {pandas.DataFrame} 標準化列
        """
        rows = []
        yahoo = self.get_yahoo_calendar(target_date)
        for _, row in yahoo.iterrows():
            yf_symbol = str(row.get("Symbol") or "").strip().upper()
            code = to_code(yf_symbol)
            raw_time = str(row.get("Earnings Call Time") or "")
            if any(x in raw_time for x in ("AMC", "After")):
                time_str = "盤後"
            elif any(x in raw_time for x in ("BMO", "Before")):
                time_str = "盤前"
            else:
                time_str = "不確定"
            rows.append({
                "Symbol": yf_symbol,
                "code": code,
                "Earnings Call Time": time_str,
                "Market Cap": row.get("Market Cap"),
                "Reported EPS": row.get("Reported EPS"),
                "Surprise (%)": row.get("Surprise (%)"),
                "source": "yahoo",
            })
        mops = self.get_mops_ir_calendar(target_date.year, target_date.month)
        seen = {r["code"] for r in rows}
        for event in mops:
            if event["date"] != target_date:
                continue
            code = event["symbol"]
            yf_symbol = self.universe.yf_symbol(code)
            if code in seen:
                continue
            seen.add(code)
            rows.append({
                "Symbol": yf_symbol,
                "code": code,
                "Earnings Call Time": event.get("time") or "盤後",
                "Market Cap": None,
                "Reported EPS": None,
                "Surprise (%)": None,
                "source": "mops",
            })
        return pd.DataFrame(rows)

    def event_date_tw(self, val):
        """
        將財報事件時間轉成台灣日期。
        @param {*} val - 時間戳
        @returns {datetime.date|null} 台灣日期
        """
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            ts = pd.Timestamp(val)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Taipei")
            return ts.date()
        except Exception:
            return None

    def get_historical_eps_from_financials(self, stock, target_date_past):
        """
        從 yfinance 季報找歷史 EPS。
        @param {yfinance.Ticker} stock - 個股
        @param {datetime.date} target_date_past - 目標日
        @returns {number|null} EPS
        """
        try:
            fin = stock.quarterly_financials
            if fin is None or fin.empty:
                return None
            eps_row = None
            if "Basic EPS" in fin.index:
                eps_row = fin.loc["Basic EPS"]
            elif "Diluted EPS" in fin.index:
                eps_row = fin.loc["Diluted EPS"]
            if eps_row is None:
                return None
            best_date = None
            min_diff = 999
            for col_date in fin.columns:
                if isinstance(col_date, str):
                    try:
                        d = datetime.strptime(col_date, "%Y-%m-%d").date()
                    except Exception:
                        continue
                else:
                    d = col_date.date()
                diff = abs((d - target_date_past).days)
                if diff < 45 and diff < min_diff:
                    min_diff = diff
                    best_date = col_date
            if best_date is None:
                return None
            val = eps_row[best_date]
            if pd.isna(val):
                return None
            return float(val)
        except Exception:
            return None

    def check_stock_detail(self, row, target_date):
        """
        抓單檔預估 EPS、歷史 EPS、年增、毛利率、股價。
        @param {pandas.Series|object} row - 日曆列
        @param {datetime.date} target_date - 公布日
        @returns {tuple} (status, message, data)
        """
        yf_symbol = str(row.get("Symbol") or "").strip().upper()
        code = to_code(row.get("code") or yf_symbol)
        if not code:
            return 0, "[?] 缺代碼", None
        if not yf_symbol.endswith(".TW") and not yf_symbol.endswith(".TWO"):
            yf_symbol = self.universe.yf_symbol(code)
        time_str = str(row.get("Earnings Call Time") or "不確定")
        mkt_cap_str = str(row.get("Market Cap") or "-")
        if mkt_cap_str in ("nan", "None", ""):
            mkt_cap_str = "-"
        eps_reported = parse_num(row.get("Reported EPS"))
        surprise_pct = parse_num(row.get("Surprise (%)"))

        try:
            stock = yf.Ticker(yf_symbol)
            earning_df = stock.earnings_dates
            found_idx = -1
            eps_est = None
            if earning_df is not None and not earning_df.empty:
                earning_df = earning_df.sort_index(ascending=False)
                for k in range(len(earning_df)):
                    try:
                        e_date = self.event_date_tw(earning_df.index[k])
                        if e_date and abs((e_date - target_date).days) <= 5:
                            found_idx = k
                            break
                    except Exception:
                        continue
                if found_idx >= 0:
                    try:
                        raw_est = earning_df["EPS Estimate"].iloc[found_idx]
                        if not pd.isna(raw_est):
                            eps_est = float(raw_est)
                    except Exception:
                        pass
                    if eps_reported is None:
                        try:
                            raw_rep = earning_df["Reported EPS"].iloc[found_idx]
                            if not pd.isna(raw_rep):
                                eps_reported = round(float(raw_rep), 2)
                        except Exception:
                            pass
                    if surprise_pct is None:
                        try:
                            surprise_col = next(
                                (c for c in earning_df.columns if "surprise" in str(c).lower()),
                                None,
                            )
                            if surprise_col is not None:
                                raw_sur = earning_df[surprise_col].iloc[found_idx]
                                if not pd.isna(raw_sur):
                                    surprise_pct = round(float(raw_sur), 2)
                        except Exception:
                            pass

            eps_last_q = np.nan
            eps_last_y = np.nan
            if found_idx >= 0 and earning_df is not None:
                try:
                    if found_idx + 1 < len(earning_df):
                        eps_last_q = earning_df["Reported EPS"].iloc[found_idx + 1]
                    if found_idx + 4 < len(earning_df):
                        eps_last_y = earning_df["Reported EPS"].iloc[found_idx + 4]
                except Exception:
                    pass

            if pd.isna(eps_last_q) or pd.isna(eps_last_y):
                fm_q, fm_y = self.finmind.last_two_eps(code, target_date)
                if pd.isna(eps_last_q) and fm_q is not None:
                    eps_last_q = fm_q
                if pd.isna(eps_last_y) and fm_y is not None:
                    eps_last_y = fm_y

            if pd.isna(eps_last_q):
                found_val = self.get_historical_eps_from_financials(stock, target_date - timedelta(days=90))
                if found_val is not None:
                    eps_last_q = found_val
            if pd.isna(eps_last_y):
                found_val = self.get_historical_eps_from_financials(stock, target_date - timedelta(days=365))
                if found_val is not None:
                    eps_last_y = found_val

            if (pd.isna(eps_last_y) or pd.isna(eps_last_q)) and eps_est is None and eps_reported is None:
                return 0, f"[{code}] ❌ 歷史 EPS 與預估皆缺", None

            curr_for_growth = eps_est if eps_est is not None else eps_reported
            yoy = growth_pct(curr_for_growth, eps_last_y)
            qoq = growth_pct(curr_for_growth, eps_last_q)

            curr_price = "-"
            pe, pb = "-", "-"
            price_val = None
            info = {}
            try:
                hist = stock.history(period="5d")
                if hist is not None and not hist.empty:
                    price_val = hist["Close"].iloc[-1]
            except Exception:
                pass
            try:
                info = stock.info or {}
            except Exception:
                info = {}
            if price_val is None and info:
                price_val = info.get("currentPrice") or info.get("previousClose")
            if price_val:
                curr_price = round(float(price_val), 2)
            if info:
                pe = info.get("trailingPE") or info.get("forwardPE") or pe
                pb = info.get("priceToBook", pb)
                if mkt_cap_str in ("-", "nan"):
                    raw_mc = info.get("marketCap")
                    if raw_mc:
                        mkt_cap_str = format_market_cap(raw_mc)
            if is_missing_num(pe):
                pe = self.ratios.pe.get(code, pe)
            if is_missing_num(pb):
                pb = self.ratios.pb.get(code, pb)
            if (pe == "-" or pe is None) and curr_price != "-" and eps_est and eps_est > 0:
                pe = curr_price / eps_est

            if surprise_pct is None and eps_reported is not None and eps_est not in (None, 0) and float(eps_est) != 0:
                try:
                    surprise_pct = round((float(eps_reported) - float(eps_est)) / abs(float(eps_est)) * 100, 2)
                except Exception:
                    pass

            gross_margin = get_gross_margin_pct(stock, info, self.finmind, code)
            name = self.universe.name_of(code)

            data = {
                "date": str(target_date),
                "symbol": code,
                "yf_symbol": yf_symbol,
                "name": name,
                "time": time_str,
                "market_cap": mkt_cap_str,
                "pe": round(pe, 2) if isinstance(pe, (int, float)) else pe,
                "pb": round(pb, 2) if isinstance(pb, (int, float)) else pb,
                "eps_est": round(float(eps_est), 2) if eps_est is not None else "-",
                "eps_reported": eps_reported if eps_reported is not None else "-",
                "surprise_pct": surprise_pct if surprise_pct is not None else "-",
                "eps_last_q": round(float(eps_last_q), 2) if not pd.isna(eps_last_q) and eps_last_q is not None else "-",
                "eps_last_y": round(float(eps_last_y), 2) if not pd.isna(eps_last_y) and eps_last_y is not None else "-",
                "qoq": qoq if qoq is not None else "-",
                "yoy": yoy if yoy is not None else "-",
                "price": curr_price,
                "gap": "-",
                "gross_margin": gross_margin,
            }
            yoy_disp = f"{data['yoy']}%" if data.get("yoy") not in (None, "-") else "-"
            return 1, f"[{code} {name}] ✅ (YoY: {yoy_disp})", data
        except Exception as exc:
            return 0, f"[{code}] ⚠️ 錯誤: {exc}", None

    def fetch_reported_from_ticker(self, symbol, target_date):
        """
        以 yfinance earnings_dates 對齊公布日，取出 Reported / Surprise。
        @param {string} symbol - 2330 或 2330.TW
        @param {datetime.date|string} target_date - 公布日
        @returns {object|null} 補值紀錄
        """
        try:
            if isinstance(target_date, str):
                target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
            code = to_code(symbol)
            yf_symbol = symbol if is_tw_yf_symbol(symbol) else self.universe.yf_symbol(code)
            stock = yf.Ticker(yf_symbol)
            earning_df = stock.earnings_dates
            if earning_df is None or earning_df.empty:
                return None
            earning_df = earning_df.sort_index(ascending=False)
            found_idx = -1
            for k in range(len(earning_df)):
                try:
                    e_date = self.event_date_tw(earning_df.index[k])
                    if e_date and abs((e_date - target_date).days) <= 2:
                        found_idx = k
                        break
                except Exception:
                    continue
            if found_idx == -1:
                return None
            eps_reported = None
            surprise_pct = None
            try:
                raw_rep = earning_df["Reported EPS"].iloc[found_idx]
                if not pd.isna(raw_rep):
                    eps_reported = round(float(raw_rep), 2)
            except Exception:
                pass
            try:
                surprise_col = next((c for c in earning_df.columns if "surprise" in str(c).lower()), None)
                if surprise_col is not None:
                    raw_sur = earning_df[surprise_col].iloc[found_idx]
                    if not pd.isna(raw_sur):
                        surprise_pct = round(float(raw_sur), 2)
            except Exception:
                pass
            if surprise_pct is None and eps_reported is not None:
                try:
                    eps_est = earning_df["EPS Estimate"].iloc[found_idx]
                    if not pd.isna(eps_est) and float(eps_est) != 0:
                        surprise_pct = round((eps_reported - float(eps_est)) / abs(float(eps_est)) * 100, 2)
                except Exception:
                    pass
            if eps_reported is None:
                fm_eps = self.finmind.nearest_eps(code, target_date, 40)
                if fm_eps is None:
                    fm_eps = self.finmind.nearest_eps(code, target_date - timedelta(days=45), 40)
                if fm_eps is not None:
                    eps_reported = round(float(fm_eps), 2)
            if eps_reported is None and surprise_pct is None:
                return None
            return {
                "date": str(target_date),
                "symbol": code,
                "eps_reported": eps_reported if eps_reported is not None else "-",
                "surprise_pct": surprise_pct if surprise_pct is not None else "-",
            }
        except Exception:
            return None

    def scrape_past_reported_eps(self, missing_pairs=None):
        """
        往前 PAST_REPORT_DAYS 補真實 EPS / Surprise。
        @param {Array<tuple>|null} missing_pairs - [(symbol, date), ...]
        @returns {Array<object>} 補值清單
        """
        pairs = list(missing_pairs or [])
        if not pairs:
            return []
        print(f"🔎 補抓 Reported EPS / Surprise {len(pairs)} 檔")
        results = []

        def _one(pair):
            symbol, date_str = pair
            return self.fetch_reported_from_ticker(symbol, date_str)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_one, p): p for p in pairs}
            for future in as_completed(futures):
                try:
                    item = future.result()
                    if item:
                        results.append(item)
                except Exception:
                    pass
        print(f"   ✅ 補到 {len(results)} 筆")
        return results


def _collect_missing_reported_pairs(records):
    """
    收集過去 PAST_REPORT_DAYS 內缺 Reported/Surprise 的鍵。
    @param {Array<object>} records - 既有紀錄
    @returns {Array<tuple>} [(symbol, date_str), ...]
    """
    today = datetime.now().date()
    start_date = today - timedelta(days=PAST_REPORT_DAYS)
    pairs = []
    seen = set()
    for item in records:
        symbol = item.get("symbol")
        date_str = item.get("date")
        if not symbol or not date_str:
            continue
        try:
            item_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if item_date < start_date or item_date > today:
            continue
        if is_missing_num(item.get("eps_reported")) or is_missing_num(item.get("surprise_pct")):
            key = (symbol, date_str)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    return pairs


def load_existing():
    """
    讀取既有 earnings_data.json。
    @returns {Array<object>} 紀錄
    """
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as handle:
            content = json.load(handle)
        if isinstance(content, list):
            return content
        if isinstance(content, dict) and "data" in content:
            return content["data"]
    except Exception as exc:
        print(f"⚠️ 讀取舊檔失敗 ({exc})，將建立新檔案。")
    return []


def save_data(new_data_list, reported_updates=None, finmind=None):
    """
    合併、清過期、補跳空／毛利率後寫檔。
    @param {Array<object>} new_data_list - 新抓紀錄
    @param {Array<object>|null} reported_updates - 補真實 EPS
    @param {FinMindClient|null} finmind - FinMind 客戶端
    """
    print("💾 正在準備存檔...")
    existing_list = load_existing()
    data_map = {}
    for item in existing_list:
        key = record_key(item)
        if key[0] and key[1]:
            data_map[key] = item

    overwrite_count = 0
    new_entry_count = 0
    for item in new_data_list:
        key = record_key(item)
        if not key[0] or not key[1]:
            continue
        if key in data_map:
            old = data_map[key]
            for keep_key in ("eps_reported", "surprise_pct", "gross_margin", "name", "yf_symbol"):
                if keep_key in ("name", "yf_symbol"):
                    if not item.get(keep_key) and old.get(keep_key):
                        item[keep_key] = old[keep_key]
                    continue
                if is_missing_num(item.get(keep_key)) and not is_missing_num(old.get(keep_key)):
                    item[keep_key] = old[keep_key]
            if item.get("gap") in (None, "-", "") and old.get("gap") not in (None, "-", ""):
                item["gap"] = old["gap"]
            data_map[key] = item
            overwrite_count += 1
        else:
            data_map[key] = item
            new_entry_count += 1

    reported_update_count = 0
    past_new_count = 0
    for item in (reported_updates or []):
        key = record_key(item)
        if not key[0] or not key[1]:
            continue
        if key not in data_map:
            if "eps_est" in item and "yoy" in item:
                data_map[key] = item
                past_new_count += 1
            continue
        if merge_reported_fields(data_map[key], item):
            reported_update_count += 1

    today = datetime.now().date()
    cutoff_date = today - timedelta(days=KEEP_DAYS)
    final_list = []
    removed_count = 0
    for item in data_map.values():
        try:
            item_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
            if item_date >= cutoff_date:
                final_list.append(item)
            else:
                removed_count += 1
        except Exception:
            final_list.append(item)

    gap_update_count = update_gap_fields(final_list)
    margin_update_count = update_gross_margin_fields(final_list, finmind)

    def sort_key(row):
        yoy = parse_num(row.get("yoy"))
        return (row.get("date") or "", -(yoy if yoy is not None else -9999))

    final_list = sorted(final_list, key=sort_key)

    with open(DATA_FILE, "w", encoding="utf-8") as handle:
        json.dump(final_list, handle, ensure_ascii=False, indent=4)

    tw_time = datetime.now(timezone.utc) + timedelta(hours=8)
    current_time_str = tw_time.strftime("%Y-%m-%d %H:%M:%S")
    log_summary = (
        f"[{current_time_str}] 執行完成報告 (台灣時間):\n"
        f"  - 新增資料: {new_entry_count} 筆\n"
        f"  - 往前20日新增: {past_new_count} 筆\n"
        f"  - 更新資料: {overwrite_count} 筆\n"
        f"  - 補抓 EPS/Surprise: {reported_update_count} 筆\n"
        f"  - 更新跳空: {gap_update_count} 筆\n"
        f"  - 更新毛利率: {margin_update_count} 筆\n"
        f"  - 清除過期: {removed_count} 筆 (早於 {cutoff_date})\n"
        f"  - 總資料數: {len(final_list)} 筆\n"
        f"------------------------------------\n"
    )
    print(log_summary)
    try:
        lines = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        lines.insert(0, log_summary)
        if len(lines) > 2000:
            print(f"⚠️ 日誌檔超過 2000 行 ({len(lines)} 行)，正在刪除最舊(末端)的 1000 行...")
            lines = lines[:-1000]
        with open(LOG_FILE, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        print(f"✅ 執行結果已寫入 (最新在首行): {LOG_FILE}")
    except Exception as exc:
        print(f"⚠️ 寫入日誌檔失敗: {exc}")


def run(start_offset=7, search_days=None):
    """
    執行台股財報爬蟲。
    @param {number} start_offset - 從今日起算的起始偏移天數
    @param {number|null} search_days - 搜尋天數，預設 SEARCH_DAYS
    """
    days = SEARCH_DAYS if search_days is None else search_days
    universe = Universe()
    universe.load()
    ratios = RatioCache()
    ratios.load()
    finmind = FinMindClient()
    scraper = StockScraper(universe, ratios, finmind)

    start_date = datetime.now().date() + timedelta(days=start_offset)
    print("🚀 開始執行台股財報爬蟲")
    print(f"   - 起始搜尋日期: {start_date} (今日+{start_offset}天)")
    print(f"   - 搜尋天數: {days} 天")
    print(f"   - 清除截止日期: {datetime.now().date() - timedelta(days=KEEP_DAYS)}")

    all_new_results = []
    curr_date = start_date
    batch_size = 12
    for _ in range(days):
        if curr_date.weekday() >= 5:
            curr_date += timedelta(days=1)
            continue
        print(f"📅 分析日期: {curr_date}")
        df_cal = scraper.collect_day_events(curr_date)
        if df_cal is not None and not df_cal.empty:
            total_stocks = len(df_cal)
            print(f"   🔍 發現 {total_stocks} 檔台股")
            chunks = [df_cal.iloc[i:i + batch_size] for i in range(0, total_stocks, batch_size)]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for i, chunk in enumerate(chunks):
                    if i > 0:
                        time.sleep(random.uniform(1.5, 3.5))
                    futures = {
                        executor.submit(scraper.check_stock_detail, row, curr_date): row
                        for _, row in chunk.iterrows()
                    }
                    for future in as_completed(futures):
                        try:
                            status, msg, data = future.result()
                            print(f"   {msg}")
                            if status == 1 and data:
                                all_new_results.append(data)
                        except Exception:
                            pass
        else:
            print("   ⚠️ 該日期無台股財報／法說資料")
        curr_date += timedelta(days=1)

    existing = load_existing()
    missing_pairs = _collect_missing_reported_pairs(existing + all_new_results)
    reported_updates = scraper.scrape_past_reported_eps(missing_pairs)
    save_data(all_new_results, reported_updates, finmind)
