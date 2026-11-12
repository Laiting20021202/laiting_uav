# Cloudflare、SSH 與開機部署

專案：`/home/hrc/laiting_uav`。這台主機主動向 Cloudflare 建立 Tunnel，不需先 VPN 回實驗室，也不需要由外部連入路由器的公開 IP。

2026-10-05 已修復主機 DNS：一般程式統一經 `systemd-resolved` 本機 stub，行動網卡持久設定主備 DNS，WireGuard 不再注入全機 DNS。修復內容、備援實測與還原方式見 [DNS 修復紀錄](INCIDENT_2026-10-05_DNS.md)。

## 已設定的對應

| 名稱 | 服務 | 驗證 |
| --- | --- | --- |
| `uav.laiting1202.com` | `http://127.0.0.1:8080` | 本系統帳密登入，包含影像／遙測／紀錄／模型 API |
| `ssh-uav.laiting1202.com` | `ssh://127.0.0.1:22` | 主機原有 SSH 使用者／金鑰或系統密碼 |
| `laiting-uav-v2` | Cloudflare named tunnel | 主機本地 Tunnel 憑證 |

公開 DNS 查詢已確認 `laiting1202.com` 的 nameserver 為 `josh.ns.cloudflare.com` 與 `joyce.ns.cloudflare.com`。

## SSH 操作端

