# 檔案與資料索引

下表所有專案路徑均相對於 **`/home/hrc/laiting_uav/`**。只保留程式、實際使用的設定／模型、操作文件與有行為驗證用途的測試。

| 位置 | 功能 |
| --- | --- |
| `README.md` | 操作入口、啟動方式、主要參數、模型切換 |
| `.gitignore` | 排除憑證、資料庫、模型二進位與 Python 快取 |
| `requirements.txt` | 本次驗證的執行依賴版本，保留主機 NVIDIA PyTorch |
| `requirements-test.txt` | 可選 pytest、HTTP 測試、瀏覽器測試依賴 |
| `.venv/` | 本專案 Python 虛擬環境，不是可刪除的無用副本 |
| `config/settings.json` | 網頁、相機、飛控、預覽、辨識、定位與保存量的主設定 |
| `config/users.json` | 兩個網頁使用者的加鹽 scrypt 雜湊；權限 600；不含明文密碼 |
| `config/cloudflared.yml` | 授權成功後產生的 Tunnel ID、憑證路徑與 HTTP／SSH ingress；不內嵌 secret |
| `models/president_cup_v5/best.pt` | 目前預設的 v5 五類車輛權重，1280 推論輸入；訓練使用 1536 |
| `models/president_cup_v5/registry.json` | v5 正式服務名稱與模型執行參數 |
| `models/competition_5class_v1-3/weights/best.pt` | 可切換的競賽五類 v1-3 權重，1536 輸入 |
| `models/competition_5class_v1-3/registry.json` | 競賽五類模型正式服務名稱與執行參數 |
| `models/competition_5class_v4finetune/weights/best.pt` | 可切換的競賽五類 v4 微調權重，1152 輸入 |
| `models/competition_5class_v4finetune/registry.json` | v4 微調模型正式服務名稱與執行參數 |
| `models/preliminary round/README.md` | 模型來源、用途、SHA256 |
| `models/preliminary round/registry.json` | 可選模型的名稱與執行參數 |
| `models/preliminary round/VisDrone_train1_5090best.engine` | Jetson TensorRT 模型 |
| `models/preliminary round/VisDrone_train1_5090best.pt` | 同一模型的 PyTorch 原始權重 |
| `models/president_cup_multialtitude/best.pt` | 既有總統盃決賽多高度 YOLO11m 權重；類別為 vehicle、tank |
| `models/president_cup_multialtitude/registry.json` | 新模型的正式服務名稱、輸入尺寸、設備與類別設定 |
| `models/president_cup_multialtitude/model_card.json` | 新模型架構、資料範圍、評估結果與 SHA256 |
| `uav/__init__.py` | Python 套件入口說明 |
| `uav/__main__.py` | CLI、日誌與單一 Uvicorn 程序；`--demo` 使用隔離的展示模式 |
| `uav/config.py` | 設定驗證、模型路徑限制、原子 JSON 寫入 |
| `uav/auth.py` | scrypt、登入限速、session 閒置逾時／續期／撤銷、修改密碼後使舊登入失效 |
| `uav/web.py` | 登入、CSRF、安全標頭、受保護 API、JPEG／MJPEG、CSV |
| `uav/camera.py` | G3P 預覽啟動、FFprobe 比例檢查、FFmpeg 最新影格、超時／重連 |
| `uav/g3p.py` | G3P 短期控制 session、低負載編碼設定、STOP_VF 後重新登入、通知解析與串流恢復 |
| `uav/telemetry.py` | MAVLink 2、來源隔離、飛控狀態、雲台姿態、斷線重連、資料時效 |
| `uav/detection.py` | YOLO、白車＋紅白 X／車頂紅色判定、IoU 追蹤及連續確認 |
| `uav/marker.py` | 從舊版抽出的純 OpenCV 紅白 X 判斷；移除未使用的 CLI，修正 Hough 斜線方向分組 |
| `uav/geo.py` | 畸變校正、相機／雲台座標、NED 地平面投影、WGS84 座標、品質閘門 |
| `uav/storage.py` | SQLite WAL、事件與 JPG／縮圖交易寫入、清除與保存量限制 |
| `uav/records.py` | 完整視野照片加上目標框、時間、類型及 TWD97，產生 JPG 與縮圖 |
| `scripts/create_record_example.py` | 手動建立一筆明確標示的格式範例，清除後不自動重建 |
| `tests/browser_records.py` | 隔離 Chromium 測試照片下載、範例置頂、分頁、手機排版與一鍵清除 |
| `uav/runtime.py` | 相機／遙測／推論生命週期、模型載入、事件寫入、合成展示 |
| `uav/control.py` | 固定的 systemd 服務重啟指令、重啟冷卻、watchdog 通知；不接受任意命令 |
| `uav/web/index.html` | 繁體中文地面站頁面 |
| `uav/web/login.html` | 登入頁面 |
| `uav/web/app.js` | 狀態與預覽更新、過期值隱藏、目標列表、模型切換、設備重連、服務重啟、登出 |
| `uav/web/login.js` | 登入表單、錯誤回饋 |
| `uav/web/style.css` | 電腦與手機排版、視覺樣式 |
| `scripts/install.sh` | 建立／安裝沿用系統 GPU 套件的虛擬環境 |
| `scripts/manage.py` | 互動式密碼管理、設定啟動模型、環境診斷 |
| `scripts/cloudflare.py` | 官方 cloudflared 下載與 SHA256 驗證、登入、Tunnel／DNS／ingress 設定 |
| `scripts/services.py` | 產生並啟用 systemd user 應用／Tunnel 服務，檢查開機 linger |
| `scripts/tunnel_dns.sh`、`config/tunnel-resolv.conf` | 只供 Tunnel 及探測程序使用的獨立 DNS 設定與 rootless namespace 啟動器 |
| `scripts/tunnel_supervisor.py` | 等候穩定上網出口，Wi-Fi／行動網路或 IP 變更時重建 Tunnel；不修改主機路由 |
| `scripts/network_recorder.py` | 持久保存 USB／網路／Tunnel 事件及 boot ID、單調時間；有限容量輪替，不操作硬體 |
| `scripts/network_health.py` | 獨立 DNS／HTTPS 分層探測；確認上游可用後才允許健康檢查觸發 Tunnel 重建 |
| `scripts/flight_network_monitor.py` | 每約 10 秒持久保存閘道、USB 網卡收發／錯誤、DNS、HTTPS、Tunnel、本機與公網狀態 |
| `scripts/modem_recovery.py` | 限定網卡的分階段恢復服務；明確卡死時加速重設、一般故障先重連、DNS 單獨修復；可只更新 recovery |
| `scripts/device_evidence.py` | 每秒只讀保存主機電壓／電流／溫度、USB runtime 狀態及指定網卡收發／錯誤 |
| `scripts/modem_radio.py` | DWM-222W WAN 訊號白名單取樣、本機 Digest 登入設定及 TLS 指紋驗證 |
| `scripts/tunnel_resolver.py` | 本機 DNS 解析器；一般 DNS 失敗時使用驗證憑證的 HTTPS 備援 |
| `scripts/camera_network.sh` | 建立不帶預設閘道的 G3P 專用 Ethernet NetworkManager 設定 |
| `scripts/calibrate_camera.py` | 用棋盤格影像計算相機內參，可明確指定 `--apply` 套用 |
| `tests/test_security.py` | 未登入存取、session、CSRF、Host、限速、模型路徑、過期影像與受保護的重連／重啟操作 |
| `tests/browser_timeout.py` | 隔離 Chromium 測試：真實短效 cookie 續期、首次載入逾時、API 局部失敗、401 確認、斷網恢復與重新登入 |
| `tests/test_auth_restart.py` | 登入 checkpoint、重啟恢復、保留到期時間、撤銷、損毀檔案與 HTTP cookie |
| `tests/browser_service_restart.py` | 公網實際重啟服務，檢查登入／CSRF 保留、模型恢复、按鈕狀態與 Tunnel |
| `tests/test_geo.py` | 已知投影方向、GPS／姿態時效、固定／垂直／雲台座標 |
| `tests/test_detection_storage.py` | 白車與紅色判定、連續確認、事件保存、重啟與淘汰 |
| `tests/test_telemetry.py` | 飛控／GCS 來源隔離、USB 身分重新尋找、UDP 斷線恢復、雲台來源、距離感測器時效、允許的命令 |
| `tests/test_g3p.py` | 真正 TCP 模擬相機：通知／心跳解析、STOP_VF 後新 session 套用設定、無回應時取消與停止 |
| `tests/test_camera.py` | 真正 FFmpeg 影片輸入、斷流、重連、RTSP 逾時參數相容、取消卡住的探測、乾淨停止 |
| `tests/test_runtime_models.py` | 模型載入失敗保留舊模型、成功後才替換 |
| `tests/test_watchdog.py` | 設備離線仍重連時正常餵 watchdog，工作迴圈停滯時停止餵送 |
| `tests/test_tunnel_supervisor.py` | 以真實子程序驗證離線等待、介面／閘道／IP 改變後重連、路由穩定等待、程序退出與卡住停止 |
| `tests/browser_recovery.py` | 明確執行的實機瀏覽器測試：相機／飛控重新連線、服務重啟、兩設備恢復、確認 Tunnel PID 未變 |
| `tests/browser_camera_soak.py` | 登入實機網頁並長時間監看影像更新、API、影格新鮮度及 JavaScript 錯誤；只讀取、不發送重連指令，報告存 `/tmp` |
| `tests/browser_smoke.py` | Chromium 實際登入、畫面、CSV、手機排版與登出；憑證由環境變數輸入 |
| `data/README.md` | 資料內容、保存策略、備份說明 |
| `data/live/events.sqlite3*` | 實機事件庫及必要 WAL／SHM |
| `data/demo/events.sqlite3*` | 展示事件庫及必要 WAL／SHM |
| `docs/DEPLOYMENT.md` | Cloudflare 授權、DNS、SSH、systemd 與排障 |
| `docs/GEOLOCATION.md` | 座標假設、相機校正、地面高度、雲台支援邊界 |
| `docs/VALIDATION.md` | 本次實測證據、環境、尚待硬體驗證的限制 |
| `docs/dashboard-demo.png` | 實際瀏覽器產生的展示截圖，由 README 引用 |

