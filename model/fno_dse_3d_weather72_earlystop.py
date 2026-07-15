# === 測試集(2025)評估(全 72 站;expm1 還原,對原始 PM2.5,NaN 跳過;用最佳驗證模型)===

# 有效評估點：1,684,069，測站數：72
# ============================================================
#   三天整體 — 中央論文 per-hour pooled（座標版,全 72 站）
#   [模型]     RMSE: 6.0826  MAE: 4.2995
#   [CAMS原始] RMSE: 19.5213  MAE: 14.1195
# ============================================================
#   第1天 (+01~+24h)  模型 RMSE: 5.6377 MAE: 3.9530  |  CAMS RMSE: 18.4506
#   第2天 (+25~+48h)  模型 RMSE: 6.2471 MAE: 4.4282  |  CAMS RMSE: 19.7715
#   第3天 (+49~+72h)  模型 RMSE: 6.3631 MAE: 4.5173  |  CAMS RMSE: 20.3418

# [參考] 先站後平均(有效站 72/72)  RMSE: 6.0465  MAE: 4.2991  R²: 0.5233
# ============================================================
#  ★★ 本檔 = weather72(含未來氣象) + 驗證集 Early Stopping 版（VAL_FRAC=0.10, PATIENCE=100）★★
#     時間序切最後10%當驗證、正規化統計(含 pm25 與氣象)只用訓練90%、以驗證loss選模型早停、
#     測試集(2025)僅最終評估。與其他早停版同協定(SEED=42)。
#     以下為「原始 weather72(無早停)」的舊執行結果，僅供對照：
# ============================================================
# 最終 CAMS gate = 0.864
# ★ 部署：把此 checkpoint 填入 predict_new_stations.py 的 CKPT_PATH,即可預測新測站。


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
# ★固定隨機種子（與其他早停版一致 SEED=42）→ 結果可重現、消融比較公平
# ==============================================================================
import random
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)

# ==============================================================================
# 0. 檔案路徑 / 開關
# ==============================================================================
cams_npz_file  = "/home/casper/air/完整DATA2/cams/CAMS_PM25_perinit.npz"
fpca_pm25_file = "/home/casper/air/完整DATA2/用FPCA去補NAN的DATA/PM2.5.csv"
raw_pm25_file  = "/home/casper/air/fda_class/merged_reshaped_PM2.5.csv"
station_file   = "/home/casper/air/完整DATA2/測站經緯度72測站.csv"
weather_files = {
    'u_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Uall.csv",
    'v_wind': "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_風Vall.csv",
    'rh':     "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_RHall.csv",
    'temp':   "/home/casper/air/fda_class/fpca之後的/FPCA_PM25_format_tempall.csv"
}

USE_CAMS_HEAD = True
STATION_EMBED_DIM = 16            # 保留常數但本版不使用站嵌入
T_PAST = 24; T_FUT = 72; T_TOTAL = T_PAST + T_FUT      # 96

# ★ 驗證集 / Early stopping 設定（與其他早停版一致）
VAL_FRAC = 0.10                  # 訓練期最後 10% 當驗證集（時間序切分）
PATIENCE = 100                   # 驗證 loss 連續 PATIENCE 個 epoch 未改善就提前停止
MAX_EPOCH = 800                  # 訓練 epoch 上限（早停通常會在此之前觸發）


