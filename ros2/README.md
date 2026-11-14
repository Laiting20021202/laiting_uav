# ROS 2 Humble、Web GUI 與 rosbag2 部署

此部署沿用宿主機 `uav-v2.service` 的 G3P RTSP、AP6 MAVLink、YOLO/GPU 與 Cloudflare。新增的 ROS 發布、實際訂閱監測、GUI、錄製、驗證、ZIP 均由一個 Docker Compose `gateway` 服務管理。宿主機只增加 opt-in 唯讀 Unix socket 橋接；不另開 RTSP session，不搶序列埠，不搬動 Jetson CUDA/TensorRT 環境。未啟用橋接時原服務維持原行為。沒有新增飛行控制命令，只有既有類型的訊息更新率要求。

- 既有網站登入後：**https://uav.laiting1202.com/ros2/**；地面站底部也有入口。
- 同區網：`http://<Jetson 的區網 IP>:8090/`，使用原有網頁帳號密碼。此 HTTP 入口適用受信任區網；外網操作使用既有 HTTPS。
- 本機：`http://127.0.0.1:8090/`。
- 實測結果與限制：[VALIDATION.md](VALIDATION.md)。實機證據在 `../data/ros2/evidence/`。

## 資料契約

| Topic | 固定型別 | 真實來源、轉換 | 預期 Hz / 逾時 s |
| --- | --- | --- | --- |
| `/uav/camera/image_raw` | `sensor_msgs/msg/Image` | 原有解碼器未畫框的 1920×1080 BGR8，原像素無損橋接 | 4 / 3 |
| `/uav/imu/data_raw` | `sensor_msgs/msg/Imu` | SCALED_IMU，mG × 9.80665/1000 → m/s²；mrad/s ÷1000 → rad/s | 20 / 2 |
| `/uav/attitude` | `geometry_msgs/msg/QuaternionStamped` | ATTITUDE 飛控估計 Euler 轉四元數；FRD→NED 轉為 FLU→ENU | 20 / 2 |
| `/uav/gps/fix` | `sensor_msgs/msg/NavSatFix` | GPS_RAW_INT 經緯度、MAVLink 2 明確提供的 alt_ellipsoid ÷1000 | 5 / 3 |
| `/uav/altitude/relative_home` | `std_msgs/msg/Float64` | GLOBAL_POSITION_INT.relative_alt ÷1000，m；**不是實測 AGL** | 10 / 2 |
| `/uav/altitude/msl` | `std_msgs/msg/Float64` | GLOBAL_POSITION_INT.alt ÷1000，飛控估計 MSL，m | 10 / 2 |
| `/uav/velocity/enu` | `geometry_msgs/msg/Vector3Stamped` | GLOBAL_POSITION_INT，`(vy,vx,-vz)/100`，m/s | 10 / 2 |
| `/uav/battery/state` | `sensor_msgs/msg/BatteryState` | SYS_STATUS，mV→V、cA→A、百分比→0–1；ROS 電流負值表示放電 | 2 / 5 |
| `/uav/imu/mag` | `sensor_msgs/msg/MagneticField` | SCALED_IMU，milliGauss × 1e-7 → Tesla | 20 / 2 |
| `/uav/barometer/pressure` | `sensor_msgs/msg/FluidPressure` | SCALED_PRESSURE，hPa ×100 → Pa | 5 / 3 |

