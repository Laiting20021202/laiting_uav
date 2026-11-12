# 2026-10-07 D-Link USB 列舉失敗與重開後復發

**已確認 USB 低層傳輸與重新列舉失敗；原始觸發原因尚未唯一定位，尚未通過「重開後 USB 持續正常」驗收。** D-Link 自己的 Wi-Fi／LTE 在 USB 失效期間仍可連上網站與 SSH，因此備援可減少斷訊，但不能當作 USB 已修好的證據。本紀錄不把短暫 USB 恢復或單次零錯誤取樣當成根治。

使用者確認這次曾重新啟動 Jetson，USB 接線穩固、供電正常，要求比對 10/06 傍晚 17:00 左右的設定。事件以各自 boot ID 與來源 monotonic 對照；開機 RTC／NTP 校時會使牆鐘跳動，不能直接串接不同 boot 的日誌時間。

## 最初保存的開機故障

開機 ID：`25de75b2-3c2f-4377-81e1-b8b653ad2e22`。核心證據範圍截至來源 monotonic **247.519 秒**。約 12:40 的牆鐘時間僅供辨識，精確順序如下。

| 開機秒數 | 已核對的事件 |
| --- | --- |
| 3.289 | D-Link 在 `1-2.2` 以 High Speed 列舉，世代 `1:4`。 |
| 9.466 | `cdc_ether` 註冊指定 USB 網路介面。 |
| 13.596 | NetworkManager 回報固定 USB profile 啟用成功。 |
| 18.537 | 被動 context recorder 觸發指定 D-Link 的 transaction error。 |
| 18.550–18.684 | 保存 77 筆 code 4，全數映射到 D-Link slot 4、xHCI endpoint 7，即 Bulk IN `0x83`。 |
| 18.719 | 第一次完整 USB disconnect。 |
| 23.898 | 路徑服務選擇機上 D-Link Wi-Fi；該次路由替換量測 0.245 秒。 |
| 25.537 | USB descriptor read/64 回報 `EPROTO`（`-71`）。 |
| 30.221 | 核心 Hub driver 開始對 port 2 做既有 power-cycle 恢復，不是應用恢復服務發出的操作。 |
| 34.346 | 首個同時確認公開網站與 SSH 正常的取樣，出口為機上 Wi-Fi。十秒取樣不能給出精確恢復延遲。 |
| 58.429 | 路徑服務寫 ARP 設定時撞上介面消失競態，出現 `FileNotFoundError`。此為原始 USB 故障之後的次要問題。 |
| 59.871 | USB recovery 才首次要求 NM reconnect，當時介面不存在。它不是第 18 秒故障的起因。 |
| 98.335 | 此份核心快照內最後成功列舉的設備世代消失，之後仍繼續列舉／port enable 失敗。 |

此 **247.519 秒**的核心快照保存 35 次指定 USB disconnect、92 次 High Speed 與 4 次 Full Speed 列舉嘗試、44 行 `error -71`、96 行 Cannot enable、31 次核心 port power cycle。它們是不同類別，不能相加作故障總次數，也不是全日總數。Context recorder 有 245 次 trace overrun；77 筆是確實保存並匹配身份的錯誤，並非整場完整計數。其餘三份 context capture 屬於其他 10/07 boot，未合併到此處。

首次錯誤取樣的 USB RX／TX 只有約 3.7／5.9 KiB，RX errors 已為 58；無需大量網路流量即出現故障。指定卡與 USB 上層均為 active、休眠累積時間為零，卡的 `power/control=on`。主機輸入電壓與過流計數當時未見異常；這些感測器不能量到網卡 USB 5 V 的快速瞬變或 USB 訊號品質。

## 第二次開機仍復發

開機 ID：`15d15c2e-bdf3-48ba-ac21-c7336b8be8bf`。另存核心快照的來源 monotonic 範圍為 **10.137–1311.490 秒**；不包含開機最早十秒的完整列舉紀錄。

