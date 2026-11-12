# AP6 USB MAVLink 斷線與通訊方式核對

2026-10-07，台灣時間。**USB 單次重設曾恢復資料，但後續開機再次斷線；尚未根治。** AP6 使用 MAVLink 2，已確認飛控來源為 system/component `1/1`。MAVLink 是訊息協定；USB、UART、無線數傳是承載它的通道，不能用更換協定版本代替修復 USB 列舉失敗。

## 單次恢復的實測範圍

Boot `5fa23ced-f29a-4866-a68b-2d1e5bacb4de`：

- 17:42:27 只重綁 AP6 `1-2.4:1.0` 的 `cdc_acm`。USB 世代 `1:13` 不變；之後 20 秒獨立讀取仍是 0 位元組，沒有恢復。
- 17:45:52 核對 VID/PID `2a58:a002`、序號 `330032000D51333331333534`、世代及 USB 字元裝置後，只對 `/dev/bus/usb/001/013` 發出一次 `USBDEVFS_RESET`，回傳成功。
- 重設後，MAVLink 1／2 地面站心跳測試各持續約 10 秒，分別收到 30,270／30,233 位元組、565／564 個封包。原正式 Telemetry 類別另連續取樣 30 秒：30 筆均 `connected=true`、`complete=true`，最後累計 90,470 位元組、1,691 個封包，解析錯誤 0；心跳、位置、GPS、姿態、速度和電池資料皆新鮮，最後狀態為未解鎖。
- 獨立序列埠測試曾短暫停止 `uav-v2`，因此同一程序提供的網頁也會暫停；各次測試均在 `finally` 恢復服務。**使用者要求保留網頁連線後，後續診斷改用既有 log 與唯讀取樣。**

原始證據：`data/maintenance/20261007-ap6-recovery-174258/`。這些結果只證明當時恢復，不代表後續開機或飛行不會復發。

## 後續開機的再次故障

Boot `a68f7990-046d-413f-8193-9c4c99a0d4d3`，NVIDIA 原核心 `5.15.185-tegra #1`。先前操作所在 boot 已結束；以下事件屬於新 boot。由於開機有校時，持續時間以 monotonic 計算。

| 開機秒數 | 台灣時間 | 已保存事件 |
| --- | --- | --- |
| 20.635 | 17:49:07 | AP6 成功連線；之後取得完整遙測 |
| 162.358 起 | — | 保留的 xHCI trace 開始出現對照為 AP6 的 `Stall Error` |
| 193.716 | 17:53:35 | 心跳逾時；累計 481,751 位元組、9,068 個封包、16 個解析錯誤 |
| 194.797 | 17:53:36 | 核心記錄 AP6 `1-2.4` disconnect；稍後重新列舉並自動接回 |
| 201.713 | 17:53:43 | 同一 AP6 再次 disconnect；稍後再次接回 |
| 203.123 | 17:53:44 | xHCI TD／DMA pointer mismatch，completion code 6（STALL） |
| 240.158 | 17:54:21 | 再次心跳逾時；這個 session 累計 253 個解析錯誤 |
| 254.701 | 17:54:36 | AP6 第三次 disconnect |
| 255.021–258.767 | 17:54:36–17:54:40 | descriptor `-32`、位址設定 `-71`，最後 `unable to enumerate USB device` |

解析錯誤是 MAVLink parser 的計數，不是 USB 核心錯誤碼；兩類數字不相加。程式在前兩次重新列舉後均自動恢復完整遙測，最後 USB 無法列舉時停在等待設備。

被動 recorder 的保留 trace 中，按收件時的 port／VID/PID 對照，AP6 有 **14,118 筆 STALL completion event**：原生 xHCI endpoint 欄位 3／1／2 分別為 9,523／4,588／7 筆。事件 monotonic 範圍為 162.358174–254.435713 秒；另有 821 筆 slot 5 event 缺少匹配的收件時設備身分，未加入 AP6 計數。這些是保留資料的事件數，不是完整故障次數；recorder 身分快照不是錯誤瞬間的 USB 世代證明。trace 中的程序名稱是事件發生時的執行上下文，不能據此指控該程序造成錯誤。

monotonic 773.474–776.008 秒的六筆唯讀 Hub 取樣均顯示：AP6 所在 port 4 `connected=true`、`enabled=false`、`power=true`；4G 所在 port 2 `connected=true`、`enabled=true`。兩者均未回報 suspend／overcurrent。`power=true` 是狀態位，不是實際 AP6 電壓量測；核心拓撲也不代表有外接 Hub。

