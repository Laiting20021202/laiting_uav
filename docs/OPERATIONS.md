# 系統操作與故障排查

本文件依 2026-11-14 的程式與實機設定整理；專案位置為 `/home/hrc/laiting_uav`。
服務管理指令請以 `hrc` 帳號執行。`systemctl --user` 管理此帳號的服務，
不要改成 `sudo systemctl --user`。

## 1. 使用哪些 Docker

目前只有一個應用容器，Compose 專案為 `laiting-ros2`：

| 項目 | 實際設定 |
| --- | --- |
| Compose 服務 | `gateway` |
| 容器名稱 | `laiting-ros2-gateway-1` |
| 應用映像 | `laiting-uav-ros2:humble-v1`，目前映像 ID 見 `ros2/image.lock` |
| 基底 | 官方 ROS 2 Humble 映像，`ros2/Dockerfile` 以 digest 固定 |
| 容器責任 | 發布十個 ROS topics、實際訂閱監測、Web GUI、rosbag2、驗證、ZIP、歷史下載 |
| 網路／ROS | host network、host IPC、domain 42、Fast DDS；GUI TCP 8090 |
| 資源 | 2 CPU、1400 MiB RAM、160 pids；Docker stdout 最多 10 MiB × 3 |
| 持久資料 | 宿主機 `data/ros2/storage/`；原始 bag、ZIP 與日誌不存容器可寫層 |

**相機 RTSP、AP6 序列埠、YOLO/GPU、原網頁、Cloudflare 與網卡恢復仍在宿主機。**
容器透過 Unix socket 取得原服務資料，不直接開 USB、相機或 GPU。
相機／飛控驅動和 Jetson CUDA 環境仍依賴宿主機。
Docker 映像二進位不會隨 GitHub 程式上傳；新機需依 `ros2/README.md` 建置或載入已保存映像。

開機鏈為：Docker daemon 隨系統啟動；`hrc` 的 Linger 讓 user services 在登入前啟動；
`uav-v2` 負責硬體，`uav-ros2` 啟動 Compose，`uav-cloudflared` 提供外網入口。
網路恢復與診斷服務另外運行。正常開機不重新 build 或 pull。

## 2. 重要程式各自做什麼

| 程式／設定 | 用途 |
| --- | --- |
| `uav/__main__.py`、`uav/runtime.py` | 原系統入口；管理取像、辨識、遙測及服務健康狀態 |
| `uav/camera.py`、`uav/g3p.py` | G3P 相機 RTSP／FFmpeg 取像、相機設定、斷線重連 |
| `uav/telemetry.py` | 唯一 AP6 MAVLink reader、來源篩選、心跳、資料頻率要求與重連 |
| `uav/detection.py`、`uav/prediction.py` | YOLO 推論、追蹤與候選類別處理 |
| `uav/geo.py`、`uav/storage.py`、`uav/flight_tracks.py` | 目標座標估算、事件儲存、飛行軌跡；估算前提見定位文件 |
| `uav/web.py`、`uav/auth.py`、`uav/web/` | 原網站、登入／session／CSRF、預覽與操作介面 |
| `uav/ros_bridge.py`、`uav/ros_proxy.py` | 宿主機的唯讀原始資料橋接，以及既有登入保護下的 ROS GUI 轉送 |
| `ros2/uav_ros/main.py`、`source.py` | 容器入口、讀取橋接、發布十 topics 並建立真實訂閱者 |
| `ros2/uav_ros/convert.py`、`contract.py` | 固定名稱／型別、SI 單位、FRD/FLU 與 NED/ENU、資料有效性及 QoS |
| `ros2/uav_ros/monitor.py` | 實收 Hz、計數、資料年齡、有效／無效、等待／逾時／來源斷線 |
| `ros2/uav_ros/bags.py` | 錄製互斥、正常停止、逐筆驗證、摘要、SHA-256、自動 ZIP 與歷史管理 |
| `ros2/uav_ros/server.py`、`ros2/web/` | ROS Web GUI、錄製／停止／下載 API |
| `ros2/uav_ros/probe.py` | 獨立 ROS 訂閱探針，檢查是否持續收到資料及內容有效性 |
| `ros2/compose.yaml`、`Dockerfile`、`config/` | 容器、鎖定依賴、頻率／逾時、磁碟限制與 DDS 設定 |
| `ros2/deploy.py`、`ros2/systemd/` | 安裝／還原 systemd 與橋接；不在開機時建置映像 |
| `scripts/services.py`、`scripts/tunnel_supervisor.py` | 原 systemd 服務安裝，以及 Cloudflare 網路切換／重連 |
| `scripts/modem_recovery.py`、`modem_path_manager.py`、`modem_rf_guard.py` | 行動網卡的恢復、路徑與無線狀態管理 |
| `kernel/usbnet-backport/`、`kernel/xhci-short-retry-backport/` | Jetson 指定核心的 USB 驅動修補來源；一般啟動不用手動編譯 |

