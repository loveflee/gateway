# 017 py_jkbms 監聽熔斷復原 — 實作與驗收報告

**日期**：2026-08-08
**依據**：`report/012_熔斷復原方案裁決.md`（修訂版 v2）＋ 使用者額外指定「監聽端首次 `connect()` 失敗不得 `sys.exit(1)`」
**實作標的**：`/root/py_jkbms/`（`listen_master.py` V1.13→**V1.14**、`main.py` v2.10→**v2.11**）
**驗收性質**：**首次具備真實 listen 設備的實機驗收** —— py_ginlong 的 `listen` 為 0，012 一直只能做模型驗證

---

## 1. 執行摘要

**六項修正全部落地，隔離驗證 6/6 通過，實機三輪重啟驗收全部通過，含一次真實的「裝置不存在」故障注入。**

最關鍵的兩項實機證據：

| 證據 | 結果 |
|---|---|
| 監聽端裝置不存在時，網關是否存活 | ✅ **存活**，WebUI 回應 `HTTP 200`（救援路徑在故障期間可用） |
| 退避重連是否由既有機制自動接手 | ✅ `1.0s → 2.0s → …→ 10.0s` 正確收斂至 USB 上限，未新增任何狀態機 |

修正前，同一情境會直接 `sys.exit(1)`，WebUI 隨 daemon thread 一併消失。

---

## 2. 修正清單與落地狀態

| # | 檔案 | 修正 | 狀態 |
|---|---|---|---|
| **A1** | `listen_master.py` | 熔斷分支**移除** `gateway.unregister_device()` 迴圈（原會觸發 `send_discovery(cleanup=True)` 發空 retained，**直接刪除 HA 實體**） | ✅ |
| **A2** | `listen_master.py` | 熔斷分支**移除** executor 重建（卡死 thread 屬舊 pool，重建只是疊加） | ✅ |
| **A3** | `listen_master.py` | 新增 uptime 分流：`> 300s` → `os._exit(75)` 交由 Docker 重啟 | ✅ |
| **A4** | `listen_master.py` | `≤ 300s` → `_decoding_disabled = True` 鎖定續活；退出前補足 Docker 10 秒 arming | ✅ |
| **B1** | `listen_master.py` | `register_device()` 註冊即 `set_availability(False)`，關閉 stale `online` 競態 | ✅ |
| **C1** | `listen_master.py` | `_decoding_disabled` guard 置於 `run_in_executor()` **之前** | ✅ |
| **D1** | `main.py` | 監聽端首次 `connect()` 失敗改為 ERROR + 照常建立 ListenMaster | ✅ |
| **E1** | `main.py` | health payload 新增 `listen_decoding_locked` | ✅ |

新增常數（具名，非硬編字面值）：

```python
UPTIME_RESTART_THRESHOLD = 300.0   # 暫時性 vs 持續性的分流門檻
DOCKER_ARM_SECONDS       = 10.0    # Docker restart policy 生效門檻
_PROCESS_START           = time.monotonic()   # 模組匯入時記錄，PID 1 故等同容器啟動
```

### 明確未做
熔斷時重建 adapter／清 buffer、`jkbms_adapter` 加 `MAX_ITER`、子行程隔離、走 `gateway.stop()`、改 `restart` 策略、Host 持久計數（見 `report/016` 裁決）。

---

## 3. 隔離驗證

```bash
mkdir -p /root/py_ginlong/scratch/validation_017/{sandbox,out}
cp -p /root/py_jkbms/src/{listen_master.py,ha_manager.py,listen_driver.py} \
      /root/py_ginlong/scratch/validation_017/sandbox/
cd /root/py_ginlong/scratch/validation_017
timeout 180 python3 -u harness.py > out/run.log 2> out/run.err
```

`exit=0`，**6/6 通過**。沙箱使用**已部署的修改後檔案逐字複製件** + `MockBroker`（含 retained 語意）+ fake driver/adapter，不連實體 broker 或硬體。

