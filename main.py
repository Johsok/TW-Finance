# -*- coding: utf-8 -*-
"""
台股財報小助手資料更新腳本。

以 FinMind `TaiwanStockInfo` 取得上市／上櫃／櫃買證券清單，再以 Yahoo Finance（yfinance）
比對近期財報日並輸出與美股版相同結構之 `earnings_data.json`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "earnings_data.json")
LOG_FILE = os.path.join(BASE_DIR, "DailyLog.txt")
SEARCH_DAYS = 7
KEEP_DAYS = 15
MAX_WORKERS = 5
SCAN_WORKERS = 10

TW_TZ = ZoneInfo("Asia/Taipei")
FINMIND_INFO_URL = "https://api.finmindtrade.com/api/v4/data"


def yahoo_ticker_for_row(stock_id: str, mtype: str) -> str:
    """依 FinMind 市場類別組出 Yahoo 代號（上市 .TW、上櫃／櫃買 .TWO）。"""
    if mtype == "twse":
        return f"{stock_id}.TW"
    return f"{stock_id}.TWO"


def should_skip_row(row: dict) -> bool:
    """排除 ETF、指數型等通常無每股盈餘預估之標的。"""
    ind = (row.get("industry_category") or "") + (row.get("stock_name") or "")
    if "ETF" in ind or "指數投資" in ind:
        return True
    return False


def dedupe_universe(rows: list[dict]) -> list[dict]:
    """同一檔保留 FinMind 回傳中最新 `date` 的那一列。"""
    best: dict[str, dict] = {}
    for row in rows:
        sid = row.get("stock_id")
        if not sid:
            continue
        d = row.get("date") or ""
        prev = best.get(sid)
        if not prev or d > (prev.get("date") or ""):
            best[sid] = row
    return list(best.values())


def finmind_latest_stock_info(session: requests.Session) -> list[dict]:
    """取得 FinMind 最近一次更新的 TaiwanStockInfo 全市場基本資料。"""
    anchor = datetime.now(tz=TW_TZ).date()
    for _ in range(14):
        ds = anchor.strftime("%Y-%m-%d")
        r = session.get(
            FINMIND_INFO_URL,
            params={"dataset": "TaiwanStockInfo", "start_date": ds},
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("msg") == "success" and payload.get("data"):
            return payload["data"]
        anchor -= timedelta(days=1)
    return []


def to_tw_date(ts) -> date:
    """將 earnings_dates 索引時間轉成台北日曆日。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(TW_TZ).date()


