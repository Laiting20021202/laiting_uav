# 行動網路持續自動恢復

**AP6 飛控 USB 斷線另案：** 10/07 單次指定 AP6 USB reset 曾恢復完整 MAVLink 遙測，但後續開機再次出現 AP6 disconnect、endpoint STALL、descriptor／address 錯誤，最後無法列舉；尚未根治。後續唯讀排查保留網頁與 4G 運作，證據、手動地面恢復工具及 UART／USB 通訊建議見 [AP6 USB MAVLink 紀錄](INCIDENT_2026-10-07_AP6_USB.md)。

**AP6 最新選擇：改用硬體 UART＋MAVLink 2。** Jetson `/dev/ttyTHS1` 已核對，接線資料、候選設定與獨立驗收工具見 [UART 遷移說明](AP6_UART_MIGRATION.md)。目前等待 AP6 接頭／電壓／埠設定確認，尚未接線或切換正式 reader；網站與 4G 保持運作。

2026-10-07 更新。指定 D-Link DWM-222W 為唯一網際網路來源，分別處理 USB／CDC、NetworkManager、LTE 射頻、DNS 與 Tunnel 故障。**USB 4G 優先，Wi-Fi 備援仍使用同一張 D-Link 的 4G，不允許實驗室或其他外部 Wi-Fi。** 雙路徑、USB 消失等待、GUI 狀態及下節的世代／冷卻與開機驗收修正已部署；兩次後續真實暖開機的機上 Wi-Fi 連續健康驗收通過，USB 均短暫出現後失效，未通過 USB 驗收。下文既有實測數字保留原日期及範圍。相機 `eno1`、相機 profile 與路由不列入恢復操作；沒有加入自動整機重開、USB 控制器或 Hub 重設。使用者確認沒有外接共用 USB Hub，核心拓撲不能當成外接 Hub 的證據。

**已改善自動恢復，USB 錯誤的觸發原因仍未根治。** 10/06 17:17、17:18、17:33、17:56 仍捕捉到收發凍結及錯誤暴增；10/07 重開後亦有 USB 描述／位址設定失敗及反覆重新列舉。`usbnet`／xHCI 回補、正常取樣或一次恢復成功，都不能證明問題不會再發生。證據與限制見 [USB 修正紀錄](INCIDENT_2026-10-06_USB_FIX.md)、[重複 USB 故障紀錄](INCIDENT_2026-10-06_REPEAT_USB.md)及 [10/07 USB 列舉故障](INCIDENT_2026-10-07_USB_ENUMERATION.md)。