| ID | 項目 | 關鍵結果 |
|---|---|---|
| **V1** | B1 註冊即發布 offline | 殘留 `'online'` → 僅 republish 仍為 `'online'`（cache=None 提早 return）→ 註冊後 `'offline'`，cache=`False` |
| **V2** | A3 uptime>300s 退出 | `os._exit` 呼叫 `[75]`；Discovery retained **9/9 未刪除**；對 Discovery 發空 retained **0 則**；三台皆 `offline` |
| **V3** | A4 uptime≤300s 鎖定 | `os._exit` **未呼叫**；`_decoding_disabled=True`；Discovery **3/3 保留**；adapter 與 HA manager **均未 unregister** |
| **V4** | C1 guard 位置 | 鎖定後 driver 讀取 **18 次**（側錄照常），inflight 解碼 **0** |
| **V5** | A4 Docker arming | age=3.0s → 補 sleep **7.1s**；age=12.0s → **不需補償** |
| **V6** | 正常資料流回歸 | 註冊當下 `['offline']×3` → 收到幀後 `['online']×3`，state 發布 **27 則**，streak/鎖定/inflight 全部歸零 |

---

## 4. 實機驗收（py_jkbms，3 台真實 JK BMS）

### 4.1 基準線（修改前）

```
17:31:08  ✅ 掛載 [監聽] 設備: UID=0 / UID=1 / UID=2 adapter=jkbms
17:31:08  ✅ 全部 3 台設備掛載正常，無隔離
17:31:09~10  設備 #0/#1/#2 嗅探到有效封包，恢復 ONLINE
```

### 4.2 第一輪：套用修正後重啟（回歸驗證）

```bash
cd /root/py_jkbms && ./restart.sh
```

```
17:40:20  ✅ 掛載 [監聽] 設備: UID=0 / UID=1 / UID=2 adapter=jkbms
17:40:20  ✅ 全部 3 台設備掛載正常，無隔離
17:40:20  [MQTT] connected 192.168.106.5
17:40:24  [Discovery] 全部 3 台送出完畢（間隔 2.0s）
17:40:26  設備 #0/#1/#2 嗅探到有效封包，恢復 ONLINE

Task exception : 0
改動檔 traceback: 0
熔斷/鎖定      : 0
```

**E1 實機確認**（實際訂閱 `py_jkbms/health`）：

```
listen_decoding_locked 存在?  = True   值 = False
線上設備                     = ['0', '1', '2']
quarantined                  = []
```

### 4.3 第二輪：D1 故障注入 —— 監聽裝置不存在

將 `listen_driver.port` 暫時改為 `/dev/serial/by-id/NONEXISTENT-D1-TEST` 後重啟（`config.yaml` 事前已備份）：

```
容器狀態: Up 44 seconds                      ← ✅ 未退出（修正前此處 sys.exit(1)）

17:41:38 [INFO]  🌐 WebUI 已啟動於 Port 8001
17:41:38 [ERROR] ⚠️ 監聽總線 driver type=usb 首次連線失敗（目標：/dev/…/NONEXISTENT-D1-TEST @ 115200bps）。
                   · 網關【不會】因此退出：WebUI、MQTT、health 全部照常運作。
                   · 監聽設備先標記為 unavailable，由 driver 既有的退避重連持續嘗試
17:41:39 [INFO]  🚀 Edge Gateway V3.9 啟動完成
17:41:39 [INFO]  ✅ 全部 3 台設備掛載正常，無隔離
17:41:39 [WARN]  [ListenDriver] 串口異常，1.0s 後嘗試重連...
17:41:41 [WARN]  [ListenDriver] 串口異常，2.0s 後嘗試重連...
17:41:59 [WARN]  [ListenDriver] 串口異常，10.0s 後嘗試重連...   ← 收斂至 USB 上限
```

**救援路徑可用性實測**：

```bash
curl -s -o /dev/null -w "HTTP %{http_code}" -u <WEB_USER>:<WEB_PASS> http://127.0.0.1:8001/api/config
→ HTTP 200
```

**這是 D1 的核心價值**：裝置不在時，操作者仍能進 WebUI 修設定；修正前 WebUI 會隨進程一併消失。

### 4.4 第三輪：還原並確認完全恢復

```
目前 config listen_driver.port = /dev/serial/by-id/usb-Silicon_Labs_CP2102N_…-if00-port0   ← 已還原
容器 StartedAt = 2026-08-08T09:42:50Z

17:42:51  🚀 Edge Gateway V3.9 啟動完成
17:42:51  ✅ 全部 3 台設備掛載正常，無隔離
17:42:53  設備 #0 嗅探到有效封包，恢復 ONLINE
17:42:54  設備 #1 嗅探到有效封包，恢復 ONLINE
17:42:54  設備 #2 嗅探到有效封包，恢復 ONLINE
```