# ==============================================================================
# 1. 核心模組
# ==============================================================================
class GaussianFourierFeatureTransform(nn.Module):
    """座標 random Fourier 特徵（B 為固定隨機高斯矩陣,不訓練）→ 可泛化到任意新座標。"""
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
        self.net = nn.Sequential(nn.Conv2d(width, width, 1), nn.GELU(), nn.Conv2d(width, width, 1))
    def forward(self, x):
        return self.net(self.norm(x))


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
# 2. VFT3D（與主模型完全相同）
# ==============================================================================
class VFT3D:
    def __init__(self, x_positions, y_positions, modes_s, modes_t, T):
        self.modes_s = modes_s; self.modes_t = modes_t; self.T = T
        self.device = x_positions.device
        B = x_positions.shape[0]; N = x_positions.shape[1]
        x_pos = x_positions.clone(); y_pos = y_positions.clone()
        x_pos -= x_pos.min(dim=1, keepdim=True)[0]
        x_pos  = x_pos * 6.28318 / (x_pos.max(dim=1, keepdim=True)[0] + 1e-8)
        y_pos -= y_pos.min(dim=1, keepdim=True)[0]
        y_pos  = y_pos * 6.28318 / (y_pos.max(dim=1, keepdim=True)[0] + 1e-8)
        kx = torch.cat([torch.arange(modes_s, device=self.device),
                        torch.arange(-modes_s, 0, device=self.device)], dim=0).float()
        ky = torch.cat([torch.arange(modes_s, device=self.device),
                        torch.arange(-(modes_s - 1), 0, device=self.device)], dim=0).float()
        kx = kx.unsqueeze(0).unsqueeze(-1); ky = ky.unsqueeze(0).unsqueeze(-1)
        x_pos_3d = x_pos.unsqueeze(1); y_pos_3d = y_pos.unsqueeze(1)
        X_mat = kx * x_pos_3d; Y_mat = ky * y_pos_3d
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
        self.Ks = Ks; self.Kt = 2 * modes_t
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


# ==============================================================================
# 3. SpectralConv3d_dse（與主模型完全相同）
# ==============================================================================
class SpectralConv3d_dse(nn.Module):
    def __init__(self, in_channels, out_channels, modes_s, modes_t):
        super().__init__()
        self.in_channels = in_channels; self.out_channels = out_channels
        self.modes_s = modes_s; self.modes_t = modes_t
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


# ==============================================================================
# 4. FNO_3D（★移除 station_emb;身分由座標 Fourier 承載 → 可預測未見測站）
# ==============================================================================
class FNO_3D(nn.Module):
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
        # ★ [改1] 移除 station embedding;fc0 不含站嵌入
        fc0_in = in_ch + 64 + time_embed_dim * 3               # 2+64+48 = 114
        self.fc0 = nn.Linear(fc0_in, width)
        self.conv0 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv1 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv2 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.conv3 = SpectralConv3d_dse(width, width, modes_s, modes_t)
        self.w0 = SimpleMixer(width); self.w1 = SimpleMixer(width)
        self.w2 = SimpleMixer(width); self.w3 = SimpleMixer(width)
        # ★ [v9_unseen_2] 輔助 MLP 分支：吃最初 concat(fc0_in=114) → 4 層(每層後接 GELU,最寬 512) → width(=64)
        self.aux_mlp = nn.Sequential(
            nn.Linear(fc0_in, 256), nn.GELU(),
            nn.Linear(256, 512),    nn.GELU(),
            nn.Linear(512, 256),    nn.GELU(),
            nn.Linear(256, width),  nn.GELU(),
        )
        # ★ [v9_unseen_2] head 不再直接吃 CAMS;改吃 [FNO 特徵, aux_mlp 分支輸出]
        head_extra = width if use_cams_head else 0
        self.head = nn.Sequential(nn.Linear(width + head_extra, width), nn.GELU(),
                                  nn.Linear(width, 1))
        self.cams_gate = nn.Parameter(torch.tensor(0.0))

    def forward(self, x, anchor, coords, hour, dow, month):
        # x:[B,N,96,2] anchor(=CAMS,已norm):[B,N,72] coords:[B,N,2] hour/dow/month:[B,96]
        B, N, T, C = x.shape
        last_obs = x[:, :, self.t_past - 1, 0]                                 # [B,N]
        xc = self.coord_mapper(coords).unsqueeze(2).expand(B, N, T, -1)        # [B,N,96,64]
        te = self.time_encoder(hour, dow, month).unsqueeze(1).expand(B, N, T, -1)  # [B,N,96,48]
        # ★ [改1] 移除站嵌入:fc0 只吃 [x, 座標Fourier, 時間]
        inp = torch.cat([x, xc, te], dim=-1)                                  # [B,N,96,114] 最初 concat
        aux = self.aux_mlp(inp)                                               # [B,N,96,64] ★輔助 MLP 分支
        h = self.fc0(inp)                                                     # [B,N,96,width]
        h = h.permute(0, 3, 1, 2)                                              # [B,width,N,96]
        tr = VFT3D(coords[:, :, 0], coords[:, :, 1], self.modes_s, self.modes_t, T)
        h = F.gelu(self.conv0(h, tr) + self.w0(h))
        h = F.gelu(self.conv1(h, tr) + self.w1(h))
        h = F.gelu(self.conv2(h, tr) + self.w2(h))
        h =        self.conv3(h, tr) + self.w3(h)
        h = h.permute(0, 2, 3, 1)                                              # [B,N,96,width]
        h   = h[:, :, self.t_total - self.out_hours:, :]                       # 取未來72 [B,N,72,width]
        aux = aux[:, :, self.t_total - self.out_hours:, :]                     # 取未來72 [B,N,72,64]

        lo = last_obs.unsqueeze(-1).expand(-1, -1, self.out_hours)             # [B,N,72]
        if self.use_cams_head:
            # ★ [v9_unseen_2] head 吃 [FNO 特徵, aux_mlp 分支]; 不再直接吃 CAMS
            feat = torch.cat([h, aux], dim=-1)                                 # [B,N,72,width+64]
            delta = self.head(feat).squeeze(-1)
            g = torch.sigmoid(self.cams_gate)
            base = lo + g * (anchor - lo)                                      # CAMS 去偏仍由 gate 承載
        else:
            delta = self.head(h).squeeze(-1)
            base = last_obs.unsqueeze(-1)
        return base + delta, delta


