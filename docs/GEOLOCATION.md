# 目標 GPS 與定位假設

`uav/geo.py` 將車頂標記中心（無標記時使用車輛框中心），經相機內參、姿態與高度投影到地面，再以 WGS84 橢球正算取得目標經緯度，並用 pyproj 轉換成 TWD97 / TM2 121（EPSG:3826）東距、北距，單位公尺。辨識 GPS 不是直接複製飛機 GPS；資料庫分開保存 `target_gps` 與 `aircraft`。

網頁紀錄卡片與新保存的 JPG 同時顯示 WGS84 目標緯度／經度（7 位小數）及 TWD97。CSV 保留 `target_latitude`、`target_longitude`。無效定位明確顯示無有效座標，不以飛機位置代替；歷史 JPG 保留原始內容。

## 本次採用的鏡頭方向

使用者表示雲台可能會轉動或自行穩定，後續指定先假設鏡頭固定 90° 朝下。因此 `orientation_source="assumed_nadir"`：鏡頭光軸指向地面垂直方向，影像上方假設跟隨飛機航向。雲台穩定後不再重複套用機身 roll／pitch；影像偏中心的位置仍會受 yaw 假設影響。

此模式輸出 `quality="assumed_nadir_ground_plane"`，網頁與 CSV 都會標示假設。若鏡頭不是垂直下視，或雲台 yaw 未跟隨機頭，應切換真實雲台姿態。不能以此假設宣稱測量級精度。

另外兩種支援模式：

| `orientation_source` | 用途 |
| --- | --- |
| `fixed` | 相機剛性固定在機身；使用 `mount_rpy_deg` 與完整機身姿態 |
| `mavlink_gimbal` | 接收 `GIMBAL_DEVICE_ATTITUDE_STATUS`（MAVLink 2 訊息 285），使用四元數、yaw 參考座標系與誤差旗標 |

雲台模式的 `gimbal_boresight_rpy_deg` 是鏡頭與雲台定義軸之間的校正偏差，預設 `[0,0,0]`。同一架飛機如有多個雲台，可設定 `telemetry.gimbal_component_id` 與 `gimbal_device_id`。自動模式只選定第一個符合的來源，直到飛控重新連線，避免不同雲台資料交錯。

