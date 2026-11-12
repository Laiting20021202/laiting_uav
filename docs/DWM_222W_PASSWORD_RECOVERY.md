# DWM-222W 管理密碼恢復

> **2026-10-06 最新核對：使用者完成實體重設後，原廠管理介面 `status1/sysinfo` 已認證讀回 `device_name=DWM-222W`、`hardware_version=B1`、`version_num=1.00.09`，且 `admin/admin` 登入成功。這項直接讀值解決了先前「DWM-222、沒有 Reset」的型號疑問；以下步驟適用已核對的這張卡。密碼恢復已完成，毋須再次重設。**

台灣 B1 官方 [QIG 1.10(TW)](https://www.dlinktw.com.tw/techsupport/download.ashx?file=16623) 與 [使用手冊 1.01(WW)](https://www.dlinktw.com.tw/techsupport/download.ashx?file=16649) 均指定 **按住 Reset 8 秒**。這裡採用台灣版本的步驟；其他地區較舊文件的 5 秒不能取代本次核對結果。

## 重設前

先保存目前可取得的網卡設定，尤其手動 APN、電信驗證方式、DHCP／Wi-Fi 設定，並確認 Jetson 的 Wi-Fi 備援可用。忘記密碼時可能無法匯出網卡內的設定；Jetson 的 NetworkManager 連線備份不包含網卡內的 APN。

**Factory reset 會清除網卡的全部設定，並暫時中斷 D-Link 4G／USB Ethernet 連線。** 這項操作恢復管理存取權，不能當作 USB 傳輸錯誤已根治的驗證。

## 實體操作

1. 保持 DWM-222W 插在 USB 上供電，不拔除 SIM。
2. 使用迴紋針按住機身 Reset **8 秒**，看到 LED 開始閃爍後放開，等待網卡重新啟動與列舉。Reset 位置可參照官方 QIG 圖示／使用手冊第 4 頁。
3. 開啟 `https://192.168.125.1`；預設使用者名稱為 `admin`、密碼為 `admin`。核對的是管理帳戶，與 Wi-Fi 密碼不同。
4. 確認 SIM／APN 與行動連線設定，再確認 Jetson 取得原有 4G 介面的 DHCP 位址、指定 4G 探測成功，以及網站／SSH Tunnel 恢復。

## `admin/admin` 的限制

重設後的官方管理預設值是 `admin/admin`。但 1.01(WW) 手冊第 11–12 頁規定首次登入必須設定符合強度的新密碼：至少 10 字元，並包含四類字元中的至少三類。若實卡管理介面實施這項規則，便無法把 `admin` 保存為永久管理密碼；應以實際介面與韌體版本核對，不能跳過規則而宣稱變更成功。

核對日期：2026-10-06。官方 QIG 與文字證據保存於 `data/maintenance/20261006-repeat-outage-132751/vendor-research/`。
