# 模型目錄

絕對位置：`/home/hrc/laiting_uav/models/preliminary round`。

| 檔案 | 功能 |
| --- | --- |
| `registry.json` | 模型名稱、檔名、輸入大小、裝置、半精度、車輛類別、說明 |
| `VisDrone_train1_5090best.engine` | 現有 YOLO 的 TensorRT 引擎；預設使用，固定 `imgsz=1280`，依賴相容的 Jetson/TensorRT |
| `VisDrone_train1_5090best.pt` | 同一模型的原始 PyTorch 權重；跨設備與重新匯出使用 |

模型於本次開發從 `/home/hrc/Downloads/` 同名檔案複製，不依賴原始位置。未複製 ONNX，因現階段已有可用的 TensorRT 與原始權重。

SHA256：

```text
6e953faa9879a1b70ee8071e7a4f2519a8217306c0d737f45abd27fe36850d38  VisDrone_train1_5090best.engine
95a3f4e5e82869df58417d9d0368e1be36b0ca7e46b0d970b3ea515479387fa3  VisDrone_train1_5090best.pt
```

`.engine` 不應視為能在所有 GPU/TensorRT 版本共用。切換主機時先用 `.pt`，再於目標設備重新匯出引擎。模型只接受本目錄中的 `.pt`／`.engine`／`.onnx`，不允許網頁輸入任意檔案路徑或上傳模型。PyTorch 權重僅使用可信來源。