故障定位為 **AP6 的 USB 資料／初始化路徑失效**。還不能唯一區分 AP6 USB 韌體、接頭／線材、供電／訊號條件與主機 xHCI 恢復行為，也不能單靠 USB disconnect 推定整顆飛控重新開機。Linux 官方說明 `-EPIPE` 是 endpoint stall，`-EPROTO` 可包含未回覆或其他 USB 錯誤，控制器常不能唯一區分原因。[USB 錯誤碼](https://docs.kernel.org/driver-api/usb/error-codes.html)

原始證據：`data/maintenance/20261007-ap6-dropout-180201/`，包含完整核心／UAV log、各自的時間線、AP6 trace、彙總和唯讀 Hub 狀態。後續檢查 UAV／Tunnel PID 保持 `1731`／`4000`，兩者 `NRestarts=0`；本機及使用 curl 的公開 `/healthz` 均正常。Python urllib 的一次公開請求被 HTTP 403 拒絕，另以 curl 核對成功；沒有據此重建 Tunnel 或變更網路。

## 18:17 通訊設定與網站保留核對

唯讀核對確認 `mavutil` 與使用的 dialect 都是 MAVLink wire protocol `2.0`。正式設定沿用固定 AP6 by-id 路徑、VID/PID／序號、來源 `1/1`、baud `115200`；原始碼具備單一讀取執行緒、pySerial exclusive、每秒伴飛電腦心跳、1 秒寫入逾時、8 秒心跳期限、自動重新尋找同一 USB 身分，以及分散發送的遙測頻率請求。既有 udev 規則讓 ModemManager 忽略 AP6。這些設定已有相容性依據，本輪保留它們，沒有以任意加大 baud／心跳期限或改用 MAVLink 1 宣稱修復。

隔離的遙測、watchdog 與恢復工具測試合計 **30 passed（11.05 秒）**，包含真實 PTY／UDP 的斷線及重新連線，不占用正式 AP6 序列埠。驗證前後 UAV／Tunnel PID 仍為 `1731`／`4000`、`NRestarts=0`，本機及公開 health 正常。

**此時 AP6 仍沒有 USB／序列埠，實際遙測連線尚未恢復。** 設定核對與隔離測試不能代替實機持續收資料驗收，也不能證明目前 USB 是最可靠的飛行通道。UART 建議仍須完成實際 AP6 接腳、電壓與接線核對後才能實施。稽核資料保存在 `data/maintenance/20261007-ap6-communication-audit-181755/audit.json`；本輪沒有服務或設備控制操作。

## 通訊選擇

**後續使用者已改選硬體 UART＋MAVLink 2**，取代先前繼續 USB 的偏好。Jetson 原生 UART、接線前提、候選設定及保留網站的獨立驗收見 [UART 遷移說明](AP6_UART_MIGRATION.md)。準備工作不等於正式啟用，AP6 端尚待接腳／電壓／埠設定確認。

MAVLink 是 PX4／ArduPilot 生態常用的遙測協定；現有程式已使用 MAVLink 2 並接受版本 1 封包。AP6 的兩種握手均實測收到資料，沒有證據需要退回 MAVLink 1 才能修復本次故障。[MAVLink 官方概覽](https://mavlink.io/en/about/overview.html)

| 方法 | 本案的判斷 |
| --- | --- |
| MAVLink 2 經 AP6 USB | 執行中的設定暫仍使用 USB；Linux 沒有 AP6 裝置可開啟時，軟體重連與更新率調整不足以恢復它 |
| MAVLink 2 經 AP6 UART／TELEM 至主機硬體 UART | 使用者最新選擇的遷移目標，可避開 AP6 USB 功能與主機 USB 通道。候選設定已準備，須先確認 AP6 的實際接腳、電壓、可用埠及該埠的 MAVLink 設定 |
| MAVLink 經數傳電台或網路 UDP | 適合另建地面站傳輸鏈路；新增無線或網路跳點，仍需解決飛控到發送端的可靠接入，不能直接修復現有 USB |

MAVLink 官方 UART 範例建議飛行系統優先使用 UART，理由是 USB 驅動較複雜。[官方 UART 範例](https://github.com/mavlink/c_uart_interface_example)

ALIGN 官方列出 AP6 有 7 個 UART，但公開產品規格未在本次核對中確立各接頭的 MAVLink 用途與電壓。**不能直接套用 Pixhawk 的 TELEM2 接腳或 `SERIALx` 參數。** PX4 的伴飛電腦文件可用來理解 UART／流控／電平匹配，不能當成 AP6 接線圖。使用 USB-to-UART 轉接器仍經過主機 USB；要避開整段 USB，需使用已核對的硬體 UART。[AP6 官方規格](https://shop.align.com.tw/product_info.php?language=tw&products_id=9705)、[PX4 伴飛電腦接線說明](https://docs.px4.io/main/en/companion_computer/pixhawk_companion)

## 指定 AP6 的手動地面恢復工具

`scripts/ap6_usb_recovery.py` 新增 `reset`：只接受唯一匹配的 AP6 身分，必須核對目前世代、`cdc_acm`、sysfs 裝置號、已開啟的 USB 字元裝置，且在 ioctl 前再次核對。失敗不連續重試。這是手動地面維護工具，**沒有安裝自動 USB reset 服務，也沒有接到網頁重連按鈕**；工具成功只表示 ioctl 成功，輸出保留 `telemetry_verified=false`。

飛行中的失聯不能推定未解鎖。USB reset 會中斷遙測，應在落地停槳後維護。先唯讀檢查：

```bash
cd /home/hrc/laiting_uav
.venv/bin/python scripts/ap6_usb_recovery.py inspect
```

只有唯一 AP6 已重新列舉，才將 `inspect` 當次輸出的 `generation` 原樣傳入；不沿用本文的舊值：

```bash
sudo /usr/bin/python3 scripts/ap6_usb_recovery.py reset --expected-generation '<當次 generation>'
```

目前設備缺席時，工具會拒絕選擇其他 USB 裝置。下一個有效實機步驟是在地面核對／更換 AP6 USB 資料線與接頭，必要時讓 AP6 的所有供電真正斷電再恢復；只重啟 Jetson 或只拔仍被外部供電的 USB，不等於 AP6 冷啟動。保留 4G 與網站服務，AP6 回來後由原遙測工作執行緒接回，再以長時間遙測及核心錯誤紀錄驗收。

工具驗證：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_ap6_usb_recovery.py`，**15 passed（0.19 秒）**，包含錯誤身分／過期世代拒絕、單一 CDC rebind、USB 節點裝置號檢查、開啟節點後與重設前的世代核對、ioctl 失敗關閉 FD 且不重試。這些工具測試不代表線材、供電或 AP6 韌體已根治。
