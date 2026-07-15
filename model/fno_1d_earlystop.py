# === 測試集(2025)評估(expm1 還原,對原始 PM2.5,NaN 跳過;用最佳驗證模型)===

# 有效評估點：1,684,069，測站數：72
# ============================================================
#   三天整體 — 中央論文 per-hour pooled（★無空間耦合,CAMS最近格點,early stopping）
#   [模型]     RMSE: 6.6834  MAE: 4.7583
#   [CAMS原始] RMSE: 19.5213  MAE: 14.1195
# ============================================================
#   第1天 (+01~+24h)  模型 RMSE: 6.0013 MAE: 4.2068  |  CAMS RMSE: 18.4506
#   第2天 (+25~+48h)  模型 RMSE: 6.9434 MAE: 4.9740  |  CAMS RMSE: 19.7715
#   第3天 (+49~+72h)  模型 RMSE: 7.1056 MAE: 5.0942  |  CAMS RMSE: 20.3418

# [參考] 先站後平均(有效站 72/72)  RMSE: 6.6490  MAE: 4.7579  R²: 0.4304
# ============================================================
#   fno_1d_perstation_cams_coordonly_2 — CAMS最近格點 + ★驗證集 Early Stopping 版
# ============================================================
#   ★本檔 = fno_1d_perstation_cams_coordonly_2_cams_nearest.py 再加上「驗證集 + early stopping」：
#     [新增1] ★時間序切分驗證集★：訓練期(<2025)的序列本就按時間排序，取「最後 10%」
#             （時間最新的一段）當驗證集，其餘 90%（時間較早）當訓練。這是預報問題的標準
#             時間切分（用過去驗證、不碰未來），不洩漏測試集(2025)。
#     [新增2] ★正規化統計只用訓練的 90% 計算★：避免驗證集分布洩漏進 log1p+z-score 的
#             mean/std。
#     [新增3] ★以「驗證 loss」存最佳 checkpoint + early stopping★：每個 epoch 算驗證集
#             遮罩 Huber loss（與訓練同一個 masked_huber，NaN 用遮罩排除），只有驗證 loss
#             創新低才存檔；若連續 PATIENCE(=100) 個 epoch 驗證 loss 都沒改善就提前停止。
#             → 這是「合法的 early stopping」：選模型只看驗證集，測試集(2025)完全不參與。
#     [評估] 訓練結束後載入「最佳驗證 checkpoint」對測試集(2025)評估，得到不偏的泛化表現。
#
#   ★與原本「固定 800 epoch、存 train loss 最佳」的差別：本檔會在驗證 loss 不再下降時停，
#     取的是「泛化最佳」而非「訓練擬合最佳」的模型，用於緩解過擬合。
#   ★CAMS 仍為最近格點（沿用 cams_npz_nearest_file）；模型架構、超參數與母本一致。
#   ★checkpoint 用獨立檔名，不覆蓋其他版本。本檔尚未執行過，故無 RMSE 數值可附。
# ──────────────────────────────────────────────────────────────────────────────
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset
import sys, os, warnings
from sklearn.metrics import r2_score

warnings.filterwarnings('ignore')

# ==============================================================================
# ★固定隨機種子（四支腳本一致 SEED=42）→ 結果可重現、消融比較公平
#   涵蓋所有未固定來源：座標 random Fourier B、譜卷積/線性層權重初始化、DataLoader shuffle。
#   ※GPU 上 FFT/複數運算不保證 bit-perfect 完全決定性；如需更嚴格可另加
#     torch.backends.cudnn.deterministic=True（會略降速度）。
# ==============================================================================
import random
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)

# ==============================================================================
# 0. 檔案路徑 / 開關（★與 camshead_noweather 完全相同,讀同一份資料）
# ==============================================================================
fpca_pm25_file = "/home/casper/air/完整DATA2/用FPCA去補NAN的DATA/PM2.5.csv"
raw_pm25_file  = "/home/casper/air/fda_class/merged_reshaped_PM2.5.csv"
station_file   = "/home/casper/air/完整DATA2/測站經緯度72測站.csv"

# ★ 無氣象:weather_files 留空(與 camshead_noweather 一致)
weather_files = {}

USE_CAMS_HEAD = True             # True=CAMS 進 head 去偏(與母本一致);False=退回持續性錨點
STATION_EMBED_DIM = 16
T_PAST = 24; T_FUT = 72; T_TOTAL = T_PAST + T_FUT      # 96

# ★ 驗證集 / Early stopping 設定
VAL_FRAC = 0.10                  # 訓練期最後 10% 當驗證集（時間序切分）
PATIENCE = 100                   # 驗證 loss 連續 PATIENCE 個 epoch 未改善就提前停止
MAX_EPOCH = 800                  # 訓練 epoch 上限（早停通常會在此之前觸發）


