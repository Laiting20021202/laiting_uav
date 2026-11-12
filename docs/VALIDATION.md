# 本次部署驗證

## 2026-10-09 v5 預設、即時紀錄與 GUI 信心門檻

- 新增 `models/president_cup_v5/registry.json`（`president-cup-v5`）與 `models/competition_5class_v1-3/registry.json`（`competition-5class-v1-3`），分別指向 `best.pt` 與 `weights/best.pt`。直接讀取兩份權重，確認五種類別一致、訓練尺寸均為 1536；兩份 SHA256 均與 v5 model card 的新模型／基礎模型雜湊相符。
- 兩個模型皆在本機 Jetson 完成 CUDA／FP16 warmup 及空白 1920×1080 影像推論，配置保留完整五類。此項驗證載入相容性，不是飛行辨識精度量測。
- 啟動預設改為 `president-cup-v5`；`detection.vehicle_conf` 從 0.60 降為 0.30，依隨附 v5 建議先採用 30%；`record_after_s=0.0`，首次辨識即保存照片與事件，同一段連續追蹤只記一筆，漏檢再出現重新紀錄。清除後仍拒絕在清除前擷取的影格，下一張新影格辨識到即重新紀錄。
- GUI 新增百分比輸入、滑桿與「套用信心值」，範圍 1–100%。受登入、同源與 CSRF 保護的 `POST /api/detection/confidence` 先原子儲存設定，再更新每次推論使用的門檻。保存失敗保留原門檻；切換模型／重啟保留，`--config` 指定的設定檔亦正確保存。框上分數仍是模型實際輸出。
- 模型 API 額外回傳啟動預設，GUI 在模型尚未 warmup 時也會選到 v5，避免清單首項被誤認為預設。狀態輪詢保留尚未套用的信心值編輯。
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_detection_confidence.py tests/test_detection_storage.py tests/test_records.py tests/test_runtime_models.py tests/test_security.py tests/test_auth_restart.py`：**68 項通過**。涵蓋首次保存、連續追蹤去重、清除競態、門檻範圍、權限、保存失敗、下次推論更新、自訂設定檔與登入保留。
- `tests/browser_detection_settings.py`：Chromium 通過新模型選項、warmup 期間預設選擇、百分比／滑桿同步、套用、重新整理、未套用編輯保留、越界輸入拒絕、保存失敗後重試及 390px 手機版；JavaScript 例外為 0，測試資料與實機設定隔離。
- 正式服務已重啟並確認 v5 已載入、`model_loading=false`、`model_error=null`、門檻 30%、等待 0 秒；實際登入及 CSRF 保護 API 保存成功，原登入跨重啟保留。飛控在線且未解鎖；相機在部署前後均未連上（RTSP 連線逾時、JPEG 503），因此尚未取得本次實機影像／飛行辨識驗證。臨時驗證帳號已登出並移除。
- 正式 HTTPS 網頁 Chromium 驗證通過：v5 預設選項、兩個模型皆可選、信心值 30% 與實際「套用信心值」保存、390px 手機不溢出；JavaScript 例外為 0。最終取樣與畫面保存於 `final-live-validation.json`、`public-settings-desktop.png`。
- 原始檔備份：`/home/hrc/.local/state/laiting-uav/backups/2026-10-09-models-immediate-recording/`。部署取樣：`data/maintenance/20261009-models-immediate-recording/`。


## 2026-10-02 切換總統盃 v4 精修模型

- 使用指定目錄 `models/president_cup_v4_refined/best.pt`。直接載入權重確認五種類別 `civilian_vehicle`、`light_tactical_vehicle`、`medium_tactical_vehicle`、`clouded_leopard_armored_vehicle`、`amphibious_assault_vehicle`，以及訓練尺寸 1280；新增對應 `registry.json`，避免沿用舊模型的類別篩選而漏掉新類別。
- 正式服務透過受登入與 CSRF 保護的模型切換 API 載入 `president-cup-v4-refined`，warmup 成功後才替換。確認即時推論及 JPEG 恢復後，設定 `config/settings.json` 的啟動預設為同一模型。服務 boot ID 保持不變，沒有重啟相機、飛控或中斷飛行軌跡工作執行緒。
- 5 次連續正式狀態／影像取樣皆成功，預覽序號持續增加，推論約 224–230 ms，影像資料年齡 0.25–0.45 秒；相機、飛控在線，沒有模型錯誤。信心門檻維持 0.60，目標連續 2 秒紀錄規則沿用既有設定。此為載入及執行驗證，未重新量測模型辨識精度。
- `tests/test_runtime_models.py`：2 項通過，包含新模型載入失敗時保留舊模型。驗證用臨時帳號已登出並移除。
- 原設定與驗證結果保存於 `/home/hrc/.local/state/laiting-uav/backups/2026-10-02-model-v4-refined/`。

## 2026-10-02 即時地圖與手動飛行軌跡

- 新增 Leaflet 即時飛機位置、跟隨／完整航線檢視，以及「飛行數據」區的開始、停止、JPG 下載。GPS 至少 3D、位置與 GPS 訊息皆新鮮才顯示為即時；離線或過期時保留灰色最後位置。GPS 缺失、飛控重新連線、取樣延遲會斷開路段。
- 主機獨立工作執行緒每秒保存有效點，與影像推論和瀏覽器開關無關。預設不紀錄；停止後不再加點，舊頁面重送停止不會停止下一次紀錄。資料保存在獨立 SQLite，重啟保留已寫入的點並結束當次紀錄。
- JPG 由瀏覽器產生，1400×1000，範圍包含完整已完成軌跡、底圖、起終點、台灣時間、點數與已紀錄距離。保留 OpenStreetMap 出處；模擬紀錄加註非真實飛行。[實際匯出範例（合成軌跡）](flight-track-example.jpg)。
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests`：**141 項通過**。瀏覽器測試發現 Leaflet 清除圖磚時使用內嵌 GIF，因此只在 CSP 的圖片來源補允許 `data:`；腳本及樣式仍限同源。此修改後重跑 `tests/test_security.py tests/test_flight_tracks.py`：**45 項通過**。
- `tests/browser_flight_tracks.py` 與 `tests/browser_flight_tracks.py --real-tiles` 都通過。前者使用固定圖磚，後者實際連到 OpenStreetMap；驗證位置更新、未開始不紀錄、離開網頁仍取樣、GPS 失效不記點、恢復分段、停止後凍結、實際 JPG 格式／尺寸／路線像素、重新整理恢復、390px 手機不橫向溢出、連續多次紀錄、底圖失敗提示及無 GPS 時不提供空白 JPG。JavaScript 例外與 CSP 違規皆為 0。
- 套用前確認實機飛控連線、心跳新鮮且未解鎖；16:27:57 重啟 `uav-v2`。16:28:00 飛控恢復，16:28:18 相機恢復，後續 JPEG API 回 200、預設模型 `road-vehicle-tank-v2` 正常。原有登入經重啟仍可讀取受保護 API。
- 正式 HTTPS GUI 驗證完成：登入、相機、飛控、地圖底圖、開始按鈕及軌跡 API 正常，JavaScript 例外為 0。正式紀錄維持關閉，沒有寫入測試軌跡；驗證帳號已登出並移除。實機當時 GPS `fix_type=1`，GUI 正確顯示「GPS 無效或已過期」，沒有把未定位座標當成即時位置。尚未執行真實飛行航線精度驗證。
- 底圖與完整 JPG 需要外網；底圖失敗不影響主機記錄 GPS，JPG 下載提示重試。主機服務重啟後須手動重新開始紀錄。
- 修改前主要原始檔備份：`/home/hrc/.local/state/laiting-uav/backups/2026-10-02-flight-map/`。

