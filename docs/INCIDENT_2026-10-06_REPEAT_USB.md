# 2026-10-06 D-Link 重複 USB 傳輸錯誤與網站／SSH 中斷

**已直接捕捉到 D-Link 的 USB Transaction Error，伴隨成功收發量凍結及 RX／TX errors 持續增加；更換 USB 接口、重開機及先前 `usbnet` 回補之後仍復發。因此目前不能宣稱 USB 根因已根治。**

使用者確認 USB 接頭穩固、供電正常。本輪採用此條件繼續查軟體與韌體，沒有要求再次換線、換設備，也沒有以 Jetson 的輸入電壓讀值代替 USB 5 V 的實測。飛控有獨立供電、馬達停止。

## 故障層級與已排除的解釋

D-Link 為 `1286:4e31`、序號 `200806006809080000`，網路介面 `enxac0033aa9633`。故障時介面及 carrier 仍存在，但成功收發量不變，自己的 `192.168.125.1` 閘道也不可達。本機網站 origin 持續正常。

獨立 USB recorder 記下 xHCI completion code **4（USB Transaction Error）**；本機 NVIDIA 核心 `xhci.h`／`xhci-ring.c` 將此傳輸結果對應到 `-EPROTO`。這是 USB host 所回報的傳輸錯誤，不能以 Cloudflare 網頁故障或 4G 外網無訊號直接解釋「連網卡自己的閘道都失敗」的現象。它也不是僅靠網路 errors 計數推測出來的結果。

目前可以確定 USB 資料通道失效，但 completion code 本身不能區分 D-Link 端 USB／韌體失常、共同 Hub 路徑或 Jetson host/controller 的缺陷。使用者確認接頭與供電正常，仍不等於已取得上述韌體元件中某一個的故障證明。

## 時間校正

以下時間皆為 **2026-10-06、Asia/Taipei**。多次重開機後，系統先沿用舊時鐘，之後 NTP 才校正；不能直接用 syslog 行首時間串接不同開機的事件。

本表使用同一 `boot_id` 內後段已校時的 flight samples，計算 `realtime - monotonic` 的開機時間基準，再以核心／journal／USB trace 的 monotonic 還原事件時間。USB recorder 的保存時間可能晚於事件；USB 錯誤採用 trace 行內時間。

| boot_id | 校正後開機基準 |
| --- | --- |
| `05bfe32a-6b19-4ad8-bcdd-7a229c5d24a6` | 12:38:39.901 |
| `7910a6b9-2c80-467b-a367-9813638a26b7` | 12:45:40.824 |
| `ab7d230c-45ae-4611-bb13-c6e2fec8141f` | 12:53:24.431 |
| `82dceeb2-fe6f-4324-b1e4-4440cc7d8ed2` | 13:15:46.814 |
| `fd39e63b-448d-4d6c-9bed-b912b0bdb32c` | 13:18:06.089 |

## 故障時間線