在要操作無人機的電腦安裝 [Cloudflare 官方 cloudflared](https://developers.cloudflare.com/tunnel/downloads/)，然後在那台電腦的 `~/.ssh/config` 加入：

```sshconfig
Host laiting-uav
    HostName ssh-uav.laiting1202.com
    User hrc
    ProxyCommand cloudflared access ssh --hostname %h
    ServerAliveInterval 15
    ServerAliveCountMax 3
    TCPKeepAlive yes
```

連線：

```bash
ssh laiting-uav
```

也可不修改設定檔：

```bash
ssh -o 'ProxyCommand=cloudflared access ssh --hostname %h' hrc@ssh-uav.laiting1202.com
```

這裡的 `hrc` 是 Linux 使用者。**網頁帳密不等於 Linux 密碼**，本次沒有修改 Linux 帳密，也沒有把網頁使用者 `laiting` 建成系統帳號。操作端若尚未有可登入主機的金鑰，可使用原本 SSH 系統密碼，或由主機擁有者安裝自己的 SSH 公鑰。

macOS 若 `cloudflared` 不在 SSH 的 PATH，將 ProxyCommand 改用 `command -v cloudflared` 找到的絕對路徑；Windows OpenSSH 同樣需要使用當地已安裝的 `cloudflared.exe` 路徑。

這種公開 hostname 的 SSH 方式需要操作端 `cloudflared` 作為 ProxyCommand，單純 `ssh hrc@ssh-uav.laiting1202.com` 不會自動穿過 Tunnel。[官方 SSH 連線說明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/ssh/ssh-cloudflared-authentication/)

若要額外限制誰能到達 SSH，可在 Cloudflare Zero Trust → Access → Applications 建立 **Self-hosted** 應用，domain 設為 `ssh-uav.laiting1202.com`，Allow policy 指定可登入者的電子郵件或身分提供者。啟用後，ProxyCommand 會先完成 Access 登入，再驗證 Linux SSH。網頁已有需求指定的帳密，不必為網頁再增加另一套登入。此 Access 身分政策須由擁有者選定，沒有自動加入任何 Email 或 Everyone 規則。

## 本機服務

```bash
cd /home/hrc/laiting_uav
.venv/bin/python scripts/services.py --tunnel
systemctl --user status uav-v2 uav-cloudflared
journalctl _SYSTEMD_USER_UNIT=uav-cloudflared.service -f
```

兩個服務各自 `Restart=always`、間隔 5 秒、沒有啟動次數封鎖；相機、模型失敗不會關閉 SSH Tunnel。主機已設定 `Linger=yes`，使 user service 在開機時啟動，不需要先登入桌面。SSH 系統服務本身也已 enabled。

Tunnel 開機先以 `nm-online -s -q -t 30` 等候 NetworkManager 初始化（最多 30 秒），再由 `scripts/tunnel_supervisor.py` 等候出口路由穩定 4 秒後啟動 cloudflared。每 2 秒檢查 IPv4／IPv6 出口的介面、閘道與來源 IP；新路由穩定 4 秒才重建 Tunnel，完全無路由持續 12 秒才停止舊程序。短暫斷線後回到相同路由時保留原有程序，讓 TCP／cloudflared 自行恢復，減少主動切斷 SSH。路由查詢本身失敗會保留現有程序並記錄錯誤。卡住的 cloudflared 停止程序最多等待 5 秒後強制結束。

2026-10-05 更新健康檢查：啟動寬限 20 秒後，本機 `/ready` 無有效 edge connection 持續 30 秒即重建；正常時每輪完成後等 15 秒，再檢查公開 SSH banner 及公開網頁 `/healthz`。失敗後改等 5 秒複查；同一項連續 3 次失敗、而其本機服務仍正常時重建 Tunnel。兩項失敗分別計數，單次失敗不切斷現有連線。本機 sshd 或網頁程式異常只記錄，不因此重啟 Tunnel。短暫掉線後同一路由恢復並穩定 4 秒即提前檢查；路由變更會清除累計失敗，無路由或路由尚未穩定時不做公網探測。SSH 探測不送帳密；HTTP 探測要求成功狀態及正確 JSON，保留 TLS 憑證驗證。每輪還包含探測耗時（SSH 公網 banner 最多 8 秒，HTTP 公網最多 5 秒及本機檢查），因此 15／5 秒不是總偵測或恢復時間。

同日 16:13 再補強：上述健康失敗觸發重建前，先證實 DNS 與独立 HTTPS 路徑可用；上游故障時保留 cloudflared 自行重連，上游恢復後再處理仍卡住的 Tunnel。HTTP 探測遇到 DNS／連線逾時，會依序以兩個 HTTPS DNS 端點重試，每次最多 5 秒，故最長探測時間比前述單次時間長。依賴 `dnspython==2.8.0`。獨立 `uav-network-health.service` 保存 DNS／HTTPS 分層結果；`uav-flight-network-monitor.service` 每約 10 秒保存閘道、介面收發／錯誤計數、本機、公網與 SSH 結果，遇到慢探測可能延長。兩項均只讀取，不控制網路。[15:51 事件](INCIDENT_2026-10-05_1551.md)、[16:28 網卡傳輸卡死](INCIDENT_2026-10-05_1628.md)。

Tunnel 與健康檢查透過 `scripts/tunnel_dns.sh` 在獨立的 user／mount namespace 執行，使用 `config/tunnel-resolv.conf` 的 `1.1.1.1`、`8.8.8.8`，查詢逾時 1 秒、兩輪嘗試，並設定 `single-request-reopen` 避開本次重現的 DNS 雙查詢漏回覆。這只替該服務掛載專用 resolver，主機 `/etc/resolv.conf`、VPN 和其他服務不變，不需主機 root 權限。需要 Linux 允許 unprivileged user namespaces，以及 `unshare`、`mount`、`curl`；本機已在正式服務的權限限制下驗證。公共 DNS 故障有備援，但整條行動網路失效仍需網路本身恢復。[事件與驗證](INCIDENT_2026-10-05.md)

安裝 Tunnel 時也會啟用獨立的 `uav-network-recorder.service`。它將 USB／核心錯誤、NetworkManager、時間校正、登入管理與 Tunnel 事件保存於 `~/.local/state/laiting-uav/network/events.jsonl`；每筆含 boot ID、原始時間與開機後單調時間，可辨別時鐘校正及不同次開機。每檔 4 MiB、保留 3 個輪替檔，總量約 16 MiB，檔案權限 600，逐筆同步到磁碟。服務啟動會重播本次開機事件，重播資料可能重複；這是診斷記錄，不會重設 USB、切換路由或重啟主機。需有讀取系統 journal 的權限（本機 hrc 已具備）。一般 kernel/syslog 紀錄仍保留；這項服務不等同整套 journal 持久化，也不能保證硬體斷電時所有資料都已落盤。

公開 SSH 探測由 `--ssh-hostname ssh-uav.laiting1202.com` 啟用，適用目前沒有互動式 Cloudflare Access 政策的部署。日後加入 Access 登入政策時需移除此參數或配置相應的服務驗證，否則健康探測會被 Access 擋住。[Cloudflare readiness 原始碼](https://github.com/cloudflare/cloudflared/blob/master/metrics/readiness.go)

2026-09-16 曾確認開機第 19–22 秒 Tunnel 透過 Wi-Fi 連線成功，第 22.57 秒行動網路成為優先出口；舊 TCP socket 仍使用 Wi-Fi IP，持續重傳，Cloudflare 已無有效連線，但本機 `/ready` 仍回報 4 條連線。公開網站回 1033、SSH 出現 `websocket: bad handshake`。因此不能只憑 systemd `active` 或本機 `/ready` 判定公網可用。user service 原有的 `After=network-online.target` 也不會等待系統層的 NetworkManager，現已改成上述實際等待與持續監看。

只更新 Tunnel、不重啟影像／飛控程式：

```bash
.venv/bin/python scripts/services.py --tunnel-only
/bin/sh scripts/tunnel_dns.sh curl --connect-timeout 5 --max-time 8 https://uav.laiting1202.com/healthz
```

開機仍需等硬體、網卡及外部網路可用；這項修正處理出口切換造成的卡住，不代表通電當下即可 SSH。切換網路時既有 SSH session 可能中斷，需重新連線。此次沒有重新開機量測修正後的完整啟動秒數。

`uav-v2` 另外設定 `WatchdogSec=120`。網頁事件迴圈每 5 秒檢查相機、飛控與推論工作是否仍前進，只有健康時通知 watchdog。相機／飛控離線等待重連並不算工作卡死。若系統完全卡住，超過最後一次通知約 120 秒會由 systemd 重啟，應用自身判斷停滯的門檻另需最多 60 秒；這不是瞬間復原保證。

網頁服務重啟由固定名稱的暫時 systemd timer `uav-v2-restart-request.timer` 觸發，兩秒後只重啟 `uav-v2.service`，用後自動回收。`UAV_MANAGED_SERVICE=1` 僅由正式 service 設定；demo／手動執行不提供網頁重啟主服務的能力。所有重連／重啟 API 都要求登入、同來源、CSRF token 並記錄操作者。

```bash
loginctl show-user hrc -p Linger
systemctl is-enabled ssh
```

網路暫斷時 cloudflared 自行重連。目前固定 `protocol: http2`：實機曾在 QUIC 連線重註冊時讓網頁收到 502／逾時，cloudflared precheck 也建議使用 HTTP/2。網路恢復時可重新 SSH；已中斷的原有互動式 SSH session 不保證自動恢復。[Cloudflare Tunnel 參數](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/#protocol)

## 在另一台主機重新部署

```bash
cd /home/hrc/laiting_uav
bash scripts/install.sh
# 只需尚未安裝時執行；從官方 release 下載並核對 digest
.venv/bin/python scripts/cloudflare.py install
# 會產生專用瀏覽器授權連結，選擇 laiting1202.com → Authorize
.venv/bin/python scripts/cloudflare.py login
# 建立或重用 laiting-uav-v2；設定兩筆 DNS，不強制覆寫既有 DNS
.venv/bin/python scripts/cloudflare.py setup
.venv/bin/python scripts/services.py --tunnel
```

既有 Tunnel 若沒有本地 `<UUID>.json`，需安全移轉該 Tunnel 憑證，或另行建立新的 Tunnel；僅有相同名稱不足以執行。不要讓兩台都連著不同飛機硬體卻共同提供這兩個 hostname，否則请求可能分流到不同主機。

原始憑證放在 `/home/hrc/.cloudflared/`，不在專案內。`cert.pem` 是管理權限憑證，執行 Tunnel 只需 `<UUID>.json`。產生的 `config/cloudflared.yml` 只參照憑證路徑。[官方本地管理 Tunnel 流程](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/create-local-tunnel/)

## 行動網卡優先與無人值守開機

`DWM-222W Taiwan Mobile`（`enxac0033aa9633`）已永久設定 `connection.autoconnect=yes`、`connection.autoconnect-priority=100`、`connection.autoconnect-retries=0`，且 `connection.permissions` 留空，允許未登入桌面時由 NetworkManager 啟動。`0` 表示持續嘗試自動連線，不使用預設重試次數後的暫停；見 [NetworkManager 官方說明](https://www.networkmanager.dev/docs/api/1.30/settings-connection.html)。

網卡的 IPv4／IPv6 `route-metric=100`，五個既有上網 Wi-Fi profile（`HRC631S_5G`、`HRC631S_Experiment_5G`、`HRC631T_5G`、`Louis`、`room 631_IoT`）皆保存為 `600`。同一 IP 協定的可用預設路由以網卡優先；網卡未接上或失去路由時，仍可走已連線的 Wi-Fi。這不是在行動網路上游故障、但網卡仍保有路由時的主動切換機制。

設定已寫入 NetworkManager profile，下次開機／網卡重連會套用；目前 IPv4 出口本來就是網卡、metric 100，持續維持此路由。`autoconnect-retries` 不能對已啟用的連線使用 `device reapply`，此次沒有為了套用它中斷行動網卡。原有非機密設定備份在 `/home/hrc/.local/state/laiting-uav/backups/2026-09-16/network-priority.json`。

已確認 `NetworkManager`、`ssh` 與兩個 UAV 服務皆 enabled，且 `/var/lib/systemd/linger/hrc` 存在。即使網卡超過啟動等待時間才出現，Tunnel supervisor 仍會持續等路由；路由穩定後自動啟動，並由 systemd 在程序失敗後重新啟動，沒有重啟次數上限。

## G3P 有線網路

已建立 NetworkManager `uav-g3p` profile：`eno1` → `192.168.144.10/24`，相機 `192.168.144.135`。不設 gateway、`ipv4.never-default=yes`，接線後自動啟用。上網走 Wi-Fi／行動網路原有介面，不會被相機網段接管。

```bash
# 換介面時可重新建立對應的 profile；既有 uav-g3p 不會被脚本覆寫
bash scripts/camera_network.sh eno1
nmcli connection show uav-g3p
ip route get 192.168.144.135
```

若飛控序列埠名稱改變，查看 `ls -l /dev/serial/by-id/` 並修改 `config/settings.json`。同一個飛控 serial 不能同時被舊版與新版程式占用。

目前 AP6 的設定已鎖定 USB 序號 `330032000D51333331333534`、MAVLink system ID 1／component ID 1。每次重連會先用 by-id 路徑，必要時依相同 USB 序號重新尋找 device；不會自動挑選無關的序列埠。若更換飛控硬體或改動 MAVLink SYSID，需同步更新設定。

飛控序列埠的寫入逾時為 1 秒，避免送出遙測頻率請求時永久阻塞。pymavlink 以 `portdead` 回報的寫入失敗也會進入自動重連；關閉失效連線若發生例外，會記錄錯誤並繼續重試，不會結束遙測執行緒。心跳期限仍為 8 秒，失敗後每 3 秒重試；USB 尚未被 Linux 辨識時，應先檢查核心紀錄，重連按鈕無法修復硬體枚舉失敗。

```bash
# user service 紀錄也可透過系統 journal 的欄位查詢
journalctl _SYSTEMD_USER_UNIT=uav-v2.service --since '10 minutes ago' --no-pager
journalctl -k --since '10 minutes ago' --no-pager
ls -l /dev/serial/by-id/
```

若核心出現 `device descriptor read/64, error -110`、`device not accepting address, error -71` 或 `unable to enumerate USB device`，表示作業系統尚未成功建立飛控 USB 連線；需檢查線材、接頭、USB Hub／供電及飛控端狀態，不能僅憑這些錯誤確定是哪一項故障。[Linux USB 錯誤碼說明](https://www.kernel.org/doc/html/latest/driver-api/usb/error-codes.html)。2026-09-21 檢查時，AP6 已被 ModemManager 排除，裝置的 `power/control=on`，因此未把停用 ModemManager 或 USB 省電當成本次修復。

相機解碼會根據本機 FFmpeg RTSP 選項選擇 `-stimeout`（這台 FFmpeg 4.x）或新版 `-timeout`。FFmpeg 使用者空間讀取也有獨立完整影格期限，探測程序同樣可以被重新連線按鈕取消；不會遺留舊解碼器繼續讀串流。

ffprobe 返回成功並不代表影像可用：若 H.264 未送出必要資料，可能回傳寬高皆為 0。系統現在會先檢查正整數尺寸，再算比例，並持續等待恢復。G3P 自動恢復僅在串流失敗後進行，控制指令之間及之後保留等待時間，恢復失敗後依 30／60／120／240／300 秒退避重試；正常取像時不週期性重置相機。拒絕碼僅如實呈現，不推論成低電量或其他特定硬體原因。

`camera.refresh_s=240` 會定期重建 FFmpeg／RTSP session，但不能保證消除韌體卡死。2026-09-21 16:37–17:05 的實機紀錄顯示，多次成功重連後仍可能失效。這不送 G3P 重置命令，也不清除最後影格；正常情況下新 session 會在畫面 3 秒新鮮期限內接回。若仍超過 `camera.timeout_s=5` 沒有完整影格，才執行 G3P 編碼器恢復。連續三次 -21 會提示使用者將相機斷電 10 秒再上電；RTSP 自動重連持續運作。失敗的控制握手亦會關閉 socket，避免累積連線。

G3P 的 TCP 7878 控制通道只在服務啟動及串流故障恢復時使用。服務先查詢 `camera.g3p_settings`；設定不符時送出 `STOP_VF(260)`，另開新 session 套用 1080p/30、EIS 關閉、1080p 次串流 1 Mbps，再以 `RESETVF(259)/none_force` 啟動串流並逐項回讀。`STOP_VF` 會使本機型當前 token 失效，因此設定必須在新 session 完成。正常 RTSP 取像期間不保留控制連線，也不做週期性 viewfinder 重置。

關閉控制 socket 時一律不發送 `STOP_SESSION(258)`；實機已證實 258 會立即停止 RTSP。等待指令期間仍會接收相機通知，並回覆 `0x701/SESSION_HOLD`。GUI 及 `/api/status` 的 `camera.control` 會顯示已套用設定、session 次數、最近通知與錯誤。請勿在取像期間用其他診斷程式送出 258。

持久控制查詢、協定心跳、降低編碼負載都曾通過命令回覆測試，但實機仍在約 8–11 分鐘停止編碼，因此不能當作單獨修復。舊 `/home/hrc/xin_uav` 的長時間 ROS 日誌同樣反覆出現 `ffmpeg frame read timed out`；舊介面用一秒重開 FFmpeg，使 RTSP session 經常換新，也遮住部分中斷。現版將這項行為改成固定、可觀測的 240 秒預防性重連，故障時仍保留 G3P 自動恢復。

模型清單可放 `models/registry.json`，或各組的 `models/<組別>/registry.json`；存在根清單時優先使用，否則合併各組並拒絕重複名稱。權重路徑相對各清單的目錄，最終必須仍位於 `models/` 內。目前預設為 `models/president_cup_v5/best.pt`，服務名稱 `president-cup-v5`；另提供 `models/competition_5class_v1-3/weights/best.pt`（`competition-5class-v1-3`）。兩者均為五類車輛、輸入 1536、CUDA 可用時使用 FP16。網頁可調整 1–100% 信心門檻，初始為 30%，套用後即時更新推論並原子儲存到啟動時指定的設定檔；首次辨識立即紀錄，同一段連續追蹤只記一次。清單載入失敗會顯示錯誤，不影響獨立的影像／遙測更新。

## 常見狀態

| 現象 | 檢查 |
| --- | --- |
| 網域 1033 | `systemctl --user status uav-cloudflared`、Tunnel 日誌與主機上網狀態 |
| 網域 502 | `systemctl --user status uav-v2`；`curl http://127.0.0.1:8080/healthz` |
| `/api/status` 回 401 | 未登入、閒置逾時、密碼變更或 session 檔案遺失；持續觀看會自動續期，服務重啟保留有效登入。前端再次確認失效後顯示登入原因 |
| 本機 HTTP 無法登入實機模式 | 正式 cookie 要求 HTTPS；本機展示請用 8081 的 demo，不關閉正式 cookie 保護 |
| 網頁有畫面但 GPS 無效 | 檢查 RTK fix、資料年齡、相機校正、地面高度與姿態設定 |
| 相機連不上 | 檢查 Ethernet 連線、G3P 供電、RTSP 預覽、相機來源與寬高比例 |
| 相機 ping 通卻沒畫面 | 網路可達不等於編碼器有輸出；檢查網頁相機錯誤。若顯示拒絕串流恢復，重啟主機程式未必有效，需檢查相機本體，必要時單獨斷電重開 |
| 模型載入失敗 | 網頁顯示失敗；在 journal 查看細節；可切回 `visdrone-engine` 或相容 `.pt` |
| SSH 操作端無法找到 cloudflared | 安裝客戶端並在 ProxyCommand 使用絕對路徑 |
| SSH 要求密碼 | 是 Linux 使用者的原本 SSH 密碼，與網頁登入不同 |

這條連線不以 ping 作為可用性條件。請實際測 HTTPS 或 SSH；主機本身仍需要可用網路。
