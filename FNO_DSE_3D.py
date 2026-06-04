# 不預測氣象變數 
# 做rolling

import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset
import sys
import os
import warnings
from sklearn.metrics import r2_score

warnings.filterwarnings('ignore')


fpca_pm25_file = "/home/casper/air/fda_class/FPCA_PM25_format_all.csv"
raw_pm25_file  = "/home/casper/air/fda_class/merged_reshaped_PM2.5.csv"
station_file   = "/home/casper/air/空間/測站經緯度.csv"

weather_files = {
    'u_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Uall.csv",
    'v_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Vall.csv",
    'rh':     "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_RHall.csv",
    'temp':   "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_tempall.csv"
}




class GaussianFourierFeatureTransform(nn.Module):
    def __init__(self, mapping_size=64, scale=10):
        super().__init__()
        self.register_buffer('B', torch.randn((2, mapping_size)) * scale)

    def forward(self, x):
        x_proj = (2. * np.pi * x) @ self.B
        return torch.cat([torch.sin(x_proj), torch.cos(x_proj)], dim=-1)


class SimpleMixer(nn.Module):
    def __init__(self, width):
        super().__init__()
        self.norm = nn.GroupNorm(num_groups=min(8, width), num_channels=width)
        self.net = nn.Sequential(
            nn.Conv2d(width, width, kernel_size=1),
            nn.GELU(),
            nn.Conv2d(width, width, kernel_size=1)
        )

    def forward(self, x):
        return self.net(self.norm(x))




class VFT3D:
    def __init__(self, x_positions, y_positions, modes_s, modes_t, T):
        self.modes_s = modes_s
        self.modes_t = modes_t
        self.T = T
        self.device = x_positions.device
        B = x_positions.shape[0]
        N = x_positions.shape[1]

        x_pos = x_positions.clone()
        y_pos = y_positions.clone()
        x_pos -= x_pos.min(dim=1, keepdim=True)[0]
        x_pos  = x_pos * 6.28318 / (x_pos.max(dim=1, keepdim=True)[0] + 1e-8)
        y_pos -= y_pos.min(dim=1, keepdim=True)[0]
        y_pos  = y_pos * 6.28318 / (y_pos.max(dim=1, keepdim=True)[0] + 1e-8)

        kx = torch.cat([torch.arange(modes_s, device=self.device),
                        torch.arange(-modes_s, 0, device=self.device)], dim=0).float()
        ky = torch.cat([torch.arange(modes_s, device=self.device),
                        torch.arange(-(modes_s - 1), 0, device=self.device)], dim=0).float()

        kx = kx.unsqueeze(0).unsqueeze(-1)
        ky = ky.unsqueeze(0).unsqueeze(-1)
        x_pos_3d = x_pos.unsqueeze(1)
        y_pos_3d = y_pos.unsqueeze(1)

        X_mat = kx * x_pos_3d
        Y_mat = ky * y_pos_3d

        Ks    = (2 * modes_s) * (2 * modes_s - 1)
        X_exp = X_mat.unsqueeze(2).expand(B, 2*modes_s, 2*modes_s-1, N)
        Y_exp = Y_mat.unsqueeze(1).expand(B, 2*modes_s, 2*modes_s-1, N)
        phase = (X_exp + Y_exp).reshape(B, Ks, N)

        self.V_space_fwd = torch.exp(-1j * phase.cfloat())
        self.V_space_inv = self.V_space_fwd.conj().permute(0, 2, 1)

        t_idx   = torch.arange(T, device=self.device).float()
        kt      = torch.cat([torch.arange(modes_t, device=self.device),
                              torch.arange(-modes_t, 0, device=self.device)], dim=0).float()
        phase_t = -2 * np.pi * t_idx.unsqueeze(1) * kt.unsqueeze(0) / T
        self.V_time_fwd = torch.exp(1j * phase_t.cfloat())
        self.V_time_inv = self.V_time_fwd.conj().T

        self.Ks = Ks
        self.Kt = 2 * modes_t

    def forward(self, data):
        B, N, T, C = data.shape
        d   = data.permute(0, 1, 3, 2).reshape(B * N * C, T).cfloat()
        d_t = d @ self.V_time_fwd
        d_t = d_t.reshape(B, N, C, self.Kt).permute(0, 1, 3, 2)
        d_t2 = d_t.reshape(B, N, self.Kt * C)
        d_s  = torch.bmm(self.V_space_fwd, d_t2.cfloat())
        return d_s.reshape(B, self.Ks, self.Kt, C)

    def inverse(self, data):
        B, Ks, Kt, C = data.shape
        N   = self.V_space_inv.shape[1]
        d   = data.reshape(B, Ks, Kt * C)
        d_s = torch.bmm(self.V_space_inv, d.cfloat())
        d_s = d_s.reshape(B, N, Kt, C)
        d_s2 = d_s.permute(0, 1, 3, 2).reshape(B * N * C, Kt)
        d_t  = d_s2 @ self.V_time_inv
        d_t  = d_t.reshape(B, N, C, self.T).permute(0, 1, 3, 2)
        return d_t / (self.T * self.Ks)




