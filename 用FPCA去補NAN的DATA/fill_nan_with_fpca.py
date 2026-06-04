import pandas as pd
import numpy as np
import os

# ── 設定路徑（每次換變數只需改這三行）────────────────────────────────────────
RAW_FILE  = r"D:\論文\claude\完整data\原始資料\RH.csv"
FPCA_FILE = r"D:\論文\claude\完整data\fpca之後的\FPCA_PM25_format_RHall.csv"
OUT_FILE  = r"D:\論文\claude\完整data\用FPCA去補NAN的DATA\RH.csv"
# ─────────────────────────────────────────────────────────────────────────────

DROP_STATIONS = ['金門', '馬祖']
END_DATE      = '2025-11-30 23:00:00'

# 載入
raw  = pd.read_csv(RAW_FILE)
fpca = pd.read_csv(FPCA_FILE)

# 統一時間欄位名稱
raw.rename( columns={raw.columns[0]:  'PublishTime'}, inplace=True)
fpca.rename(columns={fpca.columns[0]: 'PublishTime'}, inplace=True)
raw['PublishTime']  = pd.to_datetime(raw['PublishTime'])
fpca['PublishTime'] = pd.to_datetime(fpca['PublishTime'])

# 以 FPCA 時間為主，截到指定日期
fpca = fpca[fpca['PublishTime'] <= END_DATE].set_index('PublishTime')
raw  = raw.set_index('PublishTime')

# 移除金門、馬祖
for s in DROP_STATIONS:
    if s in fpca.columns: fpca.drop(columns=s, inplace=True)
    if s in raw.columns:  raw.drop( columns=s, inplace=True)

# 以 FPCA 的測站為主，raw 對齊並填補 NaN
common = [c for c in fpca.columns if c in raw.columns]
raw_aligned = raw[common].reindex(fpca.index)
filled = raw_aligned.combine_first(fpca[common])

nan_before = raw_aligned.isna().sum().sum()
nan_after  = filled.isna().sum().sum()
print(f"測站數 : {len(common)}")
print(f"時間列 : {len(fpca)}")
print(f"NaN 填補前: {nan_before}  →  填補後: {nan_after}")

os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
filled.reset_index().to_csv(OUT_FILE, index=False)
print(f"已存: {OUT_FILE}")
