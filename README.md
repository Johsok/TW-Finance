# TW-Finance — 台股財報爬蟲

對應美股專案 [US-Finance](https://github.com/Johsok/US-Finance)／[US-FinanceView](https://github.com/Johsok/US-FinanceView)。

輸出 `earnings_data.json` 欄位與美股版相同（另加 `yf_symbol`、`name`），可供 GitHub Pages 與 `TW_GUI` 財報選股使用。

## 建議的 GitHub 網站（台股版 US-FinanceView）

| 倉庫 | 用途 | 對應美股 |
|---|---|---|
| **Johsok/TW-Finance** | 爬蟲 + Actions（本資料夾） | US-Finance |
| **Johsok/TW-FinanceView** | GitHub Pages 前端 | US-FinanceView |

Pages 網址建議：`https://johsok.github.io/TW-FinanceView/`

前端已放在本資料夾 `index.html`，以及 `../TW-FinanceView/`。建立遠端後：

1. 在 GitHub 新建 `Johsok/TW-Finance`、`Johsok/TW-FinanceView`（公開 View 才能當網站）
2. View 倉庫 Settings → Pages → Deploy from `main` / `/`
3. TW-Finance 設定 Secrets：`API_TOKEN_GITHUB`（寫入 View）、選填 `FINMIND_TOKEN`
4. 本機先跑一次 `python main2.py` 產生 JSON，再 push

## 資料來源

| 優先 | 網站 | 用途 |
|---|---|---|
| 1 | [Yahoo Finance](https://finance.yahoo.com/calendar/earnings) + yfinance | 財報日、預估 EPS、真實 EPS、Surprise、股價、跳空 |
| 2 | [公開資訊觀測站法說會](https://mopsov.twse.com.tw/mops/web/t100sb02_1) | 官方法說／財報相關日期（覆蓋 Yahoo 沒列的中小型股） |
| 3 | [FinMind 綜合損益](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/) | 歷史 EPS、毛利率 |
| 4 | [TWSE](https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL)／[TPEx](https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis) OpenAPI | 本益比、淨值比 |
| 5 | 前端連結 | [鉅亨個股](https://www.cnyes.com/twstock/2330)、[TradingView](https://www.tradingview.com/symbols/TWSE-2330/)、[Goodinfo](https://goodinfo.tw/) |

MarketScreener、Investing.com、CMoney 籌碼預估不爬：反爬／付費牆，改用 Yahoo 共識＋FinMind 歷史。

## 計算

- **年增 YoY**＝（本期預估或真實 EPS − 去年同期 EPS）／|去年同期|
- **季增 QoQ**＝（本期 − 上季）／|上季|
- **毛利率**＝FinMind 最新季 毛利／營收；沒有則用 yfinance
- **跳空**＝近 3 個交易日 `(今開 − 昨收) / 昨收`，絕對值 ≥ 2% 才顯示

## 排程（與美股相同）

| 腳本 | 台灣時間 | 視窗 |
|---|---|---|
| `main.py` | 18:17 | 今日 +7 起 7 天 |
| `main2.py` | 06:17 | 今日起 7 天 |
| `main3.py` | 11:17 | 今日 +14 起 7 天 |
| `calender.py` | 每月 1 日 08:17 | 台／美休市日 |

## 本機執行

```bash
pip install -r requirements.txt
python main2.py
```

選填環境變數 `FINMIND_TOKEN`（提高 FinMind 額度）。

以本機伺服器開前端（不要直接雙擊 HTML）：

```bash
python -m http.server 8080
```

瀏覽 `http://localhost:8080/`
