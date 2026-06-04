# ==============================================================================
# CNN-BASE 風格 baseline（無 CMAQ 輸入）
#
# 架構參考：Lee et al. (2024) Atmospheric Environment, CNN-BASE
#   - PM2.5 路徑：1D-Conv (16 filters, kernel 3) × 2
#   - 氣象路徑：1D-Conv (32 filters, kernel 3) × 3   ← 取代原論文 CMAQ 路徑
#   - Auxiliary：lat / lon
#   - FC: 108 → 72 → 72 → 72 (每層 ReLU)
#   - Loss: MSE，早停 patience=10
#
# 與原論文差異：
#   1. 沒有 CMAQ 預測值（user 只有歷史觀測資料）
#   2. 沒有 SWP 天氣型態 / land use 索引
#   3. 輸入長度 24h（與 FNO v7 和中央論文一致）
#   4. 直接輸出 72h（不滾動）
# ==============================================================================

import os
import sys
import warnings
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset
from sklearn.metrics import r2_score

warnings.filterwarnings('ignore')


# ==============================================================================
# 0. 設定
# ==============================================================================
INPUT_HOURS  = 24    # 與 FNO v7 和中央論文一致
OUTPUT_HOURS = 72
STRIDE       = 24

fpca_pm25_file = "/home/casper/air/fda_class/FPCA_PM25_format_all.csv"
raw_pm25_file  = "/home/casper/air/fda_class/merged_reshaped_PM2.5.csv"
station_file   = "/home/casper/air/空間/測站經緯度.csv"

weather_files = {
    'u_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Uall.csv",
    'v_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Vall.csv",
    'rh':     "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_RHall.csv",
    'temp':   "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_tempall.csv",
}


# ==============================================================================
# 1. 模型
# ==============================================================================