| 時間 | 直接觀測 |
| --- | --- |
| 12:40:54–12:41:29 | 舊接口 `1-2.1` 的 RX／TX bytes 固定為 `408053/115361`，errors 從 `310/14` 增至 `10174/613`；卡自己的閘道及指定介面的外網探測失敗。 |
| 12:41:16.301–12:45:20.153 | 該次開機保留 **17,012** 筆 code 4；其中 **16,522** 筆當時可直接映射到 D-Link，另 **490** 筆 slot 身分未知，不把未知紀錄全算成 D-Link。 |
| 12:41:29 | 恢復程式先採一般 `reconnect`，12:41:38 因設備狀態變動失敗。 |
| 12:41:42 | 核心另報 `TRB DMA ptr not part of current TD ... ep_index 6 comp_code 4`。它伴隨低層 USB 錯誤，但不能只憑這行認定是 Jetson 韌體根因。 |
| 12:43:46–12:44:07 | 同次開機再次出現收發凍結及 errors 成長；12:44:07 執行指定網卡 reset。 |
| 12:51:35.821–12:51:44.074 | 飛控 `1-2.4` 有 **4** 筆 code 6（STALL）：bulk IN 的 endpoint ID 3 有 3 筆、control endpoint ID 1 有 1 筆；當時主機的 4G 網站／SSH 探測仍成功。本段沒有相應長時間 heartbeat 失聯證據。 |
| 約 12:53:37–12:53:54 | 使用者重開機並更換接口附近的拔除／重新列舉；這段的 65 筆 USB 錯誤不當作換接口後的新穩態故障。網卡由 `1-2.1` 移到 `1-2.2`。 |
| 13:14:53.420–13:15:00.347 | **新接口 `1-2.2`** 再次出現 code 4；保留 2,182 筆直接映射到 D-Link 的紀錄。 |
| 13:17:21.223–13:17:44.695 | 下一次開機、新接口又有 **7,041** 筆直接映射到 D-Link 的 code 4。 |
| 13:20:25–13:20:46 | 再次重開機後，RX／TX bytes 固定為 `853079/450817`，errors 從 `1378/73` 增至 `7829/256`；carrier 仍為 true，閘道／外網／公開網站均失敗。 |
| 13:20:46 | 恢復程式確認卡身分後只 reset D-Link。 |
| 13:20:52 | 指定 D-Link 的閘道及外網已恢復，公開網站尚未恢復。 |
| 13:21:02–13:21:29 | Tunnel supervisor 因公開 HTTP 502／SSH 不可達連續三次、而本機 origin 正常，重建 tunnel；13:21:09 啟動新程序，13:21:11 tunnel ready，13:21:29 公開 HTTP 及 SSH transport 才 ready。 |
| 13:22:09／13:22:34 | 網卡消失時出口切到 Wi-Fi，網卡回來後再切回 4G；兩次路由變動各引發 tunnel 重建。僅憑此紀錄不判定設備消失是使用者操作或自行斷開。 |

`fd39…` 開機的第二份 USB 快照保留 **8,883** 筆直接映射到新接口 D-Link 的 code 4，以及 3 筆未知設備 code 4；USB trace 整體範圍為 13:20:19.467–13:22:24.135。另有飛控 3 筆 STALL，不與 D-Link 的 transaction error 混算。

USB recorder 有檔案輪替上限，以上都是**已保存的筆數**，不是完整故障總量。第二份快照最後一行無法解析，分析明確跳過 1 行，沒有補造紀錄。兩份快照有重疊，不能相加當作唯一總量。

## 換接口與先前回補的意義

舊接口 `1-2.1` 與新接口 `1-2.2` 都接在同一 `1-2` Realtek USB2 Hub、同一 `3610000.usb` Tegra xUSB controller；不是更換獨立 USB controller。新接口已復發，因此不能把換接口視為已解決問題。

多次開機後仍載入 `laiting-usbnet-1.0`。先前回補修正的是官方已知 `usbnet` 工作佇列／carrier／RX 缺陷；現在捕捉到的 code 4 大量錯誤仍發生，證明該回補不足以根治本次 USB transaction failure。沒有證據可反向宣稱回補製造了這個昨天已存在的故障。

NVIDIA 36.5.0／36.5.2 的 XUSB 韌體 SHA-256 相同，`xhci-tegra.c` 也相同，先前比較不能用來宣稱升級就能得到一份修正此問題的新控制器韌體。D-Link 韌體是否存在相符的已知缺陷，仍須核對此硬體版本及官方支援資訊，不能刷入其他型號或不明來源韌體。

## 恢復程序中已找到的延遲缺口

原 policy 在第一次外網失敗時保存收發基準；設備之後重新列舉或仍有少量收發，再進入凍結時，當前 bytes 與舊基準不同，導致原本 15 秒的資料通道卡死判斷無法成立，最後走 45 秒的一般 reconnect。

