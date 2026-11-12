# 初賽模型與多高度模型比較（2026-09-16）

多高度模型在既有模擬測試有良好成績；先前同一張實拍截圖的結果則是初賽模型較好。兩者可以同時成立。目前缺少初賽模型在相同模擬測試集上的成績，尚不能量化多高度模型相對初賽模型的提升，也不能用單張實拍截圖概括整體實拍準確度。

## 模型與訓練差異

以下架構、類別、參數量與訓練設定直接讀取本機 `.pt` checkpoint；資料組成另參照隨模型提供的 README 與 model card。

| 項目 | 初賽 VisDrone 模型 | 多高度模型 |
| --- | --- | --- |
| 架構 | YOLO11m | YOLO11m，從初賽權重再訓練 |
| 參數量 | 20,060,718 | 20,054,550 |
| 類別 | 10 類，含 car、van、truck、bus，沒有 tank | vehicle、tank 共 2 類 |
| 訓練資料設定 | VisDrone.yaml | 多高度模擬資料與 tank_dataset 混合 |
| 訓練輸入大小 | 1024 | 1280 |
| 目前部署輸入大小 | 1280 | 1280 |
| 多高度資料規模 | 本機無對應訓練／測試資料 | model card 記錄訓練 500 張、驗證 100 張、模擬測試 80 張 |
| 高度涵蓋 | checkpoint 無相同高度報告 | 50、75、100、150、225、325、450 公尺 |
| 訓練方式 | checkpoint 設定 epochs=600；此值不代表已證實完成的輪數 | 設定 epochs=80，記錄實際完成 30 輪，選第 28 輪 |
| 骨幹保護 | — | freeze=null，沒有設定凍結骨幹 |

