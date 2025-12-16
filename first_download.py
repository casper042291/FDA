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



import requests
import json

url = ""

payload = json.dumps({})
headers = {
  'Accept': '',
  'Accept-Language': ,
  'Connection': ,
  'Content-Type': ,
  'Origin': ,
  'Referer': ,
  'Sec-Fetch-Dest': ,
  'Sec-Fetch-Mode': ,
  'Sec-Fetch-Site': ,
  'User-Agent': ,
  'sec-ch-ua': ,
  'sec-ch-ua-mobile': ,
  'sec-ch-ua-platform': ,
  'x-csrf-token': ,
  'Cookie': 
}

response = requests.request("POST", url, headers=headers, data=payload)




# 將回傳內容轉成 json
data = response.json()

# 抓出所有的 path
file_paths = [file['path']
    for file in data['data']['files']
    if file['type'] == 'file' and re.search(r"OD_\d+", file['path'])]


download_urls = []

for path in file_paths:
    
    original_path = path

    
    base64_path = base64.b64encode(original_path.encode("utf-8")).decode("utf-8")

    
    url_encoded_path = urllib.parse.quote(base64_path)

    
    download_url = f""


    download_urls.append(download_url)


for url in download_urls:
  print(url)
  
  
  
  
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
    try:
        original_bytes = base64.b64decode(b64_str)
        original_path = original_bytes.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Base64 解碼失敗 ({b64_str}): {e}")
    return os.path.basename(original_path)


original_paths = []


for url in download_urls:
  fname = get_filename_from_url(url)
  original_paths.append(fname)


# 非同步批次解壓＋讀 CSV：對 to_async_urls 進行處理
def extract_csv_from_zip(zip_bytes):
    dfs = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if name.endswith('.zip'):
                dfs.extend(extract_csv_from_zip(z.read(name)))
            elif name.endswith('.csv'):
                try:
                    csv_bytes = z.read(name)
                    df = pl.read_csv(io.BytesIO(csv_bytes), encoding="utf8", ignore_errors=True)
                    cols = [col for col in target_columns if col in df.columns]
                    df = df.select(cols)
                    for col in cols:
                        if col not in ["PublishTime", "SiteName"]:
                            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
                    df = df.filter(pl.col("SiteName").is_in(site_list))
                    dfs.append(df)
                except:
                    pass
    return dfs

async def fetch_and_process(session, url):
    async with session.get(url) as resp:
        content = await resp.read()
        return extract_csv_from_zip(content)

async def main_async(url_list):
    all_dfs = []
    async with aiohttp.ClientSession() as aiosession:
        tasks = [fetch_and_process(aiosession, u) for u in url_list]
        for future in tqdm(asyncio.as_completed(tasks), total=len(tasks)):
            dfs = await future
            all_dfs.extend(dfs)
    return all_dfs

# (8) 執行非同步合併與 pivot，並寫成 pickle 檔
async def run_all():
    all_dfs = await main_async(download_urls)

    if all_dfs:
        full_df = pl.concat(all_dfs)
    else:
        full_df = pl.DataFrame()
        print("注意：沒有任何新下載並解壓的資料！")

    for indicator in ["O3", "O3_8hr", "PM10", "PM2.5"]:
        if indicator not in full_df.columns:
            continue

        
        pivot_df = full_df.select(["PublishTime", "SiteName", indicator]).pivot(
            values=indicator,
            index="PublishTime",
            columns="SiteName",
            aggregate_function="mean"
        )

        
        pivot_df = (
            pivot_df
            .with_columns([
                pl.col("PublishTime").str.to_datetime().alias("PublishTime")
            ])
            .sort("PublishTime")
        )

        out_path = f"{indicator}.pkl"

        if os.path.exists(out_path):
            try:
               
                with open(out_path, "rb") as f:
                    old_df = pickle.load(f)

                
                if old_df.schema.get("PublishTime") == pl.Utf8:
                    old_df = (
                        old_df
                        .with_columns([
                            pl.col("PublishTime").str.to_datetime().alias("PublishTime")
                        ])
                    )

               
                combined = pl.concat([old_df, pivot_df]).sort("PublishTime")

                
                with open(out_path, "wb") as f:
                    pickle.dump(combined, f)
                print(f"{indicator} 已存在，已將新資料 append 並更新：{out_path}")
            except Exception as e:
                print(f"{indicator} 無法讀取或合併舊的 pickle 檔 ({out_path})：{e}")
        else:
            # 如果不存在，先把新的 pivot_df（已排序）寫成 pickle
            with open(out_path, "wb") as f:
                pickle.dump(pivot_df, f)
            print(f"{indicator} 檔案不存在，已新建 pickle：{out_path}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())


print("所有原始檔名：", original_paths)

with open("original_paths.pkl", "wb") as f:
    pickle.dump(original_paths, f)