修正以**最後一次成功收發進展／USB 裝置世代**更新卡死基準，原一般外網失敗計時仍保留。兩項新增測試覆蓋「先一般外網故障、後才凍結」及「同一 USB 路徑重新列舉」。以已保存的真實樣本離線重播，舊程式首次為 12:41:29 `reconnect`，修正後為 12:41:15 `reset`，判斷提早 **14 秒**。

這修正的是恢復延遲，**不是消除 code 4 根因**；14 秒是離線判斷差異，不是保證實際重連時間。

13:33:29 已用 `update-recovery` 部署，只重啟網卡恢復服務。64 項相關測試通過（9.34 秒）；部署前及 13:35:27 驗證均由 D-Link 的 `192.168.125.100` 取得公開 health HTTP 200，RX/TX errors=0。UAV PID `1601`、Tunnel supervisor PID `3862`、USB recorder PID `936`、sshd PID `1443` 均保持不變。原始碼及正式 root 程式 SHA-256 一致；修改前正式程式已保存，`update-recovery` 也保留 root 還原備份。本次部署沒有主動重設 USB、重新啟用網路連線或重啟主機。

## 網頁恢復後 VS Code 仍連不上

使用者提供的 VS Code 主機名稱為 `ssh.laiting1202.com`；目前專案 Cloudflare ingress 設定及主機成功探測的名稱是 `ssh-uav.laiting1202.com`。13:31 的查詢及 banner 探測，前者失敗，後者成功。13:32:22 另以固定 `1.1.1.1` 的 Cloudflare HTTPS DNS 核對：前者回 `Status=3`（NXDOMAIN），後者回 `Status=0` 及兩個 A 記錄。這是獨立公共遞迴 DNS 查驗，並非本機 resolver 的單一快取結果；未直接查權威 nameserver，也沒有修改 DNS。

因此「網站恢復而 VS Code 不通」需要同時核對 SSH 主機名稱與 tunnel 恢復時間。操作端 SSH 設定的 `HostName` 及 `ProxyCommand` 應指向 `ssh-uav.laiting1202.com`；若使用者提供的是 `Host` 別名，仍須看其實際 `HostName`，不能單憑別名認定設定錯誤。主機的 `Public SSH transport: ready` 是對設定中的 SSH ingress 的驗證，不能當作另一個名稱已可用；也不能只憑探測连線主動關閉的 `kex_exchange_identification` 訊息，判定 sshd 失效。

## 直接相關的官方核心修正（建置完成、尚未載入）

