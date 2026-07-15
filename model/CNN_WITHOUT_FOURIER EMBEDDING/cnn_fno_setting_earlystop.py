# 有效評估點：1,684,069，測站數：72
# =======================================================
#   CNN + CAMS + 時間embedding（無氣象）72h（中央論文 per-hour pooled）
#   RMSE: 6.8063  MAE: 4.8525
# =======================================================
#   Day1  RMSE=6.1357  MAE=4.2915
#   Day2  RMSE=7.0352  MAE=5.0394
#   Day3  RMSE=7.2482  MAE=5.2266


import os
import sys
import warnings
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

warnings.filterwarnings('ignore')

# ==============================================================================
# ★固定隨機種子（與 FNO 早停版一致 SEED=42）→ 結果可重現、消融比較公平
# ==============================================================================
import random
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)


# ==============================================================================
# 0. 設定
# ==============================================================================
INPUT_HOURS  = 24
OUTPUT_HOURS = 72
T_TOTAL      = INPUT_HOURS + OUTPUT_HOURS          # 96（時間 embedding 的長度）
STRIDE       = 24
TIME_EMBED_DIM = 16                                # 與 v9 相同

# ★ 驗證集 / Early stopping 設定（與 FNO 早停版一致）
VAL_FRAC = 0.10                  # 訓練期最後 10% 當驗證集（時間序切分）
PATIENCE = 100                   # 驗證 loss 連續 PATIENCE 個 epoch 未改善就提前停止

fpca_pm25_file = "PM2.5.csv"
raw_pm25_file  = "merged_reshaped_PM2.5.csv"
station_file   = "測站經緯度 72.csv"
cams_npz_file  = "CAMS_PM25_perinit_nearest.npz"  # ★最近格點版 CAMS


# ==============================================================================
# 1a. 時間 Encoder（★與 v9 fno_dse_3d_v9_camshead 完全相同）
# ==============================================================================
class TimeEncoder(nn.Module):
    def __init__(self, embed_dim=16):
        super().__init__()
        self.hour_emb  = nn.Embedding(24, embed_dim)
        self.dow_emb   = nn.Embedding(7,  embed_dim)
        self.month_emb = nn.Embedding(12, embed_dim)
        self.proj = nn.Sequential(nn.Linear(embed_dim * 3, embed_dim * 3), nn.GELU())
    def forward(self, hour, dow, month):
        emb = torch.cat([self.hour_emb(hour), self.dow_emb(dow), self.month_emb(month)], dim=-1)
        return self.proj(emb)                       # [..., embed_dim*3]


# ==============================================================================
# 1b. 模型（觀測PM2.5-1DCNN + CAMS-1DCNN + 時間embedding + aux → FC）
# ==============================================================================
class CNN_CAMS_TIME(nn.Module):
    def __init__(self, T_obs=24, T_cams=72, t_total=96, n_aux=2, out_hours=72, time_embed_dim=16):
        super().__init__()
        self.t_total = t_total
        # 觀測 PM2.5 路徑：1D-Conv(16) ×2
        self.conv_obs = nn.Sequential(
            nn.Conv1d(1, 16, kernel_size=3, padding=1), nn.ReLU(),
            nn.Conv1d(16, 16, kernel_size=3, padding=1), nn.ReLU(),
        )
        # CAMS 模擬 PM2.5 路徑：1D-Conv(32) ×3
        self.conv_sim = nn.Sequential(
            nn.Conv1d(1, 32, kernel_size=3, padding=1), nn.ReLU(),
            nn.Conv1d(32, 32, kernel_size=3, padding=1), nn.ReLU(),
            nn.Conv1d(32, 32, kernel_size=3, padding=1), nn.ReLU(),
        )
        # ★ 時間 Embedding（與 v9 相同）：每小時 → embed_dim*3，攤平 96 小時後 concat
        self.time_encoder = TimeEncoder(embed_dim=time_embed_dim)
        time_flat = (time_embed_dim * 3) * t_total                 # 48 * 96 = 4608
        # Full connected layers：flat → 108 → 72 → 72(=輸出)
        flat_dim = 16 * T_obs + 32 * T_cams + time_flat + n_aux    # 384 + 2304 + 4608 + 2 = 7298
        self.fc = nn.Sequential(
            nn.Linear(flat_dim, 108), nn.ReLU(),
            nn.Linear(108, 72), nn.ReLU(),
            nn.Linear(72, out_hours),
        )

    def forward(self, x_obs, x_cams, aux, hour, dow, month):
        # x_obs:[B,1,24]  x_cams:[B,1,72]  aux:[B,n_aux]  hour/dow/month:[B,96]
        parts = [self.conv_obs(x_obs).flatten(start_dim=1),        # [B,16*24]
                 self.conv_sim(x_cams).flatten(start_dim=1)]       # [B,32*72]
        te = self.time_encoder(hour, dow, month).flatten(start_dim=1)   # [B,48*96]
        parts.append(te)
        parts.append(aux)                                          # [B,n_aux]
        return self.fc(torch.cat(parts, dim=1))