| 此份快照事件 | 數量／首次來源時間 |
| --- | --- |
| 指定網卡 USB disconnect | 298 次；首次 59.305 秒。 |
| High Speed／Full Speed 列舉嘗試 | 合計 461 次。 |
| 指定卡 `error -71` | 190 行。 |
| Cannot enable | 114 行。 |
| 核心 port 2 power cycle | 56 次。 |
| 指定卡 `reset high-speed USB` | 0 行。 |

Recovery 首次動作在 **711.016 秒**，為 `rebind fault=usb-stall`；大量斷開與重新列舉早已發生。USB 消失等待分支已在 **65.221 秒**記錄，沒有對不存在的介面反覆執行 NM 啟用。重新列舉後可短暫取得 USB 外網，但後續又有收發凍結、錯誤增加與 CDC bind `EPROTO`。這次重開未通過 USB 穩定驗收。

## USB Hub 與實卡狀態

只讀指定 Realtek `0bda:5489` Hub `1-2`，世代 `1:2` 的標準 descriptor／port status，沒有 claim、reset、SetFeature／ClearFeature 或修改 port power。這是核心拓撲中的 Hub，不能由此稱為使用者外接 Hub。

六個保存的有效取樣中，D-Link 所在 **port 2** 均為 `connected=true`、`enabled=false`、`power=true`；飛控所在 **port 4** 均為 `connected=true`、`enabled=true`、Full Speed。這支持故障集中在 Hub 至 D-Link 的 USB 連線／初始化路徑；Host 至 Hub 與另一個飛控 port 並未同時完全失效。它仍無法單獨區分 D-Link USB 功能／PHY／韌體、port 2 的訊號條件，或 Host 互動缺陷。`power=true` 是軟體狀態，不是網卡實際 5 V 電壓量測。

另經固定機上 Wi-Fi profile、BSSID、介面綁定及既有 TLS 指紋核對，登入同一張網卡的只讀即時管理頁：實卡 **DWM-222W／B1、韌體 1.01.00**，LTE 已連線，RSRP 約 -92 dBm、SINR 約 0 dB。公開網站與 SSH 亦能使用此卡的 Wi-Fi／LTE。訊號品質可能影響 LTE 傳輸，但單純 LTE 無訊號或 Cloudflare 故障不能解釋 USB descriptor／SetAddress／port enable 失敗。

