# PM2.5 72hr Forecast (FNO 3D)

FNO 3D (Fourier Neural Operator)-based **72-hour PM2.5 forecasting** for Taiwan air-quality monitoring stations (72 stations, pure observational data).

This repo contains the **data**, **FPCA imputation method**, **FNO 3D model**, and **CNN baseline** for reproducing the results.

> 📁 **[`model/`](model/)** contains the **final thesis "early-stop" model scripts** (FNO-DSE-3D main model, FNO-1D spatial-coupling ablation, four CNN baseline variants, weather ablation, and FPCA preprocessing), with a per-file mapping to the thesis experiments/tables — see **[`model/README.md`](model/README.md)**.

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

### Data sources / download links

- **Ground-station observations** — Taiwan Ministry of Environment (MOENV), National Air Quality Monitoring Network (historical data query):
  <https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx>
- **CAMS global atmospheric-composition forecasts** — Copernicus Atmosphere Monitoring Service, Atmosphere Data Store (ADS):
  <https://ads.atmosphere.copernicus.eu/datasets/cams-global-atmospheric-composition-forecasts?tab=overview>

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

測站經緯度.csv                     # Station coordinates (lon/lat; 72 stations used after obs ∩ CAMS intersection)
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

## 4. Model (`FNO_DSE_3D.py`)

**FNO-DSE-3D** — a coordinate-based non-uniform spectral neural operator for 72-hour PM2.5 forecasting (final thesis early-stop version; identical to `model/fno_dse_3d_earlystop.py`).

**Architecture:**

```
Input  [B, 72 stations, 96h, 2ch]   (24h past PM2.5 obs + 72h CAMS forecast on one 96-h axis;
                                     channels = PM2.5 + obs/forecast mask)
  │
  ├─ Gaussian Fourier Feature Transform (station coords → 64-dim)
  ├─ TimeEncoder (hour / day-of-week / month embedding → 48-dim)
  ├─ fc0 → width
  ├─ 4× SpectralConv3D  (VFT non-uniform spatial FFT + temporal FFT) + residual mixer
  ├─ aux-MLP de-biasing head  +  CAMS residual anchor with a learnable gate
  └─ output → [B, 72 stations, 72h]   (direct 72h; no rolling)
```

**Key settings:**
- `log1p(PM2.5)` in / `expm1` out
- CAMS as a **learnable-gated residual anchor** (nearest-grid-point interpolation)
- **Time-series 10% validation split + early stopping** (patience=100), fixed `SEED=42`
- Test-set RMSE = **6.5003** μg/m³

Identical to `model/fno_dse_3d_earlystop.py`; for the full model set and thesis mapping see [`model/README.md`](model/README.md).

---

## 4b. Baseline (`cnn_baseline.py`)

CNN baseline (Lee et al. 2024 style) — **unified-protocol + Fourier-coordinate** variant, the main cross-architecture comparison in the thesis (identical to `model/CNN_WITH_FOURIER_EMBEDDING/cnn_fno_setting_fourier_earlystop.py`).

- Observation path: 1D-Conv (16, k=3) × 2;  CAMS path: 1D-Conv (32, k=3) × 3
- Time embedding + **Fourier station-coordinate features (64-dim, same encoding as FNO-DSE-3D)**
- FC: 108 → 72 → 72;  direct 72h output (no rolling)
- **Unified protocol** (log1p+z-score, masked Huber, AdamW+StepLR, time-series validation + early stopping), nearest CAMS, `SEED=42`
- Test-set RMSE = **6.7733** μg/m³

The other three CNN variants (raw coordinates / reference-baseline protocol) are in [`model/`](model/) — see [`model/README.md`](model/README.md).

---

## 5. Reproduce

```bash
python FNO_DSE_3D.py     # FNO-DSE-3D main model (early-stop; test RMSE 6.5003)
python cnn_baseline.py   # CNN baseline (unified + Fourier; test RMSE 6.7733)
```

FPCA preprocessing requires `fill_nan_with_fpca.py` (Python) and `空品pffr_ffpc_all.ipynb` (R + `fdapace` for initial FPCA computation).

Requires PyTorch (CUDA optional), pandas, numpy, scikit-learn.

---

## 6. Final thesis scripts — early-stop versions (`model/`)

The scripts in **[`model/`](model/)** are the **final versions used in the thesis** — trained with a
**time-series validation split + early stopping**, **nearest-grid-point CAMS**, and fixed `SEED=42`.
The root-level `FNO_DSE_3D.py` / `cnn_baseline.py` are copies of the two main scripts here
(`fno_dse_3d_earlystop.py` and `CNN_WITH_FOURIER_EMBEDDING/cnn_fno_setting_fourier_earlystop.py`);
this folder additionally provides the FNO-1D, weather-ablation, and all four CNN variants.

| Script | Role | Test RMSE (μg/m³) |
|---|---|---|
| `model/fno_dse_3d_earlystop.py` | **FNO-DSE-3D main model** (3D spatio-temporal coupling) | **6.5003** |
| `model/fno_1d_earlystop.py` | Spatial-coupling ablation (per-station 1D) | 6.6834 |
| `model/fno_dse_3d_weather24_earlystop.py` | Weather ablation: + past 24h weather | 6.5704 |
| `model/fno_dse_3d_weather72_earlystop.py` | Weather ablation: + true future weather (oracle) | 6.0826 |
| `model/CNN_WITH_FOURIER_EMBEDDING/…` | CNN baselines, Fourier coords (2 protocols) | 6.7733 / 7.1909 |
| `model/CNN_WITHOUT_FOURIER EMBEDDING/…` | CNN baselines, raw coords (2 protocols) | 6.8063 / 7.5135 |
| `model/fpca/fdapace_all_DenseWithMV.R` | FPCA missing-value reconstruction (R, `fdapace`) | — |

See **[`model/README.md`](model/README.md)** for the full file-to-experiment mapping, the two training
protocols (unified vs. reference-baseline), and common settings.