# ==============================================================================
# 2. CAMS 載入（同 FNO_DSE_3D）
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
# 3. 序列建立（★無氣象；觀測24h + CAMS 72h + 目標72h + 時間96h，以 t0 對齊 CAMS）
# ==============================================================================
def create_sequences(df_pm25, raw_pm25_df, cams_anc, cams_lookup, station_names,
                     input_hours=24, output_hours=72, stride=24):
    n_rows = len(df_pm25); n_sta = len(station_names)
    window = input_hours + output_hours
    n_seq = (n_rows - window) // stride + 1
    if n_seq <= 0:
        return (None,) * 7

    Xobs = np.zeros((n_seq, n_sta, input_hours),        dtype=np.float32)
    Xcam = np.full((n_seq, n_sta, output_hours), np.nan, dtype=np.float32)
    Yfp  = np.zeros((n_seq, n_sta, output_hours),       dtype=np.float32)
    Yraw = np.full((n_seq, n_sta, output_hours), np.nan, dtype=np.float32)
    Hh = np.zeros((n_seq, window), dtype=np.int64)             # ★ 時間特徵：整段96h
    Dd = np.zeros((n_seq, window), dtype=np.int64)
    Mm = np.zeros((n_seq, window), dtype=np.int64)

    dt_all = pd.to_datetime(df_pm25['datetime'])
    keep = []; t0s = []
    for s in range(n_seq):
        start = s * stride; end_obs = start + input_hours; end_pred = end_obs + output_hours
        if end_pred > n_rows:
            break
        t0 = df_pm25['datetime'].iloc[end_obs]
        row = cams_lookup.get(pd.Timestamp(t0))
        if row is None:
            continue                                     # 無對應 CAMS 起報 → 跳過(同 FNO)
        Xcam[s] = cams_anc[row].T
        tl = dt_all.iloc[start:end_pred]                 # ★ 整段96h 的時間戳
        Hh[s] = tl.dt.hour.values
        Dd[s] = tl.dt.dayofweek.values
        Mm[s] = (tl.dt.month - 1).values
        for si, st in enumerate(station_names):
            if st in df_pm25.columns:
                Xobs[s, si] = df_pm25[st].iloc[start:end_obs].values
                Yfp[s, si]  = df_pm25[st].iloc[end_obs:end_pred].values
            if raw_pm25_df is not None and st in raw_pm25_df.columns:
                rw = raw_pm25_df[st].iloc[end_obs:end_pred].values
                if len(rw) == output_hours:
                    Yraw[s, si] = rw
        keep.append(s); t0s.append(pd.Timestamp(t0))

    keep = np.array(keep, dtype=int)
    return Xobs[keep], Xcam[keep], Yfp[keep], Yraw[keep], Hh[keep], Dd[keep], Mm[keep], t0s


# ==============================================================================
# 4. 資料載入 / 切分（★無氣象）
# ==============================================================================
def _align(df, full_idx, common):
    df = df.drop_duplicates('datetime').set_index('datetime')[common].reindex(full_idx)
    for c in common:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.reset_index().rename(columns={'index': 'datetime'})


