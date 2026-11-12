# 2026-10-06 D-Link USB 傳輸修正與地面驗證

> **晚間新增要求：** 開機獨立使用 D-Link 4G，USB 優先、同一張網卡自己的 Wi-Fi 作熱備援；禁止實驗室及其他外部 Wi-Fi。新增 GUI 顯示實際出口並允許手動切換，部署與實機驗收結果待補，見下方「機上 D-Link 雙路徑與 GUI」。
>
> **18:26 歷史更新：** 官方網卡韌體 1.01.00、`usbnet` 回補與 xHCI short-transfer 修補核心均已載入。RF guard 已在五次重開後自動恢復射頻、連上 4G 網站與 SSH，**當時 Wi-Fi 停用**。18:22 部署缺失 CDC 快速恢復、`-71` 同程序 reset＋bind 及 USB 世代保護，236 項測試通過；18:24 實機解除 D-Link CDC 後，無需獨立復原措施介入，首次確認網站恢復約 **12.5 秒**。**原始 USB transaction failure 仍曾在新核心復發，不能宣稱已根治。** 詳細故障、修復與實測範圍見 [約 17:13 斷訊分析](INCIDENT_2026-10-06_1713.md)；下文保留早前驗證的時間與適用範圍。

**已部署 `usbnet` 驅動回補與網卡恢復修正，目前預設出口及實測皆走 D-Link 4G。原始空中卡死的觸發原因仍未確定，不能以這次地面測試宣稱已徹底根治。**

使用者確認接口及接線與昨天相同，飛控有獨立供電、馬達停止，沒有可用的替換設備。依最新要求，本輪優先處理 D-Link；沒有重設飛控／USB 主控制器或發送飛行命令。

## 機上 D-Link 雙路徑與 GUI

**2026-10-06 晚間新增；部署與實機驗收結果待補。** 最新「4G-only」要求是網際網路來源只用機上 D-Link／SIM，USB 4G 為主路徑、網卡自己的 Wi-Fi AP 為熱備援，小電腦開機不依賴實驗室網路。NetworkManager 的 Wi-Fi 裝置 `allowed-connections` 僅允固定 D-Link UUID，profile 固定網卡 SSID／BSSID，禁止外部 Wi-Fi profile 手動或自動接管；其他 profile 的 autoconnect 亦停用。備援 AP 不可用時只重試指定網卡，不選擇其他網路。

`uav-modem-paths.service` 核對每條路徑的裝置、profile、閘道、Wi-Fi BSSID 及外網可達性後，選擇實際預設路由。自動模式 USB 優先；USB 出問題而網卡 Wi-Fi 的 4G 仍可上網時才切換。自動切回 USB 要連續健康 30 秒、距上次切換至少 60 秒，並通過故障後 5 分鐘冷卻，避免反覆換路；手動選擇已驗證健康的 USB 可立即略過這段自動等待。手動偏好仍保留故障備援能力。

GUI 顯示「USB 4G／網卡 Wi-Fi 4G」的**實際出口**、兩條路徑健康及最近切換實測，提供自動和手動選擇。Root status 的 `active_path` 依實際路由、固定 profile／BSSID 核對，和偏好設定分開；未知或過期資訊不顯示為已連線。切換 API 受登入、CSRF 與 root socket 身分驗證保護，只接受固定路徑及 request ID。切換導致 HTTP 回應中斷時，瀏覽器自行重連並查詢同一 request ID，不重送操作。

畫面依準備連線、切換出口、網頁重連顯示進度。出口變更可能中斷既有 TCP／SSH；Tunnel supervisor 對已驗證的路由交接提早重建 connector，保留 UAV／相機／飛控程序。公開網站未確認恢復時不宣稱完成，不能保證零中斷或每次相同恢復秒數。