def classify_tw_session(ts) -> str:
    """
    依台北時間粗分盤前／盤後；落在一般交易時段則標示為不確定。
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    lo = t.tz_convert(TW_TZ)
    minutes = lo.hour * 60 + lo.minute
    open_m = 9 * 60
    close_m = 13 * 60 + 30
    if minutes < open_m:
        return "盤前"
    if open_m <= minutes <= close_m:
        return "不確定"
    return "盤後"


class StockScraper:
    """台股財報資料擷取與驗證。"""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (TW-StockEarnings/1.0; FinMind+YahooFinance)",
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            }
        )

    def log(self, msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def format_market_cap(self, val) -> str:
        if not isinstance(val, (int, float)):
            return str(val)
        if val >= 1e12:
            return f"{val / 1e12:.2f}T"
        if val >= 1e9:
            return f"{val / 1e9:.2f}B"
        if val >= 1e6:
            return f"{val / 1e6:.2f}M"
        return str(val)

    def read_next_earnings_from_calendar(self, yahoo_ticker: str) -> list[date]:
        """讀取 Yahoo `calendar` 中的財報日期列表。"""
        try:
            tkr = yf.Ticker(yahoo_ticker)
            cal = tkr.calendar or {}
            raw = cal.get("Earnings Date") or []
            if isinstance(raw, datetime):
                raw = [raw.date()]
            elif isinstance(raw, date):
                raw = [raw]
            elif not isinstance(raw, list):
                raw = []
            out: list[date] = []
            for x in raw:
                if isinstance(x, datetime):
                    out.append(x.date())
                elif isinstance(x, date):
                    out.append(x)
            return out
        except Exception:
            return []

    def scan_calendar_hit(
        self, row: dict, window_start: date, window_end: date
    ) -> tuple[str, list[date]] | None:
        """
        以輕量 `calendar` API 篩選：若視窗內有財報日則回傳 (Yahoo 代號, 命中日期列表)。
        並嘗試 .TW／.TWO 互補，降低 FinMind `type` 與 Yahoo 映射偶發不一致之影響。
        """
        stock_id = str(row["stock_id"])
        mtype = row.get("type") or "twse"
        primary = yahoo_ticker_for_row(stock_id, mtype)
        alt = f"{stock_id}.TWO" if primary.endswith(".TW") else f"{stock_id}.TW"
        for ysym in (primary, alt):
            hits = self.read_next_earnings_from_calendar(ysym)
            in_win = [d for d in hits if window_start <= d <= window_end]
            if in_win:
                return ysym, in_win
        return None

    def get_historical_eps_from_financials(self, stock, target_date_past: date):
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
                    except ValueError:
                        continue
                else:
                    d = col_date.date()
                diff = abs((d - target_date_past).days)
                if diff < 45 and diff < min_diff:
                    min_diff = diff
                    best_date = col_date
            if best_date:
                val = eps_row[best_date]
                if pd.isna(val):
                    return None
                return float(val)
        except Exception:
            return None
        return None

    def check_stock_detail(self, row: dict, hint_dates: list[date]):
        """
        以 yfinance 驗證財報並輸出與美股版欄位一致之 dict。

        :param row: FinMind 列，可含 `yahoo_override` 指定 Yahoo 代號。
        :param hint_dates: 由 calendar 先篩到的日期（曆日），用於對齊 earnings_dates。
        """
        stock_id = str(row["stock_id"])
        yahoo_ticker = row.get("yahoo_override") or yahoo_ticker_for_row(
            stock_id, row.get("type") or "twse"
        )

        try:
            stock = yf.Ticker(yahoo_ticker)
            earning_df = stock.earnings_dates

            if earning_df is None or earning_df.empty:
                return 0, f"[{stock_id}] ❌ 無財報日期資料", None

            earning_df = earning_df.sort_index(ascending=False)
            found_idx = -1
            matched_tw_date: date | None = None

            for k in range(len(earning_df)):
                try:
                    tw_d = to_tw_date(earning_df.index[k])
                except Exception:
                    continue
                if any(abs((tw_d - h).days) <= 10 for h in hint_dates):
                    found_idx = k
                    matched_tw_date = tw_d
                    break

            if found_idx == -1:
                return 0, f"[{stock_id}] ❌ 無法對齊財報日", None

            esp_idx_time = earning_df.index[found_idx]
            time_str = classify_tw_session(esp_idx_time)

            eps_est = earning_df["EPS Estimate"].iloc[found_idx]
            if pd.isna(eps_est):
                return 0, f"[{stock_id}] ❌ 缺乏本次 EPS 預估值", None

            eps_last_q = np.nan
            eps_last_y = np.nan
            try:
                if found_idx + 1 < len(earning_df):
                    eps_last_q = earning_df["Reported EPS"].iloc[found_idx + 1]
                if found_idx + 4 < len(earning_df):
                    eps_last_y = earning_df["Reported EPS"].iloc[found_idx + 4]
            except Exception:
                pass

            anchor_date = matched_tw_date or hint_dates[0]
            date_last_q = anchor_date - timedelta(days=90)
            date_last_y = anchor_date - timedelta(days=365)

            if pd.isna(eps_last_q):
                found_val = self.get_historical_eps_from_financials(stock, date_last_q)
                if found_val is not None:
                    eps_last_q = found_val

            if pd.isna(eps_last_y):
                found_val = self.get_historical_eps_from_financials(stock, date_last_y)
                if found_val is not None:
                    eps_last_y = found_val

            if pd.isna(eps_last_y) or pd.isna(eps_last_q):
                return 0, f"[{stock_id}] ❌ 歷史 EPS 缺失", None

            yoy = (eps_est - eps_last_y) / abs(eps_last_y) * 100 if eps_last_y != 0 else 0
            qoq = (eps_est - eps_last_q) / abs(eps_last_q) * 100 if eps_last_q != 0 else 0

            curr_price = "-"
            pe, pb = "-", "-"
            price_val = None
            mkt_cap_str = "-"

            try:
                hist = stock.history(period="5d")
                if not hist.empty:
                    price_val = hist["Close"].iloc[-1]
            except Exception:
                pass

            info: dict = {}
            try:
                info = stock.info or {}
            except Exception:
                pass

            if price_val is None and info:
                price_val = info.get("currentPrice") or info.get("previousClose")

            if price_val:
                curr_price = round(float(price_val), 2)

            try:
                raw_mc = info.get("marketCap")
                if raw_mc:
                    mkt_cap_str = self.format_market_cap(raw_mc)
            except Exception:
                pass

            try:
                pe = info.get("trailingPE") or info.get("forwardPE")
                pb = info.get("priceToBook", "-")
            except Exception:
                pass

            if (pe == "-" or pe is None) and curr_price != "-" and eps_est and eps_est > 0:
                pe = float(curr_price) / float(eps_est)

            data = {
                "date": str(anchor_date),
                "symbol": stock_id,
                "yahoo_ticker": yahoo_ticker,
                "time": time_str,
                "market_cap": mkt_cap_str,
                "pe": round(pe, 2) if isinstance(pe, (int, float)) else pe,
                "pb": round(pb, 2) if isinstance(pb, (int, float)) else pb,
                "eps_est": round(float(eps_est), 2),
                "eps_last_q": round(float(eps_last_q), 2),
                "eps_last_y": round(float(eps_last_y), 2),
                "qoq": round(float(qoq), 1),
                "yoy": round(float(yoy), 1),
                "price": curr_price,
            }
            return 1, f"[{stock_id}] ✅ (YoY: {data['yoy']}%)", data
        except Exception:
            return 0, f"[{stock_id}] ⚠️ 錯誤", None


def save_data(new_data_list: list) -> None:
    """合併、清理過期資料並寫入 JSON（邏輯與美股版一致，以 `symbol` 為唯一鍵）。"""
    print("💾 正在準備存檔...")
    existing_list: list = []

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    existing_list = content
                elif isinstance(content, dict) and "data" in content:
                    existing_list = content["data"]
                else:
                    print("⚠️ JSON 格式不明，將建立新檔案。")
        except Exception as e:
            print(f"⚠️ 讀取舊檔失敗 ({e})，將建立新檔案。")

    data_map = {item["symbol"]: item for item in existing_list}

    overwrite_count = 0
    new_entry_count = 0
    skip_count = 0

    for item in new_data_list:
        symbol = item["symbol"]
        new_date = item["date"]

        if symbol in data_map:
            old_date = data_map[symbol]["date"]
            if old_date == new_date:
                skip_count += 1
            else:
                data_map[symbol] = item
                overwrite_count += 1
        else:
            data_map[symbol] = item
            new_entry_count += 1

    today = datetime.now(tz=TW_TZ).date()
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

    final_list = sorted(final_list, key=lambda x: (x["date"], -x["yoy"]))

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=4)

    tw_time = datetime.now(timezone.utc) + timedelta(hours=8)
    current_time_str = tw_time.strftime("%Y-%m-%d %H:%M:%S")

    log_summary = (
        f"[{current_time_str}] 執行完成報告 (台灣時間):\n"
        f"  - 新增資料: {new_entry_count} 筆\n"
        f"  - 更新資料: {overwrite_count} 筆\n"
        f"  - 跳過重複: {skip_count} 筆\n"
        f"  - 清除過期: {removed_count} 筆 (早於 {cutoff_date})\n"
        f"  - 總資料數: {len(final_list)} 筆\n"
        f"------------------------------------\n"
    )

    print(log_summary)

    try:
        lines: list[str] = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        lines.insert(0, log_summary)

        if len(lines) > 2000:
            print(f"⚠️ 日誌檔超過 2000 行 ({len(lines)} 行)，正在刪除最舊(末端)的 1000 行...")
            lines = lines[:-1000]

        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)

        print(f"✅ 執行結果已寫入 (最新在首行): {LOG_FILE}")

    except Exception as e:
        print(f"⚠️ 寫入日誌檔失敗: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="台股財報資料更新")
    parser.add_argument(
        "--start-offset",
        type=int,
        default=7,
        help="自今日起算幾日後開始搜尋（與美股 main.py 預設 7 對齊；main2 用 0、main3 用 14）",
    )
    parser.add_argument(
        "--max-universe",
        type=int,
        default=0,
        help="僅掃描前 N 檔證券（0 表示全部；供本機測試）",
    )
    args = parser.parse_args()
    start_offset = args.start_offset
    max_universe = args.max_universe

    scraper = StockScraper()
    today_tw = datetime.now(tz=TW_TZ).date()
    window_start = today_tw + timedelta(days=start_offset)
    window_end = window_start + timedelta(days=SEARCH_DAYS - 1)

    print("🚀 開始執行台股財報掃描")
    print(f"   - 視窗起日: {window_start} (今日+{start_offset}天)")
    print(f"   - 視窗迄日: {window_end}")
    print(f"   - 視窗天數: {SEARCH_DAYS} 天")

    raw_rows = finmind_latest_stock_info(scraper.session)
    if not raw_rows:
        print("❌ 無法自 FinMind 取得 TaiwanStockInfo，結束。")
        sys.exit(1)

    universe = [r for r in dedupe_universe(raw_rows) if not should_skip_row(r)]
    universe.sort(key=lambda r: str(r.get("stock_id", "")))
    if max_universe > 0:
        universe = universe[:max_universe]
        print(f"   ⚠️ 已啟用 --max-universe={max_universe}（測試模式）")
    print(f"   - FinMind 證券檔數（已排除 ETF 等）: {len(universe)}")

    hits: list[tuple[dict, str, list[date]]] = []

    def scan_one(row: dict):
        h = scraper.scan_calendar_hit(row, window_start, window_end)
        return row, h

    print("📡 Phase 1：以 Yahoo calendar 篩選財報視窗內標的…")
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futs = [ex.submit(scan_one, r) for r in universe]
        for fut in as_completed(futs):
            row, h = fut.result()
            if h:
                ysym, dlist = h
                hits.append((row, ysym, dlist))

    print(f"   - 視窗內初步命中: {len(hits)} 檔")

    all_new_results: list[dict] = []
    batch_size = 20

    if hits:
        chunks = [hits[i : i + batch_size] for i in range(0, len(hits), batch_size)]
        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(random.uniform(2, 5))

            def detail_task(tup: tuple[dict, str, list[date]]):
                row0, ysym, dlist = tup
                row_d = dict(row0)
                row_d["yahoo_override"] = ysym
                return scraper.check_stock_detail(row_d, dlist)

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(detail_task, tup): tup for tup in chunk}
                for future in as_completed(futures):
                    try:
                        status, msg, data = future.result()
                        if status == 1:
                            print(f"   {msg}")
                            all_new_results.append(data)
                    except Exception:
                        pass

    save_data(all_new_results)


if __name__ == "__main__":
    main()