`-71` **並非今天才出現**：同 boot 校時錨定的 10/05 18:38:23 已有指定卡 descriptor `-71`，10/06 17:18 的 CDC rebind 也回傳同碼，早於晚間雙路徑及今天的恢復更新。昨天 17:00 的 e765 Image、`usbnet` 和 1.01.00 網卡韌體與今天先前失敗的開機相同。此先後關係不能排除既有修改影響故障頻率，也不能唯一決定原始觸發原因；實際套用與僅準備的變更時序見 [變更與錯誤因果核對](INCIDENT_2026-10-07_USB_ENUMERATION.md#-71-是否今天才出現是否由修改造成)。

## 2026-10-07 反覆斷線定位與穩定性修正

本次開機 `15d15c2e-bdf3-48ba-ac21-c7336b8be8bf` 的取樣截至 monotonic **826.09 秒**，保存 **256 次**指定網卡 USB disconnect、**353 次**重新列舉嘗試、**145 行** `error -71`；這些數字代表不同事件，不能相加當作故障總次數。第一個 USB disconnect 的來源 monotonic 在開機後 **59.305 秒**（journal 收件時間約 59.304 秒），recovery 第一個動作在 **711.016 秒**，核心沒有指定卡的 `reset high-speed USB` 紀錄。大量重新列舉已在恢復動作前發生，不能歸因於 recovery 重設迴圈。後續較長快照的數量另見 [10/07 USB 列舉故障](INCIDENT_2026-10-07_USB_ENUMERATION.md#第二次開機仍復發)，不與本段相加。

指定卡及 USB 上層取樣的 `runtime_suspended_time` 都是 `0`，指定卡 `power/control=on`。此段沒有省電休眠造成故障的證據。外網同時可經已驗證的 D-Link Wi-Fi 上網、本機及公開 `/healthz` 正常，將此次故障定位在 USB 通道；仍不能單靠 `-71` 區分設備韌體、USB 電氣條件或 host/controller。Linux 官方[USB 錯誤碼說明](https://docs.kernel.org/5.15/driver-api/usb/error-codes.html)也明確指出，控制器常無法區分協定錯誤、未回覆與其他故障。沒有把這些證據當成原始 USB 問題已根治。

**本節修正已於 13:39:55 首次正式部署，其後完成操作前世代競態修正並再次原子部署；最新版 292 項相關測試通過（3.33 秒）。** 首次備份在 `/var/backups/laiting-uav/network-stability-mw6p5pnl/`，最後版本備份在 `/var/backups/laiting-uav/network-stability-zpi9jvnt/`。路徑、USB recovery、RF guard 服務均 active，watchdog 為兩分鐘。以下命令供後續安裝／維護使用：

- USB 路徑健康附帶指定設備及 `bus:devnum` 世代。探測完成後再次核對身份；探測期間已重新列舉的成功結果不能用來授權切換。
- USB 世代改變會重算連續健康時間。Wi-Fi 承接後，USB 重新恢復健康或又換世代，五分鐘冷卻從該次恢復重算；短暫可用的 USB 不會反覆搶回出口。手動選擇仍可切至剛驗證健康的 USB。
- 路徑服務把冷卻、最後切換時間及已選路徑保存於 root 私有 `routing.json`，同次開機重啟服務會保留；不同 boot ID 重新計時。只在狀態改變時寫持久檔案，不每秒寫入快閃儲存。
- 路徑服務新增 120 秒 watchdog，Wi-Fi 維護執行緒停止也會觸發服務重啟。
- 已驗證的機上 Wi-Fi 外網正常時，RF guard 略過 USB AT 查詢，因兩條路徑共用相同 LTE 射頻。兩條外網均失敗時，原 `CFUN=4`／SIM READY 恢復條件仍保留。
- CDC bind 的 `EPROTO` 遇到 Wi-Fi 承接時，直接記錄延後 reset，不再先輸出「正在 reset」後才被保護拒絕。reset 的原有操作前核對保留。
- 安裝並啟用每次開機的唯讀驗收服務；不用 `network-online.target` 作啟動前提。要求實際出口、固定 profile、當次開機的新鮮狀態、本機／公開網站及 Tunnel 連續正常 20 秒；USB 出口另核對世代。180 秒內未通過會由 systemd 每 30 秒重試，恢復服務持續運作。

部署會備份六個正式檔案、原服務啟用狀態並在失敗時還原；只重啟網路路徑／USB recovery／RF guard 及唯讀驗收，不重啟 UAV、Tunnel 或整台主機。即使指定 USB 功能消失，仍可核對固定網路設定並部署。

```bash
cd /home/hrc/laiting_uav
sudo /usr/bin/python3 scripts/deploy_network_stability.py
systemctl status uav-modem-paths uav-4g-boot-validation --no-pager
sudo cat /var/lib/laiting-uav-boot-validation/network-stability/latest.json
```

驗收報告的 `accepted=true` 表示觀測到連續健康，不是所有未來開機的保證；`usb_ready=false` 而 `active_path=wifi` 明確表示 USB 尚未修好、網際網路由機上備援承接。`started_boot_seconds` 保留開始觀測的開機秒數，部署當下的驗收不能冒充從開機起測量，也不能代替真實重新開機驗證。報告按 boot ID 保存，保留最近 16 次開機，且不覆蓋 10/06 舊驗收資料。供電、設備韌體與電信條件仍可能造成兩條路徑共同失效，因此不能承諾每次開機或 USB 連線永不失敗。

首次部署後同一 boot 的驗收從 **1644.120 秒**開始，至 **1667.270 秒**觀測到連續正常 20 秒：`accepted=true`、`active_path=wifi`、`wifi_ready=true`、`usb_ready=false`，公開 health 正常。這是本次部署後的運行驗收，並非新的重開機驗收。最後版本路徑程式 SHA-256 為 `b23d4d4397a012fb7847c1f593266d46da0c998a84050582b4e1625bf0165605`；操作前會再次核對 USB 世代，拒絕把前一世代的健康證據用來切到新設備。

另已啟用唯讀 `uav-network-boot-observer.service`，每次開機保存最多 180 秒的指定 USB、實際出口、公開網站及飛控 USB 世代觀測；輸出按 boot ID 分別保存於 `/var/lib/laiting-uav-network/boot-observation/`。此服務不登入網卡、不重啟網卡；`usb_verified_30s` 和 `permanent_fix_proven` 分開，有限觀測不會標記為永久修復。

後續 boot `ed1b68f4-bf17-4fea-9a18-da35ac00ca77`／`86004921-9b83-4afa-8c8c-ccea24212d06`，機上 Wi-Fi 連續 20 秒驗收分別於開機 88.040／181.524 秒通過；USB 各自的 180 筆觀測都未通過連續 30 秒驗收。使用者確認網卡本體已拔下至少 10 秒再插回，USB 仍只短暫列舉。14:22 原核心對照 boot `4ce360e8-1cf5-4a1e-82a5-488c9f01b438` 亦有 `-71`，但實卡 port 已從 `1-2.2` 改為 `1-2.3`，不能稱為嚴格單變量對照，也不能沿用固定舊 port 的 observer 缺席判斷。診斷程式已正式原子安裝 SHA-256 `5adb60cccc5b9df7b4ef5eec80deebb1dbfa1e5100a2b09ce91c24debd4d8c94`，按唯一 VID／PID／序號動態尋找實卡，核對每次 stats／TLS 探測的前後世代；新 boot 證據另行記錄，舊報告的限制仍保留。

另找到尚未回補的官方 f781 xHCI endpoint halt-cleanup 缺陷，已完成保留 e765 的獨立候選 Image 建置、13,492 個內建 export CRC 相容性及 65,536 個決策分支案例驗證；**目前僅完成建置，尚待正式載入及 USB 實測**。它修正已知的錯誤恢復機制，現有證據尚未證明它是本次最初 `-71` 的唯一原因。候選版本與對照證據見 [USB 列舉紀錄](INCIDENT_2026-10-07_USB_ENUMERATION.md#尚未回補的-xhci-錯誤恢復缺陷與候選核心)。

本輪原始證據保存於 `data/maintenance/20261007-network-stability/`（目錄 700、紀錄 600）。原始碼相關測試及部署／開機驗收結果見 [10/07 紀錄](INCIDENT_2026-10-07_NETWORK_STABILITY.md)。

## USB 與行動網路恢復

`uav-modem-recovery.service` 每輪探測後等 5 秒，探測及操作耗時另計。閘道、外網 TLS、公共 DNS 與公開網站探測固定綁定 D-Link，避免其他出口掩蓋故障。NM 取樣只讀固定介面的數字狀態、active profile UUID 與 IPv4。

| 狀態 | 處理 |
| --- | --- |
| 正常上網且 DNS 可用 | 只監看；連續正常 30 秒後清除重試計數及等待期限 |
| 外網正常、DNS 失敗 30 秒 | 修復 loopback resolver、DNS 快取／能力狀態及行動網卡設定，必要時重啟 DNS 服務 |
| 同一 USB 世代的 NM disconnected／failed，或固定 profile activated 卻無 IPv4，持續至少 5 秒 | 只重新啟用固定行動 profile；未知、過渡狀態或其他 profile 不走此快速分支。若先前已確認 USB 完全消失，設備及完整 CDC 回來後，首個符合上述明確 NM／IPv4 故障的取樣可立即重新啟用，不沿用消失前的等待期限 |
| 同一網卡收發完全停止、錯誤增加至少 50、閘道與外網失敗，持續至少 5 秒 | 優先 reset 指定網卡；五分鐘內復發可升級為該卡 CDC Ethernet rebind。機上網卡 Wi-Fi 正承接出口時改用 CDC rebind，拒絕整卡 reset，保留備援。直接 USB 證據優先於同時的 NM／DHCP 異常 |
| 同一 USB 身分及世代、網路介面消失，且完整 CDC 描述確認 control／data 都未綁定，持續至少 5 秒 | 直接 bind 指定 CDC control；沿用 USB 重試時鐘，不再先等一般故障的 45 秒，也不先對不存在的介面執行 NM reconnect |
| CDC bind 回傳 `EPROTO`（`-71`） | 同一子程序再核對指定設備及 USB 世代；機上網卡 Wi-Fi 未承接出口時，只 reset 該卡一次，再明確 bind 一次。Wi-Fi 備援使用中則拒絕整卡 reset。第二次 bind 失敗便記錄並交回有退避的監測重試，不連續 reset |
| USB／CDC 描述或 driver 狀態未知、部分綁定或有外來驅動 | 拒絕 USB reset／rebind；不以任意 USB port 或 driver 代替 |
| 外網失敗 45 秒，本地閘道仍可達 | 只重新啟用固定 profile；RF／APN／上游故障不觸發 USB reset／rebind |
| 外網與閘道失敗 45 秒，CDC 正常綁定，但沒有充分 USB 凍結證據 | 先重新啟用固定 profile，仍失敗才依序指定網卡 reset／rebind／reconnect |
| USB 網卡完全消失／尚未接上 | 只等待重新列舉，跳過無介面的外網探測及 NM 啟用，不 reset 其他設備／控制器；保留機上 Wi-Fi。指定設備、完整 CDC 及介面回來後，才依核對過的狀態重新啟用固定 profile |
| 外網恢復，Tunnel／公開 SSH 或網站仍卡住 | 既有 Tunnel supervisor 依本機 origin、DNS、路由及公開探測重建 Tunnel |

一般故障與直接 USB 故障各有獨立的 **10、20、40、60 秒**退避；明確的本機 profile／DHCP 故障另採 **15 秒**重試。沒有總重試次數上限，同次開機重啟服務保留各自狀態。CDC 未綁定分支仍尊重既有 USB 等待期限，先前 USB 故障可讓這次操作等待最多 60 秒；它略過的是不適用的 45 秒一般故障等待與 NM 操作。

USB 凍結與一般外網失敗分別計時。成功收發量改變、錯誤計數下降或 USB 世代改變時更新凍結基準。五分鐘內卡死歷史不會因短暫正常 30 秒消失。**5 秒是確認門檻，不是網頁恢復保證**；探測、USB／DHCP、電信註冊與 Tunnel 建立耗時另計。

裝置核對 MAC `ac:00:33:aa:96:33`、VID／PID `1286:4e31`、序號 `200806006809080000`。固定 profile 為 `8391cb7e-8a3a-317c-bf18-612aa9c5300e`，介面必須為 `enxac0033aa9633`。介面消失時，USB fallback 必須找到恰好一個匹配設備。

缺失 CDC 修復嚴格核對 control `00` 為 class `02`／subclass `06`、data `01` 為 class `0a`，並核對兩者 driver；只寫該卡的 `cdc_ether` control 介面。監測程序把**觀測到的 USB 路徑與 bus:devnum 世代**傳給 reset／rebind 子程序，在入口及操作前再次驗證。故障判斷後若已重新列舉，拒絕把舊判斷套到新世代。

## 開機 LTE 射頻恢復

重開後紅燈的另一個問題已直接重現：USB／管理頁正常、SIM `READY`，但 `AT+CFUN?` 回傳 `4`，射頻收發關閉。改為 `AT+CFUN=1` 後可重新註冊及建立 PDP。這是已觀測到的射頻狀態問題，不代表 USB 凍結原因已找出。

已啟用 `uav-modem-rf-guard.service`，在 `NetworkManager.service` 之後啟動，**不等待 `network-online.target`**，避免等尚未恢復的 4G 才開始修復。

- 只有指定網卡存在、carrier 正常、閘道可達而外網失敗，才讀 AT；外網健康時略過 AT 操作。
- 只使用已核對描述、endpoint 且原本無 driver 的 Mobile AT Interface `4`，短暫 USBFS claim 後釋放。沒有 detach driver、USB reset、USB configuration／APN 變更、電信掃描或 SIM PIN 操作。
- 相同 USB 世代連續確認 `CFUN=4` 且 SIM `READY` 至少 5 秒，才進入恢復；變更前重新獨立查詢兩項狀態，仍符合才送唯一允許的變更 `AT+CFUN=1`。
- 重試間隔 15 秒。狀態未知、SIM 未 READY、其他 CFUN 狀態或 USB 世代改變時，不發出射頻變更。

USB recovery、RF guard 與 Tunnel supervisor 均有 120 秒 systemd watchdog，沒有啟動次數封鎖；RF guard 重啟間隔 5 秒。原應用 watchdog 與相機／飛控各自自動重連保留。

## 每次開機只走 4G

固定 USB 4G profile 已設定 autoconnect、priority `100`、route metric `100`、autoconnect retries `0`（持續重試）。最新要求改為開機同時準備 D-Link 自身的 Wi-Fi 4G 熱備援，詳見下一節；小電腦不依賴實驗室網路。

**18:26 前的歷史設定與驗證：** 當時所有 Wi-Fi profile 的 autoconnect 停用、Wi-Fi radio 持久設定 off，沒有 Wi-Fi 自動備援。五次開機均確認經 D-Link USB 恢復公開網站，開機到公開 health 成功約 **43–72 秒**；以 boot ID／monotonic 計算，避免開機 NTP 校時誤差。這是當時已測範圍，不能代替新雙路徑開機驗收，也不是所有開機或電信條件下的時間保證。摘要與取樣保存於 root 私有的 `/var/lib/laiting-uav-boot-validation/`。

## 機上 D-Link 雙路徑與 GUI

**2026-10-06 晚間已部署；10/07 已觀測到重開後由機上 Wi-Fi 恢復網站及 SSH。** 兩條路徑均透過同一張 D-Link／SIM 上 4G：`enxac0033aa9633` 為 USB 主路徑，`wlP1p1s0` 只連 D-Link 自己的無線 AP，平時保持連接並獨立探測外網，作為熱備援。USB 資料通道故障而網卡 AP 與 LTE 仍正常時，系統可使用已驗證的 Wi-Fi 路徑；不是等待故障後才搜尋實驗室網路。此項實測證明備援運作，USB 本身在同次開機仍故障。

Wi-Fi 裝置的 NetworkManager `allowed-connections` 僅允固定 UUID `7b8a7e5f-0e63-46ed-9c9c-9666959890b2`，profile 固定 D-Link 的 SSID／BSSID，其他 Wi-Fi profile 的 autoconnect 停用。這層限制同時拒絕外部 profile 的手動或自動啟用，不能只依賴 route metric 或 autoconnect 優先序。無線電開啟是為了機上網卡 AP，**不表示允許實驗室 Wi-Fi**；備援不可用時持續重試固定 AP，不選擇其他網路。

Root 路徑服務 `uav-modem-paths.service` 先核對介面、固定 profile、閘道與 Wi-Fi BSSID，再探測每條路徑的外網，最後變更預設路由。自動模式以 USB 優先；USB 失敗時，只有已驗證可上網的機上 Wi-Fi 才能接手。自動切回 USB 需 USB 連續健康至少 30 秒、距上次切換至少 60 秒，且 USB 故障備援後的 **5 分鐘冷卻**已結束。GUI 手動選擇已驗證健康的 USB 可略過自動切回等待。手動偏好仍允許故障備援，避免指定失效路徑後長期失聯；選擇會持久保存供下次開機使用。

網頁「4G 連接方式」顯示 **USB 4G／網卡 Wi-Fi 4G**，提供「自動選擇 · USB 優先」及兩種手動選擇。`active_path` 來自實際出口路由與固定裝置核對，和使用者偏好分開，不能以按下的按鈕推定切換成功。狀態檔過期、服務不可用或路由屬於未知網路時，明確顯示待確認，不沿用舊的健康狀態。登入及 CSRF 保護的 API 只送固定 target 和唯一 request ID 至 root-owned Unix socket；瀏覽器不傳 SSID、密碼、profile 或指令，也不取得 sudo 權限。切換回應中斷後只持續讀狀態，不自動重送操作。

每條路徑必須同時 `available=true`、`connected=true`、`internet=true` 才可按下手動切換；故障、尚未確認或服務狀態過期時呈灰色不可按，新的健康取樣通過後自動恢復可按。已使用中的健康路徑仍可選擇。只有操作仍在排隊／準備／換路由時暫時鎖住按鈕；`recovering_tunnel` 階段仍可選另一條健康路徑。

Root 保留最近 16 筆切換 request ID 與 `queued`／`route_ready`／`failed` 結果。瀏覽器以自己的 request ID 解鎖，不因另一個客戶端覆寫最近切換紀錄而長期顯示不能切換。`route_ready` 只代表該操作完成出口變更；畫面仍另核對目前實際出口和公開網站，避免把歷史操作結果顯示成現在的連線方式。10/07 同時修正介面消失時寫 ARP 設定的 `FileNotFoundError`，避免這個競態使健康狀態停留在舊錯誤。

介面依序顯示 `preparing`（準備連線）、`switching`（切換出口）、`recovering_tunnel`（網頁重連），確認完成才顯示正常。既有 TCP／SSH／Tunnel 連線可能因出口變更而中斷；已驗證的路由交接讓 Tunnel supervisor 略過一般路由等待並重建 connector，保留 UAV 應用及相機／飛控程序。GUI 顯示本次出口與公開網站的實測時間；出口已改但公開網站未恢復時，顯示持續重連，不能把換路由當成網站恢復，也不承諾物理零中斷。

USB recovery 在實際出口使用固定網卡 Wi-Fi profile 時拒絕整卡 USB reset，優先只修復 CDC 綁定，避免同時打掉網卡的 AP。操作前再次核對出口；`EPROTO` 的 reset fallback 也受同一保護。此備援只避開 **Jetson－USB－網卡資料通道**：LTE 無訊號、SIM／PDP 故障、整卡當機或缺電會同時影響兩條路徑，仍需恢復相應條件。原始 USB transaction failure 的觸發原因尚未根治，雙路徑是減少其斷訊影響。

## DNS 傳輸備援

`uav-tunnel-resolver.service` 只監聽本機 `127.0.0.55:53` 的 UDP／TCP，不開放外部查詢。它並行詢問 `1.1.1.1`／`8.8.8.8`；一般 DNS 無回覆時，改用兩個以 IP 連線並驗證 TLS 憑證的 HTTPS DNS 端點。快取最多 256 項、最多 30 秒且不超過上游 TTL，失敗不快取。

Tunnel namespace resolver 優先使用 `127.0.0.55`，公共 DNS 保留為程序啟動備援。主機仍經 `/etc/resolv.conf` 原有 `systemd-resolved` stub；`/etc/systemd/resolved.conf.d/91-uav-https-dns.conf` 增加全域本機解析路徑。行動網卡原有公共 DNS 保留。

本機舊版 resolved 對 per-link DNS 使用實體介面，因此測試中的 loopback per-link 設定已還原，正式版本使用全域設定。`127.0.0.55` 也避開新版本 resolved 的 `127.0.0.54`。相機 `eno1` 設定沒有改動。

## 驗證與更新歷史

- **2026-10-06 本次 CDC 修復：236 項相關測試通過，9.01 秒，含 74 項新增 CDC 測試。** 涵蓋未綁定快速分支、既有 USB 退避、未知／外來／部分 driver 拒絕、觀測世代傳遞、EPROTO 一次 reset＋bind，以及再次失敗交回重試。
- **2026-10-06 18:24 受控實機驗證：** 只解除指定 D-Link CDC control 的綁定，`automatic_recovery_verified=true`、`fallback_used=false`。首個介面恢復取樣為 **11.1067 秒**、外網探測 **11.0747 秒**、公開 health **12.4592 秒**。45 秒共 45 次取樣，本機 health 全成功；飛控 `1-2.4`／USB 世代 `1:5` 不變，UAV PID `1662`、Tunnel supervisor PID `3573` 不變且 `NRestarts=0`。Wi-Fi 維持 disabled，4G 預設路由 metric `100`，USB RX／TX errors 為零；測試後公開 SSH banner 正常。
- **2026-10-06 晚間雙路徑受控驗證 v2：** 故障後首個機上 Wi-Fi 出口取樣約 **0.503 秒**，首個公開網站成功約 **12.513 秒**；USB 介面約 **10.974 秒**恢復。出口和網站是不同完成時點。此次模擬 CDC 解除綁定，不是原始 transaction error storm；亦不是 10/07 新 Tunnel supervisor 的驗收。
- **2026-10-07 重開故障觀測：** boot `25de75b2-3c2f-4377-81e1-b8b653ad2e22` 的 USB 首次錯誤在 **18.537 秒**，首次 disconnect **18.719 秒**；首個同時確認公開網站及 SSH 正常的機上 Wi-Fi 取樣在 **34.346 秒**。取樣間隔 10 秒，不能將 34.346 秒當作精確重連時間，也不能說 USB 已修復。
- **實機驗證限制：** 上述測試模擬 CDC 未綁定，沒有重現原始 USB transaction error storm。EPROTO reset fallback 由離線測試覆蓋，不能把它說成實機已重現驗證。12.4592 秒是本次量測，不是所有斷訊的恢復承諾。私有證據見 `data/maintenance/20261006-1713-outage/` 與 `/var/lib/laiting-uav-recovery/cdc-live-validation-summary.json`。
- **2026-10-06 15:36：** 48 項新增快速恢復測試通過，涵蓋故障並存、三種獨立時鐘、跨服務重啟及錯誤設備拒絕；確認門檻調為目前 5 秒。
- **2026-10-06 13:33：** 分開一般失敗與 USB 凍結基準，64 項當時的相關測試通過，只重啟 recovery；當時 15 秒門檻已由後續更新取代。
- **2026-10-05 初次部署：** 52 項當時的相關測試通過（8.78 秒），含持續重試、退避保存、USB 消失／回來、DNS 備援及 Tunnel 恢復。隔離 DNS 測試讓一般上游失敗，10 次 UDP／TCP 查詢由 HTTPS 備援完成，最慢約 0.22 秒。相機 profile、介面與 IPv4 設定比對相同；當日實機連線關閉測試保存於 `data/maintenance/20261005-persistent-recovery/link-test.jsonl`。

## 紀錄與維護

```bash
systemctl status uav-modem-recovery uav-modem-rf-guard uav-modem-paths uav-tunnel-resolver
journalctl -u uav-modem-recovery -u uav-modem-rf-guard -u uav-modem-paths -u uav-tunnel-resolver -f
systemctl --user status uav-cloudflared uav-flight-network-monitor
nmcli -f DEVICE,TYPE,STATE,CONNECTION device
nmcli radio wifi
ip -4 route get 1.1.1.1
cat /run/laiting-uav-network/status.json
sudo cat /var/lib/laiting-uav-rf-recovery/status.json
sudo tail -n 20 /var/lib/laiting-uav-rf-recovery/actions.jsonl
```

必要時執行唯讀 RF 檢查；手動命令會短暫讀 AT，正常監測則在外網健康時略過：

```bash
sudo /usr/bin/python3 -I /usr/local/lib/laiting-uav/modem_rf_guard.py check
```

管理員程式在 `/usr/local/lib/laiting-uav/`。USB recovery 退避及備份在 `/var/lib/laiting-uav-recovery/`；RF guard 的 `state.json`、`status.json`、`actions.jsonl` 在 `/var/lib/laiting-uav-rf-recovery/`，目錄 700、檔案 600，動作記錄為 1 MiB 加兩個輪替備份，不保存完整 AT 回覆或管理帳密。

10/07 已設定有容量上限的 persistent journal，改善重開後的核心證據保存。然而檢查時 `journalctl --list-boots` 仍只列當次開機，並有 journal 損壞／開機校時的限制；不能據此宣稱所有舊開機紀錄都已保存。已另將本次核心、恢復與 USB context 快照存於 `data/maintenance/20261007-usb-outage/`（目錄 700、檔案 600），事件順序以 boot ID／來源 monotonic 核對。

只更新已安裝的網卡恢復程式：

```bash
sudo /usr/bin/python3 scripts/modem_recovery.py update-recovery
```

此命令備份原 root 程式並只重啟 recovery，失敗時還原，不重啟 DNS／Tunnel／UAV。首次完整安裝使用 `sudo /usr/bin/python3 scripts/modem_recovery.py install`；更新 Tunnel user service 用 `.venv/bin/python scripts/services.py --tunnel-only`。

只安裝／更新診斷服務用 `.venv/bin/python scripts/services.py --diagnostics-only`。持久取樣及分層事件保存於 `~/.local/state/laiting-uav/network/`：

- `hardware-samples.jsonl`：`uav-device-evidence.service` 每秒記錄 Jetson INA3221 主機輸入電壓／電流、過流事件、溫度、指定網卡及 USB 上層 runtime、收發與錯誤；每檔 8 MiB 加三個輪替備份、權限 600。沒有設備重設；主機感測器不能直接量測網卡 USB 5 V，也不能排除短於一秒的瞬變。
- `flight-samples.jsonl`：記錄本機／公開網站、Tunnel、序列設備及 `radio`。管理頁登入須在本機執行 `.venv/bin/python scripts/modem_radio.py --configure`；讀到原廠 dashboard 即時 `status1/wan` 後，Digest 衍生值及網卡 TLS 指紋保存於 `~/.config/laiting-uav/modem-radio-auth.json`（600，不保存明文密碼）。可編輯 `file=wan` 含占位值，不能代替即時狀態。空值、越界或非 LTE 不當成正常訊號，登入遭拒後不持續重試。`radio.ok=true` 只表示讀取成功，LTE 註冊及 PDP／WAN IP 另核對。
- `events.jsonl`：分層網路與核心事件；以同 boot ID／monotonic 對照，避免 NTP 跳時或跨開機混用。

## 還原

只停用 RF guard，不改 USB recovery、DNS 或既有網路設定：

```bash
sudo systemctl disable --now uav-modem-rf-guard.service
```

停用後，下一次 `CFUN=4` 不會由此服務自動恢復。此命令不改機上 D-Link Wi-Fi 的限制或備援設定。

停用 USB recovery 用 `sudo systemctl disable --now uav-modem-recovery.service`。若也停用 loopback resolver，先移除新增的 `91-uav-https-dns.conf`、從 maintenance 備份還原 `config/tunnel-resolv.conf`，再重啟 `systemd-resolved` 及 Tunnel，避免 DNS 指向已停止的 resolver。

依據：[ETSI TS 127 007 的 CFUN 定義](https://www.etsi.org/deliver/etsi_TS/127000_127099/127007/16.07.00_60/ts_127007v160700p.pdf)、[systemd 249 DNS 介面來源](https://github.com/systemd/systemd/blob/v249/src/resolve/resolved-dns-server.c)、[Cloudflare HTTPS DNS](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/)。
