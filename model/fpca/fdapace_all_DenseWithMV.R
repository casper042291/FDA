# ==============================================================================
# fdapace_all_DenseWithMV.R
#
# 改自 fdapace_all.R，兩個關鍵改動：
#   ① dataType: "Sparse"(PACE,重平滑) → "DenseWithMV"(dense+容許缺值,平滑大幅減輕)
#      理由：你的逐時 PM2.5 每天 24 點、整體只缺 ~1.93% = 近乎完整(dense)，
#            sparse PACE 是設計給「每曲線少數零星點」的，對近完整資料過度平滑。
#            DenseWithMV 用 cross-sectional(樣本)估計均值/共變異，特徵函數更銳利、保細節。
#   ② FVEthreshold(解釋度): 0.999 → 0.9999（保留更多成分，重建更貼近原值）
#
# 其餘（資料清洗、mat_to_list、predict、輸出格式）與原版相同。
#   註：mat_to_list 仍丟 NA 只留觀測點，但觀測時間都是整數小時、落在共同網格 0:23 上，
#       fdapace 內部 List2Mat 會自動對齊成矩陣(缺值→NA)，DenseWithMV 即據此做 dense 估計。
#
# ★ 重要：本檔輸出的是「完整 FPCA 重建檔」(FPCA_PM25_format_DenseWithMV.csv)。
#   你模型實際讀的是「用FPCA去補NAN」版(只補缺值、保留觀測)。所以跑完這支後，
#   還要【重跑 fill_nan_with_fpca.py】、把它指向這個新重建檔，才會產生新的補洞版 PM2.5.csv。
#   否則模型仍讀到舊的 sparse 補洞版。
#
# 誠實提醒：DenseWithMV/高解釋度只改變那 ~1.93% 缺格怎麼填，你 98% 真實峰值不受影響。
#   這是「方法論更正確、論文好辯護」的改動，不是提升尖峰能力的槓桿。
# ==============================================================================

library(tidyr)
library(dplyr)
library(lubridate)
library(fdapace)


file_path <- "C:\\Users\\caspe\\Downloads\\merged_reshaped_PM2.5.csv"
data <- read.csv(file_path, stringsAsFactors = FALSE)


TRAIN_START_DATE <- as.Date("2018-01-01")
TRAIN_END_DATE   <- as.Date("2024-12-31")
TEST_START_DATE  <- as.Date("2025-01-01")
TEST_END_DATE    <- as.Date("2025-11-30")


target_dates_train <- seq(TRAIN_START_DATE, TRAIN_END_DATE, by = "day")
target_dates_test  <- seq(TEST_START_DATE, TEST_END_DATE, by = "day")

cat("\n目標訓練日期數:", length(target_dates_train), "天\n")
cat("目標測試日期數:", length(target_dates_test), "天\n")

FPCA_PVE <- 0.9999          # ★ 解釋度 99.99%（原 0.999）
FPCA_DATATYPE <- "DenseWithMV"  # ★ dense + 容許缺值（原 "Sparse"）
MIN_Train_Days <- 1

results_df <- data.frame(
  Station = character(),
  MAE = numeric(),
  RMSE = numeric(),

  Test_Days = numeric(),
  stringsAsFactors = FALSE
)

fpca_reconstructed_df <- data.frame()

ignore_cols <- c("PublishTime", "Date", "Time", "date", "SubjectID", "year", "month", "day")
all_stations <- setdiff(colnames(data), ignore_cols)

cat(paste0("總共發現 ", length(all_stations), " 個測站，開始分析...\n"))
cat(sprintf("設定：dataType=%s，FVEthreshold=%.4f\n\n", FPCA_DATATYPE, FPCA_PVE))


station_count <- 0