# ==============================================================================
# 0b. CAMS 最近格點（Nearest-neighbor）建置 / 快取
# ==============================================================================
#   cams_npz_file          原本雙線性內插版的 CAMS 錨點 npz（僅供對照，本檔不使用）。
#   cams_npz_nearest_file  ★本檔實際使用的「最近格點」版 CAMS 錨點 npz（會自動建置/快取）。
#   cams_raw_nc_dir        原始 CAMS 網格 netCDF 月檔（cams_pm25_YYYYMM.nc）所在目錄。
#   ★★建議做法★★：直接把本機預先建好、與 bilinear 完全同站同序(75站)的
#     CAMS_PM25_perinit_nearest.npz 上傳到 cams_npz_nearest_file，即可跳過 fallback 重建。
cams_npz_file          = "/home/casper/air/完整DATA2/cams/CAMS_PM25_perinit.npz"          # 原雙線性版（對照用，不使用）
cams_npz_nearest_file  = "/home/casper/air/完整DATA2/cams/CAMS_PM25_perinit_nearest.npz"  # ★本檔實際使用
cams_raw_nc_dir        = "/home/casper/air/完整DATA2/cams/12_00/pm25"                     # ★原始網格月檔目錄，請確認


def _open_ascii_nc(path):
    """netCDF4 在含中文/特殊字元路徑時可能開不了（Windows 常見），先複製到 ASCII 暫存路徑再開。"""
    import shutil, tempfile
    tmp = os.path.join(tempfile.gettempdir(), "_cams_open_nearest.nc")
    shutil.copy(path, tmp)
    return tmp


def build_cams_nearest_npz(raw_nc_dir, station_csv, out_npz, ref_npz):
    """從原始 CAMS 網格 netCDF 月檔，用「最近格點」(method='nearest') 取代雙線性內插，
    重建與 CAMS_PM25_perinit.npz 相同結構（pm25/stations/valid_local）的錨點檔。
    ★測站清單與站序以 ref_npz(bilinear 錨點) 為準，座標查自 station_csv，確保 nearest 與
      bilinear 兩檔「只差在內插方法」；station_csv 若缺任一站座標 → 報錯中止。"""
    import glob
    import xarray as xr

    names = list(np.load(ref_npz, allow_pickle=True)['stations'])
    coord = pd.read_csv(station_csv).drop_duplicates('SiteName').set_index('SiteName')
    miss = [s for s in names if s not in coord.index]
    if miss:
        sys.exit(f"錯誤：station_file 缺少下列測站座標，無法與 bilinear 對齊建 nearest：{miss}"
                  f"（請改用含全部 {len(names)} 站座標的測站經緯度 csv）")
    lon_da = xr.DataArray(coord.reindex(names)['TWD97Lon'].values, dims='station')
    lat_da = xr.DataArray(coord.reindex(names)['TWD97Lat'].values, dims='station')
    files = sorted(glob.glob(os.path.join(raw_nc_dir, "*.nc")))
    if not files:
        sys.exit(f"錯誤：找不到原始 CAMS 網格 netCDF：{raw_nc_dir}/*.nc"
                  f"（最近格點需要原始網格資訊，無法從既有雙線性 npz 回推）")
    print(f"[CAMS-nearest] 測站數: {len(names)}  月檔數: {len(files)}")

    pm_blocks, valid_list = [], []
    for i, f in enumerate(files):
        tmp = _open_ascii_nc(f)
        ds = xr.open_dataset(tmp)
        da = ds['pm2p5'] * 1e9                                            # kg/m3 -> µg/m3
        interp = (da.interp(longitude=lon_da, latitude=lat_da, method='nearest')  # ★最近格點
                    .transpose('forecast_reference_time', 'forecast_period', 'station'))
        arr = interp.values
        vt  = ds['valid_time'].transpose('forecast_reference_time', 'forecast_period').values
        frt = ds['forecast_reference_time'].values
        ds.close()
        order = np.argsort(frt)
        arr, vt = arr[order], vt[order]
        for d in range(arr.shape[0]):
            valid_local = pd.to_datetime(vt[d]) + pd.Timedelta(hours=8)
            pm_blocks.append(arr[d]); valid_list.append(valid_local.values)
        if (i + 1) % 12 == 0 or i == len(files) - 1:
            print(f"[CAMS-nearest]  已處理 {i+1}/{len(files)}  累積起報 {len(pm_blocks):,}")

    pm25 = np.stack(pm_blocks, axis=0).astype(np.float32)
    valid_local = np.stack(valid_list, axis=0)
    out_dir = os.path.dirname(out_npz)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    np.savez_compressed(out_npz, pm25=pm25, valid_local=valid_local,
                        stations=np.array(names, dtype=object))
    print(f"[CAMS-nearest] 已建立最近格點快取：{out_npz}  shape={pm25.shape}")


def ensure_cams_nearest(raw_nc_dir, station_csv, out_npz, ref_npz):
    if os.path.exists(out_npz):
        print(f"[CAMS-nearest] 使用既有快取：{out_npz}")
    else:
        print(f"[CAMS-nearest] 快取不存在，從原始網格重建（僅需一次）：{out_npz}")
        build_cams_nearest_npz(raw_nc_dir, station_csv, out_npz, ref_npz)


