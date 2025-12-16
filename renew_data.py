import requests
import json
import base64
import urllib.parse
import re
import urllib.parse
import zipfile
import tempfile
import os
from tqdm import tqdm
import datetime
import aiohttp
import asyncio
import polars as pl
import io
import os
from tqdm.asyncio import tqdm
import pickle






with open("original_paths.pkl", "rb") as f:
    original_paths = pickle.load(f)


def extract_year_month(filename: str):
    m = re.search(r"OD_(\d{6})", filename)
    if not m:
        return None
    yyyymm = m.group(1)
    year = int(yyyymm[:4])
    month = int(yyyymm[4:])
    return (year, month)


max_year_month = (0, 0)
max_filename = None
for y in original_paths:
    ym = extract_year_month(y)
    if ym and ym > max_year_month:
        max_year_month = ym
        max_filename = True

if max_filename:
    print(f"最大年月檔案：{max_year_month} → {max_filename}")
else:
    print("沒有任何符合 'OD_YYYYMM' 格式的檔案，將嘗試下載全部檔案。")
    
    
    
import datetime
import base64
import urllib.parse


last_year = max_year_month[0]
last_month = max_year_month[1]


today = datetime.date.today()
current_year = today.year
current_month = today.month


all_year_month = []
for y in range(last_year, current_year + 1):
    for m in range(1, 13):
        all_year_month.append((y, m))


target_year_month = []
for y, m in all_year_month:
    after_last = (y > last_year) or (y == last_year and m >= last_month)
    before_or_equal_current = (y < current_year) or (y == current_year and m <= current_month)
    if after_last and before_or_equal_current:
        target_year_month.append((y, m))


file_names = [f"MOENV_OD_{y:04d}{m:02d}.zip" for (y, m) in target_year_month]


new_urls = []
for fname in file_names:
    
    full_path = f"/空氣品質/環境部_國家空品測站/{fname}"
    
    b64_bytes = base64.b64encode(full_path.encode("utf-8"))
    b64_str = b64_bytes.decode("utf-8")
    
    url_encoded_path = urllib.parse.quote(b64_str, safe="")
    
    url = f""
    new_urls.append(url)


for url in new_urls:
    print(url)
    
    




local_dir = r"路徑"
os.makedirs(local_dir, exist_ok=True)


station = pl.read_csv("moenv_station.csv", encoding="utf8")
site_list = set(station["SiteName"].to_list())


target_columns = ["PublishTime", "SiteName", "O3", "O3_8hr", "PM10", "PM2.5"]



def get_filename_from_url(download_url: str) -> str:
    
    parsed = urllib.parse.urlparse(download_url)
    qs = urllib.parse.parse_qs(parsed.query)
    encoded_path = qs.get("path", [None])[0]
    if not encoded_path:
        raise ValueError(f"無法從 URL 找到 path 參數：{download_url}")
    b64_str = urllib.parse.unquote(encoded_path)
    original_bytes = base64.b64decode(b64_str)
    original_path = original_bytes.decode("utf-8")
    return os.path.basename(original_path)

def extract_csv_from_zip(zip_bytes: bytes) -> list[pl.DataFrame]:
   
    dfs: list[pl.DataFrame] = []
    
    if not zip_bytes.startswith(b"PK\x03\x04"):
        return dfs

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for member in z.namelist():
                lower = member.lower()
                if lower.endswith(".zip"):
                    inner_bytes = z.read(member)
                    dfs.extend(extract_csv_from_zip(inner_bytes))
                elif lower.endswith(".csv"):
                    try:
                        csv_bytes = z.read(member)
                        df = pl.read_csv(io.BytesIO(csv_bytes),
                                         encoding="utf8", ignore_errors=True)
                        
                        cols = [c for c in target_columns if c in df.columns]
                        df = df.select(cols)
                        
                        for c in cols:
                            if c not in ["PublishTime", "SiteName"]:
                                df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
                        # 過濾 SiteName
                        df = df.filter(pl.col("SiteName").is_in(site_list))
                        dfs.append(df)
                    except:
                        pass
    except zipfile.BadZipFile:
        return []

    return dfs