主要設定分別是宿主機 `config/settings.json`、模型的 `models/*/registry.json`、
以及 ROS 的 `ros2/config/settings.json`。密碼與 Tunnel 憑證不在公開 Git 內。

## 3. 如何確定各部分正常

### 先確認服務，再確認硬體及資料

```bash
cd /home/hrc/laiting_uav
systemctl is-active docker
systemctl --user status uav-v2 uav-ros2 uav-cloudflared --no-pager
docker compose -f ros2/compose.yaml ps
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8090/healthz
.venv/bin/python scripts/manage.py doctor
```

`active`、`healthy`、HTTP 200 只表示對應程式有回應，不能證明感測器有資料。

- 原地面站：登入 https://uav.laiting1202.com/，看相機影像是否繼續變化、飛控是否在線、
  模型是否載入且沒有錯誤；推論時間／影格更新持續變化才表示辨識迴圈有工作。
  無目標時沒有框並不表示模型故障；辨識正確率與定位精度需另外用真實目標驗證。
- ROS 面板：https://uav.laiting1202.com/ros2/；或同區網 `http://<Jetson IP>:8090/`，
  使用原帳號。逐項查看實際 Hz、累計數是否增加、最後資料年齡及有效狀態。
  `waiting`、`timeout`、`disconnected`、`invalid` 都不能當成正常。
- GPS：有訊息不代表定位有效。須檢查 fix、座標與 WGS84 橢球高；不能拿 MSL 或相對 home 高度代填。
- 錄製：開始前檢查缺項，停止後須經 `validating → packaging → ready`。
  failed 包保留診斷資料，不能當完成 ZIP；partial 包仍有缺少／無效資料。
  歷史清單可選包下載，依 ZIP 內 README 執行 SHA 校驗、`ros2 bag info` 與回放。
- 網路：本機頁面正常但外網不通，檢查 `uav-cloudflared` 及行動網卡恢復日誌。
  HTTPS 網站通不等於另一台電腦能收到 DDS。

獨立訂閱容器內實際資料（不是只列 topic）：

```bash
docker compose -f ros2/compose.yaml exec -T gateway bash -c \
  'source /opt/ros/humble/setup.bash; python3 -m uav_ros.probe --seconds 20 --require-all-valid'
```

宿主機或另一台已安裝 Humble 的電腦，從專案根目錄執行：

```bash
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=42 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE="$PWD/ros2/config/fastdds.xml"
PYTHONPATH="$PWD/ros2:$PYTHONPATH" python3 -m uav_ros.probe \
  --seconds 20 --require-all-valid --output receiver.json
```

通過條件包括每個 topic 持續收到多筆、內容有效，且沒有無效筆數。
未加 `--require-all-valid` 的 probe 只輸出報告，exit 0 不代表十項通過。
外部端須 domain、DDS 雙向 UDP 與 QoS 相容：影像 reliable，其餘 best_effort；
Cloudflare 不轉送 DDS。外部接收與完整驗收限制見 `ros2/VALIDATION.md`。

### 本次上傳前的實際狀態（2026-11-14）

- `docker`、`uav-v2`、`uav-ros2`、`uav-cloudflared` active；ROS 容器 healthy。
- 上述服務皆已設定開機啟用，`Linger=yes`。這不是整機重新開機的實測證據。
- 相機 Ethernet `eno1` 無 carrier，RTSP 來源 `192.168.144.135` 連線逾時。
  AP6 設定的 USB 裝置未出現；目前未更換設定或搶開其他序列埠。
- GUI 如實顯示來源斷線；容器與宿主機各自訂閱十秒，十 topics 的新訊息數均為 0。
  **目前未達硬體資料正常條件**。請先確認相機與飛控供電、線路及裝置枚舉。