## 2026-09-21 模型 v2、經緯度顯示與相機故障恢復

- 預設模型已改為 `models/road_vehicle_tank_v2/best.pt`（registry 名稱 `road-vehicle-tank-v2`）。實際讀取權重確認五種類別 car／van／truck／bus／tank，採訓練記錄的 1024 輸入尺寸；在 Jetson 實機載入及推論成功。
- 目標紀錄卡片與新保存的完整 JPG 增加 WGS84 緯度／經度（7 位小數），保留 TWD97 與 CSV 的既有經緯度欄位。JPG 的 EXIF ImageDescription 亦包含兩種座標。無有效定位時明示不可用；歷史 JPG 保持原樣。
- 本次檢查既有 9 筆實際紀錄皆為 `gps_fix_below_gate`。設定仍要求 RTK Fixed（6），相機內參尚未校正，亦未設定地面高度。新增顯示不代表這些紀錄已取得有效目標位置，沒有使用飛機座標替代。
- 16:37:37 相機曾成功連線；後續六次 RTSP 定期更新成功，但 17:05:28 仍出現完整影格逾時，控制持續回覆 -21。診斷時 ping 無丟包、控制端可回讀編碼設定、`app_status=idle`；停止預覽回 -14，啟動預覽回 -21，證明單純重連主機串流不足以恢復此次相機故障。
- 使用者重新上電後，17:24:08 重新套用並驗證低負載編碼設定，17:24:11 取得 1920×1080、約 10 FPS 的實機影格。僅能確認重新上電已恢復本次故障，不能把此視為永久修復相機韌體。
- 補上 G3P 控制握手失敗的 socket 關閉；設定讀取被拒絕時保留錯誤碼，避免將拒絕回覆誤判成缺少設定而執行 STOP_VF。相機控制恢復失敗後改為 30／60／120／240／300 秒退避，RTSP 繼續嘗試；連續三次 -21 提示相機重新上電，有效影格到達時清除故障狀態。手動相機重連可跳過退避等待。
- 新增真實 TCP 重複拒絕握手／恢復測試，確認 socket 關閉且按需控制器停止後仍可再次啟動；合成 FFmpeg 測試確認真實影格回來後解除故障提示。這些是軟體恢復測試，不代表實機韌體永不斷流。
- 最終服務版本 17:30:32 接回影像；17:34:32 定期更新，17:34:34 接回（約 2.2 秒）。登入後每 5 秒讀取實機狀態與 JPEG；最終版本首次有效影格後 73 次取樣、0 次失敗、73 張不同影格，約 363.2 秒。整輪 131 次取樣中的 9 次非就緒狀態出現在本次部署服務重啟／模型載入期間，未算成相機自行故障。最終模型為 road-vehicle-tank-v2、相機約 10 FPS；公網 health 與最新 JS 均回 200。
- 實機取樣保存於上述備份目錄的 `live-verification.json`；暫時驗證帳號已登出並移除。有限時間觀察不能保證相機永久不再卡死。
- 完整 `pytest` 共 **104 項通過**。
- Chromium 桌面／390px 手機紀錄頁通過：WGS84 數值、完整 JPG／EXIF、分頁、刪除與重新整理；沒有 JavaScript 例外。測試資料庫與帳號獨立於實際紀錄。
- 控制錯誤碼與預覽命令對照來源：[Ambarella RemoteCam CmdChannel](https://github.com/PeterPan506/RemoteCam/blob/master/app/src/main/java/com/ambarella/remotecam/connectivity/CmdChannel.java)。持續 -21 的相機韌體問題仍需廠商修正，或使用可受控的相機供電恢復設備；本次沒有刷寫韌體或接入供電控制器。
- 變更前原始檔保存於 `/home/hrc/.local/state/laiting-uav/backups/2026-09-21-model-camera/`。


## 2026-09-21 飛控 USB 斷線診斷與重連補強

本次檢查時，Linux 核心記錄 AP6 所在的 `1-2.4` USB 埠在 15:16:45–15:17:54 多次發生 `device descriptor read/64, error -110`、`device not accepting address, error -71`，最後回報 `unable to enumerate USB device`。15:18:57 才成功建立 `ttyACM0`；既有服務於 15:18:59 自動接回 system 1／component 1。這次不可用的直接原因是 USB 裝置未成功枚舉，無法僅憑紀錄區分線材、接頭、Hub／供電或飛控韌體問題；恢復發生在本次程式修補部署之前。

裝置辨識恢復後，已確認只有正式 UAV 程序開啟 `/dev/ttyACM0`、udev 有 `ID_MM_DEVICE_IGNORE=1`、AP6 的 `power/control=on`／`runtime_status=active`。沒有證據顯示這次是 ModemManager 搶占或飛控 USB 自動休眠，因此沒有修改這些設定，也沒有重置 USB Hub。

另外發現並修正重連程式的兩個缺口：

- 為真實序列埠設置 1 秒寫入逾時；pymavlink 將寫入失敗吞掉並設置 `portdead` 時，明確轉入重連。
- 關閉故障連線若拋出例外，記錄後繼續重試；在關閉前先使連線及時間戳失效，避免清理失敗終止唯一的重連執行緒。

測試與實機驗證：

- 修補前，注入關閉例外的真實 UDP 重連測試，以及 `portdead` 寫入錯誤測試均失敗，重現執行緒退出／錯誤未被處理。
- 修補後執行 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_telemetry.py tests/test_watchdog.py tests/test_geo.py`，23 項全部通過，耗時 20.42 秒。包含真實 Linux PTY／pySerial 的連續三次裝置消失、符號連結改指不同 tty 後自動恢復，以及心跳過期、關閉例外、來源隔離、定位新鮮度與 watchdog 檢查。PTY 測試模擬裝置拔插，不是實體 USB 線材測試。
- 15:23:11 重啟正式 `uav-v2` 套用；15:23:14 飛控連線成功，15:23:31 相機恢復 1920×1080。既有登入保留、模型載入完成。
- 在飛控回報未解鎖的狀態下，透過正式 API 執行一次飛控重連。過程中先確認連線與舊時間戳失效，1.02 秒內恢復心跳、GPS 位置與姿態；相機保持在線。沒有發送解鎖、模式切換或飛行命令。
- 15:23:38–15:26:38 連續觀察 180 秒、每 10 秒取樣，共 19 筆；飛控與相機全部在線，飛控成功連線次數維持 2，沒有額外重連。心跳／GPS 位置／姿態的最大資料年齡分別為 0.616／0.093／0.051 秒；恢復後沒有新的 USB 核心訊息，服務 `NRestarts=0`。
- 使用 Chromium 開啟正式 HTTPS 網站，確認畫面顯示「已連線／遙測接收中」及「即時影像」，JavaScript 例外為 0。驗證用臨時帳號與登入 session 已清除。

原始程式／文件備份、核心 USB 紀錄及實機重連結果保存於 `/home/hrc/.local/state/laiting-uav/backups/2026-09-21-flight/`。這次修補提高程式在故障後恢復的能力，不能消除硬體 USB 枚舉錯誤，也不能保證永久不斷線；若同類核心錯誤重現，仍需實際檢查線材、接頭及供電。

## 2026-09-16 網卡優先與自動啟動補強

依使用者指定，將 DWM-222W 行動網卡設定永久保存為自動連線、profile 優先度 100、IPv4／IPv6 route metric 100、`autoconnect-retries=0`（持續重試）。五個既有上網 Wi-Fi profile 的 IPv4／IPv6 metric 明確保存為 600。網卡 profile 無使用者登入限制；`NetworkManager`、`ssh`、`uav-v2`、`uav-cloudflared` 都是 enabled，且 hrc 的 linger 檔案與兩個 user service 的啟動連結均存在。

目前 IPv4 路由維持經 `enxac0033aa9633`、gateway `192.168.125.1`、source `192.168.125.100`。NetworkManager 不支援對已啟用的連線 reapply `autoconnect-retries`，因此持續重試設定保存於 profile，供後續啟動／自動重連使用；沒有為套用設定斷開網卡。非機密設定備份為 `/home/hrc/.local/state/laiting-uav/backups/2026-09-16/network-priority.json`。

另以真正的 `systemd --user` 暫時服務，執行正式 `scripts/tunnel_supervisor.py`，只在隔離服務內模擬路由和 Tunnel 子程序；驗證 7 項全部通過：

1. 開機沒有路由時服務保持 active，等候網路而不反覆啟動子程序。
2. 行動網卡路由稍後出現時，自動啟動子程序。
3. 對測試子程序送出 SIGKILL 後，systemd 的 `NRestarts` 增加並自動重新啟動。
4. 改為 Wi-Fi 路由時，終止旧程序並建立新程序。
5. 完全失去路由時，回收失效程序並持續等待。
6. 網卡路由恢復後，重新啟動子程序。
7. 正式 `uav-v2`、`uav-cloudflared` 的 PID 保持不變。

測試暫時服務及檔案已清除，結果保存於 `/tmp/uav-autostart-verification.json`。正式公開 `/healthz` 回 200。這是實際 systemd 自動恢復測試，路由變化為模擬；沒有重新開機或實際拔插網卡，因此完整冷開機時間仍待實機確認。

## 2026-09-16 開機 SSH／公網網站不可用

本次直接檢查 Jetson 的開機紀錄（boot ID `aaee260d7f77410c8342ba6f70a9b7d6`），以 journal 的 monotonic 秒數判讀，避免 NTP 校時干擾：

- 第 12.39 秒 SSH 已監聽 22；第 15.29 秒 Uvicorn 已監聽本機 8080。`ssh`、`uav-v2`、`uav-cloudflared` 都已 enabled，`Linger=yes`；不是需要登入桌面才啟動。
- 第 17.46 秒 Wi-Fi 取得 `192.168.0.237`；第 19.34–21.96 秒 cloudflared 透過 Wi-Fi 註冊四條 HTTP/2 連線。
- 第 21.78 秒行動網卡取得 `192.168.125.100`；第 22.57 秒 NetworkManager 將預設出口切到行動網卡。原 TCP 連線仍以 Wi-Fi IP 為來源，`ip route get <edge IP> from 192.168.0.237` 卻走行動網卡。`ss -tinp` 顯示多次重傳、120 秒 RTO、數分鐘未收到回應。
- 故障當下本機 `/healthz` 正常，公開網址 HTTP 530、本文 `error code: 1033`；Cloudflare SSH 回 `websocket: bad handshake`。cloudflared 本機 `/ready` 與 metrics 仍報 4 條連線，但管理查詢 `tunnel info` 明確回報沒有有效連線，證實本機 ready 不能識別這種卡住。
- 重新啟動 Tunnel 後，四條 socket 均改用行動網卡 IP，公開 `/healthz` 恢復 200。原版停止程序也曾卡住，需等 systemd 原本的停止期限才完成。

修正已部署：Tunnel 的 user service 以 `nm-online -s -q -t 30` 等待 NetworkManager 初始化，再透過 `scripts/tunnel_supervisor.py` 等候路由穩定 4 秒。每 2 秒查詢 IPv4／IPv6 出口的介面、閘道及來源 IP，改變時重建 Tunnel；無路由時等待，恢復後自動啟動。子程序無法正常停止時 5 秒後強制結束，整體 service 的 `TimeoutStopSec=15`。新增 `scripts/services.py --tunnel-only`，可獨立套用而不重啟相機／飛控。

驗證結果：

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_tunnel_supervisor.py`：7 項通過。使用真正子程序與模擬路由變化，涵蓋離線開機、介面／閘道／IP 變化、離線後恢復、路由穩定等待、非零退出與無法正常停止的強制回收。
- 產生的 Tunnel unit 通過 `systemd-analyze --user verify`，正式服務維持 enabled、active。10:04:45 開始等待穩定出口，10:04:49 啟動 cloudflared，第 1 條連線於 10:04:50 完成、四條於 10:04:53 完成。
- 套用後公開 `/healthz` 與 `/login` 均 HTTP 200；Cloudflare SSH 收到 `SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.15` banner。沒有使用 Linux 密碼／金鑰完成 shell 登入的測試。
- `uav-v2` 主程序維持 PID 1654，`NRestarts=0`；此次沒有重啟影像或飛控程式、修改網路優先順序或重新開機。
- 舊 Tunnel unit 備份：`/home/hrc/.local/state/laiting-uav/backups/2026-09-16/uav-cloudflared.service`。

限制：尚未以真正重新開機或拔插網卡量測修正後的端到端恢復時間。網卡、行動網路及 DNS 本身仍需完成初始化；沒有承諾通電立即可連線。此次 SSH 公網驗證針對 `ssh-uav.laiting1202.com`；未取得使用者實際連線指令，未驗證另一台電腦經區網／WireGuard 登入。

## 2026-09-15 總統盃多高度模型部署

- `models/president_cup_multialtitude/best.pt` 的 SHA256 為 `8a346c0c5bf842c8c35a01764da72247a9c3dec9f1b54412b1320a657846be20`，與隨附 `model_card.json` 一致；Ultralytics 讀取結果為 YOLO11m detect、類別 0 `vehicle`、類別 1 `tank`、輸入 1280。
- 已新增分組 registry 並將開機預設切換為 `president-cup-multialtitude`。正式服務重新啟動後完成 CUDA/FP16 warmup；兩次 `/api/status` 驗證均回報模型名稱正確、`model_loading=false`、`model_error=null`，實機推論約 337–377 ms，相機約 9.8–9.9 FPS且飛控在線。
- 模型 registry 與路徑測試 2 項通過。驗證用臨時帳號已刪除，未保存明文密碼。

## 2026-09-15 登入逾時與頁面恢復修正

- 舊程式的登入是固定 8 小時期限，持續觀看不會延長；`/api/me` 首次請求若超過 6 秒，前端初始化結束，後續影像與遙測輪詢不會啟動。兩處均已修正。
- 目前改為 8 小時閒置逾時，每次已通過驗證的網頁／API 請求同步延長伺服器 session 與瀏覽器 cookie；相機暫時無影像的 503 也能續期。公開靜態檔、被拒絕的操作及登出不續期；修改密碼和真正閒置到期仍會使登入失效。
- 影像、遙測、登入資料、事件及模型清單獨立更新與重試；單次 401 會共用一次登入確認，只有 `/api/me` 明確回覆登入失效才導回登入頁，並顯示原因。請求期限 12 秒，恢復連線／回到分頁時立即重試。
- `pytest` **75 項通過**。測試覆蓋連續觀看跨越原始期限、瀏覽器 cookie 續期、相機 503、閒置到期、禁止不合法續期、登出 cookie 刪除、重啟後登入恢復、相機 STOP_VF 後用新 session 套用編碼設定，以及預防性 RTSP 更新保留最後影格。
- `tests/browser_timeout.py` 使用獨立臨時帳號、loopback demo 與 Chromium，將真實 session／cookie 壽命縮短成 **4 秒**，讓首次登入資料請求超過 **12 秒**，仍保持登入、影像、遙測，且登入資料自動重試成功。另通過事件／模型 503 後恢復、並發 401、代理 HTML 401、短暫離線後恢復、閒置 6 秒後到期、重新登入恢復畫面、登出撤銷；JavaScript 例外為 0。未用合成影像測試宣稱實機相機穩定。
- 11:00:30（Asia/Taipei）首次套用後接回 1920×1080 相機與飛控。其後依使用者追加的重啟問題，加入權限 600 的私有 session checkpoint；儲存 token 雜湊、CSRF、credential 與實際到期時間，啟動時僅恢復尚未到期且帳密未變的登入，登出立即落盤撤銷。活躍登入最多每 60 秒寫入一次，正常停止時儲存最新期限。
- 11:24 公網 Chromium 實際點擊「重啟影像辨識服務」：回覆 202、boot ID 改變、原登入及 CSRF 保留、模型重新載入、影像恢復、按鈕重新可用、JavaScript 例外 0。`tests/browser_service_restart.py` 通過；Cloudflare Tunnel PID 一直維持 `72134`。

### 相機自行斷流的調查

- 11:01 起的公網監看前約 7 分鐘均正常；11:08:01 日誌記錄使用者按下重啟服務，隨後 502／401 屬該次舊版重啟造成，不能算成相機自行故障。後續部署已保留重啟後登入。
- 11:13:20 真正出現 `Camera frame timeout`，當時服務與飛控持續運作，相機仍可 ping；後续探測回傳 H.264 寬高 0×0，預覽恢復逾時／被拒絕。單独停止主機取流再送預覽啟動／停止命令仍回 -14，不能將其解讀為網頁登入逾時，也沒有證據可斷言為低電量、過熱或其他特定硬體原因。
- 查到控制程式會丟棄非目前命令的通知，並在等待與編碼器恢復期間不讀 socket；此外，`258/STOP_SESSION` 在實機會直接結束 RTSP。現已持續接收通知、對收到的 `0x701/SESSION_HOLD` 回覆確認，關閉控制 socket 時一律不發送 STOP_SESSION。
- 11:28:21 至 11:48:17 的公網 Chromium 實機監看重現了固定型態：前 10 分 47 秒持續有新影格，11:39:13 首次取圖失敗，11:39:18 完整影格逾時。當時網站 boot ID 未變、飛控持續更新、Ethernet 沒有 link flap 或錯誤、相機 TCP 554／7878 仍可達，控制狀態仍為 `vf`；RTSP 交握成功但 H.264 寬高為 0×0。這將問題定位在相機內部影像／預覽工作階段，而非網站登入、Tunnel 或網路斷線。
- 原程式每 15 秒讀取 `app_status`，後來依 [Ambarella RemoteCam 控制程式](https://github.com/PeterPan506/RemoteCam/blob/master/app/src/main/java/com/ambarella/remotecam/connectivity/CmdChannel.java) 改為每 8 秒送出 `0x10000001` 心跳與遞增 `msg_index`。兩種方式均收到正常回覆，但實機仍斷流，因此它們不是根因修復，現已取消正常取像期間的持久控制 session。
- 舊恢復流程送出未由協定定義的 `msg_id=259` stop/start 參數，故障後曾把相機狀態改成 `idle`。現只用協定定義的 `STOP_VF(260)` 與 `RESETVF(259)/none_force`。實測 STOP_VF 會使當前 token 失效，設定流程因此會關閉舊 socket、另開 session 後再設定與啟動。
- 真實 TCP 模擬測試涵蓋通知與命令 ACK 同包、碎片化通知、`0x701` 回覆、失敗不發送 STOP_SESSION、STOP_VF 後重開 session、設定回讀及取消卡住的控制連線。
- 13:09:39 使用者再次重新上電後接回 1920×1080 影像。13:10:31 開始的第二輪公網監看在 524.5 秒首次失敗，13:19:26 記錄完整影格逾時；當時 108 次取樣中前 101 次成功且影格均不同，服務 boot ID、飛控、控制 session、`vf` 狀態與主動心跳均正常。相機也明確對獨立心跳回覆成功，證明主動心跳仍不足以阻止 H2 編碼器約 9–11 分鐘後卡死。
- 相機識別為 Ambarella H2、API 4.1.0、韌體 `JDHF201-20251120.01`。`cam_stb` 軟體待機回覆 `OPERATION_UNSUPPORTED(-23)`，UDP discovery 可回覆但不能重啟編碼器；已進入 `SYSTEM_BUSY(-21)` 後仍需重新上電。
- 15:29 部署「不維持控制 session、FFmpeg 參數對齊舊系統」後，相機先前已卡死，重開機後才恢復。16:02 查到重開機會恢復高負載設定：4K/30 主串流、EIS 開啟、1080p/2 Mbps 次串流。16:07 部署設定流程；服務先 STOP_VF、重開控制 session，再成功套用並回讀 `normal_record`、EIS off、1080p/30 主串流、1080p/1 Mbps 次串流，16:08:11 接回 1920×1080 約 10 FPS 影像。正常取像時控制 socket 已關閉。
- 低負載設定仍未消除相機韌體期限：16:16:26 再次記錄完整影格逾時。這次 `RESETVF` 成功，16:16:48 已自動接回同解析度，服務與相機皆未重新上電。由開始無新影格估算到恢復約 30 秒，由程式確認逾時到恢復約 22 秒；證明恢復路徑有效，也證明降低負載不是根治。
- 最終加入每 240 秒的 FFmpeg／RTSP 預防性 session 更新，在已觀察到的 8–11 分鐘故障點前換新連線；預防更新保留最後影格、不中斷 G3P viewfinder，5 秒仍無影格才執行 RESETVF。網頁端把影格請求期限縮短為 5 秒，短暫失敗時保留最後有效畫面並明確標示秒數。
- 16:24:36 第一次預防更新實際執行，16:24:39 新 RTSP session 接回；約 3.2 秒內沒有 G3P 重置、服務重啟或相機錯誤，公網 Chromium 的影像元素保持可見。
- 16:28:39 第二次預防更新執行，16:28:42 接回，約 2.1 秒；總運行時間已跨過前一輪約 8 分鐘的故障點，相機沒有再觸發影格逾時，連線次數為 3、預防更新次數為 2。
- 16:15:21 同時捕捉到另一個獨立問題：Cloudflare QUIC 回報 `failed to accept QUIC stream`，四條 Tunnel 連線重新註冊，公網瀏覽器曾收到 502／逾時。本機相機在該時段仍持續出圖。Tunnel 已依 cloudflared 自身 precheck 建議固定 `protocol: http2`；16:19:31–16:19:33 四條 HTTP/2 連線註冊，公網 health 回 200。
- 舊 `/home/hrc/xin_uav` 並非穩定基準：57 份含 G3P 事件的 ROS 日誌中有 56 份出現 `ffmpeg frame read timed out`；其中一份 66 分鐘日誌有 307 次失聯。舊程式用一秒重開 FFmpeg，會讓介面看起來較快恢復，但相機端一樣曾長時間卡住。
- 每 360 秒重置 viewfinder 的版本未能完成正向驗證，且會主動製造畫面中斷，已移除。最終版本只在啟動或偵測到串流故障時使用 G3P 控制，RTSP 讀取流程則沿用舊系統已在現場使用的低延遲 FFmpeg 參數。

以下為 2026-09-14 的歷史驗證紀錄。

日期：2026-09-14（Asia/Taipei）。主機：Ubuntu／ARM64 Jetson，Python 3.10.12。

## 軟體與環境

| 項目 | 實測 |
| --- | --- |
| 自動測試 | 52 項通過；涵蓋登入、安全、確認追蹤、儲存、定位、雲台、FFmpeg、MAVLink UDP、USB 身分識別、watchdog、零尺寸探測及 G3P 拒絕碼 |
| Chromium 152 | 真正瀏覽器登入、合成即時影像、遙測、事件、CSV 下載、登出；零 JavaScript 例外 |
| 手機排版 | 390×844，無整頁水平溢出 |
| TensorRT 模型 | `visdrone-engine` 正常 warmup；1920×1080 全黑測試影格單次推論約 286 ms，非整體直播 FPS 或準確率保證 |
| PyTorch 模型 | `visdrone-pt` 正常 warmup 與推論，這台主機明顯較慢 |
| 網頁模型切換 | 正在執行的服務實際透過 API engine → pt → engine，成功；目前使用 engine |
| FFmpeg | 真正解碼合成影片，最新影格與遙測配對、影片結束清除舊畫面、自動重連、乾淨停止 |
| MAVLink | ALIGN AP6 已由 USB 實際連線，system 1／component 1；姿態、電量、模式持續更新；另以真正 loopback UDP 測試斷線／恢復 |
| systemd | `uav-v2`、`uav-cloudflared` enabled 且 active，`Linger=yes`；應用 watchdog 120 秒且持續收到通知；沒有實際重新開機或刻意觸發 watchdog 殺程序 |
| 相機網路 | G3P `192.168.144.135` 的 RTSP 554／控制 7878 可達；`uav-g3p` Ethernet profile 自動連線、不帶 default route |
| 真實影像 | 曾實際解碼 H.264 1920×1080，取像約 10 FPS；但後續兩次約十分鐘後斷流，長時間穩定性尚未通過，詳見下節 |
| GUI 恢復功能 | 公網 Chromium 實際點擊相機重連、飛控重連、辨識服務重啟；兩設備皆恢復，重啟後重新登入，Cloudflare Tunnel PID 未變 |
| 上網路由 | `1.1.1.1` 經 Wi-Fi `wlP1p1s0`，未經既有 `wg0` VPN |

## Cloudflare 公網驗證

- 已授權、建立 named tunnel `laiting-uav-v2`，並建立 `uav.laiting1202.com`／`ssh-uav.laiting1202.com` 的 DNS。
- Cloudflare Tunnel 的 QUIC 與 HTTP/2 連通性檢查通過，服務持續執行。
- HTTPS `/` → 303 導向登入，`/login` → 200。
- 未登入 `/api/status`、`/api/frame.jpg`、`/api/video.mjpg`、`/api/events.csv` 均回 401。
- `laiting` 與 `hrc` 兩組指定帳密，均在實際 Cloudflare HTTPS 通道驗證登入 200、讀取實機狀態、登出 200，登出後恢復 401。
- 登入資料使用 Secure／HttpOnly／SameSite cookie；私有回應含 no-store，沒有把模型檔或憑證目錄掛成公開靜態路徑。
- SSH 經 `cloudflared access ssh` 取得 `OpenSSH_8.9p1` banner，並比對本機公開 SSH host key 成功。測試時本機既有客戶端金鑰未獲允許，**沒有宣稱已用 Linux 帳密完成互動式 shell 登入**。使用者可用自己原本的主機 SSH 帳密／金鑰登入。
- 尚未設定 Cloudflare Access Email 身分政策；SSH 現在由主機原有 SSH 驗證把關，可依 `DEPLOYMENT.md` 額外設定 Access。

建立 DNS 後，主機原本使用的 UDP DNS resolver 曾保留 NXDOMAIN 快取。後續已確認一般系統 DNS 恢復正常：`getent ahostsv4` 可解析兩個子網域，直接 HTTPS 登入頁回 200，未登入 API 回 401。另以未設定 host resolver override 的 Chromium 經公網完成登入、實機狀態顯示與登出，零 JavaScript 錯誤。沒有修改 `/etc/hosts`、系統 DNS 或關閉 HTTPS 憑證驗證。若個別瀏覽器仍保留舊快取，可重新啟動瀏覽器後使用完整 HTTPS 網址。

## 連線與恢復修正

- 這台主機的 FFmpeg RTSP 不接受原先的 `-rw_timeout`，會在開流前直接退出。現在讀取本機 RTSP demuxer 能力，使用舊版 `-stimeout` 或新版 `-timeout`；已用實體 G3P 驗證。
- 飛控固定使用 `/dev/serial/by-id/usb-ALIGN_AP6-M490-ds_330032000D51333331333534-if00`，每次重試重新尋找相同 USB 身分，不依賴 `ttyACM0` 的編號。未出現 USB 時持續等待，回來後重新取得 heartbeat 與設定遙測頻率。
- AP6 USB 也會轉送其他地面站心跳；已排除 GCS，並固定 system 1／component 1，避免重連後錯認來源而沒有姿態資料。
- 相機探測、解碼、飛控心跳都有逾時與持續重試。重新連線時清除舊資料，丟棄舊相機連線的推論結果。一般硬體離線仍持續重試，不會被 watchdog 當成工作迴圈卡死。
- GUI 重啟使用固定 systemd timer，延後兩秒讓 HTTP 回覆完成；僅重啟 `uav-v2.service`。重連與重啟 API 需要登入、同源、CSRF，並限制連續操作。

## 實體中斷監看

2026-09-14 13:21:50 首次觀察到相機影格逾時，期間出現 RTSP 逾時與 `No route to host`。13:22:30 自動恢復影像，相機連線次數從 1 變為 2；期間沒有手動重連或重啟服務，應用 boot ID 與 Cloudflare Tunnel PID 均未變。飛控在這段時間持續收到姿態；這段紀錄只證明相機恢復，不代表飛控也曾完整斷電。

接著依使用者配合進行 USB 拔插：核心日誌在 13:24:51 記錄 AP6 `USB disconnect`，13:24:57 重新列舉 `ttyACM0`。應用日誌同步記錄序列埠斷開、等待 USB，13:25:00 自動接回相同 by-id 裝置、system 1／component 1，成功連線次數從 1 變為 2。從 USB 重新出現到連回飛控約 3 秒，後續姿態資料年齡約 0.01–0.03 秒；沒有執行手動重連或應用重啟。這證明實體 USB 消失後能自動恢復，無法單憑主機日誌判斷飛控其他電源是否也完全切斷。

兩次測試中應用 PID 保持 `160062`、Tunnel PID 保持 `72134`，systemd `NRestarts=0`。此次已驗證設備中斷後恢復；Linux 主機整機斷電後的開機自啟尚未實際斷電測試。

## 放置後失去影像的實機問題

13:32:51 相機發生完整影格逾時；重連時 ffprobe 仍正常退出，但回傳 H.264 `width=0, height=0`，原先長寬比計算出現 `ZeroDivisionError`。已加入尺寸檢查，並以「先回傳 0×0、再恢復正常影片」測試確認同一相機工作執行緒能自動恢復。

14:07–14:10 透過 Ethernet `eno1` 可 ping 通相機（約 2 ms），網卡無 RX／TX error；相機控制 7878 和 RTSP 554 也可達。相機卻持续沒有可解碼影像，預覽 stop／start 都回傳 `-21`。在暫停主機取流後獨立執行舊版 `camera_mode=normal_record` 與 stop／start，以及另一種 Ambarella 預覽指令格式，均未恢復輸出。因此不能把本次故障歸因於網頁登入、Cloudflare 或網路線不通，也不能宣稱軟體重啟已排除相機端問題。

相機設定回報 `Battery_level=battery:6`，但此機型欄位含義未驗證，使用者確認穩定供電且沒有低電量提示；不將該數字當成 6% 電量或故障原因。

14:11:18 相機有線 link down，14:12:56 網路恢復後自動取得 1920×1080 影像，期間飛控持續連線。14:13:42 另部署錯誤顯示與遙測逾時邊界修正，應用重新啟動後繼續監看。GUI 現在會明確標示相機為網路串流、主機服務重啟的範圍、未取得可解碼影像及相機端拒絕碼。

**長時間測試未通過：** 14:13:50 重啟後接回影像，14:23:35 左右再次停止出圖，14:23:45 記錄完整影格逾時。`browser_camera_soak.py --minutes 12` 實際觀察 724.4 秒，共 140 次取樣、97 張不同影格、43 次失敗、0 個 JavaScript 例外。第一次失敗時網頁仍回 200、飛控持續更新，JPEG 回 503；其後又出現 0×0 和相機控制拒絕／逾時。此結果不能寫成「已保證長時間正常」。

獨立 RTSP trace 顯示 OPTIONS／DESCRIBE／SETUP／PLAY 皆回 200，卻無可解碼的 H.264 尺寸。僅重啟相機專用 NetworkManager profile `uav-g3p` 未恢復影像；上網與 SSH 的介面未受影響。另測試相機列出的模式切換（沒有按快門或開始錄影），切換及還原命令均未取得回應，故不能確認相機端已完成切換／還原；正式恢复流程會先要求原設定 `normal_record`。需要完整品牌／型號或原廠協定，才能確認相機端遠端重啟與保活方式，不能以反覆重啟主機代替此確認。

## 曾嘗試的持久控制連線

17:11:34 無人機重新開啟後接回影像；17:17:42 開始獨立實驗，保持 TCP 7878 的控制 session 並每 15 秒讀取 `app_status`。17:23:35 已連續取像 721 秒，相機連線次數仍為 1，控制查詢持续回覆 `vf`；相較先前兩次約十分鐘斷流，這支持控制 session 保活缺失是原因。17:23:58 結束診斷 session 後，RTSP 立即 EOF，也證明此控制通道會影響串流生命週期。

初版加入 `uav/g3p.py` 維持單一控制 session 並讀取狀態，後續又加入正式協定心跳；兩者後來都在長時間實測中再次斷流。最終版本只保留短期設定／恢復控制，工作執行緒仍納入 watchdog，控制逾時不阻塞取像。

另發現模型已搬到 `models/preliminary round/`，但原 API 固定讀取已不存在的 `models/registry.json`，使前端初始化在啟動影像更新之前停止。已保留使用者的分組目录並支援各組 registry，權重與清單最終路徑仍限制於 `models/`。影像／遙測輪詢現在獨立啟動；在真正瀏覽器攔截模型 API 並回 503 後，仍確認畫面可見、飛控已連線且顯示模型清單錯誤。

## 已知待現場驗證項目

2026-09-15 最終公開站點連續監測 725.3 秒，共 121 次取樣、116 個不同影格、單一後端 boot ID、0 個瀏覽器 JavaScript 錯誤。期間完成 3 次預防性 RTSP 更新（約 3.2、2.1、2.0 秒），跨越先前約 8 分鐘的失效點後沒有再出現 `Camera frame timeout`，也沒有觸發 G3P 緊急復原或服務重啟。

監測結束後服務繼續運行，16:36:44 第 4 次預防更新、16:36:46 第 5 個 RTSP session 接回；後端 PID 維持不變、systemd `NRestarts=0`。

嚴格監測器另記錄 5 次失敗取樣：1 次是診斷用 `ffprobe` 額外建立第二條 RTSP 連線所造成；4 次是公開網址請求逾時，對應 cloudflared HTTP/2 連線於 16:28:18 至 16:28:32 重建。已顯示的相機影像在這些短暫中斷中會保留並繼續自動重試。

G3P 與 ALIGN AP6 均已做過實際連線測試。尚待現場多小時與實際飛行震動驗證。若相機整個網路堆疊停止回應、連 ARP 都消失，現有軟體無法替代硬體斷電重啟；需要可控電源繼電器或 PoE 電源控制，才能對此類硬體鎖死提供有界時間內的復原保證。室內測試時飛控回報 LOITER、未解鎖、約 24.6 V／92%，GPS fix 1、衛星 0；這表示遙測通暢，但沒有有效 GPS 定位。尚未做飛行影片辨識準確率、端到端影像延遲、弱網路頻寬與真正 GPS 定位誤差測試。

依使用者最後指定，鏡頭先假設垂直下視、影像方向跟隨機頭。相機內參未量測，`calibrated=false`；地面高度尚未設定，因此實機事件可以保存，但有效目標 GPS 必須在校正／高度資料齊備後才會產生。詳見 `GEOLOCATION.md`。

現有 torchvision 的 `image.so` 載入會出現主機原有版本警告；本系統用 OpenCV 讀影像，實測兩種模型均可 warmup／推論，沒有為此覆蓋主機 GPU 套件。pytest 出現 Starlette 對測試客戶端 httpx 的棄用提示，測試仍全部通過。

## 重現

```bash
cd /home/hrc/laiting_uav
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
.venv/bin/python scripts/manage.py doctor
```

瀏覽器測試先啟動 `--demo`，將 `UAV_TEST_USERNAME`／`UAV_TEST_PASSWORD` 以環境變數提供給 `tests/browser_smoke.py`。測試會更新 `docs/dashboard-demo.png`，密碼不寫入截圖與檔案。

`tests/browser_recovery.py` 使用相同帳密環境變數，預設連到公網 HTTPS；會實際重連硬體及重啟辨識服務，僅在允許短暫中斷畫面時手動執行。桌機與手機實機截圖只存於 `/tmp/uav-recovery-desktop.png`、`/tmp/uav-recovery-mobile.png`，不將實驗室影像加入專案。

`tests/browser_camera_soak.py --minutes 15` 使用相同環境變數，持續開啟公網實機頁面，檢查畫面可見、JPEG 是否成功、影像新鮮度及 JavaScript 例外；不發送硬體命令。報告與最後截圖分別存於 `/tmp/uav-camera-soak.json`、`/tmp/uav-camera-soak.png`。
