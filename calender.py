import pandas_market_calendars as mcal
import pandas as pd
import json
from datetime import datetime

# ==========================================
# 1. 核心邏輯：找出「平日」但「沒開盤」的日子
# ==========================================
def get_closed_weekdays(market_code, year_list):
    # 建立行事曆
    cal = mcal.get_calendar(market_code)
    
    # 設定時間範圍
    start_date = f"{min(year_list)}-01-01"
    end_date = f"{max(year_list)}-12-31"
    
    # A. 找出這段時間所有的「平日」 (週一 ~ 週五)
    # freq='B' 代表 Business Day (預設就是週一到週五)
    all_weekdays = pd.date_range(start=start_date, end=end_date, freq='B')
    
    # B. 找出股市實際「開盤」的日子 (Schedule)
    schedule = cal.schedule(start_date=start_date, end_date=end_date)
    open_dates = schedule.index
    
    # 確保兩者格式一致 (都轉成正規化日期，去除時間)
    all_weekdays_set = set(all_weekdays.strftime('%Y-%m-%d'))
    open_dates_set = set(open_dates.strftime('%Y-%m-%d'))
    
    # C. 邏輯相減：平日 - 開盤日 = 平日休市日
    closed_weekdays = sorted(list(all_weekdays_set - open_dates_set))
    
    print(f" -> {market_code} 在 {year_list} 期間共有 {len(closed_weekdays)} 個平日休市日")
    return closed_weekdays

# ==========================================
# 2. 設定動態年份 (去年 + 今年)
# ==========================================
current_year = datetime.now().year
target_years = [current_year , current_year + 1] # [2026, 2027]

print(f"正在分析年份: {target_years} ...")

# ==========================================
# 3. 執行並存檔
# ==========================================
output_data = {
    "TW": get_closed_weekdays("XTAI", target_years),
    "US": get_closed_weekdays("NYSE", target_years)
}

filename = 'holidays.json'
with open(filename, 'w', encoding='utf-8') as f:
    json.dump(output_data, f, indent=4)

print(f"\n★ 成功！已儲存純日期清單至 {filename}")