進一步核對找到官方 commit [`e765ab012f73`](https://github.com/torvalds/linux/commit/e765ab012f73717238c95ab9c34bfc3c767fa48c)，2026-06-03 的 `usb: xhci: Improve Soft Retries after short transfers`：`process_bulk_intr_td()` 遇到成功的 `COMP_SHORT_PACKET` 時也應清除 `ep->err_count`。現有 NVIDIA 36.5.0 及所比較的 36.5.2 都缺少此行；USB 接收成功短封包時若沒有清除計數，端點可能在其生命週期中僅獲得三次軟體重試，而非每個 URB 各自的重試機會。

這是與 USB 錯誤恢復直接相關、可核對的核心缺陷，仍不能證明它造成第一個 transaction error 或能消除整段錯誤風暴。此主機 xHCI／Tegra host driver 編入核心（`CONFIG_USB_XHCI_HCD=y`、`CONFIG_USB_XHCI_TEGRA=y`），無法靠卸載 D-Link 的 `usbnet` 模組套用。13:42 被動取樣中 D-Link IN 端點的 2,529 次成功完成全部是 `COMP_SHORT_PACKET`，說明缺漏的成功路徑實際頻繁使用。

14:46 已完成只回補這一行的 NVIDIA 36.5.0 核心 Image；版本維持 `5.15.185-tegra`。全部 13,492 個 built-in 匯出符號與現有核心 CRC 相符，無缺漏、無新增符號、無強制略過 ABI；現有模組保留。功能設定相同，設定差异只有編譯器 11.3→11.4 的版本資訊。Image SHA-256 為 `898fb51a3ee68920f4b04d841663a9a56567c5d4f0941f4402ebbb5e00f24160`。

14:48 已透過 `scripts/xhci_kernel_stage.py` 放置 `/boot/Image.uav-xhci-retry` 與獨立 boot entry `uav-xhci-retry`，stock `/boot/Image`、`/boot/initrd` 及 primary entry 保留，私有備份位於 `/var/backups/laiting-uav/xhci-short-retry-20261006/`。目前 `DEFAULT primary`，仍執行原核心。使用者已明確允許**網卡設定完成後**切換並重開 Jetson；尚未在 4G 未恢復時執行。

## D-Link 官方韌體核對

台灣 B1 官方下載提供 signed delta `1.00.09_TO_1.01.00`，檔案說明只有「APN保存」，未列 USB 修正。其他型號 DWM-222（無 W）及不符硬體／地區／來源版本的映像不能混用。TWCERT 的 1.02.00 公告是管理登入限制修正，本次未找到對應的可驗證下載或 USB 修復說明。

已保存官方檔案、支援 API、說明頁及雜湊。早先提供的帳密被拒絕後已停止嘗試；使用者其後完成實體重設，原廠瀏覽器登入已回報成功，`status1/sysinfo` 已讀回 **DWM-222W / B1 / 1.00.09**，與台灣官方增量映像來源版本完全匹配。這項管理介面直接讀值取代先前的型號疑問；USB `bcdDevice=0100` 不曾用作版本判定。

目前 Wi-Fi 是維護路由（metric 50），D-Link profile metric 100；自動 modem recovery 暫停，以免設定／燒錄期間重設設備。台灣大哥大 APN 與 LTE APN 已設定 `internet`、不需帳密驗證、自动連線；4G-only/FDD-preferred 已讀回。網卡軟體重啟後 APN 值保存，但 `auto_apn=0` 變回 `1`，記錄於 `post-modem-reboot-settings.json`。即時狀態 `sys_mode=0`、RSSI 99、WAN IP `0.0.0.0`，指定 D-Link 的外網探測失敗，不能以 Wi-Fi 網站可用或 `connected` 字串宣稱 4G 恢復。

15:04 開始使用原廠管理表單套用匹配的 1.00.09→1.01.00 官方更新（只標示 APN 保存）。將以重新連線後的實際版本、APN 重啟後保存及指定 D-Link 的對外探測驗證，未宣稱完成或修復 USB 根因。

使用者沒有提供瀏覽器錯誤的精確時間與 VS Code 錯誤全文，所以不能把所有客戶端中斷都精確歸屬到同一段 USB failure。13:20 這段同時有低層 USB 故障、明確的資料凍結、公開 HTTP／SSH 不可達及隨後恢復，證據鏈完整。

## 保存的證據

- `data/maintenance/20261006-repeat-outage-125751/`：首次快照、四次開機的 flight／hardware samples、NUL 清理後 syslog／kernel log、USB recorder 輪替檔、`usb-error-summary.json`、修改前 recovery 及 `policy-replay.json`。
- `data/maintenance/20261006-repeat-outage-132751/`：新接口復發後的快照、current kernel／recovery／tunnel／SSH journal、USB recorder 輪替檔及 `usb-analysis.json`。
- `data/maintenance/20261006-usb-fix/nvidia-source-36.5.0/xhci.h`／`xhci-ring.c`：completion code 及錯誤轉換的本機官方來源。

維護證據目錄為私有目錄；不保存 sudo、網卡管理或其他密碼。前輪變更與還原方式見 [USB 傳輸修正與地面驗證](INCIDENT_2026-10-06_USB_FIX.md)。