新增即時地圖與飛行軌跡相關檔案：

| 路徑 | 功能 |
| --- | --- |
| `uav/flight_tracks.py` | 獨立每秒取樣、GPS 有效性檢查、斷線分段、SQLite 保存、重啟恢復 |
| `uav/web/flight-map.js` | Leaflet 即時位置、紀錄操作、完整路線與 JPG 匯出 |
| `uav/web/vendor/leaflet/` | 本機 Leaflet 1.9.4 JS、CSS 與 BSD 授權 |
| `data/live/flight-tracks.sqlite3*` | 實機飛行軌跡及 WAL／SHM；模擬資料在 `data/demo/` |
| `tests/test_flight_tracks.py` | 紀錄邊界、失效 GPS、分段、持久化、分頁與權限 |
| `tests/browser_flight_tracks.py` | 隔離 Chromium 地圖、開始／停止、背景紀錄、JPG、手機與失敗恢復驗證 |

## 專案外但實際使用的位置

| 絕對位置 | 功能 |
| --- | --- |
| `/home/hrc/.local/bin/cloudflared` | 已安裝的 Cloudflare 官方 ARM64 程式 |
| `/home/hrc/.cloudflared/cert.pem` | 瀏覽器授權後產生的帳號憑證；只供設定管理，勿公開 |
| `/home/hrc/.cloudflared/<UUID>.json` | 指定 Tunnel 的執行憑證，權限 600 |
| `/home/hrc/.config/systemd/user/uav-v2.service` | 已安裝的應用服務 |
| `/home/hrc/.config/systemd/user/uav-cloudflared.service` | Tunnel 設定完成後安裝的連線服務 |
| NetworkManager `uav-g3p` profile | `eno1`、192.168.144.10/24、不提供預設路由、接線後自動連線 |
| systemd journal | 服務執行與登入成功／失敗日誌，不記錄密碼 |

舊版參考來源為 `/home/hrc/xin_uav/orin/real-time_bag_rtl/real_time_new_camera_orin/`，模型来源為 `/home/hrc/Downloads/`。舊專案未修改，新版執行不需要這些來源目錄。
