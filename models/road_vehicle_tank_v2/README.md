# Road vehicle / tank v2

本機預設使用 `best.pt`，registry 名稱為 `road-vehicle-tank-v2`。

- 輸入尺寸：1024（依 `train_metrics.json`）。
- 權重內的類別：0 car、1 van、2 truck、3 bus、4 tank。
- 執行裝置：自動選擇 CUDA；GPU 使用半精度，CPU 使用全精度。
- `last.pt`、`selected_init.pt` 保留供比對；目前服務不載入這兩個檔案。

查看／設定開機預設：

```bash
.venv/bin/python scripts/manage.py model
.venv/bin/python scripts/manage.py model road-vehicle-tank-v2
systemctl --user restart uav-v2
```

`resolved_dataset.yaml` 與 `train_metrics.json` 內的 `/home/david/...` 是原訓練主機路徑，不是本機推論相依。2026-09-21 已在 Jetson 載入 `best.pt` 並以實機相機影像完成推論。
