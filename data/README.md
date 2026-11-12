# 執行資料

絕對位置：`/home/hrc/laiting_uav/data`。

`live/events.sqlite3` 是實機目標紀錄；`demo/events.sqlite3` 是合成展示紀錄，兩者完全分開。SQLite 運作時可能產生 `events.sqlite3-wal`、`events.sqlite3-shm`，它們是交易資料的一部分，不是多餘檔案。

`live/flight-tracks.sqlite3`／`demo/flight-tracks.sqlite3` 保存手動開始／停止的飛行軌跡，權限 600。包含開始／停止時間、結束原因、是否模擬、點數、已紀錄距離，以及每點 WGS84 座標、時間與連續路段編號；有效 GPS 每秒取樣。未開始時不寫入，服務重啟後不自動續錄。軌跡不隨目標事件清除；歷史軌跡保留於資料庫，GUI 提供最近一次已完成軌跡下載。可用空間低於 `storage.min_free_mb` 時停止新增點並顯示錯誤，恢復後以新路段接續。JPG 由瀏覽器按需產生，不額外保存於主機。備份時亦需包含這個資料庫及其 WAL，或使用 SQLite backup API。

`live/sessions.json`／`demo/sessions.json` 是權限 600 的私有登入 checkpoint，包含 token 雜湊、CSRF 與到期時間，供服務重啟後恢復有效登入；不含原始登入 token。不要公開或放入版本控制，刪除會使下一次啟動時必須重新登入。

每筆資料包含 UTC 時間、啟動識別碼、是否模擬、模型名稱、追蹤 ID、車種／信心值／框／標記座標、持續出現秒數、目標 WGS84 與 TWD97（EPSG:3826）座標有效性與無效原因、當下飛機 GPS／姿態／電池等遙測與資料年齡，以及本次使用的定位設定。網頁以台灣時間顯示，CSV 原始時間為 UTC。

預設保留最新 10,000 筆；SQLite 刪除後會重用資料頁，檔案不會每次立即縮小。低於 512 MB 可用空間時，網頁會顯示無法寫入。每筆新事件在 `event_images` 表保存完整視野標註 JPG 與縮圖，和事件交易一起提交；淘汰事件時連帶刪除照片。不儲存連續影片或每一幀 JPEG。

匯出可用每筆紀錄的「下載 JPG」或整體「下載 CSV」。照片下方附上時間（台灣 UTC+8）、類型、信心值、目標經緯度（WGS84，7 位小數）、TWD97 或定位失敗原因；資料不足時不會用飛機座標冒充目標座標。舊版 41 筆紀錄沒有照片，遷移後保留文字資料。

「清除全部紀錄」會刪除事件、JPG 與縮圖，並嘗試壓縮資料庫釋出空間；追蹤重新計時 2 秒，已開始推論的舊影格不會重新寫入。事件 ID 不重用，已清除的照片 URL 回 404。範例紀錄以 `example=true`、`demo=true` 明確標記，列表置頂；清除後不會自行重建。完整備份請用 SQLite backup API，或停止服務後再複製資料庫，避免漏掉 WAL 內尚未 checkpoint 的資料。

日誌在 systemd journal：`journalctl _SYSTEMD_USER_UNIT=uav-v2.service` 與 `journalctl _SYSTEMD_USER_UNIT=uav-cloudflared.service`。正式資料與密碼雜湊不納入 Git。