# ==============================================================================
# 5. CAMS 錨點載入
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
# 6. 序列建立（★額外擷取 FutWx＝真實未來72h氣象,供 oracle 設定使用）
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
        return (None,) * 7, []

    Xpast = np.zeros((n_seq, n_stations, t_past, V), dtype=np.float32)
    FutWx = np.full((n_seq, n_stations, t_fut, V - 1), np.nan, dtype=np.float32)  # ★真實未來72h氣象(oracle)
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
                    if v_idx >= 1:                      # ★氣象的未來72h(真實觀測 → oracle)
                        fw = df[st_name].iloc[end_obs:end_pred].values
                        if len(fw) == t_fut:
                            FutWx[seq_idx, st_idx, :, v_idx - 1] = fw
            if st_name in raw_pm25_df.columns:
                rw = raw_pm25_df[st_name].iloc[end_obs:end_pred].values
                if len(rw) == t_fut:
                    Yraw[seq_idx, st_idx, :] = rw
        timestamps.append(ts); keep.append(seq_idx)

    keep = np.array(keep, dtype=int)
    return (Xpast[keep], A[keep], Yraw[keep], FutWx[keep], Hh[keep], Dd[keep], Mm[keep]), timestamps


def build_x96(Xpast_n_pm, Xpast_n_wx, A_norm, FutWx_norm):
    """★未來氣象(oracle):過去24h = pm2.5+氣象;未來72h = CAMS + 真實未來72h氣象觀測。
       ★誤差上界設定:未來段填入真實觀測氣象(實際作業拿不到),非可部署預報。"""
    n, N = A_norm.shape[0], A_norm.shape[1]
    fut_wx = FutWx_norm                                          # [n,N,72,n_wx] 真實未來氣象
    past_block = torch.cat([Xpast_n_pm, Xpast_n_wx], dim=-1)     # [n,N,24,1+n_wx]
    fut_block  = torch.cat([A_norm.unsqueeze(-1), fut_wx], dim=-1)  # [n,N,72,1+n_wx]
    x_vars = torch.cat([past_block, fut_block], dim=2)          # [n,N,96,1+n_wx]
    mask = torch.cat([torch.ones(n, N, 24, 1), torch.zeros(n, N, 72, 1)], dim=2)
    return torch.cat([x_vars, mask], dim=-1)                    # [n,N,96,2+n_wx]


