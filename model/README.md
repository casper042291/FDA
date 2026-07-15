# 模型程式碼（`model/`）

本資料夾為碩士論文《不規則測站之非均勻譜神經算子 PM2.5 72 小時預報（FNO-DSE-3D）》之模型訓練程式，
對應論文第四章之各項實驗。所有模型皆採「早停版」協定（時間序驗證切分 + early stopping）。

## 共同設定

- **資料切分**：訓練 2018-01-01 ~ 2024-12-31、測試 2025-01-01 ~ 2025-11-30；測站數 **72**。
- **CAMS 背景場**：一律採用**最近格點內插**（nearest-neighbour）之 CAMS 錨點 npz。
- **評估口徑**：中央論文式「逐提前小時池化後再對 72 小時平均」之 RMSE / MAE；真值一律採**原始觀測**（預測經 expm1 還原至 μg/m³）。
- **隨機種子**：`SEED = 42`（結果可重現、消融比較公平）。
- CAMS 原始預報之基準 RMSE 為 **19.5213 μg/m³**（未經在地校正）。

> ⚠️ 程式內之資料路徑為訓練機（Linux）之絕對路徑，請依自己的環境自行調整
> （`fpca_pm25_file` / `raw_pm25_file` / `station_file` / `cams_npz_file` 等）。

## 兩種訓練協定

| | **統一協定**（與 FNO 一致） | **對照基準協定**（Lee et al. 原始配置） |
|---|---|---|
| 正規化 | log1p + z-score | min–max 至 [0,1] |
| 損失 | 遮罩 Huber | 遮罩 MSE |
| 優化器 | AdamW + StepLR | Adam(1e-3) |
| 驗證／選模 | **時間序** 10% 驗證 + 早停(patience=100) | **隨機** 10% 驗證 + 早停(patience=10) |

## 檔案對照表

| 檔案 | 對應論文 | 協定 | 座標編碼 | 測試 RMSE |
|---|---|---|---|---|
| `fno_dse_3d_earlystop.py` | **主模型 FNO-DSE-3D**（3D 時空耦合）表4.3 | 統一 | 傅立葉 | **6.5003** |
| `fno_1d_earlystop.py` | 空間耦合消融（逐站 1D，無跨站耦合）表4.1 | 統一 | 傅立葉 | 6.6834 |
| `fno_dse_3d_weather24_earlystop.py` | 氣象消融：+ 過去 24h 歷史氣象 表4.8 | 統一 | 傅立葉 | 6.5704 |
| `fno_dse_3d_weather72_earlystop.py` | 氣象消融：+ 真實未來氣象（oracle 上界）表4.8 | 統一 | 傅立葉 | 6.0826 |
| `CNN_WITH_FOURIER_EMBEDDING/cnn_fno_setting_fourier_earlystop.py` | **主要跨架構對照 CNN**（與 FNO 僅差骨幹）表4.3 | 統一 | 傅立葉(64維) | 6.7733 |
| `CNN_WITH_FOURIER_EMBEDDING/cnn_base_setting_fourier_earlystop.py` | CNN 穩健性 表4.2robust | 對照基準 | 傅立葉(64維) | 7.1909 |
| `CNN_WITHOUT_FOURIER EMBEDDING/cnn_fno_setting_earlystop.py` | CNN 穩健性 表4.2robust | 統一 | 原始經緯度(2維) | 6.8063 |
| `CNN_WITHOUT_FOURIER EMBEDDING/cnn_base_setting_earlystop.py` | CNN 穩健性 表4.2robust | 對照基準 | 原始經緯度(2維) | 7.5135 |
| `fpca/fdapace_all_DenseWithMV.R` | FPCA 缺值重建前處理（§3.1.3；R `fdapace` / PACE） | — | — | — |

## 說明

- **FNO 模型**（`fno_*`）：以直接譜估計（DSE / Vandermonde）於測站真實座標上做三維時空譜卷積，
  免插值處理不規則測站；CAMS 以可學閘門 + 輔助分支去偏，作為殘差錨點。
- **CNN 基準**（`cnn_*`）：重現 Lee et al. (2024) 之一維時序 CNN（觀測 1D-CNN + CAMS 1D-CNN
  + 時間 embedding + 座標輔助 → 全連接）。依「座標編碼（原始 vs 傅立葉）× 訓練協定（統一 vs 對照基準）」
  產生四組變體，用於分離「座標編碼」與「訓練協定」對效能之影響（論文表4.2robust）。
- **主要架構結論**：於同協定同座標下，FNO-DSE-3D（6.5003）優於卷積骨幹 CNN（6.7733），
  改善可乾淨歸因於「時空耦合算子骨幹 vs 卷積骨幹」之差異。
- **FPCA 前處理**（`fpca/…​.R`）：以 `fdapace` 對每測站每日 24 小時序列做函數型主成分分析、
  以條件期望法（PACE）穩健重建缺值；基底僅以訓練期估計以避免洩漏。