def load_and_split_data():
    print("--- [Step 1] 讀取數據（觀測PM2.5 + CAMS預報；★無氣象 + 時間embedding）---")
    df_stations = pd.read_csv(station_file)

    def read_dt(path):
        df = pd.read_csv(path)
        t_col = next((c for c in ['PublishTime', 'date', 'time', 'datetime'] if c in df.columns), None)
        df.rename(columns={t_col: 'datetime'}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        return df.sort_values('datetime')

    df_pm25 = read_dt(fpca_pm25_file)

    if not os.path.exists(cams_npz_file):
        sys.exit(f"錯誤：找不到 CAMS npz：{cams_npz_file}")

    # 共同測站：觀測 ∩ 經緯度 ∩ CAMS（★無氣象交集）
    common = set(df_stations['SiteName']) & set(df_pm25.columns) & cams_station_set(cams_npz_file)
    common = sorted(common)
    print(f"共同測站數量（觀測 ∩ CAMS）: {len(common)}")
    if not common:
        sys.exit("錯誤：找不到共同測站")

    df_st = (df_stations[df_stations['SiteName'].isin(common)]
             .set_index('SiteName').reindex(common).reset_index())
    lons = df_st['TWD97Lon'].values; lats = df_st['TWD97Lat'].values
    x_norm = (lons - lons.min()) / (lons.max() - lons.min() + 1e-8)
    y_norm = (lats - lats.min()) / (lats.max() - lats.min() + 1e-8)
    coords = np.stack([x_norm, y_norm], axis=1).astype(np.float32)

    full_idx = pd.date_range(start=df_pm25['datetime'].min(), end=df_pm25['datetime'].max(), freq='H')
    df_pm25 = _align(df_pm25, full_idx, common)

    # 原始 PM2.5（評估真值）
    raw_aligned = None
    if os.path.exists(raw_pm25_file):
        print(f"載入原始 PM2.5：{raw_pm25_file}")
        df_raw = read_dt(raw_pm25_file).drop_duplicates('datetime').set_index('datetime')
        raw_cols = [c for c in common if c in df_raw.columns]
        df_raw = df_raw[raw_cols].reindex(full_idx)
        for c in raw_cols:
            df_raw[c] = pd.to_numeric(df_raw[c], errors='coerce')
        for c in common:
            if c not in df_raw.columns:
                df_raw[c] = np.nan
        raw_aligned = df_raw[common]
    else:
        print(f"警告：找不到原始 PM2.5 {raw_pm25_file}")

    print(f"載入 CAMS 錨點：{cams_npz_file}")
    cams_anc, cams_lookup = load_cams_anchor(cams_npz_file, common)

    split_date = pd.Timestamp('2025-01-01'); test_end = pd.Timestamp('2025-11-30')

    def slice_period(lo, hi):
        pm = df_pm25[(df_pm25['datetime'] >= lo) & (df_pm25['datetime'] < hi)].copy().reset_index(drop=True)
        rw = (raw_aligned[(raw_aligned.index >= lo) & (raw_aligned.index < hi)].copy()
              if raw_aligned is not None else None)
        return pm, rw

    LO = pd.Timestamp('1900-01-01')
    # ── 訓練集 ──
    tr_pm, tr_raw = slice_period(LO, split_date)
    # ★ 訓練目標改為「原始觀測」(Yraw),不再用 FPCA 補值(Yfp)
    Xobs_tr, Xcam_tr, _, Yraw_tr, H_tr, D_tr, M_tr, _ = create_sequences(
        tr_pm, tr_raw, cams_anc, cams_lookup, common, INPUT_HOURS, OUTPUT_HOURS, STRIDE)
    Xobs_tr = torch.tensor(Xobs_tr); Xcam_tr = torch.tensor(Xcam_tr); Yraw_tr = torch.tensor(Yraw_tr)
    H_tr = torch.tensor(H_tr); D_tr = torch.tensor(D_tr); M_tr = torch.tensor(M_tr)

    # ★[早停版] 時間序切分：序列已按時間排序，取「最後 VAL_FRAC」當驗證集（時間最新一段）
    n_total_tr = Xobs_tr.shape[0]
    n_val   = max(1, int(round(n_total_tr * VAL_FRAC)))
    n_train = n_total_tr - n_val
    print(f"  訓練期序列 {n_total_tr} → 訓練(前90%) {n_train} / 驗證(最後{int(VAL_FRAC*100)}%) {n_val}")

    # ★ log1p + z-score 正規化(統計★只用訓練部分(前90%)★，避免驗證集洩漏進 normalization)
    Xobs_tr_log = torch.log1p(torch.clamp(Xobs_tr, min=0))
    y_mean = Xobs_tr_log[:n_train].mean(); y_std = Xobs_tr_log[:n_train].std() + 1e-6

    Xobs_all_n = (Xobs_tr_log - y_mean) / y_std
    Xcam_all_n = (torch.log1p(torch.clamp(Xcam_tr, min=0)) - y_mean) / y_std
    # ★ 目標=原始觀測,log1p 後以同一 y_mean/y_std 標準化;NaN 保留→以 mask 跳過
    Y_all_n    = (torch.log1p(torch.clamp(Yraw_tr, min=0)) - y_mean) / y_std
    mask_all   = ~torch.isnan(Yraw_tr)

    # ★[早停版] 切分：前 n_train=訓練(較早)、後 n_val=驗證(最新)
    Xobs_tr_n, Xobs_val_n = Xobs_all_n[:n_train], Xobs_all_n[n_train:]
    Xcam_tr_n, Xcam_val_n = Xcam_all_n[:n_train], Xcam_all_n[n_train:]
    Y_tr_n,    Y_val_n    = Y_all_n[:n_train],    Y_all_n[n_train:]
    mask_tr,   mask_val   = mask_all[:n_train],   mask_all[n_train:]
    H_val = H_tr[n_train:]; H_tr = H_tr[:n_train]
    D_val = D_tr[n_train:]; D_tr = D_tr[:n_train]
    M_val = M_tr[n_train:]; M_tr = M_tr[:n_train]

    # ── 測試集 ──
    te_pm, te_raw = slice_period(split_date, test_end)
    Xobs_te, Xcam_te, _, Yraw_te, H_te, D_te, M_te, t0_te = create_sequences(
        te_pm, te_raw, cams_anc, cams_lookup, common, INPUT_HOURS, OUTPUT_HOURS, STRIDE)
    if Xobs_te is not None and len(Xobs_te) > 0:
        Xobs_te_n = (torch.log1p(torch.clamp(torch.tensor(Xobs_te), min=0)) - y_mean) / y_std
        Xcam_te_n = (torch.log1p(torch.clamp(torch.tensor(Xcam_te), min=0)) - y_mean) / y_std
        Y_te_eval = torch.tensor(Yraw_te)
        H_te = torch.tensor(H_te); D_te = torch.tensor(D_te); M_te = torch.tensor(M_te)
    else:
        Xobs_te_n = Xcam_te_n = Y_te_eval = torch.tensor([])
        H_te = D_te = M_te = torch.tensor([], dtype=torch.long)

    print(f"訓練集: {n_train} | 驗證集: {n_val} | 測試集: {len(Xobs_te_n)} | ★最近格點CAMS + 時間embedding + log1p+z-score + 原始觀測目標")
    return {
        'Xobs_train': Xobs_tr_n, 'Xcam_train': Xcam_tr_n, 'Y_train': Y_tr_n, 'mask_train': mask_tr,
        'H_train': H_tr, 'D_train': D_tr, 'M_train': M_tr,
        'Xobs_val': Xobs_val_n, 'Xcam_val': Xcam_val_n, 'Y_val': Y_val_n, 'mask_val': mask_val,
        'H_val': H_val, 'D_val': D_val, 'M_val': M_val,
        'Xobs_test':  Xobs_te_n, 'Xcam_test':  Xcam_te_n, 'Y_test_raw': Y_te_eval,
        'H_test': H_te, 'D_test': D_te, 'M_test': M_te,
        'coords': torch.tensor(coords), 'station_names': common,
        't0_test': (t0_te if t0_te is not None else []),
        'stats': {'y_mean': y_mean, 'y_std': y_std},
    }


def flat_1d(X):     # [S,N,T] -> [S*N,1,T]
    S, N, T = X.shape
    return X.reshape(S * N, 1, T)


def flat_time(X, N):  # [S,96] -> [S*N,96]（每站共用同一序列時間）
    S, T = X.shape
    return X.unsqueeze(1).expand(-1, N, -1).reshape(S * N, T)


def save_predictions_csv(path, P, T_true, C, t0_list, station_names):
    """P/T_true/C:[S,N,72] µg/m³;t0_list:長度S的首預報時刻;依 valid_time 排序輸出長表。"""
    S, N, H = P.shape
    if S == 0 or len(t0_list) != S:
        print(f"略過存 CSV(S={S}, t0={len(t0_list)})")
        return
    t0 = pd.to_datetime(list(t0_list)).values.astype('datetime64[h]')
    lead = np.arange(H)                                          # 0..71
    valid = t0[:, None] + lead[None, :].astype('timedelta64[h]')        # [S,H] 各預報時刻
    valid_f = np.broadcast_to(valid[:, None, :], (S, N, H)).reshape(-1)
    init_f  = np.broadcast_to(t0[:, None, None], (S, N, H)).reshape(-1)
    lead_f  = np.broadcast_to((lead + 1)[None, None, :], (S, N, H)).reshape(-1)   # lead 1..72
    sta_f   = np.broadcast_to(np.array(station_names)[None, :, None], (S, N, H)).reshape(-1)
    df = pd.DataFrame({'valid_time': valid_f, 'init_time': init_f, 'lead_hour': lead_f,
                       'station': sta_f, 'pred': P.reshape(-1),
                       'true': T_true.reshape(-1), 'cams': C.reshape(-1)})
    df = df.sort_values(['valid_time', 'station', 'lead_hour']).reset_index(drop=True)
    df.to_csv(path, index=False, encoding='utf-8-sig')
    print(f"已儲存預測 CSV: {path}  ({len(df):,} 列,依 valid_time 排序)")


def masked_huber(pred, target, mask):
    """★ 遮罩 Huber(與 camshead FNO 完全一致);NaN 以 mask 跳過。"""
    target_safe = torch.nan_to_num(target, nan=0.0)
    loss_pt = F.huber_loss(pred, target_safe, reduction='none')
    m = mask.float()
    return (loss_pt * m).sum() / m.sum().clamp(min=1.0)


# ==============================================================================
# 5. 主程式
# ==============================================================================
if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        raise SystemExit(f"找不到檔案: {fpca_pm25_file}")

    data = load_and_split_data()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"\nDevice: {device} | ★無氣象 + 時間embedding(與v9相同)")

    Xobs_tr = data['Xobs_train']; Xcam_tr = data['Xcam_train']; Y_tr = data['Y_train']
    mask_tr = data['mask_train']
    Xobs_te = data['Xobs_test'];  Xcam_te = data['Xcam_test'];  Y_te = data['Y_test_raw']
    coords = data['coords']
    y_mean = data['stats']['y_mean']; y_std = data['stats']['y_std']
    N_sta = Xobs_tr.shape[1]; S_tr, S_te = Xobs_tr.shape[0], Xobs_te.shape[0]

    # 攤平成 (S*N, ...)
    Xobs_tr_f = flat_1d(Xobs_tr); Xcam_tr_f = flat_1d(Xcam_tr)
    Y_tr_f = Y_tr.reshape(S_tr * N_sta, -1); Msk_tr_f = mask_tr.reshape(S_tr * N_sta, -1).float()
    Xobs_te_f = flat_1d(Xobs_te); Xcam_te_f = flat_1d(Xcam_te); Y_te_f = Y_te.reshape(S_te * N_sta, -1)
    H_tr_f = flat_time(data['H_train'], N_sta); D_tr_f = flat_time(data['D_train'], N_sta); M_tr_f = flat_time(data['M_train'], N_sta)
    H_te_f = flat_time(data['H_test'],  N_sta); D_te_f = flat_time(data['D_test'],  N_sta); M_te_f = flat_time(data['M_test'],  N_sta)
    aux_tr = coords.unsqueeze(0).expand(S_tr, -1, -1).reshape(-1, 2)
    aux_te = coords.unsqueeze(0).expand(S_te, -1, -1).reshape(-1, 2)
    idx_te = torch.arange(N_sta).unsqueeze(0).expand(S_te, -1).reshape(-1)

    # ★[早停版] 驗證集攤平（與訓練同方式）
    Xobs_val = data['Xobs_val']; Xcam_val = data['Xcam_val']; Y_val = data['Y_val']; mask_val = data['mask_val']
    S_val = Xobs_val.shape[0]
    Xobs_val_f = flat_1d(Xobs_val); Xcam_val_f = flat_1d(Xcam_val)
    Y_val_f = Y_val.reshape(S_val * N_sta, -1); Msk_val_f = mask_val.reshape(S_val * N_sta, -1).float()
    H_val_f = flat_time(data['H_val'], N_sta); D_val_f = flat_time(data['D_val'], N_sta); M_val_f = flat_time(data['M_val'], N_sta)
    aux_val = coords.unsqueeze(0).expand(S_val, -1, -1).reshape(-1, 2)

    print(f"訓練樣本: {len(Xobs_tr_f):,} ({S_tr}×{N_sta}) | 驗證樣本: {len(Xobs_val_f):,} ({S_val}×{N_sta}) | 測試樣本: {len(Xobs_te_f):,} ({S_te}×{N_sta})")

    # ★[早停版] 切時間序驗證集(最後10%)、以驗證 loss 早停(patience=100)、SEED=42；與 FNO 早停版同協定
    train_loader = DataLoader(TensorDataset(Xobs_tr_f, Xcam_tr_f, aux_tr,
                                            H_tr_f, D_tr_f, M_tr_f, Y_tr_f, Msk_tr_f),
                              batch_size=256, shuffle=True)
    val_loader   = DataLoader(TensorDataset(Xobs_val_f, Xcam_val_f, aux_val,
                                            H_val_f, D_val_f, M_val_f, Y_val_f, Msk_val_f),
                              batch_size=256, shuffle=False)
    test_loader  = DataLoader(TensorDataset(Xobs_te_f, Xcam_te_f, aux_te,
                                            H_te_f, D_te_f, M_te_f, Y_te_f, idx_te),
                              batch_size=256, shuffle=False)

    model = CNN_CAMS_TIME(T_obs=INPUT_HOURS, T_cams=OUTPUT_HOURS, t_total=T_TOTAL,
                          n_aux=2, out_hours=OUTPUT_HOURS, time_embed_dim=TIME_EMBED_DIM).to(device)
    print(f"\n模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)   # ★ 與 camshead FNO 一致
    sch = torch.optim.lr_scheduler.StepLR(opt, step_size=400, gamma=0.5)
    MAX_EPOCHS = 800
    BEST_PATH = "best_cnn_cams_timeembed_nearest_earlystop.pth"; CKPT_PATH = "cnn_cams_timeembed_nearest_earlystop_checkpoint.pth"

    # ★ Early stopping 狀態：以「驗證 loss」為準（測試集只做最終評估）
    best_val = float('inf'); best_ep = 0; epochs_no_improve = 0
    print(f"\n=== 開始訓練 (★early stopping,patience={PATIENCE},上限{MAX_EPOCHS},存 val loss 最佳,遮罩 Huber) ===")
    stopped = False
    for ep in range(1, MAX_EPOCHS + 1):
        # --- 訓練 ---
        model.train(); loss_sum = 0.0
        for xo, xc, aux, hh, dd, mm, y, msk in train_loader:
            xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
            hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
            y, msk = y.to(device), msk.to(device)
            opt.zero_grad()
            pred = model(xo, xc, aux, hh, dd, mm)
            loss = masked_huber(pred, y, msk)                    # ★ 遮罩 Huber
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            loss_sum += loss.item()
        sch.step()
        avg = loss_sum / len(train_loader)

        # --- 驗證（同一個 masked_huber；NaN 目標用遮罩排除）---
        model.eval(); val_sum = 0.0
        with torch.no_grad():
            for xo, xc, aux, hh, dd, mm, y, msk in val_loader:
                xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
                hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
                y, msk = y.to(device), msk.to(device)
                pred = model(xo, xc, aux, hh, dd, mm)
                val_sum += masked_huber(pred, y, msk).item()
        val_avg = val_sum / len(val_loader)

        # --- Early stopping：只有「驗證 loss 創新低」才存最佳 checkpoint ---
        if val_avg < best_val:
            best_val = val_avg; best_ep = ep; epochs_no_improve = 0
            torch.save(model.state_dict(), BEST_PATH)
        else:
            epochs_no_improve += 1

        if ep % 10 == 0 or ep == 1:
            print(f"Epoch {ep:4d} | Train: {avg:.6f} | Val: {val_avg:.6f} | "
                  f"BestVal: {best_val:.6f}@{best_ep} | noImp: {epochs_no_improve}/{PATIENCE} | LR: {opt.param_groups[0]['lr']:.6f}")

        if epochs_no_improve >= PATIENCE:
            print(f"\n★ Early stopping：驗證 loss 已連續 {PATIENCE} 個 epoch 未改善，於 epoch {ep} 停止。")
            stopped = True
            break

    if not stopped:
        print(f"\n達到 epoch 上限 {MAX_EPOCHS}（未觸發早停）。")
    print(f"★ 最佳(驗證)模型：epoch {best_ep}，val loss = {best_val:.6f}")
    print(f"\n載入最佳(驗證)模型: {BEST_PATH}")
    model.load_state_dict(torch.load(BEST_PATH))
    torch.save({
        'model_state': model.state_dict(),
        'stats': {k: v for k, v in data['stats'].items()},
        'station_names': data['station_names'],
        'config': {'INPUT_HOURS': INPUT_HOURS, 'OUTPUT_HOURS': OUTPUT_HOURS, 'USE_WEATHER': False,
                   'TIME_EMBED_DIM': TIME_EMBED_DIM,
                   'arch': 'obs16x2 + cams32x3 + timeEmb(48x96) + aux2 -> fc108/72/72'},
    }, CKPT_PATH)
    print(f"已儲存完整 checkpoint: {CKPT_PATH}")

    # ── 測試集評估（per-station 反標準化 + 中央論文 per-hour pooled）──
    print("\n=== 測試集評估（expm1 還原 + 中央論文 per-hour pooled）===")
    model.eval()
    ym_d = y_mean.to(device); ys_d = y_std.to(device)
    preds, tgts, idxs = [], [], []
    with torch.no_grad():
        for xo, xc, aux, hh, dd, mm, y, idx in test_loader:
            xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
            hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
            pred = model(xo, xc, aux, hh, dd, mm)
            preds.append(torch.expm1((pred * ys_d + ym_d).clamp(min=0)).cpu()); tgts.append(y); idxs.append(idx)
    P_all = torch.cat(preds).numpy(); T_all = torch.cat(tgts).numpy(); I_all = torch.cat(idxs).numpy()

    P_3d = np.full((S_te, N_sta, 72), np.nan, np.float32)
    T_3d = np.full((S_te, N_sta, 72), np.nan, np.float32)
    seq_of = np.arange(len(I_all)) // N_sta
    for k in range(len(I_all)):
        P_3d[seq_of[k], I_all[k], :] = P_all[k]
        T_3d[seq_of[k], I_all[k], :] = T_all[k]

    # ★ 輸出統一寬表 CSV（供 compare_models.py 同基準比較；t0/測站對齊）
    C_3d = torch.expm1((data['Xcam_test'] * y_std + y_mean).clamp(min=0)).numpy()   # [S,N,72] µg/m³ (expm1 還原)

    # ★ 存預測結果 CSV(依 valid_time 時間排序)
    save_predictions_csv("pred_cnn_timeembed_nearest_earlystop.csv", P_3d, T_3d, C_3d,
                         data['t0_test'], data['station_names'])

    try:
        from compare_models import dump_wide_csv
        if len(data['t0_test']) == S_te:
            dump_wide_csv("pred_cnn_timeembed_nearest_earlystop_wide.csv", P_3d, T_3d, C_3d, data['t0_test'], data['station_names'])
    except Exception as e:
        print("dump 略過：", e)

    def paper_metric(P, T, metric='rmse', seg=None):
        s, e = (0, 72) if seg is None else seg
        vals = []
        for h in range(s, e):
            m = ~np.isnan(T[:, :, h]) & ~np.isnan(P[:, :, h])
            if m.sum() < 2:
                continue
            d = P[:, :, h][m] - T[:, :, h][m]
            vals.append(np.sqrt(np.mean(d**2)) if metric == 'rmse' else np.mean(np.abs(d)))
        return float(np.mean(vals)) if vals else float('nan')

    print(f"\n有效評估點：{(~np.isnan(T_3d)).sum():,}，測站數：{N_sta}")
    print("=" * 55)
    print("  CNN + CAMS + 時間embedding（無氣象）72h（中央論文 per-hour pooled）")
    print(f"  RMSE: {paper_metric(P_3d, T_3d, 'rmse'):.4f}  MAE: {paper_metric(P_3d, T_3d, 'mae'):.4f}")
    print("=" * 55)
    for s, e, lb in [(0, 24, 'Day1'), (24, 48, 'Day2'), (48, 72, 'Day3')]:
        print(f"  {lb}  RMSE={paper_metric(P_3d, T_3d, 'rmse', (s, e)):.4f}  "
              f"MAE={paper_metric(P_3d, T_3d, 'mae', (s, e)):.4f}")