for (station in all_stations) {

  station_count <- station_count + 1

  # ---------------------- 3.1 清洗資料 ----------------------
  cleaned_data <- tryCatch({
    data %>%
      dplyr::select(PublishTime, all_of(station)) %>%
      rename(value = all_of(station)) %>%
      mutate(PublishTime = gsub("T", " ", PublishTime)) %>%
      mutate(PublishTime = gsub("\\.\\d+$", "", PublishTime)) %>%
      mutate(PublishTime = ymd_hms(PublishTime, quiet = TRUE)) %>%
      filter(!is.na(PublishTime)) %>%
      mutate(date = as.Date(PublishTime)) %>%
      mutate(Time = hour(PublishTime)) %>%
      mutate(value = as.numeric(as.character(value))) %>%
      filter(date >= TRAIN_START_DATE & date <= TEST_END_DATE) %>%
      drop_na(value) %>%
      filter(is.finite(value)) %>%
      arrange(PublishTime)
  }, error = function(e) {
    cat(sprintf("[!] 測站 %s 資料清洗失敗: %s，跳過\n", station, e$message))
    return(NULL)
  })


  if (is.null(cleaned_data) || nrow(cleaned_data) == 0) {
    cat(sprintf("[skip] [%d/%d] %s - 無資料，跳過\n", station_count, length(all_stations), station))
    next
  }

  cat(sprintf("[%d/%d] %s - 資料處理中...", station_count, length(all_stations), station))

  cleaned_data <- cleaned_data %>%
    mutate(SubjectID = as.numeric(factor(date)))


  valid_days <- cleaned_data %>%
    group_by(SubjectID) %>%
    summarise(n = n(), .groups = "drop") %>%
    filter(n >= 1) %>%
    pull(SubjectID)

  cleaned_data <- cleaned_data %>% filter(SubjectID %in% valid_days)
  if (nrow(cleaned_data) == 0) {
    cat(" 無有效日資料，跳過\n")
    next
  }


  wide_data <- cleaned_data %>%
    group_by(SubjectID, date, Time) %>%
    summarise(value = mean(value, na.rm = TRUE), .groups = "drop") %>%
    pivot_wider(id_cols = c(SubjectID, date),
                names_from = Time,
                values_from = value) %>%
    arrange(date)


  for (h in 0:23) {
    if (!as.character(h) %in% names(wide_data))
      wide_data[[as.character(h)]] <- NA
  }

  Y_matrix <- as.matrix(wide_data[, as.character(0:23)])
  Y_matrix <- matrix(as.numeric(Y_matrix), nrow = nrow(Y_matrix), ncol = ncol(Y_matrix))
  date_vec <- wide_data$date


  train_idx <- which(date_vec >= TRAIN_START_DATE & date_vec <= TRAIN_END_DATE)
  test_idx  <- which(date_vec >= TEST_START_DATE  & date_vec <= TEST_END_DATE)

  if (length(train_idx) < MIN_Train_Days) {
    cat(sprintf(" 訓練資料不足 (%d 天)，跳過\n", length(train_idx)))
    next
  }


  if (length(test_idx) == 0) {
    cat(" 無測試集(Test)資料，跳過\n")
    next
  }

  Y_train <- Y_matrix[train_idx, , drop = FALSE]
  Y_test  <- Y_matrix[test_idx, , drop = FALSE]


  mat_to_list <- function(mat) {
    Ly <- list(); Lt <- list(); valid_rows <- c()
    for (i in 1:nrow(mat)) {
      y <- mat[i, ]; t <- 0:23
      y <- as.numeric(y)
      valid <- !is.na(y) & is.finite(y)
      if (sum(valid) >= 2) {
        # 只留觀測點；觀測時間皆整數小時，落在共同網格 0:23 上，
        # fdapace 內部會對齊成矩陣供 DenseWithMV 做 cross-sectional 估計
        Ly[[length(Ly)+1]] <- as.numeric(y[valid])
        Lt[[length(Lt)+1]] <- as.numeric(t[valid])
        valid_rows <- c(valid_rows, i)
      }
    }
    return(list(Ly = Ly, Lt = Lt, idx = valid_rows))
  }

  train_input <- mat_to_list(Y_train)
  test_input  <- mat_to_list(Y_test)

  if (length(train_input$Ly) < 10) {
    cat(sprintf(" FPCA訓練樣本不足 (%d 曲線)，跳過\n", length(train_input$Ly)))
    next
  }


  if (length(test_input$Ly) == 0) {
    cat(" 測試集轉換後無有效曲線，跳過\n")
    next
  }


  optns <- list(
    dataType = FPCA_DATATYPE,    # ★ "DenseWithMV"
    error = TRUE,
    kernel = "epan",
    verbose = FALSE,
    FVEthreshold = FPCA_PVE,     # ★ 0.9999
    methodXi = "CE",             # 容許缺值，保留 CE 較穩（dense 亦可試 "IN"）
    nRegGrid = 24
  )

  fpca_res <- tryCatch({
    FPCA(Ly = train_input$Ly, Lt = train_input$Lt, optns = optns)
  }, error = function(e) {
    cat(sprintf(" FPCA計算失敗: %s，跳過\n", e$message))
    return(NULL)
  })

  if (is.null(fpca_res)) next

  mu <- fpca_res$mu
  phi <- as.matrix(fpca_res$phi)




  Y_train_final <- matrix(rep(mu, length(target_dates_train)),
                          nrow = length(target_dates_train),
                          byrow = TRUE)

  obs_dates_train <- date_vec[train_idx][train_input$idx]
  match_idx_train <- match(obs_dates_train, target_dates_train)
  train_scores <- fpca_res$xiEst

  if (length(match_idx_train) > 0 && nrow(train_scores) == length(match_idx_train)) {
    Y_train_final[match_idx_train, ] <- Y_train_final[match_idx_train, ] + (train_scores %*% t(phi))
  }


  Y_test_final <- matrix(rep(mu, length(target_dates_test)),
                         nrow = length(target_dates_test),
                         byrow = TRUE)


  predict_success <- FALSE
  if (length(test_input$Ly) > 0) {
    pred_test <- tryCatch({
      predict(fpca_res,
              newLy = test_input$Ly,
              newLt = test_input$Lt,
              K = fpca_res$selectK)
    }, error = function(e) {
      return(NULL)
    })


    if (!is.null(pred_test)) {
      scores_test <- as.matrix(pred_test$scores)
      obs_dates_test <- date_vec[test_idx][test_input$idx]
      match_idx_test <- match(obs_dates_test, target_dates_test)

      if (length(match_idx_test) > 0 && nrow(scores_test) == length(match_idx_test)) {
        Y_test_final[match_idx_test, ] <- Y_test_final[match_idx_test, ] + (scores_test %*% t(phi))
        predict_success <- TRUE
      }
    } else {
      cat(" 預測計算失敗(predict error)，")

    }
  }



  has_valid_eval <- FALSE
  if (length(test_input$idx) > 0 && predict_success) {
    obs_matrix <- Y_test[test_input$idx, , drop=FALSE]
    obs_dates_eval <- date_vec[test_idx][test_input$idx]
    eval_indices <- match(obs_dates_eval, target_dates_test)
    pred_matrix <- Y_test_final[eval_indices, , drop=FALSE]

    mask <- !is.na(obs_matrix)
    if (sum(mask) > 0) {
      obs <- obs_matrix[mask]
      pred <- pred_matrix[mask]
      mae  <- mean(abs(obs - pred))
      rmse <- sqrt(mean((obs - pred)^2))

      cat(sprintf(" | RMSE: %.2f | MAE: %.2f [OK]\n", rmse, mae))

      results_df <- rbind(results_df, data.frame(
        Station = station,
        MAE = round(mae, 4),
        RMSE = round(rmse, 4),
        Test_Days = length(test_input$Ly)
      ))
      has_valid_eval <- TRUE
    }
  }

  if (!has_valid_eval) {
    cat(" | 未能進行評估 (無有效測試數據或預測失敗)\n")

  }


  hours <- 0:23

  train_df <- as.data.frame(Y_train_final)
  colnames(train_df) <- paste0("H", hours)
  train_df$Station <- station
  train_df$Date <- target_dates_train
  train_df$Set <- "Train"

  test_df <- as.data.frame(Y_test_final)
  colnames(test_df) <- paste0("H", hours)
  test_df$Station <- station
  test_df$Date <- target_dates_test
  test_df$Set <- "Test"

  fpca_reconstructed_df <- rbind(fpca_reconstructed_df, train_df, test_df)
}