class CNN_Baseline(nn.Module):
    """1D CNN baseline，仿 Lee et al. (2024) CNN-BASE 架構。沒有 CMAQ 輸入。"""
    def __init__(self, T_in=24, n_pm25=1, n_wx=8, n_aux=2, out_hours=72):
        super().__init__()
        self.conv_pm25 = nn.Sequential(
            nn.Conv1d(n_pm25, 16, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv1d(16, 16, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        self.conv_wx = nn.Sequential(
            nn.Conv1d(n_wx, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv1d(32, 32, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv1d(32, 32, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        flat_dim = 16 * T_in + 32 * T_in + n_aux
        self.fc = nn.Sequential(
            nn.Linear(flat_dim, 108),
            nn.ReLU(),
            nn.Linear(108, 72),
            nn.ReLU(),
            nn.Linear(72, 72),
            nn.ReLU(),
            nn.Linear(72, out_hours),
        )

    def forward(self, x_pm25, x_wx, aux):
        h_pm25 = self.conv_pm25(x_pm25).flatten(start_dim=1)
        h_wx   = self.conv_wx(x_wx).flatten(start_dim=1)
        h      = torch.cat([h_pm25, h_wx, aux], dim=1)
        return self.fc(h)


# ==============================================================================
# 2. 資料處理
# ==============================================================================

def make_temporal_features(timestamps):
    """從 datetime array 產生 4 個時間語境 channel: hour_sin/cos, doy_sin/cos"""
    hours = np.array([ts.hour       for ts in timestamps], dtype=np.float32)
    doys  = np.array([ts.dayofyear  for ts in timestamps], dtype=np.float32)
    return np.stack([
        np.sin(2 * np.pi * hours / 24),
        np.cos(2 * np.pi * hours / 24),
        np.sin(2 * np.pi * doys  / 365),
        np.cos(2 * np.pi * doys  / 365),
    ], axis=-1)


def create_sequences_3d(data_dict, station_names,
                        input_hours=24, output_hours=72, stride=24,
                        raw_pm25_df=None):
    ordered_keys = ['pm25'] + [k for k in data_dict if k != 'pm25']
    df_pm25 = data_dict['pm25']
    n_stations = len(station_names)
    n_rows = len(df_pm25)
    window = input_hours + output_hours
    V = len(ordered_keys)

    n_seq = (n_rows - window) // stride + 1
    if n_seq <= 0:
        return None, None, None, None

    X     = np.zeros((n_seq, n_stations, input_hours, V),     dtype=np.float32)
    Y     = np.zeros((n_seq, n_stations, output_hours),        dtype=np.float32)

    has_raw = (raw_pm25_df is not None)
    Y_raw   = np.full((n_seq, n_stations, output_hours), np.nan, dtype=np.float32) \
              if has_raw else None

    st_map = {name: i for i, name in enumerate(station_names)}
    actual = 0

    for seq_idx in range(n_seq):
        start    = seq_idx * stride
        end_obs  = start + input_hours
        end_pred = end_obs + output_hours
        if end_pred > n_rows:
            break

        for st_name, st_idx in st_map.items():
            if st_name not in df_pm25.columns:
                continue
            for v_idx, key in enumerate(ordered_keys):
                df = data_dict[key]
                if st_name in df.columns:
                    X[seq_idx, st_idx, :, v_idx] = df[st_name].iloc[start:end_obs].values
            Y[seq_idx, st_idx, :] = df_pm25[st_name].iloc[end_obs:end_pred].values

            if has_raw and st_name in raw_pm25_df.columns:
                raw_win = raw_pm25_df[st_name].iloc[end_obs:end_pred].values
                if len(raw_win) == output_hours:
                    Y_raw[seq_idx, st_idx, :] = raw_win

        actual = seq_idx + 1

    X = X[:actual]; Y = Y[:actual]
    if has_raw:
        Y_raw = Y_raw[:actual]
    return X, Y, Y_raw, ordered_keys


def load_and_split_data():
    print("--- [Step 1] 讀取數據與標準化 ---")

    df_stations = pd.read_csv(station_file)
    data_frames = {'pm25': pd.read_csv(fpca_pm25_file)}
    for name, path in weather_files.items():
        if os.path.exists(path):
            data_frames[name] = pd.read_csv(path)

    for name, df in data_frames.items():
        t_col = next((c for c in ['PublishTime','date','time','datetime']
                      if c in df.columns), None)
        df.rename(columns={t_col: 'datetime'}, inplace=True)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.sort_values('datetime', inplace=True)
        data_frames[name] = df

    common_stations = set(df_stations['SiteName']) & set(data_frames['pm25'].columns)
    for df in data_frames.values():
        common_stations &= set(df.columns)
    common_stations = sorted(common_stations)
    print(f"共同測站數量: {len(common_stations)}")
    if not common_stations:
        sys.exit("錯誤：找不到共同測站")

    df_st = (df_stations[df_stations['SiteName'].isin(common_stations)]
             .set_index('SiteName').reindex(common_stations).reset_index())
    lons   = df_st['TWD97Lon'].values
    lats   = df_st['TWD97Lat'].values
    x_norm = (lons - lons.min()) / (lons.max() - lons.min() + 1e-8)
    y_norm = (lats - lats.min()) / (lats.max() - lats.min() + 1e-8)
    coords = np.stack([x_norm, y_norm], axis=1).astype(np.float32)

    full_idx = pd.date_range(
        start=data_frames['pm25']['datetime'].min(),
        end  =data_frames['pm25']['datetime'].max(), freq='H')

    aligned_dfs = {}
    for name, df in data_frames.items():
        df = df.drop_duplicates('datetime').set_index('datetime')
        df = df[common_stations].reindex(full_idx)
        for c in common_stations:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        aligned_dfs[name] = df.reset_index().rename(columns={'index': 'datetime'})

    # 時間特徵 channel（hour_sin/cos, doy_sin/cos）─ 廣播到每個測站
    temporal_feat = make_temporal_features(full_idx)
    for i, col in enumerate(['hour_sin', 'hour_cos', 'doy_sin', 'doy_cos']):
        df_temp = pd.DataFrame(index=full_idx)
        for st in common_stations:
            df_temp[st] = temporal_feat[:, i]
        df_temp = df_temp.reset_index().rename(columns={'index': 'datetime'})
        aligned_dfs[col] = df_temp

    # 原始 PM2.5（評估用）
    raw_pm25_aligned = None
    if os.path.exists(raw_pm25_file):
        print(f"載入原始 PM2.5：{raw_pm25_file}")
        df_raw = pd.read_csv(raw_pm25_file)
        t_col  = next((c for c in ['PublishTime','date','time','datetime']
                       if c in df_raw.columns), None)
        df_raw.rename(columns={t_col: 'datetime'}, inplace=True)
        df_raw['datetime'] = pd.to_datetime(df_raw['datetime'])
        df_raw = df_raw.drop_duplicates('datetime').set_index('datetime')
        raw_cols = [c for c in common_stations if c in df_raw.columns]
        df_raw   = df_raw[raw_cols].reindex(full_idx)
        for c in raw_cols:
            df_raw[c] = pd.to_numeric(df_raw[c], errors='coerce')
        for c in common_stations:
            if c not in df_raw.columns:
                df_raw[c] = np.nan
        raw_pm25_aligned = df_raw[common_stations]
        print(f"  測站覆蓋率：{len(raw_cols)}/{len(common_stations)}")
    else:
        print(f"警告：找不到原始 PM2.5 {raw_pm25_file}")

    split_date = pd.Timestamp('2025-01-01')
    test_end   = pd.Timestamp('2025-11-30')

    # 訓練集
    train_dfs = {n: df[df['datetime'] < split_date].copy()
                 for n, df in aligned_dfs.items()}
    X_tr, Y_tr, _, ordered_keys = create_sequences_3d(
        train_dfs, common_stations, INPUT_HOURS, OUTPUT_HOURS, STRIDE,
        raw_pm25_df=None)
    X_tr = torch.tensor(X_tr); Y_tr = torch.tensor(Y_tr)

    # 分站標準化
    #   X_tr  : (S, N, T_in, V)  → x_mean/x_std : (1, N, 1, V)
    #   Y_tr  : (S, N, 72)        → y_mean/y_std : (1, N, 1)
    x_mean = X_tr.mean(dim=(0, 2), keepdim=True)
    x_std  = X_tr.std( dim=(0, 2), keepdim=True) + 1e-6
    y_mean = Y_tr.mean(dim=(0, 2), keepdim=True)
    y_std  = Y_tr.std( dim=(0, 2), keepdim=True) + 1e-6

    X_tr_norm = (X_tr - x_mean) / x_std
    Y_tr_norm = (Y_tr - y_mean) / y_std

    # 測試集
    test_dfs = {n: df[(df['datetime'] >= split_date) &
                      (df['datetime'] < test_end)].copy()
                for n, df in aligned_dfs.items()}
    raw_pm25_test = None
    if raw_pm25_aligned is not None:
        raw_pm25_test = raw_pm25_aligned[
            (raw_pm25_aligned.index >= split_date) &
            (raw_pm25_aligned.index <  test_end)
        ].copy()

    X_te, Y_te, Y_te_raw, _ = create_sequences_3d(
        test_dfs, common_stations, INPUT_HOURS, OUTPUT_HOURS, STRIDE,
        raw_pm25_df=raw_pm25_test)

    if X_te is not None:
        X_te_norm = (torch.tensor(X_te) - x_mean) / x_std
        Y_te_eval = torch.tensor(Y_te_raw) if Y_te_raw is not None else torch.tensor(Y_te)
    else:
        X_te_norm = Y_te_eval = torch.tensor([])

    n_vars = X_tr.shape[-1]
    print(f"訓練集: {len(X_tr_norm)} | 測試集: {len(X_te_norm)}")
    print(f"輸入窗口: {INPUT_HOURS}h | channels: {n_vars} ({ordered_keys})")

    return {
        'X_train':       X_tr_norm,
        'Y_train':       Y_tr_norm,
        'X_test':        X_te_norm,
        'Y_test_raw':    Y_te_eval,
        'coords':        torch.tensor(coords, dtype=torch.float32),
        'station_names': common_stations,
        'n_vars':        n_vars,
        'T_in':          X_tr_norm.shape[2],
        'stats':         {'y_mean': y_mean, 'y_std': y_std},
    }


# ==============================================================================
# 3. Utilities
# ==============================================================================

def to_flat(X, Y):
    """(S, N, T_in, V) → (S*N, V, T_in);  (S, N, 72) → (S*N, 72)"""
    S, N, T, V = X.shape
    X = X.permute(0, 1, 3, 2).reshape(S * N, V, T)
    Y = Y.reshape(S * N, -1)
    return X, Y


# ==============================================================================
# 4. 主程式
# ==============================================================================

if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        raise SystemExit(f"找不到檔案: {fpca_pm25_file}")

    data   = load_and_split_data()
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"\nDevice: {device}")

    X_tr     = data['X_train']
    Y_tr     = data['Y_train']
    X_te     = data['X_test']
    Y_te_raw = data['Y_test_raw']
    coords   = data['coords']
    y_mean   = data['stats']['y_mean']
    y_std    = data['stats']['y_std']

    T_IN    = data['T_in']
    N_sta   = X_tr.shape[1]
    V_total = X_tr.shape[-1]
    N_WX    = V_total - 1                 # PM2.5 以外的全部 channel

    X_tr_flat, Y_tr_flat = to_flat(X_tr, Y_tr)
    X_te_flat, Y_te_flat = to_flat(X_te, Y_te_raw)

    S_tr = X_tr.shape[0]
    S_te = X_te.shape[0]

    aux_tr = coords.unsqueeze(0).expand(S_tr, -1, -1).reshape(-1, 2)
    aux_te = coords.unsqueeze(0).expand(S_te, -1, -1).reshape(-1, 2)

    idx_te = torch.arange(N_sta).unsqueeze(0).expand(S_te, -1).reshape(-1)

    print(f"訓練樣本: {len(X_tr_flat):,} ({S_tr} sequences × {N_sta} stations)")
    print(f"測試樣本: {len(X_te_flat):,} ({S_te} sequences × {N_sta} stations)")
    print(f"T_in: {T_IN}h | PM2.5 channels: 1 | 氣象/時間 channels: {N_WX}")

    # 切 10% 驗證集
    n_train = len(X_tr_flat)
    n_val   = max(1, int(n_train * 0.1))
    perm    = torch.randperm(n_train, generator=torch.Generator().manual_seed(42))
    val_idx = perm[:n_val]
    tr_idx  = perm[n_val:]

    train_loader = DataLoader(
        TensorDataset(X_tr_flat[tr_idx], aux_tr[tr_idx], Y_tr_flat[tr_idx]),
        batch_size=256, shuffle=True)
    val_loader = DataLoader(
        TensorDataset(X_tr_flat[val_idx], aux_tr[val_idx], Y_tr_flat[val_idx]),
        batch_size=256, shuffle=False)
    test_loader = DataLoader(
        TensorDataset(X_te_flat, aux_te, Y_te_flat, idx_te),
        batch_size=256, shuffle=False)

    model = CNN_Baseline(T_in=T_IN, n_pm25=1, n_wx=N_WX, n_aux=2, out_hours=72).to(device)
    print(f"\n模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.Adam(model.parameters(), lr=1e-3)

    MAX_EPOCHS = 300
    PATIENCE   = 10
    BEST_PATH  = "best_cnn_baseline.pth"

    best_val_loss = float('inf')
    no_improve    = 0

    print(f"\n=== 開始訓練 (early stopping patience={PATIENCE}) ===")
    for ep in range(1, MAX_EPOCHS + 1):
        model.train()
        tr_loss_sum = 0.0; n_seen = 0
        for x, aux, y in train_loader:
            x, aux, y = x.to(device), aux.to(device), y.to(device)
            x_pm = x[:, 0:1, :]; x_wx = x[:, 1:, :]

            opt.zero_grad()
            pred = model(x_pm, x_wx, aux)
            loss = F.mse_loss(pred, y)
            loss.backward()
            opt.step()

            tr_loss_sum += loss.item() * x.size(0)
            n_seen      += x.size(0)
        tr_loss = tr_loss_sum / n_seen

        model.eval()
        val_loss_sum = 0.0; n_seen_val = 0
        with torch.no_grad():
            for x, aux, y in val_loader:
                x, aux, y = x.to(device), aux.to(device), y.to(device)
                x_pm = x[:, 0:1, :]; x_wx = x[:, 1:, :]
                pred = model(x_pm, x_wx, aux)
                val_loss_sum += F.mse_loss(pred, y).item() * x.size(0)
                n_seen_val   += x.size(0)
        val_loss = val_loss_sum / n_seen_val

        if val_loss < best_val_loss - 1e-6:
            best_val_loss = val_loss
            no_improve    = 0
            torch.save(model.state_dict(), BEST_PATH)
        else:
            no_improve += 1

        if ep % 5 == 0 or ep == 1 or no_improve == 0:
            print(f"Epoch {ep:3d} | Train: {tr_loss:.6f} | Val: {val_loss:.6f} | "
                  f"Best Val: {best_val_loss:.6f} | no_improve: {no_improve}")

        if no_improve >= PATIENCE:
            print(f"\nEarly stopping at epoch {ep}. Best val loss: {best_val_loss:.6f}")
            break

    print(f"\n載入最佳模型: {BEST_PATH}")
    model.load_state_dict(torch.load(BEST_PATH))

    # ──────────────────────────────────────────────────────────────────────────
    # 5. 測試集推論 + 反標準化
    # ──────────────────────────────────────────────────────────────────────────
    print("\n=== 測試集評估 ===")
    model.eval()

    y_mean_flat = y_mean.squeeze().to(device)
    y_std_flat  = y_std.squeeze().to(device)

    preds_all, targets_all, idx_all = [], [], []
    with torch.no_grad():
        for x, aux, y, idx in test_loader:
            x, aux = x.to(device), aux.to(device)
            idx_dev = idx.to(device)
            x_pm = x[:, 0:1, :]; x_wx = x[:, 1:, :]
            pred = model(x_pm, x_wx, aux)
            mean_per_sample = y_mean_flat[idx_dev].unsqueeze(-1)
            std_per_sample  = y_std_flat[idx_dev].unsqueeze(-1)
            pred_denorm     = pred * std_per_sample + mean_per_sample
            preds_all.append(pred_denorm.cpu())
            targets_all.append(y)
            idx_all.append(idx)

    P_all = torch.cat(preds_all).numpy()
    T_all = torch.cat(targets_all).numpy()
    I_all = torch.cat(idx_all).numpy()

    # per-station 平均（舊方法，供參考）
    sta_rmse, sta_mae, sta_r2 = [], [], []
    for n in range(N_sta):
        mask_sta = (I_all == n)
        if mask_sta.sum() < 2: continue
        p_n = P_all[mask_sta]; t_n = T_all[mask_sta]
        valid = ~np.isnan(t_n)
        if valid.sum() < 2: continue
        p_v = p_n[valid]; t_v = t_n[valid]
        sta_rmse.append(np.sqrt(np.mean((p_v - t_v) ** 2)))
        sta_mae.append(np.mean(np.abs(p_v - t_v)))
        if np.var(t_v) > 1e-8:
            sta_r2.append(r2_score(t_v, p_v))

    avg_rmse = np.mean(sta_rmse) if sta_rmse else float('nan')
    avg_mae  = np.mean(sta_mae)  if sta_mae  else float('nan')
    avg_r2   = np.mean(sta_r2)   if sta_r2   else float('nan')

    print(f"\n有效測站數: {len(sta_rmse)}/{N_sta}")
    print(f"\n{'='*55}")
    print(f"  CNN-Baseline  72h 整體評估 (per-station 平均，供參考)")
    print(f"  RMSE: {avg_rmse:.4f}  MAE: {avg_mae:.4f}  R²: {avg_r2:.4f}")
    print(f"{'='*55}")

    # ──────────────────────────────────────────────────────────────────────────
    # 6. 中央論文方法：每小時跨站×樣本 RMSE → 對 72h 平均
    # ──────────────────────────────────────────────────────────────────────────
    P_3d = np.full((S_te, N_sta, 72), np.nan, dtype=np.float32)
    T_3d = np.full((S_te, N_sta, 72), np.nan, dtype=np.float32)
    seq_idx_all = np.arange(len(I_all)) // N_sta
    for k in range(len(I_all)):
        s = seq_idx_all[k]; n = I_all[k]
        P_3d[s, n, :] = P_all[k]
        T_3d[s, n, :] = T_all[k]

    hourly_rmse, hourly_mae = [], []
    for h in range(72):
        m = ~np.isnan(T_3d[:, :, h])
        if m.sum() < 2: continue
        hourly_rmse.append(np.sqrt(np.mean((P_3d[:,:,h][m] - T_3d[:,:,h][m])**2)))
        hourly_mae.append(np.mean(np.abs(P_3d[:,:,h][m] - T_3d[:,:,h][m])))

    paper_rmse = float(np.mean(hourly_rmse))
    paper_mae  = float(np.mean(hourly_mae))

    print(f"\n{'='*55}")
    print(f"  CNN-Baseline  72h 評估（中央論文方法）")
    print(f"  RMSE: {paper_rmse:.4f}  MAE: {paper_mae:.4f}")
    print(f"  目標（中央論文 CNN-BASE 含 CMAQ）: 6.88")
    print(f"{'='*55}")

    for s, e, label in [(0,24,'Day1'),(24,48,'Day2'),(48,72,'Day3')]:
        rmse_seg = []
        for h in range(s, e):
            m = ~np.isnan(T_3d[:, :, h])
            if m.sum() < 2: continue
            rmse_seg.append(np.sqrt(np.mean((P_3d[:,:,h][m] - T_3d[:,:,h][m])**2)))
        print(f"  {label}  RMSE={np.mean(rmse_seg):.4f}")