# ==============================================================================
# 1. 核心模組
# ==============================================================================
class GaussianFourierFeatureTransform(nn.Module):
    def __init__(self, mapping_size=64, scale=10):
        super().__init__()
        self.register_buffer('B', torch.randn((2, mapping_size)) * scale)
    def forward(self, x):
        x_proj = (2. * np.pi * x) @ self.B
        return torch.cat([torch.sin(x_proj), torch.cos(x_proj)], dim=-1)


class PerStationMixer(nn.Module):
    """逐站 channel mixer(★無跨站)。N 折進 batch，GroupNorm 與 1×1 conv 皆逐站獨立。"""
    def __init__(self, width):
        super().__init__()
        self.norm = nn.GroupNorm(num_groups=min(8, width), num_channels=width)
        self.net = nn.Sequential(nn.Conv1d(width, width, 1), nn.GELU(), nn.Conv1d(width, width, 1))
    def forward(self, x):
        B, C, N, T = x.shape
        xr = x.permute(0, 2, 1, 3).reshape(B * N, C, T)
        y = self.net(self.norm(xr))
        return y.reshape(B, N, C, T).permute(0, 2, 1, 3)


class TimeEncoder(nn.Module):
    def __init__(self, embed_dim=16):
        super().__init__()
        self.hour_emb  = nn.Embedding(24, embed_dim)
        self.dow_emb   = nn.Embedding(7,  embed_dim)
        self.month_emb = nn.Embedding(12, embed_dim)
        self.proj = nn.Sequential(nn.Linear(embed_dim * 3, embed_dim * 3), nn.GELU())
    def forward(self, hour, dow, month):
        emb = torch.cat([self.hour_emb(hour), self.dow_emb(dow), self.month_emb(month)], dim=-1)
        return self.proj(emb)


# ==============================================================================
# 2. ★ 逐站 1D 時間譜卷積（取代 3D VFT;無空間耦合）
# ==============================================================================
class SpectralConv1dTime(nn.Module):
    def __init__(self, in_channels, out_channels, modes_t):
        super().__init__()
        self.in_channels = in_channels; self.out_channels = out_channels
        self.modes_t = modes_t
        scale = 1 / (in_channels * out_channels)
        self.weights = nn.Parameter(scale * torch.rand(in_channels, out_channels, modes_t,
                                                       dtype=torch.cfloat))
    def forward(self, x):
        B, C, N, T = x.shape
        xr = x.permute(0, 2, 1, 3).reshape(B * N, C, T)                     # [B*N, C, T]
        x_ft = torch.fft.rfft(xr, dim=-1)                                   # [B*N, C, T//2+1]
        m = min(self.modes_t, x_ft.shape[-1])
        out_ft = torch.zeros(B * N, self.out_channels, x_ft.shape[-1],
                             dtype=torch.cfloat, device=x.device)
        out_ft[:, :, :m] = torch.einsum("bit,iot->bot", x_ft[:, :, :m], self.weights[:, :, :m])
        xo = torch.fft.irfft(out_ft, n=T, dim=-1)                          # [B*N, out, T]
        return xo.reshape(B, N, self.out_channels, T).permute(0, 2, 1, 3)  # [B,out,N,T]


