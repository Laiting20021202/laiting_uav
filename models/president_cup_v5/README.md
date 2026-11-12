# president_cup_v5

以 `models/competition_5class_v1-3/weights/best.pt`（YOLO11m）為唯一初始權重，保留原本五個車輛類別。

訓練狀態見 `training_status.json`。訓練中的 `best.pt` 與 `last.pt` 每個 epoch 會更新，包含訓練狀態；完成後更新為 Ultralytics 的最終權重。本次訓練與評估已完成，實際改善程度見 [比較報告](比較報告.md)。

## 資料

訓練 2,377 張、驗證 135 張、測試 120 張。保留原有參考圖與多陰影模擬資料，以及軍用車輛結構裁切。新增 450 張保持幾何標註的飛行影像退化版本（輕度運動模糊／低解析度、陰影／曝光／薄霧、噪點／壓縮），並保留 300 張既有、與所有原始標註物件零重疊的地形負樣本，新增 150 張城市背景負樣本。

另外從 `testing/實驗室走動.MOV` 的下緣裁切 63 張衣物、椅子及室內背景，逐張目視檢查，不含地板上的車輛圖樣；全部僅用於訓練。不將同影片的改善視為獨立測試成果。

驗證新增 15 張原始 VisDrone val 的背景裁切，排除 120 張空拍地形誤報測試集使用的原始影片／影像群組。完全重複影像跨 train/val/test 的數量為 0；原有參考背景與模擬 episode 群組分割沿用。未使用 test 影像生成訓練增強。

## 訓練

先以 1536、batch 4、AdamW、lr0=0.00015 完成 3 epoch 的環境適應，再從該階段 best.pt，以 batch 8、lr0=0.0001 進行最多 17 epoch 的聯合訓練（patience=7）。凍結前 6 個 backbone 模組，降低對既有視覺特徵的改動，微調後續模組及偵測頭。旋轉、縮放、透視、上下翻轉與亮度增強見 `training_config.yaml`。

訓練暫存位於 `/dev/shm/president_cup_v5_work`，每個 epoch 的 checkpoint 與 results.csv 都會保存到本資料夾。程式：`training/finetune_president_cup_v5.py`；資料建立程式：`training/build_president_cup_v5_dataset.py`。

原始及新模型都需在同一組資料、1536 輸入尺寸下比較。本地資料包含模擬及合成參考圖，無法取代真正飛行影片驗證。

[Ultralytics 訓練設定說明](https://docs.ultralytics.com/modes/train/) · [影像增強說明](https://docs.ultralytics.com/guides/yolo-data-augmentation/)

收到使用者確認的目標順序後，新增 48 張正樣本訓練項目：三個人工標註的原始影格、保持標註幾何的退化影像、完整保留五個目標的脈絡裁切，以及一份重複取樣權重。順序為輕型、輕型、中型、兩棲登陸、雲豹；攝影機轉向後按物件身份反轉順序。這些是同場景的訓練資料，不能視為獨立測試。詳見 lab_positive_annotations.json。

可先試 `imgsz=1536, conf=0.30, iou=0.60`；根據同一測試集，誤判框比 0.25 少且漏檢數未增加。實際飛行仍需驗證。
