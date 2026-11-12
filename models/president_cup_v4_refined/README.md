# 總統盃 v4 精修模型

地面站模型名稱：`president-cup-v4-refined`。使用本目錄的 `best.pt`，類別與輸入尺寸已直接讀取權重確認。

| 項目 | 設定 |
| --- | --- |
| 輸入尺寸 | 1280（沿用訓練尺寸） |
| 執行裝置 | 自動選擇 CUDA；CUDA 使用 FP16 |
| 類別 0 | `civilian_vehicle` |
| 類別 1 | `light_tactical_vehicle` |
| 類別 2 | `medium_tactical_vehicle` |
| 類別 3 | `clouded_leopard_armored_vehicle` |
| 類別 4 | `amphibious_assault_vehicle` |

推論信心門檻沿用 `config/settings.json` 的 `detection.vehicle_conf`（目前 0.60），連續目標記錄仍為 2 秒。模型選單由本目錄的 `registry.json` 自動加入。

`train_metrics.json`、資料集 YAML、訓練輸出與影片驗證報告是原有訓練資料；其中其他主機的絕對路徑不參與地面站執行。