# ==============================================================================
# 3. FNO 主模型（★無空間耦合版）
# ==============================================================================
class FNO_1D_NoSpatial(nn.Module):
    def __init__(self, in_ch=2, n_stations=77, t_total=96, out_hours=72,
                 modes_s=16, modes_t=12, width=64, time_embed_dim=16, station_embed_dim=16,
                 use_cams_head=True):
        super().__init__()
        self.modes_s = modes_s; self.modes_t = modes_t; self.width = width
        self.t_total = t_total; self.out_hours = out_hours
        self.t_past = t_total - out_hours                       # 24
        self.use_cams_head = use_cams_head
        self.coord_mapper = GaussianFourierFeatureTransform(mapping_size=32, scale=10)  # ->64
        self.time_encoder = TimeEncoder(embed_dim=time_embed_dim)                       # ->48
        fc0_in = in_ch + 64 + time_embed_dim * 3                                        # 2+64+48=114
        self.fc0 = nn.Linear(fc0_in, width)
        self.conv0 = SpectralConv1dTime(width, width, modes_t)
        self.conv1 = SpectralConv1dTime(width, width, modes_t)
        self.conv2 = SpectralConv1dTime(width, width, modes_t)
        self.conv3 = SpectralConv1dTime(width, width, modes_t)
        self.w0 = PerStationMixer(width); self.w1 = PerStationMixer(width)
        self.w2 = PerStationMixer(width); self.w3 = PerStationMixer(width)
        self.aux_mlp = nn.Sequential(
            nn.Linear(fc0_in, 256), nn.GELU(),
            nn.Linear(256, 512),    nn.GELU(),
            nn.Linear(512, 256),    nn.GELU(),
            nn.Linear(256, width),  nn.GELU(),
        )
        head_extra = width if use_cams_head else 0
        self.head = nn.Sequential(nn.Linear(width + head_extra, width), nn.GELU(),
                                  nn.Linear(width, 1))
        self.cams_gate = nn.Parameter(torch.tensor(0.0))

    def forward(self, x, anchor, coords, hour, dow, month):
        B, N, T, C = x.shape
        last_obs = x[:, :, self.t_past - 1, 0]                                 # [B,N]
        xc = self.coord_mapper(coords).unsqueeze(2).expand(B, N, T, -1)        # [B,N,96,64]
        te = self.time_encoder(hour, dow, month).unsqueeze(1).expand(B, N, T, -1)  # [B,N,96,48]
        inp = torch.cat([x, xc, te], dim=-1)                                   # [B,N,96,114]
        aux = self.aux_mlp(inp)                                                # [B,N,96,64]
        h = self.fc0(inp)                                                      # [B,N,96,width]
        h = h.permute(0, 3, 1, 2)                                              # [B,width,N,96]
        h = F.gelu(self.conv0(h) + self.w0(h))
        h = F.gelu(self.conv1(h) + self.w1(h))
        h = F.gelu(self.conv2(h) + self.w2(h))
        h =        self.conv3(h) + self.w3(h)
        h = h.permute(0, 2, 3, 1)                                              # [B,N,96,width]
        h   = h[:, :, self.t_total - self.out_hours:, :]                       # 取未來72
        aux = aux[:, :, self.t_total - self.out_hours:, :]                     # 取未來72

        lo = last_obs.unsqueeze(-1).expand(-1, -1, self.out_hours)             # [B,N,72]
        if self.use_cams_head:
            feat = torch.cat([h, aux], dim=-1)                                 # [B,N,72,width+64]
            delta = self.head(feat).squeeze(-1)
            g = torch.sigmoid(self.cams_gate)
            base = lo + g * (anchor - lo)
        else:
            delta = self.head(h).squeeze(-1)
            base = last_obs.unsqueeze(-1)
        return base + delta, delta


# ==============================================================================
# 4. CAMS 錨點載入
# ==============================================================================
def load_cams_anchor(npz_path, station_names):
    d = np.load(npz_path, allow_pickle=True)
    cams = d['pm25']; stns = list(d['stations'])
    valid0 = pd.to_datetime(d['valid_local'][:, 0])
    n_init = cams.shape[0]
    idx = np.array([stns.index(s) for s in station_names])
    anc = cams[:, :, idx].astype(np.float32)
    lookup = {pd.Timestamp(valid0[i]): i for i in range(n_init)}
    return anc, lookup


def cams_station_set(npz_path):
    d = np.load(npz_path, allow_pickle=True)
    return set(list(d['stations']))


# ==============================================================================
# 5. 序列建立 + 組 96 軸
# ==============================================================================
def create_sequences(data_dict, station_names, raw_pm25_df, cams_anc, cams_lookup,
                     t_past=24, t_fut=72, stride=24, align_t0_hour=0):
    ordered_keys = ['pm25'] + [k for k in data_dict if k != 'pm25']
    df_pm25 = data_dict['pm25']
    n_stations = len(station_names); n_rows = len(df_pm25)
    V = len(ordered_keys); t_total = t_past + t_fut
    base_hour = pd.Timestamp(df_pm25['datetime'].iloc[t_past]).hour
    offset = (align_t0_hour - base_hour) % stride
    n_seq = (n_rows - offset - t_total) // stride + 1
    if n_seq <= 0:
        return (None,) * 6, []

    Xpast = np.zeros((n_seq, n_stations, t_past, V), dtype=np.float32)
    A     = np.full((n_seq, n_stations, t_fut), np.nan, dtype=np.float32)
    Yraw  = np.full((n_seq, n_stations, t_fut), np.nan, dtype=np.float32)
    Hh = np.zeros((n_seq, t_total), dtype=np.int64)
    Dd = np.zeros((n_seq, t_total), dtype=np.int64)
    Mm = np.zeros((n_seq, t_total), dtype=np.int64)
    st_map = {name: i for i, name in enumerate(station_names)}
    timestamps = []; keep = []
    dt_all = pd.to_datetime(df_pm25['datetime'])
    for seq_idx in range(n_seq):
        start = offset + seq_idx * stride
        end_obs = start + t_past; end_pred = end_obs + t_fut
        if end_pred > n_rows:
            break
        ts = df_pm25['datetime'].iloc[end_obs]
        row = cams_lookup.get(pd.Timestamp(ts))
        if row is None:
            continue
        A[seq_idx] = cams_anc[row].T
        tl = dt_all.iloc[start:end_pred]
        Hh[seq_idx] = tl.dt.hour.values
        Dd[seq_idx] = tl.dt.dayofweek.values
        Mm[seq_idx] = (tl.dt.month - 1).values
        for st_name, st_idx in st_map.items():
            if st_name not in df_pm25.columns:
                continue
            for v_idx, key in enumerate(ordered_keys):
                df = data_dict[key]
                if st_name in df.columns:
                    Xpast[seq_idx, st_idx, :, v_idx] = df[st_name].iloc[start:end_obs].values
            if st_name in raw_pm25_df.columns:
                rw = raw_pm25_df[st_name].iloc[end_obs:end_pred].values
                if len(rw) == t_fut:
                    Yraw[seq_idx, st_idx, :] = rw
        timestamps.append(ts); keep.append(seq_idx)

    keep = np.array(keep, dtype=int)
    return (Xpast[keep], A[keep], Yraw[keep], Hh[keep], Dd[keep], Mm[keep]), timestamps