class SpectralConv3d_dse(nn.Module):
    def __init__(self, in_channels, out_channels, modes_s, modes_t):
        super().__init__()
        self.in_channels  = in_channels
        self.out_channels = out_channels
        self.modes_s = modes_s
        self.modes_t = modes_t
        scale = 1 / (in_channels * out_channels)
        shape = (in_channels, out_channels, modes_s, modes_s, modes_t)
        self.weights_pp = nn.Parameter(scale * torch.rand(*shape, dtype=torch.cfloat))
        self.weights_np = nn.Parameter(scale * torch.rand(*shape, dtype=torch.cfloat))
        self.weights_pn = nn.Parameter(scale * torch.rand(*shape, dtype=torch.cfloat))
        self.weights_nn = nn.Parameter(scale * torch.rand(*shape, dtype=torch.cfloat))

    def compl_mul3d(self, x, w):
        return torch.einsum("bixyz,ioxyz->boxyz", x, w)

    def forward(self, x, transformer):
        B, C, N, T = x.shape
        Ms, Mt = self.modes_s, self.modes_t

        x_ft = transformer.forward(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        x_ft = x_ft.reshape(B, C, 2*Ms, 2*Ms-1, 2*Mt)

        out_ft = torch.zeros(B, self.out_channels, 2*Ms, 2*Ms-1, 2*Mt,
                             dtype=torch.cfloat, device=x.device)
        out_ft[:,:,  :Ms,  :Ms,  :Mt] = self.compl_mul3d(x_ft[:,:,  :Ms,  :Ms,  :Mt], self.weights_pp)
        out_ft[:,:, -Ms:,  :Ms,  :Mt] = self.compl_mul3d(x_ft[:,:, -Ms:,  :Ms,  :Mt], self.weights_np)
        out_ft[:,:,  :Ms,  :Ms, -Mt:] = self.compl_mul3d(x_ft[:,:,  :Ms,  :Ms, -Mt:], self.weights_pn)
        out_ft[:,:, -Ms:,  :Ms, -Mt:] = self.compl_mul3d(x_ft[:,:, -Ms:,  :Ms, -Mt:], self.weights_nn)

        out_ft = out_ft.reshape(B, self.out_channels, (2*Ms)*(2*Ms-1), 2*Mt)
        x_out  = transformer.inverse(out_ft.permute(0, 2, 3, 1))
        return x_out.permute(0, 3, 1, 2).real


# FNO_3D 主模型


class FNO_3D(nn.Module):
    def __init__(self, in_vars, out_hours=24, T_in=24,
                 modes_s=8, modes_t=6, width=32):
        super().__init__()
        self.modes_s   = modes_s
        self.modes_t   = modes_t
        self.width     = width
        self.T_in      = T_in
        self.out_hours = out_hours

        self.coord_mapper = GaussianFourierFeatureTransform(mapping_size=32, scale=10)
        coord_dim = 64

        self.fc0 = nn.Linear(in_vars + coord_dim, width)

        self.conv0 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv1 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv2 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv3 = SpectralConv3d_dse(width, width, modes_s, modes_t)

        self.w0 = SimpleMixer(width)
        self.w1 = SimpleMixer(width)
        self.w2 = SimpleMixer(width)
        self.w3 = SimpleMixer(width)

        self.fc1 = nn.Linear(width * T_in, 128)
        self.fc2 = nn.Linear(128, out_hours)

    def forward(self, x, coords):
        B, N, T_in, V = x.shape
        last_obs = x[:, :, -1, 0]

        x_coords = self.coord_mapper(coords)
        x_coords = x_coords.unsqueeze(2).expand(B, N, T_in, -1)

        h = self.fc0(torch.cat([x, x_coords], dim=-1))
        h = h.permute(0, 3, 1, 2)

        transformer = VFT3D(
            x_positions=coords[:, :, 0],
            y_positions=coords[:, :, 1],
            modes_s=self.modes_s,
            modes_t=self.modes_t,
            T=T_in
        )

        h = F.gelu(self.conv0(h, transformer) + self.w0(h))
        h = F.gelu(self.conv1(h, transformer) + self.w1(h))
        h = F.gelu(self.conv2(h, transformer) + self.w2(h))
        h =        self.conv3(h, transformer) + self.w3(h)

        h = h.permute(0, 2, 3, 1).reshape(B, N, T_in * self.width)
        h = F.gelu(self.fc1(h))
        delta = self.fc2(h)

        return last_obs.unsqueeze(-1) + delta




def rolling_predict_72h(model, x_init, weather_obs, coords, device,
                        y_mean=None, y_std=None, denorm=False):
    model.eval()
    preds     = []
    x_current = x_init.clone()

    with torch.no_grad():
        for step in range(3):
            pred_24h = model(x_current, coords)
            preds.append(pred_24h)

            if step < 2:
                pm25_feedback = pred_24h.unsqueeze(-1)
                weather_next  = weather_obs
                x_current     = torch.cat([pm25_feedback, weather_next], dim=-1)

    pred_72h = torch.cat(preds, dim=-1)

    if denorm and y_mean is not None:
        pred_72h = pred_72h * y_std + y_mean

    return pred_72h




def create_sequences_3d(data_dict, station_names,
                        input_hours=24, output_hours=72, stride=24,
                        raw_pm25_df=None):
    """
    輸出:
      X      : [S, N, T_in, V]     完整輸入（PM2.5 + 氣象）
      X_wx   : [S, N, T_in, V-1]   僅氣象部分
      Y      : [S, N, 72]          FPCA PM2.5（訓練目標）
      Y_3seg : [S, N, 3, 24]       拆成3段
      Y_raw  : [S, N, 72] or None  原始 PM2.5（含 NaN，評估用）

    資料對齊：raw_pm25_df 已對齊到相同 full_idx，用相同 iloc 切片確保時間一致。
    """
    ordered_keys = ['pm25'] + [k for k in data_dict if k != 'pm25']
    df_pm25 = data_dict['pm25']
    n_stations = len(station_names)
    n_rows = len(df_pm25)
    window = input_hours + output_hours
    V = len(ordered_keys)

    n_seq = (n_rows - window) // stride + 1
    if n_seq <= 0:
        return None, None, None, None, None

    X      = np.zeros((n_seq, n_stations, input_hours, V),     dtype=np.float32)
    X_wx   = np.zeros((n_seq, n_stations, input_hours, V - 1), dtype=np.float32)
    Y      = np.zeros((n_seq, n_stations, output_hours),        dtype=np.float32)

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

            X_wx[seq_idx, st_idx, :, :] = X[seq_idx, st_idx, :, 1:]
            Y[seq_idx, st_idx, :]       = df_pm25[st_name].iloc[end_obs:end_pred].values

            # ★ 切出與 X 時間窗對齊的原始 PM2.5（保留 NaN）
            if has_raw and st_name in raw_pm25_df.columns:
                raw_win = raw_pm25_df[st_name].iloc[end_obs:end_pred].values
                if len(raw_win) == output_hours:
                    Y_raw[seq_idx, st_idx, :] = raw_win

        actual = seq_idx + 1

    X      = X[:actual]
    X_wx   = X_wx[:actual]
    Y      = Y[:actual]
    Y_3seg = Y.reshape(actual, n_stations, 3, 24)
    if has_raw:
        Y_raw = Y_raw[:actual]

    return X, X_wx, Y, Y_3seg, Y_raw


def load_and_split_data(fpca_file, raw_file, station_file, weather_files):
    print("--- [Step 1] 讀取數據與標準化 ---")

    df_stations = pd.read_csv(station_file)
    data_frames = {}
    data_frames['pm25'] = pd.read_csv(fpca_file)
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
    common_stations = sorted(list(common_stations))
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

    # ★ full_idx：所有資料對齊基準
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

    # ★ 載入並對齊原始 PM2.5（對齊到相同 full_idx，確保 iloc 一致）
    raw_pm25_aligned = None
    if os.path.exists(raw_file):
        print(f"載入原始 PM2.5：{raw_file}")
        df_raw = pd.read_csv(raw_file)
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
        nan_pct = df_raw[raw_cols].isna().mean().mean()
        print(f"  測站覆蓋率：{len(raw_cols)}/{len(common_stations)}，缺值率：{nan_pct:.1%}")
    else:
        print(f"警告：找不到原始 PM2.5 {raw_file}，評估將使用 FPCA 版本")

    split_date = pd.Timestamp('2025-01-01')
    test_end   = pd.Timestamp('2025-11-30')

    # 訓練集（不需要 raw Y）
    train_dfs = {n: df[df['datetime'] < split_date].copy()
                 for n, df in aligned_dfs.items()}
    X_tr, X_wx_tr, Y_tr, Y_3seg_tr, _ = create_sequences_3d(
        train_dfs, common_stations, 24, 72, 24, raw_pm25_df=None)

    X_tr      = torch.tensor(X_tr)
    X_wx_tr   = torch.tensor(X_wx_tr)
    Y_tr      = torch.tensor(Y_tr)
    Y_3seg_tr = torch.tensor(Y_3seg_tr)

    x_mean = X_tr.mean(dim=(0,1,2), keepdim=True)
    x_std  = X_tr.std( dim=(0,1,2), keepdim=True) + 1e-6
    y_mean = Y_tr.mean()
    y_std  = Y_tr.std() + 1e-6

    X_tr_norm      = (X_tr - x_mean) / x_std
    X_wx_tr_norm   = (X_wx_tr - x_mean[:,:,:,1:]) / x_std[:,:,:,1:]
    Y_tr_norm      = (Y_tr - y_mean) / y_std
    Y_3seg_tr_norm = (Y_3seg_tr - y_mean) / y_std

    # ★ 測試集：傳入對齊好的原始 PM2.5
    test_dfs = {n: df[(df['datetime'] >= split_date) &
                      (df['datetime'] < test_end)].copy()
                for n, df in aligned_dfs.items()}

    raw_pm25_test = None
    if raw_pm25_aligned is not None:
        raw_pm25_test = raw_pm25_aligned[
            (raw_pm25_aligned.index >= split_date) &
            (raw_pm25_aligned.index <  test_end)
        ].copy()

    X_te, X_wx_te, Y_te, Y_3seg_te, Y_te_raw = create_sequences_3d(
        test_dfs, common_stations, 24, 72, 24, raw_pm25_df=raw_pm25_test)

    if X_te is not None:
        X_te_norm     = (torch.tensor(X_te)    - x_mean) / x_std
        X_wx_te_norm  = (torch.tensor(X_wx_te) - x_mean[:,:,:,1:]) / x_std[:,:,:,1:]
        Y_3seg_te_raw = torch.tensor(Y_3seg_te)
        # ★ 評估用：優先用原始 PM2.5，否則退回 FPCA 版
        Y_te_eval     = torch.tensor(Y_te_raw) if Y_te_raw is not None \
                        else torch.tensor(Y_te)
    else:
        X_te_norm = X_wx_te_norm = Y_te_eval = Y_3seg_te_raw = torch.tensor([])

    print(f"訓練集: {len(X_tr_norm)}, 測試集: {len(X_te_norm)}")
    print(f"X shape : {X_tr_norm.shape}    (S, N, T_in=24, V)")
    print(f"X_wx    : {X_wx_tr_norm.shape}  (S, N, T_in=24, V-1)")
    print(f"Y shape : {Y_tr_norm.shape}     (S, N, 72)")
    if Y_te_raw is not None:
        nan_r = np.isnan(Y_te_raw).mean()
        print(f"Y(評估) : {Y_te_eval.shape}  ← 原始 PM2.5，缺值率 {nan_r:.1%}（NaN 跳過）")

    return {
        'X_train':       X_tr_norm,
        'X_wx_train':    X_wx_tr_norm,
        'Y_train':       Y_tr_norm,
        'Y_3seg_train':  Y_3seg_tr_norm,
        'X_test':        X_te_norm,
        'X_wx_test':     X_wx_te_norm,
        'Y_test_raw':    Y_te_eval,       # ★ 原始 PM2.5（含 NaN）
        'Y_3seg_te_raw': Y_3seg_te_raw,
        'coords':        torch.tensor(coords, dtype=torch.float32),
        'station_names': common_stations,
        'n_vars':        X_tr.shape[-1],
        'T_in':          X_tr_norm.shape[2],
        'stats': {'y_mean': y_mean, 'y_std': y_std,
                  'x_mean': x_mean, 'x_std': x_std}
    }




def rolling_train_loss(model, bx, bx_wx, by_3seg, coords):
    total_loss = 0.0
    x_current  = bx.clone()

    for step in range(3):
        pred_24h   = model(x_current, coords)
        target     = by_3seg[:, :, step, :]
        total_loss += F.huber_loss(pred_24h, target)

        if step < 2:
            pm25_fb   = pred_24h.unsqueeze(-1).detach()
            x_current = torch.cat([pm25_fb, bx_wx], dim=-1)

    return total_loss / 3.0




if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        sys.exit(f"找不到檔案: {fpca_pm25_file}")

    data   = load_and_split_data(fpca_pm25_file, raw_pm25_file, station_file, weather_files)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"\nDevice: {device}")

    N_VARS = data['n_vars']
    T_IN   = data['T_in']

    model = FNO_3D(
        in_vars   = N_VARS,
        out_hours = 24,
        T_in      = T_IN,
        modes_s   = 16,
        modes_t   = 6,
        width     = 64
    ).to(device)

    print(f"模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    sch = torch.optim.lr_scheduler.StepLR(opt, step_size=400, gamma=0.5)

    train_loader = DataLoader(
        TensorDataset(data['X_train'], data['X_wx_train'], data['Y_3seg_train']),
        batch_size=16, shuffle=True)

    has_test   = len(data['X_test']) > 0
    test_loader = DataLoader(
        TensorDataset(data['X_test'], data['X_wx_test'],
                      data['Y_test_raw'], data['Y_3seg_te_raw']),
        batch_size=16, shuffle=False
    ) if has_test else None

    coords_base = data['coords'].unsqueeze(0).to(device)
    y_mean = data['stats']['y_mean'].to(device)
    y_std  = data['stats']['y_std'].to(device)

    best_loss       = float('inf')
    best_model_path = "best_ffno_3d_72h.pth"

    print(f"\n=== 開始訓練（滾動72小時，無未來氣象）===")

    for ep in range(1, 801):
        model.train()
        loss_sum = 0.0

        for bx, bx_wx, by_3seg in train_loader:
            bx      = bx.to(device)
            bx_wx   = bx_wx.to(device)
            by_3seg = by_3seg.to(device)

            B      = bx.shape[0]
            coords = coords_base.expand(B, -1, -1)

            opt.zero_grad()
            loss = rolling_train_loss(model, bx, bx_wx, by_3seg, coords)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            loss_sum += loss.item()

        sch.step()
        avg_loss = loss_sum / len(train_loader)

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(model.state_dict(), best_model_path)

        if ep % 100 == 0 or ep == 1:
            print(f"Epoch {ep:4d} | Loss: {avg_loss:.6f} | "
                  f"Best: {best_loss:.6f} | "
                  f"LR: {opt.param_groups[0]['lr']:.6f}")

    print(f"\n載入最佳模型: {best_model_path}")
    model.load_state_dict(torch.load(best_model_path))

    # ==========================================================================
    # 測試集評估（滾動72小時）
    #
    # ★ 評估口徑：中央論文方法（per-hour pooled）
    #   1. 對比原始 PM2.5 真實值（Y_test_raw），NaN 點跳過
    #   2. 每個「提前小時」h 跨所有測站 × 所有樣本池化算 RMSE / MAE，
    #      再對該段（72h 或各天）的提前小時取平均。（先小時、後平均）
    #   3. 另附「先各站再平均」作為參考對照
    # ==========================================================================
    print("\n=== 測試集評估（滾動72小時，中央論文 per-hour pooled，NaN 跳過）===")
    model.eval()

    if test_loader:
        preds_list = []

        for bx, bx_wx, by_raw, _ in test_loader:
            bx    = bx.to(device)
            bx_wx = bx_wx.to(device)
            B     = bx.shape[0]
            coords = coords_base.expand(B, -1, -1)

            # 模型輸出反標準化到 FPCA 尺度
            pred_72h = rolling_predict_72h(
                model, bx, bx_wx, coords, device,
                y_mean=y_mean, y_std=y_std, denorm=True)   # [B, N, 72]
            preds_list.append(pred_72h.cpu())

        P      = torch.cat(preds_list, dim=0).numpy()   # [S, N, 72]，預測值
        T_true = data['Y_test_raw'].numpy()              # [S, N, 72]，★ 原始 PM2.5（含 NaN）

        station_names = data['station_names']
        N_sta = P.shape[1]

        # ======================================================================
        # [主要] 中央論文方法（per-hour pooled）：
        #   每個「提前小時」h，跨「所有測站 × 所有樣本」池化算 RMSE / MAE，
        #   再對該段（72h 或各天）的提前小時取平均。（先小時、後平均）
        # ======================================================================
        def paper_metric(pred, true, metric='rmse', seg=None):
            s, e = (0, 72) if seg is None else seg
            vals = []
            for h in range(s, e):
                m = ~np.isnan(true[:, :, h]) & ~np.isnan(pred[:, :, h])
                if m.sum() < 2:
                    continue
                d = pred[:, :, h][m] - true[:, :, h][m]
                vals.append(np.sqrt(np.mean(d ** 2)) if metric == 'rmse'
                            else np.mean(np.abs(d)))
            return float(np.mean(vals)) if vals else float('nan')

        mask_all    = ~np.isnan(T_true)
        valid_total = mask_all.sum()

        paper_rmse = paper_metric(P, T_true, 'rmse')
        paper_mae  = paper_metric(P, T_true, 'mae')

        print(f"\n有效評估點：{valid_total:,}（NaN 跳過），測站數：{N_sta}")
        print(f"\n{'='*55}")
        print(f"  三天整體 — 中央論文方法（per-hour pooled，72h 均值）")
        print(f"  RMSE: {paper_rmse:.4f}  MAE: {paper_mae:.4f}")
        print(f"{'='*55}")

        print("\n[每天分段評估（中央論文方法：每提前小時池化，再對該日 24h 平均）]")
        for s, e, label in [
            (0,  24, '第1天 (+01h~+24h)'),
            (24, 48, '第2天 (+25h~+48h)'),
            (48, 72, '第3天 (+49h~+72h)')
        ]:
            d_rmse = paper_metric(P, T_true, 'rmse', seg=(s, e))
            d_mae  = paper_metric(P, T_true, 'mae',  seg=(s, e))
            if np.isnan(d_rmse):
                print(f"  {label}  （無有效資料）")
                continue
            print(f"  {label}  RMSE: {d_rmse:.4f}  MAE: {d_mae:.4f}")

        # ======================================================================
        # [參考] 先算各測站再取平均（每站等權）—— 與中央方法的差異對照用
        # ======================================================================
        sta_rmse_list, sta_mae_list, sta_r2_list = [], [], []
        for n in range(N_sta):
            t_n = T_true[:, n, :]
            m_n = ~np.isnan(t_n)
            if m_n.sum() < 2:
                continue
            p_valid = P[:, n, :][m_n]
            t_valid = t_n[m_n]
            sta_rmse_list.append(np.sqrt(np.mean((p_valid - t_valid) ** 2)))
            sta_mae_list.append(np.mean(np.abs(p_valid - t_valid)))
            if np.var(t_valid) > 1e-8:
                sta_r2_list.append(r2_score(t_valid, p_valid))

        avg_rmse = np.mean(sta_rmse_list) if sta_rmse_list else float('nan')
        avg_mae  = np.mean(sta_mae_list)  if sta_mae_list  else float('nan')
        avg_r2   = np.mean(sta_r2_list)   if sta_r2_list   else float('nan')
        print(f"\n[參考] 先站後平均（每站等權，有效站數 {len(sta_rmse_list)}/{N_sta}）")
        print(f"  RMSE: {avg_rmse:.4f}  MAE: {avg_mae:.4f}  R²: {avg_r2:.4f}")

    else:
        print("沒有測試資料，跳過評估")