async def fetch_and_process(session: aiohttp.ClientSession, url: str) -> list[pl.DataFrame]:

    try:
        async with session.get(url) as resp:
            content = await resp.read()
            return extract_csv_from_zip(content)
    except Exception:
        return []

async def main_async(url_list: list[str]) -> list[pl.DataFrame]:
    
    all_dfs: list[pl.DataFrame] = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_and_process(session, u) for u in url_list]
        for fut in tqdm(asyncio.as_completed(tasks), total=len(tasks)):
            dfs = await fut
            all_dfs.extend(dfs)
    return all_dfs

async def run_all(new_urls: list[str]):
    
    all_dfs = await main_async(new_urls)

    if all_dfs:
        full_df = pl.concat(all_dfs)
    else:
        full_df = pl.DataFrame()
        print("注意：沒有任何新下載並解壓的資料！")


    for indicator in ["O3", "O3_8hr", "PM10", "PM2.5"]:
        if indicator not in full_df.columns:
            
            continue

       
        pivot_df = (
            full_df
            .select(["PublishTime", "SiteName", indicator])
            .pivot(
                values=indicator,
                index="PublishTime",
                columns="SiteName",
                aggregate_function="mean"
            )
            .with_columns([
                
                pl.col("PublishTime").str.to_datetime().alias("PublishTime")
            ])
            .sort("PublishTime")
        )

        out_path = os.path.join(f"{indicator}.pkl")

        if os.path.exists(out_path):
            
            with open(out_path, "rb") as f:
                old_df: pl.DataFrame = pickle.load(f)

            
            if old_df.schema.get("PublishTime") == pl.Utf8:
                old_df = old_df.with_columns([
                    pl.col("PublishTime").str.to_datetime().alias("PublishTime")
                ])

            
            cols_old = set(old_df.columns)
            cols_new = set(pivot_df.columns)
            all_cols = list(cols_old.union(cols_new))

            
            for c in all_cols:
                if c not in cols_old:
                    old_df = old_df.with_columns(pl.lit(None).cast(pl.Float64).alias(c))

            
            for c in all_cols:
                if c not in cols_new:
                    pivot_df = pivot_df.with_columns(pl.lit(None).cast(pl.Float64).alias(c))

            
            old_df = old_df.select(all_cols)
            pivot_df = pivot_df.select(all_cols)

            
            combined = pl.concat([old_df, pivot_df])
            combined = combined.unique(subset=["PublishTime"], keep="first").sort("PublishTime")

            
            all_cols_current = combined.columns
            cols_without_pt = [c for c in all_cols_current if c != "PublishTime"]
            new_order = ["PublishTime"] + cols_without_pt
            combined = combined.select(new_order)
           
            
            with open(out_path, "wb") as f:
                pickle.dump(combined, f)
            print(f"{indicator} 已存在，已將新資料 append 並更新：{out_path}")

        else:
           
            all_cols_current = pivot_df.columns
            cols_without_pt = [c for c in all_cols_current if c != "PublishTime"]
            new_order = ["PublishTime"] + cols_without_pt
            pivot_df = pivot_df.select(new_order)

            with open(out_path, "wb") as f:
                pickle.dump(pivot_df, f)
            print(f"{indicator} 檔案不存在，已新建 pickle：{out_path}")



new = []
for url in new_urls:
    fname = get_filename_from_url(url)
    quoted = f"{fname}"
    new.append(quoted)

missing_paths = [p for p in new if p not in original_paths]

for p in missing_paths:
    original_paths.append(p)

print("合併後的 original_paths：", original_paths)






if __name__ == "__main__":
    asyncio.run(run_all(new_urls))