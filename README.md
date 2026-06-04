# PM2.5 72hr Forecast (FNO 3D)

FNO 3D (Fourier Neural Operator)-based **72-hour PM2.5 forecasting** for Taiwan air-quality monitoring stations (74 stations, pure observational data).

This repo contains the **data**, **FPCA imputation method**, **FNO 3D model**, and **CNN baseline** for reproducing the results.

---

## 1. Task

**Input**

Past **24 hours** × 5 variables (PM2.5, WIND_U, WIND_V, RH, AMB_TEMP) + station lat/lon + time features (hour, day-of-week, month)

**Output**

Next **72 hours** of PM2.5 concentration (via oracle rolling: 3 × 24h)

**Stations**

72 stations across Taiwan (excluding Kinmen/Matsu)

Train : 2018-01-01 ~　2024-12-31

Test : 2025-01-01 ~ 2025-11-30

---

## 2. Data

```
原始資料/                          # Raw station observations (one CSV per variable)
├── merged_reshaped_PM2.5.csv      # Raw PM2.5 (hourly, 2018~2025)
├── AMB_TEMP.csv                   # Raw temperature
├── RH.csv                         # Raw relative humidity
├── WIND_U.csv                     # Raw U-component wind
└── WIND_V.csv                     # Raw V-component wind

用FPCA去補NAN的DATA/               # FPCA-reconstructed series (fills missing cells)
├── PM2.5.csv
├── AMB_TEMP.csv
├── RH.csv
├── WIND_U.csv
├── WIND_V.csv
└── fill_nan_with_fpca.py         # FPCA imputation script

測站經緯度.csv                     # Station coordinates (lon/lat for 74 stations)
```

### Missing-value handling: **raw-first, FPCA-fill**

Input uses **raw observations as primary**; a cell is filled by the FPCA reconstruction **only when the raw value is missing**. See `fill_nan_with_fpca.py` for implementation details.

---

## 3. FPCA imputation (`用FPCA去補NAN的DATA/fill_nan_with_fpca.py`)

Fills missing values in raw observations using FPCA-reconstructed series:

- Loads raw CSV and FPCA-precomputed CSV for the same variable
- Aligns time index, **raw values take priority**, FPCA fills only NaN cells
- Drops Kinmen/Matsu stations
- Truncates to `2025-11-30 23:00:00`

The FPCA precomputation itself is done via R `fdapace::FPCA` in `空品pffr_ffpc_all.ipynb`.

---

## 4. Model (`fno_dse_3d_v7.py`)

**FNO 3D** (Fourier Neural Operator with variable-splitting time dimension) + oracle rolling.

**Architecture:**

```
Input  [B, 74, 24, 5]  (PM2.5 + 4 weather variables, 24h history)
  │
  ├─ Gaussian Fourier Feature Transform (coords → 64-dim)
  ├─ TimeEncoder (hour/dow/month embedding → 48-dim)
  ├─ fc0: Linear(117 → 64) ──→ [B, 64, 74, 24]
  │
  ├─ 4× SpectralConv3D_dse + SimpleMixer (Fourier layers)
  │   ├─ VFT (non-uniform spatial FFT) + temporal FFT
  │   ├─ Complex spectral multiplication (modes_s=16, modes_t=6)
  │   └─ Residual SimpleMixer (Conv2d k=1)
  │
  ├─ fc1: Linear(1536 → 128)
  ├─ fc2: Linear(128 → 24) + residual (last_obs)
  └─ Oracle rolling × 3 → [B, 74, 72]
```

**Key features:**
- **Log-transform**: `log1p(PM2.5)` in, `expm1` out (mitigates high-concentration underestimation)
- **Time embedding**: hour-of-day / day-of-week / month learnable embeddings
- **GFF spatial encoding**: Random Fourier features for station coordinates (scale=10)
- **SpectralConv3D**: Variable-splitting time dimension + non-uniform spatial FFT
- **Oracle rolling**: Uses future known weather variables as oracle input during 3-step rolling

See `fno_v7架構.md` for full architectural details.

---

## 4b. Baseline (`cnn_baseline.py`)

CNN-BASE style baseline (reference: Lee et al. 2024, *Atmospheric Environment*):

- PM2.5 path: 1D-Conv (16 filters, kernel 3) × 2
- Weather path: 1D-Conv (32 filters, kernel 3) × 3
- Auxiliary: lat/lon
- FC: 108 → 72 → 72 → 72 (ReLU)
- Direct 72h output (no rolling)
- Early stopping patience=10

**Differences from original paper:**
1. No CMAQ forecast inputs
2. No SWP weather type / land use index
3. Input length 24h (consistent with FNO v7)
4. Direct 72h output (non-autoregressive)

---

## 5. Reproduce

```bash
python fno_dse_3d_v7.py       # FNO 3D main model
python cnn_baseline.py        # CNN baseline
```

FPCA preprocessing requires `fill_nan_with_fpca.py` (Python) and `空品pffr_ffpc_all.ipynb` (R + `fdapace` for initial FPCA computation).

Requires PyTorch (CUDA optional), pandas, numpy, scikit-learn.
