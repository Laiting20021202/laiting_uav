# AP6 改用 Jetson 硬體 UART 與 MAVLink 2

2026-10-07。使用者已改選 **AP6 UART／TELEM → Jetson 原生 UART → MAVLink 2**。這個方案避開本次失效的 AP6 USB 功能及主機 USB 傳輸路徑；MAVLink 官方 UART 範例也建議飛行系統優先使用 UART。[MAVLink 官方說明](https://github.com/mavlink/c_uart_interface_example)

**目前完成主機核對、候選設定及獨立驗收工具，尚未接線或切換正式通訊。** AP6 的空閒接頭、TX／RX／GND 腳位、UART 邏輯電壓及該埠的 MAVLink 設定仍待確認。不能用產品的 5V 電源輸入規格推定 UART 電壓，也不能套用其他品牌的 TELEM 腳位。ALIGN 官方列出 AP6 有 7 個 UART，但本次取得的公開規格未提供所需接線圖。[AP6 官方規格](https://shop.align.com.tw/product_info.php?language=tw&products_id=9705)

## 已確認的 Jetson 端

本機為 Jetson Orin NX，設備樹相容型號 `p3768-0000+p3767-0001`，L4T R36.5。原生 UART `/dev/ttyTHS1` 對應 `3100000.serial`，驅動 `serial-tegra`，設備樹狀態 `okay`，不在核心 console 中；唯讀 root FD 掃描時無程序占用。UART1 TX／RX 的現行 pinmux 都是 `uarta`，不需要為這兩腳修改 boot 設定。

以下是 **P3768 載板 J12 的實體腳號**，不是 BCM 編號。NVIDIA 官方載板規格第 3.3 節、圖 3-1 與表 3-3 標明訊號電平為 **3.3V**。請先確認現場確實是這款載板及接頭方向。[NVIDIA P3768 載板規格](https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf)

| Jetson J12 | 功能 | 接到 AP6 的訊號，AP6 腳號待確認 |
| --- | --- | --- |
| 8 | UART1 TXD，3.3V | AP6 UART RX |
| 10 | UART1 RXD，3.3V | AP6 UART TX |
| 9，或 6 | GND | AP6 同一 UART 的 GND |

**只接已匹配電平的 TX／RX／GND。** AP6 與 Jetson 使用各自既有供電；不要把 AP6 的電源腳接到 Jetson J12 的 3.3V／5V 電源腳。若 AP6 UART 不是相容的 3.3V TTL，先確立正確的電平轉換方式，不能直接接入。

現行 UART1 RTS／CTS pinmux 為 `rsvd1`，因此候選方案採 **8N1、無 RTS／CTS、無 XON／XOFF**，AP6 端也必須允許這種配置。J12 11 是 RTS、36 是 CTS；目前不接、不啟用。若 AP6 必須使用硬體流控，先重新評估 AP6 接腳與主機 pinmux，不能只開啟軟體的 `rtscts`。

現場接線需在落地停槳後完成，使用短線並固定接頭；不要占用既有 RC、GPS 或其他必要周邊的 UART。原始核對資料及官方 PDF 保存於 `data/maintenance/20261007-ap6-uart-preparation-182315/`。

## 候選設定

`config/telemetry.uart.pending.json` 只包含五個傳輸欄位，尚未合併到正式 `config/settings.json`：

```json
{
  "connection": "/dev/ttyTHS1",
  "baud": 115200,
  "usb_serial": null,
  "usb_vid": null,
  "usb_pid": null
}
```

**115200 是待確認的初始候選值，不是已讀到的 AP6 UART 參數。** USB 的 baud 值不能證明 UART 使用相同速度。須先確認 AP6 選定 UART 已啟用雙向 MAVLink 2，且兩端 baud 與流控一致。未取得此機韌體／埠對照前，不自行寫入 `SERIALx_PROTOCOL`、`SERIALx_BAUD` 或 PX4 參數。

程式既有 MAVLink 2 reader 支援原生序列埠；單一執行緒、來源固定 `1/1`、序列埠 exclusive、1 秒寫入逾時、8 秒心跳期限和自動重連均保留。候選欄位清除 USB 身分，UART 不會回退到其他 USB 設備。其餘設定在使用時從正式設定載入，避免複製過期的網站／相機設定。

現有遙測頻率為位置 10Hz、GPS 5Hz、姿態 20Hz、速度／電池 2Hz；選用測距 5Hz、雲台姿態 10Hz。初始驗收先保留這些頻率，再依實機錯誤率與更新新鮮度決定是否調整，不能把加大 baud 當成穩定性證明。

## 保留網站的獨立驗收

以下唯讀檢查現在即可執行；不會開啟 UART 或改變接腳：

```bash
cd /home/hrc/laiting_uav
.venv/bin/python scripts/ap6_uart_check.py inspect
```

`host_uart_available=true` 只表示主機端設備／驅動／console 核對通過，**不表示 AP6 已接線或收到遙測**。

**只有 AP6 接腳、電壓、接線及該埠設定確認後**，才執行實際地面測試：

```bash
.venv/bin/python scripts/ap6_uart_check.py check --duration 600
```

工具只啟動正式程式的 Telemetry reader，不啟動或停止網頁、相機、Tunnel、4G，也不修改設定、pinmux 或飛控參數。它會發送每秒伴飛電腦心跳與既有遙測頻率請求，不包含解鎖、起飛或飛行模式指令。若正式設定已選用此 UART，工具會拒絕再啟動第二個 reader。

驗收先最多等待 30 秒取得完整資料，再要求 **同一 session 連續 600 秒**符合：

- 飛控來源固定 `1/1`；心跳與五種必要遙測維持原有新鮮度門檻。
- 實際收到的心跳、位置、GPS、姿態、速度、電池封包均已觀測到 MAVLink 2；不是僅檢查本機環境變數。
- 被選中的 AP6 不混用 MAVLink 1；轉送的其他 GCS 來源不參與此判定。
- parser 錯誤為 0，沒有 session／連線嘗試變化，收件位元組及封包計數持續期間確實增加。

驗收以每秒取樣及 reader 的來源／新鮮度監測為依據。進入驗收期間後，任一取樣異常就失敗，不用後來恢復的資料掩蓋中途斷線。工具輸出 `report.json`、逐秒 `samples.jsonl` 和主機檢查，權限為目錄 700、檔案 600。`accepted=true` 只代表這次觀測通過，報告始終保留 `permanent_reliability_proven=false`、`production_switched=false`；不保證未來飛行永不斷線。

## 正式切換狀態

本輪 **未開啟實體 UART、未寫 AP6 參數、未修改正式設定或重新啟動服務**。獨立 PTY 測試不等於實機驗收。

通過接線核對及實機驗收後，正式切換只需把候選檔的五個欄位合併到正式 `telemetry`。現有 `Runtime` 在啟動時載入設定，網頁「飛控重連」按鈕不會重讀設定；單改檔案也不代表執行中的程式已切到 UART。由於同一 UAV 程序提供網站，後續啟用需處理設定載入與網站不中斷的要求，不能在本輪偷偷重啟 `uav-v2`。

USB 故障證據與先前恢復方式見 [10/07 AP6 紀錄](INCIDENT_2026-10-07_AP6_USB.md)。

軟體驗證：`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_ap6_uart_check.py tests/test_telemetry.py tests/test_watchdog.py tests/test_ap6_usb_recovery.py`，**40 passed（13.31 秒）**。包含真實 PTY 上六種必要 MAVLink 2 訊息、轉送 GCS 的 MAVLink 1 隔離、資料停滯／過期／重連／解析錯誤判定，以及既有遙測重新連線與指定 USB 恢復工具。測試沒有開啟實體 UART 或停止正式服務。