- 沒有進行新的錄製、重啟硬體服務或整機重開機；歷史錄製仍保留。
  本次可重現檢查摘要見 `ros2/validation/system-check-2026-11-14.json`。
- 本次 ROS gateway、遙測、runtime、登入與安全相關回歸測試為 **81 passed、1 warning**；
  warning 是既有 FastAPI TestClient 的 Starlette 棄用提示，不是硬體驗收通過證據。
- 先前 11/12 的實機錄製／回放與 11/14 的時長修正驗證見 `ros2/VALIDATION.md`，
  不能將歷史結果當成目前硬體在線。

### 常用日誌與儲存檢查

```bash
journalctl --user -u uav-v2 -n 100 --no-pager
journalctl --user -u uav-ros2 -n 100 --no-pager
journalctl --user -u uav-cloudflared -n 100 --no-pager
docker compose -f ros2/compose.yaml logs --tail 100 gateway
systemctl --user list-units --all 'uav*'
systemctl list-units --all 'uav*'
df -h /home/hrc/laiting_uav
ls -l /dev/serial/by-id/
ip -brief address
```

bag 與 ZIP 在 `data/ros2/storage/recordings/`。目前磁碟可用約 14 GiB，
設定保留 2 GiB；每包最多 4 GiB／1800 秒，壓縮還需要額外空間。
定期把完成 ZIP 備份到外接碟，校驗後再管理歷史資料；不要刪正在錄製／打包的目錄。

## 4. 開機自動啟動失敗時如何手動啟動

### 已安裝服務、映像仍存在時

以 `hrc` 登入終端或 SSH：

```bash
cd /home/hrc/laiting_uav
# Docker daemon 是系統服務；這一條可能需要管理員密碼
sudo systemctl start docker
# 依現有 unit 啟動，不另開第二套硬體 reader
systemctl --user start uav-v2.service
systemctl --user start uav-cloudflared.service
systemctl --user start uav-ros2.service
systemctl --user status uav-v2 uav-cloudflared uav-ros2 --no-pager
docker compose -f ros2/compose.yaml ps
```

`start` 不會重啟已在運行的服務。已在運行卻異常時先查日誌，再只重啟故障項目：

```bash
systemctl --user restart uav-ros2       # ROS GUI／錄製；會影響進行中的錄製
systemctl --user restart uav-cloudflared # 僅外網／SSH Tunnel
systemctl --user restart uav-v2         # 相機／飛控／辨識會短暫中斷
```

### 恢復下次開機自啟

```bash
sudo systemctl enable --now docker
sudo loginctl enable-linger hrc
systemctl --user enable --now uav-v2 uav-cloudflared uav-ros2
loginctl show-user hrc -p Linger
systemctl --user is-enabled uav-v2 uav-cloudflared uav-ros2
```

如果顯示 `Unit ... not found`，代表服務檔尚未安裝或遺失。
確認原環境、模型、使用者密碼、Cloudflare 設定與映像已還原後才執行：

```bash
# 重建原服務定義；會重啟原網站與 Tunnel
.venv/bin/python scripts/services.py --tunnel
# 確認 ROS 映像已存在；不存在時依 ros2/README.md 建置或載入
docker image inspect laiting-uav-ros2:humble-v1 --format '{{.Id}}'
# 安裝 ROS unit 及橋接 override；會重啟一次原服務
python3 ros2/deploy.py install
```

若只想排查 Compose，不透過 user service 管理 ROS，可先停止管理者再前景啟動：

```bash
systemctl --user stop uav-ros2
docker compose -f ros2/compose.yaml up --no-build --pull never --abort-on-container-exit
# Ctrl+C 結束後，回到正常管理方式
systemctl --user start uav-ros2
```

此方式仍需 `uav-v2` 與橋接正常。不要在 `uav-v2` 已啟動時另執行
`python -m uav`，否則會搶用相機、飛控或 port 8080。

停用 ROS 自啟：`systemctl --user disable --now uav-ros2`。
完整還原到原本無 ROS 的啟動方式：`python3 ros2/deploy.py rollback`；錄製檔保留。

## GitHub 備份範圍

上傳包含程式、Dockerfile／Compose／鎖定設定、服務範本、文件、測試與小型驗證報告。
不包含密碼、Tunnel 憑證、虛擬環境、模型權重、錄影、執行資料庫、bag／ZIP、
Docker 映像及已安裝核心模組；這些仍須另外保存。詳細還原限制見 `GITHUB_BACKUP.md`。
