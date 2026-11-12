# 2026-10-05 主機 DNS 修復

時間均為台灣時間（UTC+08:00）。13:15:41 開始套用主機修復，13:17:57 完成設定重載驗證。

## 問題與實測

原 `/etc/resolv.conf` 指向 `/run/resolvconf/resolv.conf`。WireGuard 的 `DNS = 8.8.8.8` 透過 `resolvconf` 排在本機 stub `127.0.0.53` 前面；NetworkManager 同時把行動網卡 DHCP 提供的 `192.168.125.1` 送入 `systemd-resolved`。因此一般程式和 Tunnel 的獨立 DNS 使用不同解析路徑。

同主機、同網路、隔離 mount namespace 的 `socket.getaddrinfo()` 對照：

| 設定 | 四個網站／Tunnel 網域的單次解析時間 |
| --- | --- |
| 原主機設定 | 5.074–5.259 秒 |
| `8.8.8.8` 加 `single-request-reopen` | 0.073–0.131 秒 |
| `1.1.1.1` 加 `single-request-reopen` | 0.067–0.087 秒 |
| 本機 `127.0.0.53` stub | 0.041–0.111 秒 |

原設定的約 5 秒延遲會超過網站探測的 3 秒連線期限。直接向 `8.8.8.8` 查詢 A／AAAA／SRV 的 UDP／TCP 測試均成功，因此不能說 Google DNS 完全不可達。結果支持一般程式原先的查詢方式在目前網路路徑上會觸發重試；未以封包擷取定位到哪一個 NAT／韌體環節。行動網卡的 `192.168.125.1` 另有一次 UDP 查詢逾時。

## 已套用的持久設定

- `/etc/resolv.conf` 改指向 `/run/systemd/resolve/stub-resolv.conf`，一般程式統一透過本機 DNS 服務。
- `/etc/NetworkManager/conf.d/90-uav-dns.conf` 設定 `dns=systemd-resolved`、`rc-manager=unmanaged`。
- 行動網卡 profile `8391cb7e-8a3a-317c-bf18-612aa9c5300e` 保存 `ipv4.dns=1.1.1.1,8.8.8.8`，IPv4／IPv6 都忽略 DHCP／自動提供的 DNS。IP 位址、路由與自動連線設定保留。
- `/etc/systemd/resolved.conf.d/90-uav-dns.conf` 開啟本機 stub，設定沒有介面 DNS 時的 fallback 為 `1.1.1.1 1.0.0.1 8.8.8.8`。fallback 與同一介面的主備 DNS 是不同機制。
- `/etc/wireguard/wg0.conf` 只移除 Interface 的 DNS 指令，並清除原有 `tun.wg0` resolver 紀錄。逐字比對確認金鑰、peer、AllowedIPs、endpoint 及其他設定保留，WireGuard 服務仍 active。
- 保留 Tunnel 原有的獨立 DNS 啟動機制。這次沒有重啟 UAV 應用、Tunnel 或 WireGuard，也沒有斷開行動網卡。

本機 stub 與網路管理器的整合依據：[systemd 249 DNS 管理說明](https://github.com/systemd/systemd/blob/v249/man/systemd-resolved.service.xml)、[NetworkManager 1.36 DNS 設定](https://networkmanager.dev/docs/api/1.36.0/NetworkManager.conf.html)。

## 驗證

- 修復後首次一般解析為 0.041 秒，公開 `/healthz` 正常。
- 六個不同網域的 A／AAAA 解析成功，Tunnel discovery SRV 查詢成功。
- 實際備援測試：暫把介面主 DNS 設為無回應的 `192.0.2.1`，備援保留 `8.8.8.8`。確認測試前目前 DNS 為故障位址；查詢在 5.252 秒後成功，解析器切到 `8.8.8.8`。測試後恢復正式設定。故障發生時首次請求仍可能因應用自身期限較短而逾時，後續可使用已切換的 DNS。
- 從磁碟重新載入 NetworkManager profiles、重新啟動 `systemd-resolved`、重新套用介面設定及執行 `resolvconf -u` 後，主機仍使用 stub，DNS 仍為 `1.1.1.1`／`8.8.8.8`；一般解析 0.037 秒，網站正常。
- 實際讀取磁碟上的行動網卡 profile，確認 DNS 與 ignore-auto-dns 已保存；路由表與修復前一致。
- `tests/test_host_dns.py`、`tests/test_tunnel_supervisor.py`、`tests/test_network_recorder.py`：20 passed in 5.68s。
- 13:18:44–13:20:34 連續 12 輪驗證：72 次一般程式解析、24 次直接上游 SRV 查詢、12 次公開 HTTP 與 12 次公開 SSH banner 探測全部成功；一般解析最慢 0.239 秒。此觀察包含快取命中，另外的直接上游查詢不經本機快取。
- 修復期間 UAV 應用 PID 1767、Tunnel supervisor PID 10222 與 cloudflared PID 10269 維持不變；持續日誌沒有探測失敗或 Tunnel 重建。主機 resolver 路徑改變後，另重跑 DNS namespace 隔離測試，1 passed。

沒有重開整台主機或測試實體網卡拔插；不把 DNS 修復當作 USB 掉線、電信斷網或整機卡死的保證。

## 證據與還原

非機密測試資料位於 `data/maintenance/20261005-host-dns/`，包含上游逐項探測、修復前對照、修復後查詢、實際備援切換與持久設定重載結果。

管理員備份：`/var/backups/laiting-uav/dns/20261005-131541-2rnp8xk8`。其中包含原 WireGuard 設定，目錄僅 root 可存取，不應上傳或公開。

```bash
# 唯讀檢查目前主機 DNS 與公開網站
python3 /home/hrc/laiting_uav/scripts/fix_host_dns.py check

# 必要時完整還原這次 DNS 設定
sudo python3 /home/hrc/laiting_uav/scripts/fix_host_dns.py rollback \
  /var/backups/laiting-uav/dns/20261005-131541-2rnp8xk8
```

`scripts/fix_host_dns.py apply` 會先備份，套用失敗時自動還原；不需要密碼參數，也不保存密碼。
