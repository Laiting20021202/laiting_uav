# 2026-10-07 USB 4G 反覆消失與開機恢復修正

本輪直接在 Jetson 上檢查。既有雙路徑服務、USB recovery、RF guard、DNS resolver 均為 enabled／active；使用者 manager `Linger=yes`，UAV 及 Tunnel 不依賴登入桌面才啟動。固定 USB／機上 Wi-Fi profile 的 autoconnect retries 都為 0（持續重試），Wi-Fi profile 已停用省電。原唯讀開機驗收服務為 disabled。

## 故障定位

開機 ID `15d15c2e-bdf3-48ba-ac21-c7336b8be8bf`，截至 monotonic 826.09 秒的核心快照保存 256 次指定網卡 USB disconnect、353 次重新列舉嘗試及 145 行 `error -71`。首次 USB disconnect 的來源 monotonic 在 59.305 秒（journal 收件時間約 59.304 秒），USB recovery 首次動作在 711.016 秒；沒有核心的指定卡 `reset high-speed USB` 紀錄。這是恢復動作前就已存在的 USB 列舉／傳輸故障。後續至 1311.490 秒的較長快照另見 [USB 列舉故障](INCIDENT_2026-10-07_USB_ENUMERATION.md#第二次開機仍復發)，不混計兩個觀測範圍。

此 boot 的指定設備為 `1286:4e31`／`200806006809080000`、USB 路徑 `1-2.2`；裝置世代快速改變，失敗包含 setup address、device descriptor、configuration 讀取及 CDC bind。14:22 原核心對照 boot 的同一張卡則在 `1-2.3`，不能用本段固定 port 對照後來的硬體；診斷程式已正式安裝每次取樣依唯一身份尋找實際 port 的修正。兩條路徑的外網探測固定介面，Wi-Fi 備援仍透過同一張卡的 LTE／SIM 上網；本機和公開 health 在檢查時均正常。不能把備援上網成功說成 USB 已修好。

指定卡 `power/control=on`，卡／上層 USB `runtime_suspended_time=0`。此段不支持 autosuspend 根因。`usbnet` 回補及 xHCI 修補核心均已載入，但此次錯誤仍復發。尚無足夠證據區分網卡韌體、USB 電氣條件或控制器缺陷。Linux 官方[錯誤碼說明](https://docs.kernel.org/5.15/driver-api/usb/error-codes.html)將 `EPROTO` 包含未回覆等多種情形，也提醒控制器通常不能區分這些原因。

原路徑服務在 monotonic 377.471 秒切回 USB，407.305 秒又切至 Wi-Fi；1016.502 秒再切回 USB，1034.317 秒又切至 Wi-Fi。這些既有服務的切換與復發支持需要更嚴格的 USB 穩定條件，不是新版本已驗證有效的結果。

## 本次已部署修正

`modem_path_manager.py` 增加探測前後世代核對、世代改變時重算連續健康，以及從最近 USB 恢復重新計算五分鐘冷卻。同次開機的路徑／冷卻保存在 `routing.json`，不同開機不沿用 monotonic 時鐘；不持久保存舊的連續健康判斷。

`uav-modem-paths.service` 改為 notify 並啟用 120 秒 watchdog，主循環同時檢查控制 socket 及 Wi-Fi 維護執行緒。`modem_rf_guard.py` 在固定機上 Wi-Fi 外網已驗證時略過 AT；共同 LTE 故障仍允許原 RF 恢復。`modem_recovery.py` 在 EPROTO 且 Wi-Fi 接手時，清楚記錄延後 reset。

新增 `network_boot_check.py` 及正式的 `uav-4g-boot-validation.service`，每次開機要求實際固定路徑、本機／公開網站及 Tunnel 連續健康 20 秒。每窗 180 秒，失敗後 30 秒重試；報告區分 USB 和 Wi-Fi，保存於獨立 `network-stability/` 子目錄。這是持續重試與每次驗收機制，不能保證設備或電信永不故障。

新增 `deploy_network_stability.py`，六個正式檔案採原子替換、SHA-256 核對、備份與失敗還原。部署不要求 USB 正在列舉，也不直接 reset 卡或重開主機。

## 驗證與部署狀態

- 首次擴大相關測試 281 項通過（3.20 秒）；完成最後操作前世代競態修正後，**最新版 292 項通過（3.33 秒）**，包含路徑、RF、CDC、世代／冷卻及開機驗收。兩輪不相加成測試總數。
- `systemd-analyze verify` 驗證新路徑／開機服務成功。輸出的 snapd／NVIDIA 既有 unit 警告與本輪修改無關。
- `deploy_network_stability.py --check` 成功，**13:39:55 已完成首次正式部署，其後完成最後競態修正並再次部署**。程式採原子替換，已核對 root 正式檔案與來源 SHA-256。首次備份在 `/var/backups/laiting-uav/network-stability-mw6p5pnl/`，最後備份在 `/var/backups/laiting-uav/network-stability-zpi9jvnt/`；最後路徑程式 SHA-256 為 `b23d4d4397a012fb7847c1f593266d46da0c998a84050582b4e1625bf0165605`。
- 本機執行唯讀 `network_boot_check.py --check`：當次 `active_path=wifi`、`local_ok=true`、`public_ok=true`、`tunnel_ready=true`；USB 當時的外網／閘道失敗。
- 路徑／USB recovery／RF guard 均 active，當時 `NRestarts=0`，watchdog 為兩分鐘；唯讀開機驗收服務 enabled，當次為 active／exited。
- 部署後同一 boot 的驗收從 **1644.120 秒**開始，至 **1667.270 秒**確認連續健康 20 秒；報告 `accepted=true`、`active_path=wifi`、`wifi_ready=true`、`usb_ready=false`，公開 health 正常。這是部署後運行驗收，**USB 未通過，亦不是新一次重開機驗收**。
- 已修正操作前最後世代競態：換路由前再次核對實際指定 USB 世代，拒絕把探測時的舊世代健康用於後來的設備。
- 已啟用唯讀 `uav-network-boot-observer.service`，保存每次開機最多 180 秒的 USB、機上出口、公開網站及飛控世代觀測。該服務不登入、不重啟網卡。早期固定 port 版本有 40 項工具測試；後續依唯一身份動態定位、在 stats／TLS 探測前後核對實卡 port／世代的新版本已正式安裝，root 與來源 SHA-256 為 `5adb60cccc5b9df7b4ef5eec80deebb1dbfa1e5100a2b09ce91c24debd4d8c94`，備份在 `/var/backups/laiting-uav/observer-dynamic-port-c9pfnhk_/`。46 項工具測試及較廣的 103 項管理頁／傳輸相關測試通過；部署端另重跑兩個測試檔共 56 項通過（2.02 秒），各輪不相加。報告依 boot ID 分別保存在 `/var/lib/laiting-uav-network/boot-observation/`，舊報告不因新修正而自動有效。
- **部署後已有真實暖開機驗收：** boot `ed1b68f4-bf17-4fea-9a18-da35ac00ca77` 在開機 88.040 秒、boot `86004921-9b83-4afa-8c8c-ccea24212d06` 在 181.524 秒確認機上 Wi-Fi／公開網站連續健康 20 秒。兩次 USB 均從短暫出現轉為失效，未通過 USB 連續 30 秒驗收；這是備援及自動啟動成功，不能稱為 USB 重開機驗收通過。原本「需使用者 sudo 驗證」的限制已不存在；沿用使用者既有授權完成本次部署。

## 後續 USB 核心對照與候選修正

使用者已完成 D-Link 本體拔下至少 10 秒再插回；180 筆後續觀測仍只有短暫列舉、USB 最後消失。14:22 另載入 NVIDIA 原核心的 boot `4ce360e8-1cf5-4a1e-82a5-488c9f01b438` 仍有 descriptor／configuration `-71`，同一張卡此時位於 `1-2.3`。因 port 改變，不能稱為嚴格單變量核心對照；舊 observer 固定 `1-2.2` 的缺席判斷亦不能當成此 boot 沒有列舉的證據。完整核心快照另外保存實際 port 的錯誤與飛控斷開紀錄。

已完成官方 f781 xHCI endpoint halt-cleanup 修正的獨立候選建置，保留既有 e765 回補；Image SHA-256 `4bf3a99e78cb4dc528a11813d2dad334dfe3dee0dd4be81cf992876352756e5e`，13,492 個內建 export CRC 完全相符，65,536 個原來源決策分支案例通過。此時僅完成建置／離線驗證，尚待正式載入與實機驗收。f781 修正已知錯誤後的恢復缺陷；現有證據尚未確認本次故障命中它，也不能宣稱它消除所有初始 USB 傳輸錯誤。實際整卡請求、冷拔插、暖開機、原核心及 f781 範圍見 [USB 列舉紀錄](INCIDENT_2026-10-07_USB_ENUMERATION.md)。

詳細命令見 [行動網路持續自動恢復](NETWORK_RECOVERY.md#2026-10-07-反覆斷線定位與穩定性修正)。原始核心／恢復／ModemManager journal 及摘要在 `data/maintenance/20261007-network-stability/`；快照不包含 profile 密碼或管理帳密。