if (nrow(fpca_reconstructed_df) > 0) {

  cat("\n正在轉換為寬格式...\n")

  long_df <- fpca_reconstructed_df %>%
    pivot_longer(cols = starts_with("H"),
                 names_to = "Hour",
                 values_to = "Value") %>%
    mutate(Hour = as.numeric(gsub("H", "", Hour)),
           PublishTime = as.POSIXct(Date) + hours(Hour)) %>%
    select(PublishTime, Station, Value)

  final_output <- long_df %>%
    pivot_wider(names_from = Station, values_from = Value) %>%
    arrange(PublishTime)

  final_output$PublishTime <- format(final_output$PublishTime, "%Y-%m-%d %H:%M:%S")

  # ★ 輸出檔名改為 DenseWithMV 版，避免覆蓋原 sparse 版
  write.csv(results_df,
            "C:\\Users\\caspe\\OneDrive\\桌面\\碩一\\函數型\\final\\FPCA_results_DenseWithMV_PVE9999.csv",
            row.names = FALSE)

  write.csv(final_output,
            "C:\\Users\\caspe\\OneDrive\\桌面\\碩一\\函數型\\final\\FPCA_PM25_format_DenseWithMV_PVE9999.csv",
            row.names = FALSE,
            quote = FALSE)

  cat("\n=== [完成] 已成功處理", nrow(results_df), "個測站 ===\n")
  cat("輸出重建檔：FPCA_PM25_format_DenseWithMV_PVE9999.csv\n")
  cat("★ 下一步：重跑 fill_nan_with_fpca.py 指向此新重建檔，才會更新模型實際讀的補洞版 PM2.5.csv\n")

} else {
  cat("\n[錯誤] 沒有產生任何資料。所有測站均被跳過。\n")
}