Root USB recovery 在實際出口使用固定網卡 Wi-Fi profile 時拒絕整卡 reset，改為優先只修復 CDC；操作前再次核對，`EPROTO` reset fallback 也受保護，避免 USB 背景恢復打掉備援 AP。這只能避開 USB 資料通道：兩條路徑共用同一張網卡、SIM 及 LTE，電信無訊號、PDP 故障、整卡當機或缺電無法靠本備援解決。**原始 USB 錯誤根因仍未確定。** 詳細策略及維護命令見 [行動網路恢復](NETWORK_RECOVERY.md#機上-d-link-雙路徑與-gui)。

## 早前 4G 出口與驅動現況

11:09:50 檢查：預設路由優先為 `192.168.125.1`／`enxac0033aa9633`，metric 100；Wi-Fi 備援 metric 600。強制 `curl --interface enxac0033aa9633` 連公開 `/healthz`，HTTP 200、來源位址 `192.168.125.100`。先前下載 NVIDIA 原始碼時只替該 curl 指定 Wi-Fi，沒有改動預設路由。

11:25:16 重新載入 D-Link 使用的 CDC Ethernet／`usbnet` 驅動。實際載入版本為 `laiting-usbnet-1.0`，模組在 `/lib/modules/5.15.185-tegra/updates/dkms/usbnet.ko`，目前預設出口仍為 4G。UAV PID `1568`、Tunnel supervisor PID `3432` 與 `NRestarts=0` 保持不變，相機 Ethernet 設定保留。

## 已套用的軟體修正

### 現有 `usbnet` 缺少的官方修正

主機為 Orin NX、Jetson Linux 36.5.0、核心 `5.15.185-tegra`。下載並比對 NVIDIA 36.5.0／36.5.2 原始碼後，本次回補四個官方穩定版 commit，沒有升級整套 BSP：

| 官方修正 | 處理的問題 |
| --- | --- |
| [LINK_CHANGE 的 RCU stall](https://github.com/gregkh/linux/commit/e2a4325ce21a524845f7d7ccfdf3f7d68c47e987) 與[後續 carrier-on 修正](https://github.com/gregkh/linux/commit/1946a6a9bdebbbea9f1e6dac644ee22b4f91734f) | 延後 carrier-on 到工作佇列，避免清除舊 URB 時又不斷加入新的接收工作；包含後續修正，確保關閉狀態能重新啟用 carrier。 |
| [取消仍在執行的 kevent](https://github.com/gregkh/linux/commit/43005002b60ef3424719ecda16d124714b45da3b) | 裝置 unregister 後先等待工作佇列停止，再清理設備。 |
| [paused RX 的 CPU／bottom-half 修正](https://github.com/gregkh/linux/commit/65d04291adf7c59338f87aab9c6fe0bfa9993e64) | 恢復暫停的接收佇列時保護 CPU 相關計數器的處理區段。 |

這些是實際存在的驅動缺陷，但昨天沒有保存其 RCU stall／kevent／CPU 警告的完整對應證據，**尚不能證明其中一項就是十段空中卡死的唯一原因**。

來源在 `kernel/usbnet-backport/`，保留 GPL 授權、官方 commit 及來源雜湊。只新增官方 event bit 14，沒有修改 `struct usbnet` 布局。以現有 NVIDIA headers 編譯，37 個匯出與 102 個匯入符號（含 `module_layout`）的 CRC 全部匹配。

DKMS 已安裝，僅適用 `5.15.185-tegra`，避免覆蓋未來其他版本的官方驅動。11:30 時尚未實測重開機；使用者後續多次重開機，13:35 已確認仍載入回補版本。目前 `/boot/initrd` 不含 `usbnet`／`cdc_ether`，根檔案系統的模組選擇已指向回補版本。此模組為本機建置，核心載入時記錄未簽署模組的 taint；此輪核心版本與控制器韌體沒有變更。

### 恢復判斷的兩個缺口

1. 原外網 TLS、公共 DNS、網站探測使用預設路由；D-Link 消失、Wi-Fi 接手時，可能把 Wi-Fi 成功當成網卡健康。現在閘道、外網 TLS、公共 DNS 及 HTTP 都固定綁定 D-Link，介面／carrier／USB 身分缺失也不接受其他外網的成功。loopback resolver 仍使用本機路徑，每筆證據新增 `probe_interface`。
2. 原 policy 在正常 30 秒後清除嘗試次數，反覆故障可能一直回到第一次 USB reset。現在保存五分鐘內的明確資料通道卡死歷史，跨同次開機的服務重啟保留；第一段可依原 15 秒門檻重設網卡，再次卡死可升級到指定 `cdc_ether` 介面重新綁定。一般退避、45 秒等待、無總次數上限與固定設備身分檢查保留。

新恢復程式已於 11:15:26 部署。驅動載入前短暫停止 recovery，載入後接回同一張網卡的既有 4G profile，隨即恢復 recovery；沒有重啟 UAV 或 Tunnel。

## USB 韌體假說的核對

兩版官方 `xusb_t234_prod.bin` SHA-256 完全相同：

```text
be18a8fbf280970c2736475f64f31cc68c6e370cf55d16e67cad88cdd44c7744
```

`xhci-tegra.c` 也完全相同。因此公開新版不是一份已確認能修復此故障的新 XUSB 韌體。通用 `xhci-ring.c` 有其他修正，但此次 `comp_code=6`、TRB 不在目前 TD 的報錯分支沒有因此消失。

今天 10:39:14 地面日誌也出現相同 TRB 異常，隨後飛控 `1-2.4` USB disconnect，10:39:17 應用接回。`fuser` 檢查時只有正式 UAV 程序占用 serial。這不能證明 D-Link 與飛控必定由同一個韌體缺陷造成，本輪也沒有根治飛控長時間失聯。

新增 enabled／active 的 `uav-usb-error-recorder`，在獨立 tracefs instance 中記錄 completion codes 2–12（含 transaction、babble、STALL）及當時 slot 對應設備。沒有抓封包內容或重設裝置；紀錄為 `/var/lib/laiting-uav-usb-evidence/errors.jsonl`，root 私有，每檔 4 MiB 加三個輪替備份。一般 journal／既有 recorder 繼續保存其他錯誤與斷線。

## 驗證結果與限制

- 55 項相關測試通過（7.19 秒），包括恢復判斷、Wi-Fi 假成功拒絕、反覆卡死升級、真實可達 TCP listener 無法繞過缺失的指定介面、Tunnel／DNS／紀錄及新 USB recorder。
- 驅動實際載入、37／102 符號 CRC 驗證及 DKMS `installed` 均確認成功。
- 11:24:30–11:30:00 地面取樣 320 筆；唯一一次公開 health 失敗在 11:25:16 主動切換驅動當下，本機 health 全部正常。
- 載入後 274 筆（11:25:21–11:30:00）公開／本機 health 全成功，RX／TX errors 一直為 0，成功收發持續增加。
- 32 次 1 MiB 合成 4G 下載：31 次成功，一次於主動切換當下逾時；載入後的 24 次全成功。受控切換沒有記為新故障。
- 新 USB recorder 此期間 0 筆錯誤；查驗的 trace buffer 無 overrun／dropped events。沒有新的 USB disconnect 或 TRB 核心異常。

**切換前的地面取樣也正常，本次沒有重現原始空中卡死。** 結果證明回補版本可載入、目前 4G 正常、兩個恢復邏輯缺口已修正；飛行可靠度仍未驗收。不能排除設備韌體、接點／USB 供電、控制器／Hub 路徑或無線狀態變化觸發的其他故障。

## 證據、檢查與還原

保存於 `data/maintenance/20261006-usb-fix/`（700）：修改前程式、兩版 NVIDIA 原始碼、韌體對照、driver patch／來源雜湊、ABI 驗證、切換前後狀態、4G 逐秒取樣及下載結果。沒有保存密碼。

```bash
cat /sys/module/usbnet/version
modinfo -F filename usbnet
python3 scripts/modem_recovery.py check
ip route get 1.1.1.1
journalctl -u uav-modem-recovery -u uav-usb-error-recorder -f
sudo tail -n 20 /var/lib/laiting-uav-usb-evidence/errors.jsonl

# 落地時還原 stock 驅動，指定網卡會短暫中斷
sudo python3 scripts/usbnet_backport.py rollback
```

Stock 模組另備份在 `/var/backups/laiting-uav/usbnet-20261006/usbnet.stock.ko`。原 recovery 程式是 maintenance 目錄的 `modem_recovery.py`；還原 `/usr/local/lib/laiting-uav/modem_recovery.py` 後只重啟 `uav-modem-recovery`。新增 recorder 可用 `sudo systemctl disable --now uav-usb-error-recorder` 停用，不影響其他紀錄或裝置。

## 後續復發與新的定位

13:14、13:17、13:20 在新 USB 接口仍捕捉到 D-Link 大量 code 4 及收發凍結，證明本次 `usbnet` 回補沒有根治 transaction failure。13:33:29 另部署「最後收發進展／USB 重新列舉世代」的恢復基準修正，64 項測試通過，UAV／Tunnel 保持原 PID。其後完成相容 xHCI 修補 Image，15:39 起已載入；17 時後仍復發。最新恢復流程與實機驗證以上方 18:26 更新及 [斷訊分析](INCIDENT_2026-10-06_1713.md) 為準。