# ==============================================================================
# 7. 資料載入 / 切分（★含過去氣象 + 未來氣象 oracle;FutWx 以訓練集統計標準化）
# ==============================================================================
def load_and_split_data(fpca_file, raw_file, station_file, weather_files, cams_npz):
    print("--- [Step 1] 讀取數據(v9 camshead 無氣象 + 移除站嵌入 + 未見測站實驗)---")
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

    train_dfs = {n: df[df['datetime'] < split_date].copy() for n, df in aligned.items()}
    raw_train = raw_aligned[raw_aligned.index < split_date].copy()
    (Xp_tr, A_tr, Yraw_tr, FutWx_tr, H_tr, D_tr, M_tr), ts_tr = create_sequences(
        train_dfs, common, raw_train, cams_anc, cams_lookup, T_PAST, T_FUT, 24, 0)
    Xp_tr = torch.tensor(Xp_tr); A_tr = torch.tensor(A_tr); Yraw_tr = torch.tensor(Yraw_tr)
    FutWx_tr = torch.tensor(FutWx_tr)

    # ★[早停版] 時間序切分：序列已按時間排序，取「最後 VAL_FRAC」當驗證集（時間最新一段）
    n_total_tr = Xp_tr.shape[0]
    n_val   = max(1, int(round(n_total_tr * VAL_FRAC)))
    n_train = n_total_tr - n_val
    print(f"  訓練期序列 {n_total_tr} → 訓練(前90%) {n_train} / 驗證(最後{int(VAL_FRAC*100)}%) {n_val}")

    # ★[早停版] 正規化統計(pm25 與氣象皆)「只用訓練部分(前90%)」計算，避免驗證集洩漏進 normalization
    pm_vals = Xp_tr[:n_train, :, :, 0]
    y_mean = pm_vals.mean(); y_std = pm_vals.std() + 1e-6
    wx = Xp_tr[:n_train, :, :, 1:]                              # ★氣象通道(逐特徵標準化;統計只用訓練90%)
    w_mean = wx.mean(dim=(0, 1, 2), keepdim=True); w_std = wx.std(dim=(0, 1, 2), keepdim=True) + 1e-6

    past_pm = (Xp_tr[:, :, :, 0:1] - y_mean) / y_std
    past_wx = (Xp_tr[:, :, :, 1:] - w_mean) / w_std
    fut_wx_tr = torch.nan_to_num((FutWx_tr - w_mean) / w_std, nan=0.0)   # ★真實未來氣象(正規化)
    A_tr_norm = (torch.log1p(torch.clamp(A_tr, min=0)) - y_mean) / y_std
    X96_all   = build_x96(past_pm, past_wx, A_tr_norm, fut_wx_tr)
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

    test_dfs = {n: df[(df['datetime'] >= split_date) & (df['datetime'] < test_end)].copy()
                for n, df in aligned.items()}
    raw_test = raw_aligned[(raw_aligned.index >= split_date) & (raw_aligned.index < test_end)].copy()
    (Xp_te, A_te, Yraw_te, FutWx_te, H_te, D_te, M_te), ts_te = create_sequences(
        test_dfs, common, raw_test, cams_anc, cams_lookup, T_PAST, T_FUT, 24, 0)

    if Xp_te is not None and len(Xp_te) > 0:
        Xp_te = torch.tensor(Xp_te); A_te = torch.tensor(A_te); FutWx_te = torch.tensor(FutWx_te)
        past_pm_te = (Xp_te[:, :, :, 0:1] - y_mean) / y_std
        past_wx_te = (Xp_te[:, :, :, 1:] - w_mean) / w_std
        fut_wx_te = torch.nan_to_num((FutWx_te - w_mean) / w_std, nan=0.0)
        A_te_norm = (torch.log1p(torch.clamp(A_te, min=0)) - y_mean) / y_std
        X96_te = build_x96(past_pm_te, past_wx_te, A_te_norm, fut_wx_te)
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


