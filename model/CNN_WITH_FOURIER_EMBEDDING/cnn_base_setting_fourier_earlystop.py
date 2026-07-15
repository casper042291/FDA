# 有效評估點：1,684,069，測站數：72
# =======================================================
#   CNN + CAMS + 時間embedding（無氣象）72h ★中央設定（CNN 最佳）★
#   RMSE: 7.1909  MAE: 5.1819
# =======================================================
#   Day1  RMSE=6.3770  MAE=4.5619
#   Day2  RMSE=7.4564  MAE=5.3869
#   Day3  RMSE=7.7392  MAE=5.5971

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
#   ※本檔為「中央協定」版：其餘設定(隨機切驗證/MSE/min-max/Adam/patience=10)全部原樣保留，只加種子。
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

fpca_pm25_file = "PM2.5.csv"
raw_pm25_file  = "merged_reshaped_PM2.5.csv"
station_file   = "測站經緯度 72.csv"
cams_npz_file  = "CAMS_PM25_perinit_nearest.npz"


# ==============================================================================
# 1a-0. 座標 random Fourier 編碼（★與 FNO GaussianFourierFeatureTransform 完全相同）
# ==============================================================================
class GaussianFourierFeatureTransform(nn.Module):
    def __init__(self, mapping_size=64, scale=10):
        super().__init__()
        self.register_buffer('B', torch.randn((2, mapping_size)) * scale)
    def forward(self, x):
        x_proj = (2. * np.pi * x) @ self.B
        return torch.cat([torch.sin(x_proj), torch.cos(x_proj)], dim=-1)


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
# 4. 資料載入 / 切分（★無氣象;★中央論文式 min-max 正規化）
# ==============================================================================
def _align(df, full_idx, common):
    df = df.drop_duplicates('datetime').set_index('datetime')[common].reindex(full_idx)
    for c in common:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.reset_index().rename(columns={'index': 'datetime'})


