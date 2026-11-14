# 修改前的 GitHub 程式備份

備份日期：2026-11-12（Asia/Taipei）。
儲存庫：https://github.com/Laiting20021202/laiting_uav

2026-11-14 更新：加入目前已部署的 `ros2/` 程式、Docker／Compose、
服務範本、唯讀橋接、測試與小型驗證報告。操作及手動啟動見
[系統操作與故障排查](OPERATIONS.md)。Docker 映像本體與 bag／ZIP
不包含在 Git 備份內；目前硬體在線狀態以最新檢查報告為準。

這是目前系統的程式與設定版本，包含 `uav/`、`scripts/`、`tests/`、
`kernel/`、文件、套件需求、`config/settings.json` 與模型 registry／訓練設定。
本次備份不修改應用程式行為，也不重啟實機服務。

## 未上傳內容

下列內容留在原主機，由 `.gitignore` 排除；GitHub 版本不是整台主機的完整映像：

- `.venv/`、Python 快取及產生的核心模組／編譯產物。
- `config/users.json`、`config/cloudflared.yml`、環境變數檔及私鑰。
- `data/` 執行資料（保留 `data/README.md`），包括辨識事件、飛行軌跡、session 及維護備份。
- `saving data/`、`saving data.zip`、`test_video/`、`飛行畫面/`。
- 模型權重及匯出模型（`.pt`、`.engine`、`.onnx`），包括所有子目錄。
- 訓練產生的模型預覽圖、影片及日誌；保留模型設定、評估數據及相關程式。

## 還原注意事項

1. 另行備份及還原需要的模型權重到原本 `models/` 路徑；預設模型為
   `models/president_cup_v5/best.pt`，其他模型路徑見各目錄的 `registry.json`。
2. 依 `README.md` 與 `docs/DEPLOYMENT.md` 建立 Jetson Python 環境及服務。
   CUDA、TensorRT、系統套件、systemd／udev 實際安裝狀態與主機網路設定
   不包含在此 Git 儲存庫內，需依文件與部署程式重建。
3. 重新建立使用者密碼，並由安全備份還原 Cloudflare Tunnel 憑證及設定。
   不應將這些憑證加入公開儲存庫。
4. 若要保留歷史辨識及飛行紀錄，需另行備份 `data/`；複製 SQLite 資料庫時
   應使用 SQLite backup API，或先停止相關服務，避免只複製主檔造成不一致。

公開儲存庫的內容可被所有人讀取。已在聊天中曝光的 GitHub token 應撤銷；
token 不應寫進 remote URL、程式、文件或 Git 設定。