def masked_huber(pred, target, mask):
    target_safe = torch.nan_to_num(target, nan=0.0)
    loss_pt = F.huber_loss(pred, target_safe, reduction='none')
    m = mask.float()
    return (loss_pt * m).sum() / m.sum().clamp(min=1.0)


# ==============================================================================
# 8. 主程式（★藏站訓練 / 對未見站評估）
# ==============================================================================
if __name__ == "__main__":
    if not os.path.exists(fpca_pm25_file):
        sys.exit(f"找不到檔案: {fpca_pm25_file}")

    data = load_and_split_data(fpca_pm25_file, raw_pm25_file, station_file, weather_files, cams_npz_file)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    N_ALL = len(data['station_names'])
    IN_CH = data['n_vars'] + 1                              # 2
    print(f"\nDevice: {device} | ★座標版(移除站嵌入),在全部 {N_ALL} 站上訓練（可部署於新測站）")

    model = FNO_3D(in_ch=IN_CH, n_stations=N_ALL, t_total=T_TOTAL, out_hours=T_FUT,
                   modes_s=10, modes_t=44, width=64,
                   time_embed_dim=16, station_embed_dim=STATION_EMBED_DIM,
                   use_cams_head=USE_CAMS_HEAD).to(device)
    print(f"x96 通道數 in_ch={IN_CH} | ★無站嵌入(純座標) | modes_s=10 modes_t=44 | "
          f"模型參數量: {sum(p.numel() for p in model.parameters()):,}")

    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    sch = torch.optim.lr_scheduler.StepLR(opt, step_size=400, gamma=0.5)

    # ★ 全 72 站訓練（不藏站）
    train_loader = DataLoader(
        TensorDataset(data['X96_train'], data['A_train'], data['Y_train'], data['mask_train'],
                      data['H_train'], data['D_train'], data['M_train']),
        batch_size=16, shuffle=True)
    has_test = len(data['X96_test']) > 0
    test_loader = DataLoader(
        TensorDataset(data['X96_test'], data['A_test'], data['Y_test_raw'],
                      data['H_test'], data['D_test'], data['M_test']),
        batch_size=16, shuffle=False) if has_test else None
    # ★驗證 loader（shuffle=False；★early stopping 只看它★）
    val_loader = DataLoader(
        TensorDataset(data['X96_val'], data['A_val'], data['Y_val'], data['mask_val'],
                      data['H_val'], data['D_val'], data['M_val']),
        batch_size=16, shuffle=False)
    coords_base = data['coords'].unsqueeze(0).to(device)

    y_mean = data['stats']['y_mean'].to(device); y_std = data['stats']['y_std'].to(device)

    # ★ Early stopping 狀態：以「驗證 loss」為準（測試集只做最終評估）
    best_val = float('inf'); best_ep = 0; epochs_no_improve = 0
    best_path = "best_ffno_3d_72h_v9_unseen_2_weather72_auxmlp_10_44_earlystop.pth"  # ★早停版專用(勿覆蓋原檔)
    print(f"\n=== 開始訓練(座標版,全72站,★含未來氣象 oracle,+★early stopping,patience={PATIENCE},上限{MAX_EPOCH})===")
    stopped = False
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

        # --- Early stopping：只有「驗證 loss 創新低」才存最佳 checkpoint ---
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
            stopped = True
            break

    if not stopped:
        print(f"\n達到 epoch 上限 {MAX_EPOCH}（未觸發早停）。")
    print(f"★ 最佳(驗證)模型：epoch {best_ep}，val loss = {best_val:.6f}")
    print(f"載入最佳(驗證)模型: {best_path}")
    model.load_state_dict(torch.load(best_path)); model.eval()
    print(f"最終 CAMS gate = {torch.sigmoid(model.cams_gate).item():.3f}")
    print(f"★ 部署：把此 checkpoint 填入 predict_new_stations.py 的 CKPT_PATH,即可預測新測站。")

    print("\n=== 測試集(2025)評估(全 72 站;expm1 還原,對原始 PM2.5,NaN 跳過;用最佳驗證模型)===")
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
        print("  三天整體 — 中央論文 per-hour pooled（座標版,全 72 站）")
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