「多高度」代表資料中的視角與物體尺度涵蓋，並非更大的網路架構，也不保證保留原本所有實拍能力。VisDrone 的來源是實際無人機影像，見[官方資料集說明](https://github.com/VisDrone/VisDrone-Dataset)。

已驗證多高度 best.pt 的 SHA256 與 model_card.json 一致：
`8a346c0c5bf842c8c35a01764da72247a9c3dec9f1b54412b1320a657846be20`。

初賽 checkpoint SHA256：
`95a3f4e5e82869df58417d9d0368e1be36b0ca7e46b0d970b3ea515479387fa3`。

## 已有成績如何解讀

| 評估來源 | 初賽模型 | 多高度模型 | 可否直接比較 |
| --- | --- | --- | --- |
| 各自 checkpoint 的驗證 mAP50 | 66.01%，VisDrone | 96.62%，新資料集 | 否，資料及類別不同 |
| 80 張模擬測試，整體 mAP50 | 未找到同集成績 | 94.82% | 尚無初賽基準 |
| 80 張模擬測試，vehicle recall | 未找到同集成績 | 84.11% | 尚無初賽基準 |
| 80 張模擬測試，tank recall | 沒有 tank 類別 | 92.47% | 需先定義戰車評估方式 |
| tank_dataset 驗收 | 沒有 tank 類別 | mAP50=99.50%，50 張、209 個標註目標 | 這是戰車驗收，不代表一般車輛能力 |
| 使用者實拍截圖，中間白車 | PT 信心 86.4%；TensorRT 87.5% | conf=0.01 仍無輸出 | 同圖對照成立，僅限此圖 |
| 同張截圖，左下車輛 | PT 信心 85.6%；TensorRT 85.3% | conf=0.01 仍無輸出 | 同圖對照成立，僅限此圖 |

上列截圖結果來自本次對話前一輪的實際推論。它是網頁截圖重跑，並非當時原始 RTSP 影格的逐幀回放。百分比信心值不是準確率；mAP50 也不是「有多少張圖完全答對」。

多高度模型在另一份分高度報告中的表現如下；這份測試與 80 張模擬測試不同，不能混算。

| 高度（公尺） | vehicle recall | tank recall | 測試影像／全部標註目標 |
| --- | --- | --- | --- |
| 50 | 100.00% | 100.00% | 10／8 |
| 75 | 100.00% | 100.00% | 10／11 |
| 100 | 99.32% | 100.00% | 10／10 |
| 150 | 100.00% | 99.14% | 10／13 |
| 225 | 100.00% | 92.39% | 10／30 |
| 325 | 95.83% | 95.45% | 10／46 |
| 450 | 73.94% | 93.33% | 10／86 |

每個高度只有 10 張，尤其低高度目標數很少；接近滿分不能推廣為所有場景都可靠。報告沒有保存完整推論門檻設定，以上 precision／recall 不能直接當成正式服務 conf=0.60 的命中率。

## 發現的具體訓練資料疑點

[train_batch1.jpg](../models/president_cup_multialtitude/runs/multialtitude_target_adaptation/train_batch1.jpg) 中，`target_synthetic_180.png`、`target_synthetic_172.png` 等訓練樣張有大量一般車輛，但顯示的標註只框出類別 1 的戰車。

[tank_dataset 的驗收標註圖](../models/president_cup_multialtitude/evaluation/tank_dataset_plots/val_batch0_labels.jpg) 也呈現相同情形：一般汽車與卡車沒有 vehicle 標註，只標戰車。單純戰車資料集採此標法可以符合其原任務，但併入「vehicle + tank」訓練時，就需要重新核對一般車輛的標註規則。

**已觀察到的事實：** 保存的訓練樣張存在一般車輛未被框出的情形。

**合理但尚未經對照實驗確認的解釋：** 若原始訓練標註同樣漏掉這些 vehicle，正常偵測訓練會將未配對位置作為背景監督，可能抑制對真車的預測。這比單純「模擬與實拍看起來不同」更值得優先排查。[Ultralytics 標註原則](https://academy.ultralytics.com/courses/dataset-readiness-for-yolo/annotation-best-practices) 也說明部分漏標會讓模型將目標學成背景。

另一項可能因素是再訓練後原能力退步。新資料訓練並不自動保證舊能力保留；此處未設定凍結骨幹，資料說明也未記載混回原始 VisDrone 資料。[Learning without Forgetting](https://arxiv.org/abs/1606.09282) 討論了保留既有能力的問題。但目前資料不足以將這次漏判確定歸因於特定比例的遺忘、漏標或外觀差異。

## 完成公平比較所需的資料與做法

已檢查本機專案、下載的模型 ZIP 與可能的資料目錄，未找到原始模擬測試圖片及其人工／模擬器標註。ZIP 有 144 項內容，主要是權重、報告、訓練拼圖及已畫框結果圖。YAML 指向另一台訓練主機的 `/home/david/Desktop/laiting/無人機決賽/模擬訓練/datasets/president_cup_multialtitude`，本機不存在該路徑。

已畫框結果圖不能替代乾淨測試影像；`tank_dataset_bbox/labels` 是隨推論輸出保存的標籤，也不能未經核實就當成原始 ground truth。

取得原始資料後，公平比較應採：

1. 相同模擬 test 影像、相同 img size=1280、相同推論精度與 NMS，先用兩者 `.pt` 比較，以隔離 TensorRT 部署差異。
2. 一般車輛將初賽的 car／van／truck／bus 對應為 vehicle，與新模型的 vehicle 對照。初賽沒有 tank 類別；戰車需另列新能力，或另做明確定義的類別無關定位測試。
3. 同時報告 AP，以及固定 conf=0.60 和 0.25 的漏判與誤判；按高度、物體像素大小、場景分組。
4. 另外建立實拍保留測試集；修正漏標並混合實拍／模擬資料後，兩份測試都達標才替換部署模型。

因此，本次可支持的是「多高度模型在現有模擬評估表現良好，初賽模型在使用者提供的這張實拍圖較好」。要證明多高度模型在相同模擬場景比初賽更強，仍需要原始模擬測試集或原始 A/B 評估報告。

## 本機依據

- [多高度模型資料與訓練說明](../models/president_cup_multialtitude/README.md)
- [model card](../models/president_cup_multialtitude/model_card.json)
- [80 張模擬測試成績](../models/president_cup_multialtitude/evaluation/simulation_test_metrics.json)
- [分高度成績](../models/president_cup_multialtitude/evaluation/per_altitude_metrics.json)
- [戰車驗收成績](../models/president_cup_multialtitude/evaluation/tank_dataset_metrics.json)
- [訓練設定](../models/president_cup_multialtitude/runs/multialtitude_target_adaptation/args.yaml)