MAVLink 的 vehicle yaw frame 只跟隨機頭航向，不等同整個傾斜機身座標系。本實作依 flags／`delta_yaw` 轉換，缺少或過期的雲台姿態不會退回假的固定鏡頭值。[官方 Gimbal v2 協定](https://mavlink.io/en/services/gimbal_v2.html)

## 相機內參

目前 `calibrated=false`、`fx=fy=0`，沒有真實校正資料。2026-09-22 使用者選擇先提供估算座標，故啟用 `estimated_focal_ratio=0.9`：沿用舊程式 `red_white_target_server_new_camera.py` 的 `fx=fy=0.9×影像寬度`，光心置中、畸變暫設零。這是既有經驗估值，不是 G3P 官方參數或校正成果；實際水平視角、縮放不同會造成定位誤差。

估算仍將目標像素、航向與高度投影到地面，不直接複製飛機 GPS。輸出 `method=estimated_ground_plane`、`quality=uncalibrated_ground_plane_estimate`，保存 `intrinsics_source`、`assumptions`、`gps_fix`／`gps_quality`。照片與網頁都標示估算及同高假設，沒有宣稱公尺級精度。`estimated_focal_ratio=null` 可停用未校正內參；`calibrated=true` 時永遠優先使用實際內參與畸變。

取得 G3P 在實際解析度、焦距／數位縮放設定下的內參後，填入 `geolocation`：

```json
{
  "calibrated": true,
  "width": 1920,
  "height": 1080,
  "fx": 1234.5,
  "fy": 1234.5,
  "cx": 960.0,
  "cy": 540.0,
  "distortion": [0.0, 0.0, 0.0, 0.0, 0.0]
}
```

上面的焦距數值僅示範欄位，**不是這台相機的量測結果**。畸變順序為 OpenCV `k1,k2,p1,p2,k3`。影像等比例縮放會同步調整內參；長寬比不符時拒絕定位。

也可用棋盤格影像校正：拍攝至少 12 張，讓棋盤覆蓋畫面不同位置、距離與角度。保持與飛行相同的焦距、解析度與圖像裁切，將影像放在自行指定的目錄。

```bash
cd /home/hrc/laiting_uav
# 9×6 是「內角點」數量；25 是實際單格邊長 mm
.venv/bin/python scripts/calibrate_camera.py /path/to/chessboard-images \
  --columns 9 --rows 6 --square-mm 25
# 確認輸出後套用；RMS > 2 pixel 時程式拒絕套用
.venv/bin/python scripts/calibrate_camera.py /path/to/chessboard-images \
  --columns 9 --rows 6 --square-mm 25 --apply
systemctl --user restart uav-v2
```

這個工具只估相機內參。低重投影誤差仍不代表地理定位一定精準，應以地面已知點檢查姿態、縮放、時間延遲與地面高度。

## 地面高度

优先使用未過期、朝下、未量程飽和的 `DISTANCE_SENSOR`，將機身傾斜的斜距換成垂直高度。若無有效測距，可設定 `ground_altitude_relative_m`，指定目標區地面相對於起飛高度基準的高度。

2026-09-22 使用者同意以起飛點高度近似目標地面，設定 `assume_ground_at_takeoff=true`。只有缺少有效測距、且 `ground_altitude_relative_m=null` 時，才以 relative altitude 作為估算離地高度，記錄 `height_source=assumed_takeoff_ground` 和 `ground_at_takeoff_altitude` 假設。高度非正值仍拒絕定位。將此開關設為 false 可停用同高假設；坡地、屋頂或目標區與起飛點高度不同時應提供實際地面高度。

`offset_frd_m` 表示機身參考點到鏡頭的位移（前、右、下，單位 m）。雲台旋轉造成的鏡頭小位移目前以此固定偏移近似。

## 何時不輸出有效 GPS

- 飛控斷線、GPS／位置／姿態超過 `stale_s`（預設 2 秒），或數值缺失／非有限值。
- GPS fix 低於 3（至少一般 3D GPS；不要求 RTK）。
- 相機未校正且未啟用內參估算、解析度長寬比不符或像素超界。
- 沒有有效測距、指定地面高度或已啟用的同高估算；估算高度非正值。
- 雲台模式下，沒有新鮮姿態、四元數無效或雲台回報故障。
- 光線近水平／向上、交點在鏡頭後方或投影距離超過 500 m。

不符合定位條件仍寫入目標事件，`valid=false` 並保存原因與飛機當時資料；同一連續追蹤只記錄一筆；若離開／漏檢後重新出現並持續 2 秒，才建立新紀錄。照片內會永久標出定位失敗原因，不會隨後用新的飛控位置覆蓋舊照片。

## 精度與時間限制

目前時間對齊使用 FFmpeg 解碼完成的主機時間，配對當時收到的飛控資料。RTSP 相機端編碼／網路延遲並不等同曝光時間，尚未做相機與飛控硬體同步。快速飛行或雲台轉動時需進一步校正延遲。地面模型假設局部平面，不處理坡地／建築高度，也沒有測繪級誤差保證。

GPS_RAW_INT 的 `eph` 在標準 MAVLink 表示 HDOP×100；本版只顯示 HDOP，沒有把它錯當成公尺誤差。[MAVLink GPS 與雲台欄位定義](https://mavlink.io/en/messages/common.html)

TWD97 輸出為 `target_gps.twd97 = {epsg: 3826, easting_m: ..., northing_m: ...}`，只在有效定位時產生。先投影目標像素，再轉換目標座標；不是把飛機 GPS 直接換成 TWD97。使用 `always_xy=True` 固定 pyproj 輸入為經度、緯度。[pyproj 軸順序文件](https://pyproj4.github.io/pyproj/stable/api/transformer.html)。目前設定適用台灣本島 121 度分帶；轉換座標格式不會提高 GPS 或相機定位本身的精度。