依據 [MAVLink common 定義](https://mavlink.io/en/messages/common.html)、[REP-103](https://github.com/ros-infrastructure/rep/blob/master/rep-0103.rst) 與 [rosbag2 Humble](https://github.com/ros2/rosbag2/tree/humble)。IMU、磁場 frame=`base_link`（FLU）；姿態與速度 frame=`map_enu`；GPS=`gps_link`；影像=`camera_optical_frame`；壓力=`barometer_link`。姿態表示機體 FLU 到世界 ENU 的旋轉。未量測鏡頭外參，不發布假的相機到機體 TF。相機與 IMU 物理外參須另行校正。

Header 使用宿主機 **UTC 接收時間**，相機為 FFmpeg 完成解碼取樣的時間；不把飛控開機毫秒當 Unix 時間，也不宣稱感測器硬體同步。無 header 的 Float64 以 rosbag 接收時間定位。狀態逾時、頻率與 watchdog 使用 monotonic 時鐘，監測也檢查 header 過期／未來時間。`timedatectl` 可檢查宿主機 NTP。精密融合另需相機硬體曝光時間、FC TIMESYNC 與校正，這個部署未提供該精度。

原始 IMU 沒有姿態，`orientation_covariance[0]=-1`，四元數為無效零向量，沒有假單位四元數。未知 covariance 的全零依 ROS 規範表示未知，並非量測值補零。未知電池欄位為 NaN，狀態／化學種類為 UNKNOWN。MAVLink 真實回報的零值保留，不合成資料。磁場全零、非有限值、無效 GPS、缺少電池主要欄位等會標記無效。

GPS 無 3D fix 時座標為 NaN、status=NO_FIX；GPS 定位訊息仍持續發布，GUI 分開顯示收到訊息與內容有效。MSL **不代填**橢球高。缺少 GPS_RAW_INT extension 時，altitude=NaN，GUI 說明缺少 WGS84 橢球高；即使有可用經緯度，也不宣稱完整 GPS topic 有效。只有確認來源韌體確實提供此欄位時，才可把 `gps_ellipsoid_confirmed` 設 true，以接受零值 extension（包括封包較長但欄位仍為零、或被 MAVLink 2 截去的情況）。不要用這個設定掩蓋 MAVLink 1 缺少資料。未從 HDOP 推造公尺 covariance，也不猜 GNSS 星系。MSL、姿態、速度是飛控估計值，其絕對精度不因數字有限而得到保證。

本專案未找到既有 Unity 六 topic 程式或型別定義；未刪除或替換任何介面。若外部 Unity 有獨立介面，需提供其 message 定義、frame 與座標規格，再加實際轉換層；這十個 topic 不會被六個介面替代。

## 建置及開機部署

目前映像針對本機 **arm64 Jetson / Ubuntu 22.04**。宿主機需已有正常運作的原專案、Docker Engine、Compose、使用者 docker 群組、systemd user manager 及 Linger。USB、Ethernet、Jetson GPU、NVIDIA kernel/usbnet/xHCI 驅動都仍由宿主機負責；Docker 不是獨立虛擬機。本容器不需 privileged、USB passthrough 或 GPU，因為原服務負責裝置與推論。

```bash
cd /home/hrc/laiting_uav
# 初次或程式更新時才建置（需網路）
docker compose -f ros2/compose.yaml build
# 安裝 user systemd、啟用唯讀橋接並啟動；會正常重啟一次原服務
python3 ros2/deploy.py install
```

Dockerfile 固定官方 Humble base digest；`config/apt.lock` 固定新增套件及相依版本，不偷偷改用最新版。所有實際安裝版本另存映像 `/opt/uav-packages.lock`。若套件庫移除舊版，建置會失敗；正式部署應保存已驗證映像（`docker image save laiting-uav-ros2:humble-v1 -o /外接磁碟/uav-ros2.tar`）或放入自己的不可變 registry。amd64 需另行產生對應的 apt lock，不能宣稱這份 arm64 lock 已驗證跨架構。

開機只執行 Compose `up --no-build --pull never`，不重建、不下載依賴。Docker daemon 已隨系統啟動；user service 經 Linger 在未登入時啟動，排序在 `uav-v2` 後，Docker 尚未可用時由 systemd 重試。來源尚未上線時 GUI 顯示等待／斷線，原硬體服務與 Docker 橋接均持續重連。只有原服務開啟 USB 和 RTSP。原有 Cloudflare、網卡恢復、USB 驅動服務不改動。

```bash
systemctl --user status uav-v2 uav-ros2
journalctl --user -u uav-ros2 -f
docker compose -f ros2/compose.yaml ps
docker compose -f ros2/compose.yaml logs --tail 100 -f
systemctl --user restart uav-ros2
systemctl --user stop uav-ros2
systemctl --user disable --now uav-ros2
# 完整還原原啟動方式，移除 bridge override，錄製資料保留
python3 ros2/deploy.py rollback
```

## 設定、資源與持久化

`config/settings.json` 中十個 topics 的 `expected_hz`、`timeout_s` 可分別設定；expected 是監測目標，不是憑空提高發布率。`camera_poll_hz` 控制橋接相機上限，來源原有 4 FPS 取樣、最新影格模式會跳過來不及處理的幀。飛控要求速率在宿主機 `config/settings.json` 的選填 `ros_message_rates`：預設額外要求 SCALED_IMU=20、SCALED_PRESSURE=5（實機已確認不支援 HIGHRES_IMU；只對必要舊訊息持續補要求）。實際能力請看 GUI／probe，不能把要求率當實測率。IMU／磁場／氣壓選擇明確來源，不在不同感測器之間任意交替。HIGHRES_IMU 備用來源必須選定，且只接受 fields_updated 對應量實際更新的封包；RAW_IMU 的 raw 單位未確認，不拿來冒充 SI。

修改 ROS 設定後 `systemctl --user restart uav-ros2`，修改原服務的訊息要求率後重啟 `uav-v2`。Compose 預設 domain=42，改由 Compose `.env` 或 unit 環境設定，所有接收端須一致。不同 UID/GID 用 `ros2/.env` 的 UAV_UID/UAV_GID 設定為實際使用者，並確保資料目錄擁有者相同。

- `data/ros2/source/`：唯讀來源 Unix socket，權限 600；包含原始資料與最新帳號雜湊的私有讀取介面，無法發送飛控指令。
- `data/ros2/control/`：Docker GUI 私有 socket，由原網站以登入、Origin、CSRF 保護轉送。
- `data/ros2/storage/recordings/<UTC時間_UUID>/`：每次獨立的 `bag/`、job、摘要、日誌、ZIP、ZIP SHA-256。
- `data/ros2/storage/logs/`：ROS 日誌；啟動時保留最新 50 MiB，Docker stdout 10 MiB ×3。
- `ros2/config/`：唯讀持久設定，不隨容器移除。`docker compose down` 不刪除這些 bind volumes。
- 限制：2 CPU、1400 MiB RAM、160 pids、唯讀 rootfs、128 MiB tmpfs；沒有裝置或 Docker socket 掛載。
- 預設每次最多 4 GiB／1800 秒，保留 2048 MiB 磁碟。壓縮前預留一份未壓縮大小加保留容量；不足時保留 bag 並顯示失敗。**不自動刪除歷史錄製**。定期把選定 ZIP 複製至外接碟，核對 SHA，再由管理員刪除整個已備份的錄製目錄。不可刪除正在錄製或打包的目錄。4 GiB 是上限，不代表目前磁碟可錄滿多包。

## 操作與錄製狀態

1. 登入 GUI，檢查十個 topic 的型別、實際 Hz、累計／有效／無效筆數、最後接收時間與內容。預期速率不足會另提示，與內容有效性分開。
2. 按「開始錄製 10 topics」。開始前畫面已列明缺少／無效資料，且完整 preflight 寫入 job；允許保存部分資料，不會將其標為完整。
3. 按「停止並自動 ZIP」。依序為 `recording → stopping → validating → packaging → ready`；任何錯誤進入 `failed`。互斥鎖及程序鎖防止重複錄製。停止使用 SIGINT，等待 rosbag2 正常落盤 metadata；逾時強制終止後判定失敗，保留資料。
4. 驗證逐一檢查 SQLite integrity、每筆 CDR 解析／內容／時間戳、metadata topic/type、筆數與時間範圍；再執行 `ros2 bag info`。缺少 topic 或有無效內容的包為 `partial`，不宣稱十項皆正常；結構損壞不開放下載。
5. ZIP 先寫 `.partial`，顯示按位元組計算的打包進度，CRC 檢查通過後原子改名。歷史清單選擇要下載的包；只有 `ready` 可下載。

每包包含 `bag/metadata.yaml`、完整 SQLite、README、packages.lock（映像的完整套件版本）、source.sha256（建置來源校驗）、summary.json（起訖、每 topic 筆數／有效性／抽樣／環境／已知缺少）、environment.json、bag-info.txt、recorder.log、SHA256SUMS。ZIP 本身的 SHA 另存 `.zip.sha256` 及 job/GUI API。可用一台有相應 ROS 的電腦：

```bash
unzip <選定的.zip> -d flight
cd flight
sha256sum -c SHA256SUMS
source /opt/ros/humble/setup.bash
ros2 bag info bag
# 使用獨立 domain，避免和正在運作的 UAV 重名／混資料
ROS_DOMAIN_ID=91 ros2 bag play bag --clock
```

服務異常中斷後，尚在開始／錄製／停止／驗證／打包的 job 會標為 failed；`.db3`、recorder.log、partial ZIP 都保留，不自動猜測成功。若未留下停止時間，時長顯示「未知（異常中斷）」，不以目前時間或服務恢復時間補算；尚未打包或驗證的欄位顯示文字，而非推測為 0 MB／0 topics。人工恢復先備份整包，檢查磁碟與日誌，再用 `ros2 bag reindex <bag>`、`ros2 bag info` 及 `uav_ros.bags.validate` 檢查。重建 metadata 只恢復索引，不證明最後交易完整，不會自動把 failed 改為 ready。

## 容器、宿主機及外部 DDS 驗證

本服務使用 host network + host IPC，Fast DDS 同機以 128 MiB shared-memory segment 傳輸，跨機保留 UDPv4；容器與宿主機共享 IPC，避免「看到 topic 但收不到」及大型影像 UDP 丟包。完整影像的發布、監測與 recorder 使用 reliable / volatile（大型 UDP 分片可重傳），其他小型感測資料使用 best_effort / volatile；見 `config/qos.yaml`。高頻完整影像可能超過低速 Wi-Fi，應依接收端頻寬降低 `camera_poll_hz`，並在接收端實測。Cloudflare HTTPS 網頁可以看 GUI，但 **不會轉送 DDS UDP**。

```bash
# 容器獨立訂閱者，非只查 topic list
docker compose -f ros2/compose.yaml exec -T gateway bash -c \
  'source /opt/ros/humble/setup.bash; python3 -m uav_ros.probe --seconds 20'

# 宿主機，或把 ros2/uav_ros 複製到預定的另一台 ROS Humble 電腦
source /opt/ros/humble/setup.bash
export ROS_DOMAIN_ID=42 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE=$PWD/ros2/config/fastdds.xml
PYTHONPATH=$PWD/ros2:$PYTHONPATH python3 -m uav_ros.probe --seconds 20 --output receiver.json
# 完整有效驗收時加 --require-all-valid；缺少、無效、未持續更新會回傳非零
```

外部端須確認同 domain、可達的實際 LAN/VPN IP、DDS multicast／unicast UDP 雙向通路、QoS 相容（可靠訂閱不能接小型感測 topic 的 best_effort publisher，Image 接收端應用 reliable）。Domain 42 的 RTPS base port 為 `7400+250×42=17900`，其他 discovery/user UDP ports 隨 participant 變動；依現場網路政策放行對應雙向 RTPS，不只開 TCP 8090。跨子網需配置 Fast DDS peers/discovery server 或有支援 multicast 的 VPN；這份部署未假設 Cloudflare 可做 DDS。以 `ros2 topic info -v` 看端點 QoS，以 probe 的每 topic 持續筆數、不同 header stamps、實際 Hz 和有效抽樣作為接收證據。

自動化 live GUI 驗證（會建立並移除隨機測試帳號；使用真實硬體資料，不啟動假資料 publisher）：

```bash
.venv/bin/python ros2/verify_browser.py --base https://uav.laiting1202.com
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests
```

實體相機／AP6 拔插、實際 Ubuntu 重開機及另一台 DDS 接收端都必須在現場另驗；不得以 enabled、健康檢查或 HTTPS 連線替代完整驗收。測試狀態以 [VALIDATION.md](VALIDATION.md) 為準。