def load_and_split_data():
    print("--- [Step 1] 讀取數據（觀測PM2.5 + CAMS預報；★無氣象 + 時間embedding + 中央 min-max）---")
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
    # ★ 訓練目標 = 原始觀測 (Yraw)
    Xobs_tr, Xcam_tr, _, Yraw_tr, H_tr, D_tr, M_tr, _ = create_sequences(
        tr_pm, tr_raw, cams_anc, cams_lookup, common, INPUT_HOURS, OUTPUT_HOURS, STRIDE)
    Xobs_tr = torch.tensor(Xobs_tr); Xcam_tr = torch.tensor(Xcam_tr); Yraw_tr = torch.tensor(Yraw_tr)
    H_tr = torch.tensor(H_tr); D_tr = torch.tensor(D_tr); M_tr = torch.tensor(M_tr)

    # ★ 中央論文式 min-max [0,1] 正規化(全域逐特徵;統計★只用訓練集★)
    pm_min = Xobs_tr.min(); pm_max = Xobs_tr.max(); pm_rng = (pm_max - pm_min).clamp(min=1e-8)
    cam_min = Xcam_tr.min(); cam_max = Xcam_tr.max(); cam_rng = (cam_max - cam_min).clamp(min=1e-8)

    Xobs_tr_n = (Xobs_tr - pm_min) / pm_rng
    Xcam_tr_n = (Xcam_tr - cam_min) / cam_rng
    # ★ 目標=原始觀測,以 PM2.5 obs 的 min-max 縮放;NaN 保留→以 mask 跳過
    Y_tr_n   = (Yraw_tr - pm_min) / pm_rng
    mask_tr  = ~torch.isnan(Yraw_tr)

    # ── 測試集 ──
    te_pm, te_raw = slice_period(split_date, test_end)
    Xobs_te, Xcam_te, _, Yraw_te, H_te, D_te, M_te, t0_te = create_sequences(
        te_pm, te_raw, cams_anc, cams_lookup, common, INPUT_HOURS, OUTPUT_HOURS, STRIDE)
    if Xobs_te is not None and len(Xobs_te) > 0:
        Xobs_te_n = (torch.tensor(Xobs_te) - pm_min) / pm_rng
        Xcam_te_n = (torch.tensor(Xcam_te) - cam_min) / cam_rng
        Y_te_eval = torch.tensor(Yraw_te)
        H_te = torch.tensor(H_te); D_te = torch.tensor(D_te); M_te = torch.tensor(M_te)
    else:
        Xobs_te_n = Xcam_te_n = Y_te_eval = torch.tensor([])
        H_te = D_te = M_te = torch.tensor([], dtype=torch.long)

    print(f"訓練集: {len(Xobs_tr_n)} | 測試集: {len(Xobs_te_n)} | ★無氣象 + 時間embedding + min-max + 原始觀測目標")
    return {
        'Xobs_train': Xobs_tr_n, 'Xcam_train': Xcam_tr_n, 'Y_train': Y_tr_n, 'mask_train': mask_tr,
        'H_train': H_tr, 'D_train': D_tr, 'M_train': M_tr,
        'Xobs_test':  Xobs_te_n, 'Xcam_test':  Xcam_te_n, 'Y_test_raw': Y_te_eval,
        'H_test': H_te, 'D_test': D_te, 'M_test': M_te,
        'coords': torch.tensor(coords), 'station_names': common,
        't0_test': (t0_te if t0_te is not None else []),
        'stats': {'pm_min': pm_min, 'pm_max': pm_max, 'cam_min': cam_min, 'cam_max': cam_max},
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


def masked_se_sum(pred, target, mask):
    """★ 遮罩 MSE 用:回傳 (平方誤差總和, 有效點數);NaN 以 mask 跳過。"""
    t = torch.nan_to_num(target, nan=0.0)
    se = ((pred - t) ** 2) * mask
    return se.sum(), mask.sum()


# ==============================================================================
# 5. 主程式（★中央設定:Adam + 10% 驗證 + early stop + 存 val 最佳）
# ==============================================================================
if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        raise SystemExit(f"找不到檔案: {fpca_pm25_file}")

    data = load_and_split_data()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"\nDevice: {device} | ★中央設定:min-max + MSE + early stop（CNN 最佳,主結果）")

    Xobs_tr = data['Xobs_train']; Xcam_tr = data['Xcam_train']; Y_tr = data['Y_train']
    mask_tr = data['mask_train']
    Xobs_te = data['Xobs_test'];  Xcam_te = data['Xcam_test'];  Y_te = data['Y_test_raw']
    coords = data['coords']
    pm_min = data['stats']['pm_min']; pm_max = data['stats']['pm_max']; pm_rng = (pm_max - pm_min).clamp(min=1e-8)
    cam_min = data['stats']['cam_min']; cam_max = data['stats']['cam_max']
    N_sta = Xobs_tr.shape[1]; S_tr, S_te = Xobs_tr.shape[0], Xobs_te.shape[0]

    # 攤平成 (S*N, ...)
    Xobs_tr_f = flat_1d(Xobs_tr); Xcam_tr_f = flat_1d(Xcam_tr)
    Y_tr_f = Y_tr.reshape(S_tr * N_sta, -1); Msk_tr_f = mask_tr.reshape(S_tr * N_sta, -1).float()
    Xobs_te_f = flat_1d(Xobs_te); Xcam_te_f = flat_1d(Xcam_te); Y_te_f = Y_te.reshape(S_te * N_sta, -1)
    H_tr_f = flat_time(data['H_train'], N_sta); D_tr_f = flat_time(data['D_train'], N_sta); M_tr_f = flat_time(data['M_train'], N_sta)
    H_te_f = flat_time(data['H_test'],  N_sta); D_te_f = flat_time(data['D_test'],  N_sta); M_te_f = flat_time(data['M_test'],  N_sta)
    # ★ 座標 random Fourier 編碼（與 FNO 內相同：mapping_size=32, scale=10 -> 64 維）
    coord_gff = GaussianFourierFeatureTransform(mapping_size=32, scale=10)
    coords_ff = coord_gff(coords)                                    # [N, 64]
    aux_tr = coords_ff.unsqueeze(0).expand(S_tr, -1, -1).reshape(-1, 64)
    aux_te = coords_ff.unsqueeze(0).expand(S_te, -1, -1).reshape(-1, 64)
    idx_te = torch.arange(N_sta).unsqueeze(0).expand(S_te, -1).reshape(-1)

    print(f"訓練樣本: {len(Xobs_tr_f):,} ({S_tr}×{N_sta}) | 測試樣本: {len(Xobs_te_f):,} ({S_te}×{N_sta})")

    # ★ 中央設定:切 10% 驗證（seed=42）→ 供 early stop / 選最佳模型
    n_train = len(Xobs_tr_f); n_val = max(1, int(n_train * 0.1))
    perm = torch.randperm(n_train, generator=torch.Generator().manual_seed(42))
    val_idx, tr_idx = perm[:n_val], perm[n_val:]

    def make_loader(idx, shuffle):
        return DataLoader(TensorDataset(Xobs_tr_f[idx], Xcam_tr_f[idx], aux_tr[idx],
                                        H_tr_f[idx], D_tr_f[idx], M_tr_f[idx],
                                        Y_tr_f[idx], Msk_tr_f[idx]),
                          batch_size=256, shuffle=shuffle)
    train_loader = make_loader(tr_idx, True)
    val_loader   = make_loader(val_idx, False)
    test_loader  = DataLoader(TensorDataset(Xobs_te_f, Xcam_te_f, aux_te,
                                            H_te_f, D_te_f, M_te_f, Y_te_f, idx_te),
                              batch_size=256, shuffle=False)

    model = CNN_CAMS_TIME(T_obs=INPUT_HOURS, T_cams=OUTPUT_HOURS, t_total=T_TOTAL,
                          n_aux=64, out_hours=OUTPUT_HOURS, time_embed_dim=TIME_EMBED_DIM).to(device)
    print(f"\n模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.Adam(model.parameters(), lr=1e-3)            # ★ 中央設定:Adam(無 StepLR)
    MAX_EPOCHS = 300; PATIENCE = 10                                # ★ 中央設定:early stop patience=10
    BEST_PATH = "best_cnn_cams_timeembed_central_fourier.pth"; CKPT_PATH = "cnn_cams_timeembed_central_fourier_checkpoint.pth"

    best_val = float('inf'); no_improve = 0
    print(f"\n=== 開始訓練 (中央設定:遮罩 MSE,early stopping patience={PATIENCE}) ===")
    for ep in range(1, MAX_EPOCHS + 1):
        model.train(); tr_sse = 0.0; tr_cnt = 0.0
        for xo, xc, aux, hh, dd, mm, y, msk in train_loader:
            xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
            hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
            y, msk = y.to(device), msk.to(device)
            opt.zero_grad()
            pred = model(xo, xc, aux, hh, dd, mm)
            sse, cnt = masked_se_sum(pred, y, msk)               # ★ 遮罩 MSE
            loss = sse / cnt.clamp(min=1.0); loss.backward(); opt.step()
            tr_sse += sse.item(); tr_cnt += cnt.item()
        tr_loss = tr_sse / max(tr_cnt, 1.0)

        model.eval(); v_sse = 0.0; v_cnt = 0.0
        with torch.no_grad():
            for xo, xc, aux, hh, dd, mm, y, msk in val_loader:
                xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
                hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
                y, msk = y.to(device), msk.to(device)
                sse, cnt = masked_se_sum(model(xo, xc, aux, hh, dd, mm), y, msk)
                v_sse += sse.item(); v_cnt += cnt.item()
        val_loss = v_sse / max(v_cnt, 1.0)

        if val_loss < best_val - 1e-6:
            best_val = val_loss; no_improve = 0
            torch.save(model.state_dict(), BEST_PATH)
        else:
            no_improve += 1
        if ep % 5 == 0 or ep == 1 or no_improve == 0:
            print(f"Epoch {ep:3d} | Train {tr_loss:.6f} | Val {val_loss:.6f} | Best {best_val:.6f} | no_improve {no_improve}")
        if no_improve >= PATIENCE:
            print(f"\nEarly stopping at epoch {ep}. Best val: {best_val:.6f}")
            break

    print(f"\n載入最佳模型: {BEST_PATH}")
    model.load_state_dict(torch.load(BEST_PATH))
    torch.save({
        'model_state': model.state_dict(),
        'stats': {k: v for k, v in data['stats'].items()},
        'station_names': data['station_names'],
        'config': {'INPUT_HOURS': INPUT_HOURS, 'OUTPUT_HOURS': OUTPUT_HOURS, 'USE_WEATHER': False,
                   'TIME_EMBED_DIM': TIME_EMBED_DIM, 'protocol': 'central(min-max+MSE+earlystop)',
                   'arch': 'obs16x2 + cams32x3 + timeEmb(48x96) + aux2 -> fc108/72/72'},
    }, CKPT_PATH)
    print(f"已儲存完整 checkpoint: {CKPT_PATH}")

    # ── 測試集評估（min-max 反正規化 + 中央論文 per-hour pooled）──
    print("\n=== 測試集評估（min-max 反正規化 + 中央論文 per-hour pooled）===")
    model.eval()
    rng_d = pm_rng.to(device); lo_d = pm_min.to(device)
    preds, tgts, idxs = [], [], []
    with torch.no_grad():
        for xo, xc, aux, hh, dd, mm, y, idx in test_loader:
            xo, xc, aux = xo.to(device), xc.to(device), aux.to(device)
            hh, dd, mm = hh.to(device), dd.to(device), mm.to(device)
            pred = model(xo, xc, aux, hh, dd, mm)
            preds.append((pred * rng_d + lo_d).cpu()); tgts.append(y); idxs.append(idx)
    P_all = torch.cat(preds).numpy(); T_all = torch.cat(tgts).numpy(); I_all = torch.cat(idxs).numpy()

    P_3d = np.full((S_te, N_sta, 72), np.nan, np.float32)
    T_3d = np.full((S_te, N_sta, 72), np.nan, np.float32)
    seq_of = np.arange(len(I_all)) // N_sta
    for k in range(len(I_all)):
        P_3d[seq_of[k], I_all[k], :] = P_all[k]
        T_3d[seq_of[k], I_all[k], :] = T_all[k]

    # ★ 輸出統一寬表 CSV（供 compare_models.py 同基準比較；t0/測站對齊）
    C_3d = (data['Xcam_test'] * (cam_max - cam_min) + cam_min).numpy()   # [S,N,72] µg/m³ (min-max 反轉)

    # ★ 存預測結果 CSV(依 valid_time 時間排序)
    save_predictions_csv("pred_cnn_timeembed_central_fourier.csv", P_3d, T_3d, C_3d,
                         data['t0_test'], data['station_names'])

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
    print("  CNN + CAMS + 時間embedding（無氣象）72h ★中央設定（CNN 最佳）★")
    print(f"  RMSE: {paper_metric(P_3d, T_3d, 'rmse'):.4f}  MAE: {paper_metric(P_3d, T_3d, 'mae'):.4f}")
    print("=" * 55)
    for s, e, lb in [(0, 24, 'Day1'), (24, 48, 'Day2'), (48, 72, 'Day3')]:
        print(f"  {lb}  RMSE={paper_metric(P_3d, T_3d, 'rmse', (s, e)):.4f}  "
              f"MAE={paper_metric(P_3d, T_3d, 'mae', (s, e)):.4f}")