Linux 官方說明指出，`EPROTO` 可能代表協定錯誤、期限內未收到回覆或其他 USB 錯誤；Host Controller 往往不能區分。因此目前不能從 `-71` 或核心的通用線材提示，唯一判定是線材、網卡韌體或 Jetson 韌體。[Linux USB 錯誤碼](https://docs.kernel.org/driver-api/usb/error-codes.html)

## 比對 10/06 傍晚 17:00

本輪前幾次故障時使用的 xHCI 修補 Image 與 10/06 傍晚使用的是同一份：SHA-256 `898fb51a3ee68920f4b04d841663a9a56567c5d4f0941f4402ebbb5e00f24160`。`usbnet` 為同一回補版本 `laiting-usbnet-1.0`，已核對模組 SHA-256 `4c7ac55eab96855c3b2128e73e6de3d99664c238a358467ee94252dbdc45a63d`。沒有證據顯示這兩者在該時段之後換成另一版；不能藉由「還原這兩個檔案」取得不同的 17:00 核心／驅動。其後另做 NVIDIA 原核心對照，見下節。

10/06 **17:13:09–17:13:59** 的六筆十秒取樣正常，但此前 **17:10:16–17:12:41** 已有 USB disconnect／transaction errors，此後 **17:17:28、17:18:13** 則保存完整收發凍結。使用者當時看到穩定連線與這些證據可同時成立：短時間正常不代表該版本從未復發。詳細時間與 NTP 限制見 [約 17:13 斷訊分析](INCIDENT_2026-10-06_1713.md)。

本次保留機上 Wi-Fi 備援及禁止外部 Wi-Fi 的要求。暫時 USB 外網成功後即搶回出口、隨後再故障，是另外須改善的路徑選擇問題；世代更換必須重算連續健康及冷卻，不能把多個短命 USB 世代合併成穩定五分鐘。這些恢復策略改善不等於已消除底層 USB 錯誤。

## `-71` 是否今天才出現、是否由修改造成

**不是 10/07 才首次出現。** 保存的 10/05 核心原始紀錄已有指定卡 `1-2.1` 的 `device descriptor read/64, error -71`，來源 monotonic **724.933485 秒**。以同一 boot 校時後的 event 錨點（monotonic 4041.877828 秒、10/05 19:33:40.152472）回算為 **10/05 18:38:23.208**，與核心行首日期一致；前後 CDC 紀錄核對 MAC `ac:00:33:aa:96:33`。此為落地後的 USB 故障證據，不拿來證明空中故障發生相同 descriptor 錯誤；它足以確認 `-71` 早於下表所有 10/06／10/07 修改。10/06 **17:18:25** 的 CDC rebind 也已兩次回傳 `-71`，並非今天才有這個錯誤碼。

| 時間（Asia/Taipei） | 實際狀態與變更 | 因果判讀範圍 |
| --- | --- | --- |
| 10/06 11:25–11:27 | 已載入 `laiting-usbnet-1.0` 回補；原 activation 紀錄保存開始／完成時間。 | 載入之前已存在 USB 故障；仍需對照才能判定回補是否影響故障頻率，不能只從時間先後排除所有交互作用。 |
| 10/06 13:33:29 | 已更新 USB 凍結／最後收發進展及世代的恢復判斷；只重啟 recovery。 | 修改恢復判斷，不是第一次 USB 錯誤的起點；之後仍有自然復發。 |
| 10/06 14:32–15:11 | 使用者已重設網卡；補回台灣大哥大 APN `internet` 與自動連線。15:04:11 上傳匹配的 1.00.09→1.01.00 官方映像，15:11:40 管理頁讀回 B1／1.01.00。 | 韌體實際已更新；並非今天重新燒錄。官方更新說明只有 APN 保存，不能冒稱 USB 修正。 |
| 10/06 14:48／15:39 | e765 Image 先放入獨立 boot entry；15:39:44 的真實 postboot identity 確認修補核心已載入。 | 「建置／放置」與「載入」分開；17:00、後續已失敗的 10/07 開機使用相同 e765 Image 和 `usbnet`。 |
| 10/06 17:13／17:18 | 六筆 17:13 取樣正常；17:18 收發凍結，rebind 兩次 `-71`。 | 下午曾正常和保存的復發可以同時成立；不能用短時正常宣稱該版從不出錯，也不能用一段錯誤否定整段正常使用經驗。 |
| 10/06 18:22／晚間 | 已部署缺失 CDC 的恢復分支；晚間雙路徑、固定機上 Wi-Fi 與 GUI 已啟用，19:47 起保存服務選路及受控測試。 | `-71` 及 USB 凍結早於雙路徑；晚間受控 CDC 故障注入不混作自然 USB 故障。雙路徑對今日故障頻率的影響仍不能只靠先後關係完全排除。 |
| 10/07 12:40 起／13:39 後 | 先保存 USB 自發錯誤及列舉失敗；之後才部署世代／冷卻、ARP 競態、非同步 Tunnel 與開機驗收等修正。 | 今日較晚部署不能造成先保存的初始故障；它們可能改變恢復與換路由表現，與底層錯誤分開驗收。 |
| 10/07 13:52 | 一次整卡軟體重啟請求，回覆未確認；後續 USB 仍失效。 | 請求早已有故障；本次會暫時打掉整卡 AP，已另外記錄，不列入背景重啟迴圈。 |
| 10/07 14:22 | 實際載入 NVIDIA 原核心做對照，`usbnet` 未還原；此時同一張卡位於 `1-2.3`。 | 原核心也失敗，但 port 同時改變，不能當成完整驅動或嚴格單變量對照。 |
| 10/07 本輪 f781 候選 | 目前只完成獨立建置、CRC 及離線分支測試，未改 `/boot` 或載入。 | 尚未套用的候選核心不能造成已發生的 runtime 故障；實機結果另行記錄。 |

以上證據不能唯一判定今天持續惡化的原因，也不能保證任一既有回補與故障程度無關。已確認的是：**底層錯誤早於修補，而且昨天 17:00 的核心／驅動／網卡韌體基線在今天先前失敗的開機仍相同**。APN、Wi-Fi／路由及恢復服務確實有改動，須按實際套用時序與分層結果核對，不以「已改回核心」代替完整對照。

## 已部署修正與尚待驗收項目

- **USB 消失等待：** 指定 USB 完全未列舉時，跳過 NM 啟用與無介面的外網探測，保留機上 Wi-Fi。指定卡及完整 CDC 回來後，若首個取樣確認固定 NM profile／IPv4 故障，可立即重新啟用，不等待消失前的一般 45 秒期限。完整 CDC 未綁定則仍使用既有身份、世代核對、五秒確認及有上限的重試間隔。
- **ARP 與健康狀態：** 介面消失時安全略過該介面的 ARP 設定，繼續更新 Wi-Fi 狀態，避免這個競態留下永久錯誤。
- **GUI 切換：** 路徑需 `available`、`connected`、`internet` 三者明確為 true 才能按下；故障／未知時禁用，健康後自動啟用。Root 最近 16 筆 request ID 結果供瀏覽器確認自身操作，避免其他客戶端覆寫最近切換時一直鎖住。換路由完成後的 Tunnel 重連階段不阻擋選擇健康路徑。
- **Tunnel 探測：** 當次新 boot 已運行非同步 supervisor，檔案 SHA-256 `a6f4452742bfa1e2f311426e6c0d3b29aea8cb7f714815d7676d4ae1f26e472c`。慢速公開 HTTP／SSH 探測不再阻擋主循環觀測路由；已驗證的機上出口交接略過一般 debounce。尚未以此次版本實測故障到公開網站的完整恢復延遲。
- **紀錄：** 已設定有容量上限的 persistent journal；檢查時 `--list-boots` 仍只列當次 boot，並有損壞／校時限制，未宣稱所有歷史已留存。核心／恢復／context 私有快照另外保存。
- **世代／冷卻及每次開機驗收：** 13:39:55 首次正式部署；其後完成操作前的最後世代競態修正，已再次原子部署，最新版 292 項相關測試通過（3.33 秒）。最後版本路徑程式 SHA-256 `b23d4d4397a012fb7847c1f593266d46da0c998a84050582b4e1625bf0165605`，私有備份在 `/var/backups/laiting-uav/network-stability-zpi9jvnt/`。世代更換會重新計算 USB 連續健康及五分鐘冷卻，同次開機服務重啟保留路徑／冷卻；換路由前再次核對世代，拒絕套用舊世代證據。見 [開機恢復修正](INCIDENT_2026-10-07_NETWORK_STABILITY.md)。
- **開機觀測：** 已啟用唯讀 `uav-network-boot-observer.service`，使用正式安裝的 `diagnose_modem_restart.py` 保存開機後最多 180 秒觀測，分開記錄 USB、機上 Wi-Fi、公開網站及飛控世代。早期安裝版本 SHA-256 為 `45577f0f58f431cd86fffd0bb8cc4f2d8e327afe78e078f53f7715a9a5cf4dd9`，40 項工具測試通過；它的固定 USB port 判斷在下節原核心對照中不適用。新版已正式原子安裝，依每次取樣的唯一 VID／PID／序號找實卡，核對實際 port 及世代；root 與來源 SHA-256 同為 `5adb60cccc5b9df7b4ef5eec80deebb1dbfa1e5100a2b09ce91c24debd4d8c94`，備份在 `/var/backups/laiting-uav/observer-dynamic-port-c9pfnhk_/`。46 項工具測試及較廣的 103 項管理頁／傳輸相關測試通過；部署端另重跑 `test_diagnose_modem_restart`、`test_modem_radio` 共 56 項通過（2.02 秒），各輪不相加。新 boot 結果另行記錄，不能倒改早期報告的有效範圍。開機服務只執行只讀觀測，不登入或重啟網卡。

部署後同一 boot 的唯讀驗收從 **1644.120 秒**開始，到 **1667.270 秒**確認 20 秒連續健康：`accepted=true`、`active_path=wifi`、`wifi_ready=true`、`usb_ready=false`。三個 root 恢復／路徑服務 active，當時 `NRestarts=0`。這證明機上 Wi-Fi 承接和公開網站恢復，不是 USB 通過，也不是新一次重開驗收。

背景恢復在實際出口為指定機上 Wi-Fi 時拒絕整張卡的 USB reset，以免同時打掉 AP；不 reset Hub、USB 控制器或飛控。診斷所需的整卡軟體重啟另行明確記錄，不列入持續重啟迴圈。

## 整卡軟體重啟、實體斷電與暖開機結果

13:42／13:44 兩次規劃中的整卡診斷均在登入 challenge 階段因 TLS EOF／`RemoteDisconnected` 結束，**均未產生 `action.json`，亦未發出網卡重啟請求**。不能將它們當作已執行的軟體重啟，也不能把此前 USB disconnect 歸因於這兩次診斷。後來確認網卡 `login.cgi` 不接受原先附加的時間戳查詢字串；已保留原身份、BSSID 與 TLS 指紋驗證，移除該登入查詢字串。正式管理頁程式 SHA-256 為 `9c09cdc6a67b8987d7c976f2d82f136071d30fcb1352df2f3ae57f39d3009c4b`，138 項相關測試通過。本輪沒有改管理帳密。

13:52 診斷只發出一次原廠整卡軟體重啟請求；管理頁已核對 B1／1.01.00 及登入身份，但請求回覆產生 `ParseError`，結果記為 **`exception_unconfirmed`**，不能宣稱網卡回覆已確認接受或完成。180 筆後續取樣中，USB 首次重新列舉約在觀測後 14.051 秒，最後仍消失、`usb_verified_30s=false`；Wi-Fi 有 11 筆斷開、公開網站有 60 筆失敗，飛控世代不變。這次沒有證明整卡軟體重啟能修復 USB，也沒有因回覆不明而自動重送。私有報告在 `/var/lib/laiting-uav-network/card-reboot-20261007-1352/`。

其後兩次真實 Jetson 暖開機的結果也須與 USB 分開：

| Boot ID | USB 觀測 | 機上 Wi-Fi／網站驗收 |
| --- | --- | --- |
| `ed1b68f4-bf17-4fea-9a18-da35ac00ca77` | 初始有指定卡，之後消失；180 筆觀測未通過 USB 連續 30 秒。核心首次 disconnect 在開機 51.658 秒，之後出現 `-71`。 | 機上 Wi-Fi 承接；連續 20 秒健康驗收於開機 88.040 秒通過。 |
| `86004921-9b83-4afa-8c8c-ccea24212d06` | 初始有指定卡，之後消失；180 筆觀測未通過 USB 連續 30 秒。核心首次 disconnect 在開機 61.943 秒，之後出現 `-71`。 | 機上 Wi-Fi 承接；連續 20 秒健康驗收於開機 181.524 秒通過。 |

使用者另確認已將 **D-Link 本體拔下、等待至少 10 秒再插回**。對應 14:05 的 180 筆唯讀觀測中，指定卡約在 77.288 秒短暫列舉，最後仍消失，USB 連續 30 秒驗收失敗；飛控世代不變。此為已做過的實體斷電步驟，不能把它寫成仍待執行，也不能把短暫重新列舉寫成修復成功。私有報告在 `/var/lib/laiting-uav-network/card-cold-replug-20261007-1405/`。

## NVIDIA 原核心對照：仍有錯誤，實卡位於另一個 port

14:22 的開機 ID 為 **`4ce360e8-1cf5-4a1e-82a5-488c9f01b438`**，實際載入 NVIDIA 原核心 `5.15.185-tegra #1 SMP PREEMPT Thu Jan 15 19:24:38 PST 2026`。原 `/boot/Image` SHA-256 為 `a5112e36f11e8987415eb1bce0b25baf8fd76bf29a58bb8ccec23a3b71a109a9`，既有 `usbnet` 回補未改動。

這次同一張卡的 CDC 身份出現在 **`1-2.3`**，不是前幾次的 `1-2.2`。來源 monotonic 的關鍵事件：開機 **9.518 秒**註冊指定 CDC 網路介面，**13.584 秒**完整 disconnect，**14.066 秒**讀 configuration 回傳 `-71`，後續 descriptor／SetAddress 亦失敗。核心另保存飛控 port `1-2.4` 在 **25.272、29.201 秒**斷開，以及 **29.072 秒**的 DMA pointer／TD mismatch、completion code 6。此 code 為 STALL；原碼已無條件處理 STALL，而且該 mismatch 分支在 `finish_td()` 前離開，不能將此行當作下節 f781 缺陷已命中的證明。

這次證明原核心仍會發生故障，不能把 e765 修補說成唯一原因；但因實卡 USB port 也改變，**不是嚴格只有核心版本改變的單變量對照**。更不能引用固定 `1-2.2` 的舊 observer 之「USB 未出現／首次列舉 None」來描述此次 `1-2.3` 的實卡；本節列舉與錯誤來自完整核心快照，已核對實際 port。

截至來源 monotonic **125.783 秒**的原核心完整快照，指定 `1-2.3` 保存 **12 次 disconnect、29 行 `-71`**，飛控 `1-2.4` 有兩次 disconnect；不同類別不相加為故障總次數。原始完整 journal 另保存為 `4ce360e8-1cf5-4a1e-82a5-488c9f01b438-kernel-stock-final.jsonl`。該次機上 Wi-Fi 出口切換量測約 **0.058 秒**，公開網站完成約 **20.375 秒**；出口與網站為不同完成時點，不能宣稱網站瞬間恢復。

該 boot 的 RF guard 到 **58.573 秒**才記錄射頻恢復已回覆，此前 **13.584 秒**已第一次 USB disconnect、**14.066 秒**已 `-71`。因此這次射頻恢復不是最初 USB 故障的原因；共同 LTE 恢復與 USB 資料通道異常仍分開處理。

## 尚未回補的 xHCI 錯誤恢復缺陷與候選核心

已在本機 NVIDIA `5.15.185` 原始碼確認 [官方 f7812977456c 修正](https://github.com/torvalds/linux/commit/f7812977456c3c1efc69ecf79ea88302bf472f4b)尚未回補：部分 transaction／babble／split error 的 completion event 已要求清理 endpoint，但 Endpoint Context 尚未更新為 HALTED 時，原碼可能略過該清理。這是可識別的主機錯誤恢復缺陷，可能使錯誤後的傳輸持續卡住；現有事件與事後 context 取樣**尚未證明本次故障實際命中這個狀態不同步分支**，也未證明它是最初 `-71` 的唯一觸發原因。

在獨立來源／物件目錄完成保留 e765 的 f781 回補候選 Image，SHA-256 為 **`4bf3a99e78cb4dc528a11813d2dad334dfe3dee0dd4be81cf992876352756e5e`**。全部 **13,492 個內建 export 的 CRC** 與已安裝版本相符，未更換既有 `.ko`；原來源分支配合 recording stubs 的 **65,536 個決策案例**通過，保留 NVIDIA stream、port-error guard、dequeue／cleanup 與其他既有處理。這些檢查不模擬 DMA／IRQ／硬體 command completion，也不能替代 USB 實機驗收。

**本段候選核心目前僅完成建置及離線驗證，正式安裝、載入與 USB／暖開機結果待補。** 建置來源、patch、驗證 JSON 及限制在 `data/maintenance/20261007-xhci-halt-recovery-build/REVIEW.md`。有限觀測即使成功，也不構成「所有未來 USB 傳輸永不出錯」的保證。

## 證據範圍

原始紀錄位於 `data/maintenance/20261007-usb-outage/`，目錄權限 700、檔案 600：首個 boot 的 `summary.json`、`kernel-current.jsonl`、`recovery-current.jsonl`、`capture-0.jsonl`，只讀 Hub 報告與網卡狀態摘要，以及第二次 boot 的獨立核心／恢復快照。第二次 boot 另有較早截至 826.09 秒的快照，見 `data/maintenance/20261007-network-stability/`；其數量較少是觀測範圍不同，不與後續快照相加。紀錄本文未包含管理憑證、Wi-Fi 密碼或完整管理頁回覆。