def build_x96(Xpast_n_pm, A_norm):
    n, N = A_norm.shape[0], A_norm.shape[1]
    past_block = Xpast_n_pm
    fut_block  = A_norm.unsqueeze(-1)
    x_vars = torch.cat([past_block, fut_block], dim=2)
    mask = torch.cat([torch.ones(n, N, 24, 1), torch.zeros(n, N, 72, 1)], dim=2)
    return torch.cat([x_vars, mask], dim=-1)


# ==============================================================================
# 6. 資料載入 / 切分（★訓練期再切「最後 10% 當驗證集」；正規化統計只用訓練 90%）
# ==============================================================================
def load_and_split_data(fpca_file, raw_file, station_file, weather_files, cams_npz):
    print("--- [Step 1] 讀取數據(逐站1D,無氣象,CAMS最近格點,+驗證集early stopping)---")
    df_stations = pd.read_csv(station_file)
    data_frames = {'pm25': pd.read_csv(fpca_file)}
    for name, path in weather_files.items():
        if os.path.exists(path):
            data_frames[name] = pd.read_csv(path)
    for name, df in data_frames.items():
        t_col = next((c for c in ['PublishTime','date','time','datetime'] if c in df.columns), None)
        df.rename(columns={t_col: 'datetime'}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.sort_values('datetime', inplace=True)
        data_frames[name] = df

    common = set(df_stations['SiteName']) & set(data_frames['pm25'].columns)
    for df in data_frames.values():
        common &= set(df.columns)
    if not os.path.exists(cams_npz):
        sys.exit(f"錯誤：找不到 CAMS 錨點 npz：{cams_npz}")
    common &= cams_station_set(cams_npz)
    common = sorted(list(common))
    print(f"共同測站數量(觀測 ∩ CAMS): {len(common)}")
    if not common:
        sys.exit("錯誤：找不到共同測站")

    df_st = (df_stations[df_stations['SiteName'].isin(common)]
             .set_index('SiteName').reindex(common).reset_index())
    lons = df_st['TWD97Lon'].values; lats = df_st['TWD97Lat'].values
    x_norm = (lons - lons.min()) / (lons.max() - lons.min() + 1e-8)
    y_norm = (lats - lats.min()) / (lats.max() - lats.min() + 1e-8)
    coords = np.stack([x_norm, y_norm], axis=1).astype(np.float32)

    full_idx = pd.date_range(start=data_frames['pm25']['datetime'].min(),
                             end=data_frames['pm25']['datetime'].max(), freq='H')
    aligned = {}
    for name, df in data_frames.items():
        df = df.drop_duplicates('datetime').set_index('datetime')[common].reindex(full_idx)
        for c in common:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        aligned[name] = df.reset_index().rename(columns={'index': 'datetime'})

    print("套用 log1p 至 FPCA PM2.5 ...")
    for c in common:
        aligned['pm25'][c] = np.log1p(np.maximum(aligned['pm25'][c].values, 0.0))

    if not os.path.exists(raw_file):
        sys.exit("錯誤：本版必須有原始 PM2.5(訓練目標)")
    print(f"載入原始 PM2.5：{raw_file}")
    df_raw = pd.read_csv(raw_file)
    t_col = next((c for c in ['PublishTime','date','time','datetime'] if c in df_raw.columns), None)
    df_raw.rename(columns={t_col: 'datetime'}, inplace=True)
    df_raw['datetime'] = pd.to_datetime(df_raw['datetime'])
    df_raw = df_raw.drop_duplicates('datetime').set_index('datetime')
    raw_cols = [c for c in common if c in df_raw.columns]
    df_raw = df_raw[raw_cols].reindex(full_idx)
    for c in raw_cols:
        df_raw[c] = pd.to_numeric(df_raw[c], errors='coerce')
    for c in common:
        if c not in df_raw.columns:
            df_raw[c] = np.nan
    raw_aligned = df_raw[common]

    print(f"載入 CAMS 錨點：{cams_npz}")
    cams_anc, cams_lookup = load_cams_anchor(cams_npz, common)

    split_date = pd.Timestamp('2025-01-01'); test_end = pd.Timestamp('2025-11-30')

    # ── 訓練期(<2025) 全部序列（之後再切訓練/驗證）──
    train_dfs = {n: df[df['datetime'] < split_date].copy() for n, df in aligned.items()}
    raw_train = raw_aligned[raw_aligned.index < split_date].copy()
    (Xp_tr, A_tr, Yraw_tr, H_tr, D_tr, M_tr), ts_tr = create_sequences(
        train_dfs, common, raw_train, cams_anc, cams_lookup, T_PAST, T_FUT, 24, 0)
    Xp_tr = torch.tensor(Xp_tr); A_tr = torch.tensor(A_tr); Yraw_tr = torch.tensor(Yraw_tr)

    # ★[早停版] 時間序切分：序列已按時間排序，取「最後 VAL_FRAC」當驗證集（時間最新一段）
    n_total_tr = Xp_tr.shape[0]
    n_val   = max(1, int(round(n_total_tr * VAL_FRAC)))
    n_train = n_total_tr - n_val
    print(f"  訓練期序列 {n_total_tr} → 訓練(前90%) {n_train} / 驗證(最後{int(VAL_FRAC*100)}%) {n_val}")

    # ★[早停版] 正規化統計「只用訓練部分(前90%)」計算，避免驗證集洩漏進 normalization
    pm_vals = Xp_tr[:n_train, :, :, 0]
    y_mean = pm_vals.mean(); y_std = pm_vals.std() + 1e-6

    past_pm   = (Xp_tr[:, :, :, 0:1] - y_mean) / y_std
    A_tr_norm = (torch.log1p(torch.clamp(A_tr, min=0)) - y_mean) / y_std
    X96_all   = build_x96(past_pm, A_tr_norm)
    tgt_all   = (torch.log1p(torch.clamp(Yraw_tr, min=0)) - y_mean) / y_std
    mask_all  = ~torch.isnan(Yraw_tr)
    H_tr = torch.tensor(H_tr); D_tr = torch.tensor(D_tr); M_tr = torch.tensor(M_tr)

    # ★[早停版] 切分：前 n_train=訓練(較早)、後 n_val=驗證(最新)
    X96_train, X96_val   = X96_all[:n_train],   X96_all[n_train:]
    A_train,   A_val     = A_tr_norm[:n_train], A_tr_norm[n_train:]
    Y_train,   Y_val     = tgt_all[:n_train],   tgt_all[n_train:]
    mask_train, mask_val = mask_all[:n_train],  mask_all[n_train:]
    H_train, H_val       = H_tr[:n_train],      H_tr[n_train:]
    D_train, D_val       = D_tr[:n_train],      D_tr[n_train:]
    M_train, M_val       = M_tr[:n_train],      M_tr[n_train:]
    print(f"  訓練序列: {n_train} (有效目標 {mask_train.float().mean():.1%})；"
          f"驗證序列: {n_val} (有效目標 {mask_val.float().mean():.1%})")

    # ── 測試集(2025) ──
    test_dfs = {n: df[(df['datetime'] >= split_date) & (df['datetime'] < test_end)].copy()
                for n, df in aligned.items()}
    raw_test = raw_aligned[(raw_aligned.index >= split_date) & (raw_aligned.index < test_end)].copy()
    (Xp_te, A_te, Yraw_te, H_te, D_te, M_te), ts_te = create_sequences(
        test_dfs, common, raw_test, cams_anc, cams_lookup, T_PAST, T_FUT, 24, 0)

    if Xp_te is not None and len(Xp_te) > 0:
        Xp_te = torch.tensor(Xp_te); A_te = torch.tensor(A_te)
        past_pm_te = (Xp_te[:, :, :, 0:1] - y_mean) / y_std
        A_te_norm = (torch.log1p(torch.clamp(A_te, min=0)) - y_mean) / y_std
        X96_te = build_x96(past_pm_te, A_te_norm)
        Y_te_eval = torch.tensor(Yraw_te)
        H_te = torch.tensor(H_te); D_te = torch.tensor(D_te); M_te = torch.tensor(M_te)
    else:
        X96_te = A_te_norm = Y_te_eval = torch.tensor([])
        H_te = D_te = M_te = torch.tensor([], dtype=torch.long)

    print(f"訓練集: {n_train}, 驗證集: {n_val}, 測試集: {len(X96_te)}")
    return {
        'X96_train': X96_train, 'A_train': A_train, 'Y_train': Y_train, 'mask_train': mask_train,
        'H_train': H_train, 'D_train': D_train, 'M_train': M_train,
        'X96_val': X96_val, 'A_val': A_val, 'Y_val': Y_val, 'mask_val': mask_val,
        'H_val': H_val, 'D_val': D_val, 'M_val': M_val,
        'X96_test': X96_te, 'A_test': A_te_norm, 'Y_test_raw': Y_te_eval,
        'H_test': H_te, 'D_test': D_te, 'M_test': M_te,
        'coords': torch.tensor(coords), 'station_names': common,
        'n_vars': int(Xp_tr.shape[-1]),
        'stats': {'y_mean': y_mean, 'y_std': y_std},
    }


# ==============================================================================
# 7. 訓練 loss（遮罩 Huber）
# ==============================================================================
def masked_huber(pred, target, mask):
    target_safe = torch.nan_to_num(target, nan=0.0)
    loss_pt = F.huber_loss(pred, target_safe, reduction='none')
    m = mask.float()
    return (loss_pt * m).sum() / m.sum().clamp(min=1.0)


# ==============================================================================
# 8. 主程式（★驗證集 early stopping：以驗證 loss 選最佳模型，測試集只做最終評估）
# ==============================================================================
if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        sys.exit(f"找不到檔案: {fpca_pm25_file}")

    # ★CAMS 最近格點：若快取不存在，先從原始網格月檔重建一次（測站以 bilinear npz 為準）
    ensure_cams_nearest(cams_raw_nc_dir, station_file, cams_npz_nearest_file, cams_npz_file)

    data = load_and_split_data(fpca_pm25_file, raw_pm25_file, station_file, weather_files,
                               cams_npz_nearest_file)   # ★最近格點版 CAMS 錨點
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"\nDevice: {device} | 模式: 逐站1D + CAMS最近格點 + ★驗證集early stopping"
          f"{'｜CAMS進head' if USE_CAMS_HEAD else ''}")

    N_STA = len(data['station_names'])
    IN_CH = data['n_vars'] + 1                              # pm25 + mask = 2
    model = FNO_1D_NoSpatial(in_ch=IN_CH, n_stations=N_STA, t_total=T_TOTAL, out_hours=T_FUT,
                             modes_s=10, modes_t=44, width=64,
                             time_embed_dim=16, station_embed_dim=STATION_EMBED_DIM,
                             use_cams_head=USE_CAMS_HEAD).to(device)
    print(f"x96 通道數 in_ch={IN_CH}(pm25+mask,無氣象) | ★無空間耦合(逐站1D時間譜,modes_t=44) | "
          f"模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    sch = torch.optim.lr_scheduler.StepLR(opt, step_size=400, gamma=0.5)

    train_loader = DataLoader(
        TensorDataset(data['X96_train'], data['A_train'], data['Y_train'], data['mask_train'],
                      data['H_train'], data['D_train'], data['M_train']),
        batch_size=16, shuffle=True)
    # ★驗證 loader（shuffle=False，不消耗訓練亂數流）
    val_loader = DataLoader(
        TensorDataset(data['X96_val'], data['A_val'], data['Y_val'], data['mask_val'],
                      data['H_val'], data['D_val'], data['M_val']),
        batch_size=16, shuffle=False)
    has_test = len(data['X96_test']) > 0
    test_loader = DataLoader(
        TensorDataset(data['X96_test'], data['A_test'], data['Y_test_raw'],
                      data['H_test'], data['D_test'], data['M_test']),
        batch_size=16, shuffle=False) if has_test else None

    coords_base = data['coords'].unsqueeze(0).to(device)
    y_mean = data['stats']['y_mean'].to(device); y_std = data['stats']['y_std'].to(device)

    # ★ Early stopping 狀態：以「驗證 loss」為準
    best_val = float('inf'); best_ep = 0; epochs_no_improve = 0
    best_path = "best_fno_1d_perstation_coordonly_2_auxmlp_10_44_camsnearest_earlystop.pth"  # ★早停版專用
    train_hist, val_hist = [], []
    print(f"\n=== 開始訓練(逐站1D + CAMS最近格點 + ★early stopping,patience={PATIENCE},上限{MAX_EPOCH})===")
    for ep in range(1, MAX_EPOCH + 1):
        # --- 訓練 ---
        model.train(); loss_sum = 0.0
        for bx, ba, by, bm, bh, bd, bmo in train_loader:
            bx = bx.to(device); ba = ba.to(device); by = by.to(device); bm = bm.to(device)
            bh = bh.to(device); bd = bd.to(device); bmo = bmo.to(device)
            coords = coords_base.expand(bx.shape[0], -1, -1)
            opt.zero_grad()
            pred, _ = model(bx, ba, coords, bh, bd, bmo)
            loss = masked_huber(pred, by, bm)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            loss_sum += loss.item()
        sch.step()
        avg = loss_sum / len(train_loader)
        train_hist.append(avg)

        # --- 驗證（同一個 masked_huber；NaN 目標用遮罩排除）---
        model.eval(); val_sum = 0.0
        with torch.no_grad():
            for bx, ba, by, bm, bh, bd, bmo in val_loader:
                bx = bx.to(device); ba = ba.to(device); by = by.to(device); bm = bm.to(device)
                bh = bh.to(device); bd = bd.to(device); bmo = bmo.to(device)
                coords = coords_base.expand(bx.shape[0], -1, -1)
                pred, _ = model(bx, ba, coords, bh, bd, bmo)
                val_sum += masked_huber(pred, by, bm).item()
        val_avg = val_sum / len(val_loader)
        val_hist.append(val_avg)

        # --- Early stopping：只有「驗證 loss 創新低」才存 checkpoint ---
        if val_avg < best_val:
            best_val = val_avg; best_ep = ep; epochs_no_improve = 0
            torch.save(model.state_dict(), best_path)
        else:
            epochs_no_improve += 1

        if ep % 10 == 0 or ep == 1:
            g = torch.sigmoid(model.cams_gate).item()
            print(f"Epoch {ep:4d} | Train: {avg:.6f} | Val: {val_avg:.6f} | "
                  f"BestVal: {best_val:.6f}@{best_ep} | noImp: {epochs_no_improve}/{PATIENCE} | "
                  f"gate: {g:.3f} | LR: {opt.param_groups[0]['lr']:.6f}")

        if epochs_no_improve >= PATIENCE:
            print(f"\n★ Early stopping：驗證 loss 已連續 {PATIENCE} 個 epoch 未改善，於 epoch {ep} 停止。")
            break

    print(f"\n★ 最佳(驗證)模型：epoch {best_ep}，val loss = {best_val:.6f}")
    print(f"載入最佳(驗證)模型: {best_path}")
    model.load_state_dict(torch.load(best_path)); model.eval()
    print(f"最終 CAMS gate = {torch.sigmoid(model.cams_gate).item():.3f}  (0=持續性, 1=CAMS+Δ)")

    print("\n=== 測試集(2025)評估(expm1 還原,對原始 PM2.5,NaN 跳過;用最佳驗證模型)===")
    if test_loader:
        preds = []
        with torch.no_grad():
            for bx, ba, by, bh, bd, bmo in test_loader:
                bx = bx.to(device); ba = ba.to(device)
                bh = bh.to(device); bd = bd.to(device); bmo = bmo.to(device)
                coords = coords_base.expand(bx.shape[0], -1, -1)
                pred, _ = model(bx, ba, coords, bh, bd, bmo)
                pred = torch.expm1((pred * y_std + y_mean).clamp(min=0))
                preds.append(pred.cpu())
        P = torch.cat(preds, dim=0).numpy()
        T_true = data['Y_test_raw'].numpy(); N_sta = P.shape[1]
        C = torch.expm1((data['A_test'] * y_std.cpu() + y_mean.cpu()).clamp(min=0)).numpy()

        def paper_metric(pred, true, metric='rmse', seg=None):
            s, e = (0, 72) if seg is None else seg
            vals = []
            for h in range(s, e):
                m = ~np.isnan(true[:, :, h]) & ~np.isnan(pred[:, :, h])
                if m.sum() < 2:
                    continue
                d = pred[:, :, h][m] - true[:, :, h][m]
                vals.append(np.sqrt(np.mean(d**2)) if metric == 'rmse' else np.mean(np.abs(d)))
            return float(np.mean(vals)) if vals else float('nan')

        print(f"\n有效評估點：{(~np.isnan(T_true)).sum():,}，測站數：{N_sta}")
        print("=" * 60)
        print("  三天整體 — 中央論文 per-hour pooled（★無空間耦合,CAMS最近格點,early stopping）")
        print(f"  [模型]     RMSE: {paper_metric(P,T_true,'rmse'):.4f}  MAE: {paper_metric(P,T_true,'mae'):.4f}")
        print(f"  [CAMS原始] RMSE: {paper_metric(C,T_true,'rmse'):.4f}  MAE: {paper_metric(C,T_true,'mae'):.4f}")
        print("=" * 60)
        for s, e, lb in [(0,24,'第1天 (+01~+24h)'),(24,48,'第2天 (+25~+48h)'),(48,72,'第3天 (+49~+72h)')]:
            print(f"  {lb}  模型 RMSE: {paper_metric(P,T_true,'rmse',(s,e)):.4f} "
                  f"MAE: {paper_metric(P,T_true,'mae',(s,e)):.4f}  |  "
                  f"CAMS RMSE: {paper_metric(C,T_true,'rmse',(s,e)):.4f}")

        sta_rmse, sta_mae, sta_r2 = [], [], []
        for n in range(N_sta):
            t_n = T_true[:, n, :]; m_n = ~np.isnan(t_n)
            if m_n.sum() < 2:
                continue
            pv = P[:, n, :][m_n]; tv = t_n[m_n]
            sta_rmse.append(np.sqrt(np.mean((pv - tv)**2))); sta_mae.append(np.mean(np.abs(pv - tv)))
            if np.var(tv) > 1e-8:
                sta_r2.append(r2_score(tv, pv))
        print(f"\n[參考] 先站後平均(有效站 {len(sta_rmse)}/{N_sta})  "
              f"RMSE: {np.mean(sta_rmse):.4f}  MAE: {np.mean(sta_mae):.4f}  R²: {np.mean(sta_r2):.4f}")
    else:
        print("沒有測試資料,跳過評估")