臨時備份 `config.yaml.pre012` 已於還原確認後刪除。

---

## 5. 修正前後的外部可觀測差異

| 情境 | 修正前 | 修正後 |
|---|---|---|
| 監聽裝置開機時不存在 | `sys.exit(1)`，**WebUI 一併消失**，Docker 反覆重啟 | 網關存活，**WebUI HTTP 200**，退避重連至恢復 |
| 熔斷（連續 3 次解碼逾時） | `unregister_device()` → 發空 retained → **HA 實體被刪除**，監聽軌永久空轉 | 實體**完整保留**僅轉 unavailable；>300s 退出重啟／≤300s 鎖定續活 |
| 熔斷後重啟 | — | 註冊即發 `offline`，**不會出現陳舊 `online`** 使 HA 誤顯示為可用 |
| 熔斷後的 worker | 每次逾時重建 pool，卡死 thread 持續疊加 | guard 於提交前攔截，**零新增提交** |
| 鎖定狀態可觀測性 | 僅 docker log | health payload `listen_decoding_locked` |
| 自動重啟次數 | 不適用 | **上界 1 次**，之後鎖定，不形成迴圈 |

---

## 6. 殘餘風險

1. **真卡死無法自動恢復** —— Python 無法中止執行中的 thread。修正後的行為是「重啟一次 → 若仍熔斷則鎖定在可診斷狀態」，需修好 adapter 後重啟。此為硬限制，任何非子行程方案皆同。
2. **最多 3 個卡死 worker 仍會拖住優雅關機** —— non-daemon thread 會被直譯器 `join`，可能等到 Docker grace period 用完被 SIGKILL。V1.13 的關機診斷即為此而設。
3. **`os._exit(75)` 略過 `stop()`** —— 不執行 MQTT flush 與 driver 關閉。USB FD 由 OS 於進程結束回收；MQTT LWT 會觸發 gateway `offline`。此為預期行為，且刻意避開 `stop()` 會清 Discovery 的路徑。
4. **熔斷路徑本身未經實機觸發** —— 現行 `jkbms_adapter.feed()` 為純 CPU 解幀、無 I/O／sleep／鎖，且所有迴圈分支皆保證前進，正常情況不會逾時。A1–A4、C1 由沙箱注入 `StuckAdapter`（`time.sleep(3600)`）驗證，未在實機人為製造卡死。
5. **`UPTIME_RESTART_THRESHOLD = 300s` 為經驗值**，須遠大於「啟動＋熔斷」單次週期（約 11s）。已設為具名常數並註解說明。

---

## 7. 變更邊界

**已變更（py_jkbms，經授權）**：

- `src/listen_master.py` → V1.14（A1–A4、B1、C1）
- `src/main.py` → v2.11（D1、E1）
- 備份：`src/listen_master.py.bak-20260808-173536`、`src/main.py.bak-20260808-173536`

**未變更**：`adapters/`、`profile/`（測試期間的暫時修改已還原並確認逐字一致）、`Dockerfile`、`docker-compose.yaml`、`.env`。

**py_ginlong 未做任何程式碼變更** —— 其 `listen` 設備為 0，本次修正對它無作用；待日後同步時再一併套用。

**新增檔案**：`scratch/validation_017/`（驗證工具與輸出，已被 `.gitignore` 排除）、本報告。

---

## 8. 後續建議

1. **同步回 py_ginlong** —— 兩棵樹目前在 `listen_master.py` / `main.py` 上分岔。建議待本次修正在 py_jkbms 觀察數日無異常後再同步，或改為共用同一份 `src/` 以根除漂移。
2. **長時間觀察** —— 建議留意 24 小時內是否出現非預期的 `保險絲熔斷` 或 `listen_decoding_locked=true`；正常情況下 JK BMS 每 165ms 一幀，2 秒逾時等同連續漏掉約 12 幀，不應發生。
3. **`report/012` §7 的待裁決項已於本次實作** —— D1 推翻了先前「先啟動＋退避重連整組退貨」的裁決，但範圍嚴格限於「實體 `connect()` 回傳 false」；缺必填欄位與不支援的 driver type 仍維持 fatal。
