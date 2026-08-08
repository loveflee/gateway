# 016 Host 計數看門狗方案 —— 獨立重跑裁決

**日期**：2026-08-08
**標的**：`report/015_Host計數看門狗方案裁決.md` 所評估之 Host 外部計數看門狗提案
**性質**：不採信 015 結論，對現行原始碼與 Compose 獨立重新查證 + 隔離動態驗證
**本輪變更**：**無**。未修改任何 production 程式、設定、Docker/Compose、容器、硬體或 MQTT。

---

## 1. 執行裁決

### 對 015 前次裁決的複核結果

> **015 的「原樣 REJECT，修正後條件式 ADOPT」裁決 —— 正確，予以確認。**

015 所列的十項判定,經獨立查證後 **全部成立**,且其中兩項我取得了比 015 更強的實證（見 §3 B、C 組）。未發現 015 有誤判或捏造。

### 本輪裁決

| 對象 | 裁決 |
|---|---|
| **提案原樣（`/tmp/gateway.status` 單檔 bind mount）** | ❌ **REJECT** —— 三項獨立致命缺陷,任一即足以否決 |
| **015 修正後的條件式方案** | ⚠️ **條件式 ADOPT** —— 015 的條件必要但**不充分**,尚缺 3 項（見 §5） |
| **工程建議** | 🔸 **建議改採 report/012 的 uptime 守衛** —— 外部可觀測行為等價,程式碼約 35 行 vs 約 90 行,且不引入持久狀態這個新故障面（見 §6） |

---

## 2. 決策表

| 提案要素 | 判定 | 實證依據 |
|---|---|---|
| 以跨重啟計數取代 `uptime > 300s` | ✅ 可行且較精確 | `start_time` 每進程重設（`src/main.py:169`）,確實無法承擔跨重啟計數 |
| `/tmp/gateway.status` 存放狀態 | ❌ **致命** | 本機 `/tmp` 為 **tmpfs**,reboot 後必然全失（B 組） |
| 單檔 bind mount + `os.replace()` | ❌ **致命** | 實測 `OSError errno=16 EBUSY`（C 組） |
| 熔斷後仍呼叫 `unregister_device()` | ❌ **致命** | 會送空 retained 刪除 HA 實體（`src/main.py:359`） |
| sidecar `.lock` + flock + 原子替換 | ✅ 必要且有效 | 6 進程×20 次 = 精確 120,無殘留 temp（D 組） |
| 狀態毀損／不可寫 → 安全鎖定 | ✅ 必要 | 截斷 JSON 必須鎖定而非歸零；唯讀目錄 `errno=30`（E 組） |
| guard 置於 `run_in_executor()` 之前 | ✅ 必要 | 鎖定後 submit 回傳 None、inflight=0（F 組） |
| 註冊時 `set_availability(False)` | ✅ 必要 | 關閉 stale `online` 競態（H 組） |
| 退出前補 sleep 至 process age 10.1s | ✅ 必要 | Docker 官方文件確認 10 秒 arming 門檻（I 組） |
| 15 分鐘穩定期 reset | ✅ 語意正確 | 未達 900s 不清零、timeout 使其失效、鎖定後不可自復（G 組） |
| 改用既有 `./profile` bind mount | ✅ 建議 | 已是 rw 持久掛載（`docker-compose.yaml:19`）,可完全免除 Compose 變更 |

---

## 3. 驗證：指令與結果

### 執行指令

```bash
cd /root/py_ginlong
mkdir -p scratch/validation_016/{sandbox,out,mnt_test}
cp -p src/listen_master.py src/ha_manager.py src/listen_driver.py scratch/validation_016/sandbox/
for f in listen_master.py ha_manager.py listen_driver.py; do diff -q src/$f scratch/validation_016/sandbox/$f; done   # 三檔皆逐字相同

cd /root/py_ginlong/scratch/validation_016
timeout 180 python3 -u harness.py > out/run.log 2> out/run.err
```

**結果：`exit=0`,`總計 9/9 通過`**

### 逐項結果

| 組 | 項目 | 結果 |
|---|---|---|
| **A** | 015 引用之現況事實在 v2.10 / V6.6 下仍全部成立 | **PASS** — 15 項靜態核對全中 |
| **B** | `/tmp` 不可用於跨 reboot 計數 | **PASS** — `findmnt /tmp` = `tmpfs`,`q /tmp 1777 root root 10d` |
| **C** | 單檔 bind mount 使 `os.replace` 失敗 | **PASS** — `OSError errno=16 (Device or resource busy)`;對照組（目錄內一般檔）成功 |
| **D** | 併發計數精確、無殘留 temp | **PASS** — 6×20 → 期望 120 實得 **120** |
| **E** | 狀態毀損／不可寫必須安全鎖定 | **PASS** — 截斷 JSON → `lock_safe`；唯讀目錄 → `errno=30 (Read-only file system)` |
| **F** | 熔斷語意與跨重啟保持 | **PASS** — trip1/2 `exit_restart`、trip3 `lock`；鎖定後 submit=None、inflight=0；Discovery retained 保留 3 個；availability=`offline`；下次啟動仍鎖定 |
| **G** | 15 分鐘穩定期 reset 語意 | **PASS** — 600s 不清零、timeout 使其失效、901s 清零、鎖定後不可自復 |
| **H** | 註冊即 `set_availability(False)` 關閉 stale 競態 | **PASS** — 殘留 `online` → 僅 republish 仍為 `online` → 補一行後 `offline` |
| **I** | Docker 10 秒 arming 門檻 | **PASS** — 官方文件逐字確認 |

### 關鍵原始輸出摘錄

```
[C] os.replace(tmp, <bind-mount 單檔>) : ❌ OSError errno=16 (Device or resource busy)
    os.replace(tmp, <目錄內一般檔>)      : ✅ 成功（對照組）
    umount 清理 : rc=0 ；殘留掛載檢查 : ✅ 已完全卸載

[D] 6 進程 × 20 次 → 期望 120，實得 120 ；殘留 temp 檔：無

[F] ✅ 第 1 次熔斷 (proc_age=3.0s)  → exit_restart trip_count=1 退出前補 sleep=7.1s
    ✅ 第 2 次熔斷 (proc_age=12.0s) → exit_restart trip_count=2 退出前補 sleep=0.0s
    ✅ 第 3 次熔斷 (proc_age=12.0s) → lock         trip_count=3
       鎖定後 submit 回傳=None  inflight=0  ✅ guard 生效
    Discovery retained 保留 = 3 個 ✅ ；設備 availability = 'offline' ✅
    下次啟動讀到 trip_count>=3 → _decoding_disabled=True ✅ 跨重啟仍鎖定

[H] 上一進程殘留 retained = 'online' → 僅 republish = 'online'（未覆蓋）
    註冊時補 set_availability(False) = 'offline' ✅
```

### 驗證方法的隔離性

- 僅載入 `scratch/validation_016/sandbox/` 內的 production **逐字複製件**,不 import `src/` 或容器內模組
- MQTT 全部以 `MockBroker`（含 retained 語意）替代,**未連接任何實體 broker**
- driver / adapter 全部為 fake（`StuckAdapter` 以 `time.sleep(3600)` 注入卡死）,**未碰硬體**
- 唯一的系統層操作是在 `scratch/validation_016/mnt_test/` 內建立臨時 bind mount 並立即卸載；已驗證 `findmnt | grep validation_016` 無殘留

---

## 4. 我取得的、015 未提出的三項發現

### 4.1 `/tmp` 否決理由比 015 更強

015 以 systemd-tmpfiles 立論（正確）。但本機的實際成因更直接：

```
findmnt /tmp = tmpfs tmpfs rw,nosuid,nodev,size=991452k,...
```

`/tmp` 是 **tmpfs（RAM backed）**,reboot 後內容**必然**全失,不需要等 tmpfiles 清理。此為決定性否決,無討論空間。

### 4.2 單檔 bind mount 的失敗方式已取得 errno 級實證

015 稱「不能保證 rename 成功/原子」。實測結果比推測更明確：

```
os.replace(tmp, <bind-mount 單檔>) → OSError errno=16 (Device or resource busy)
```

`rename()` 以掛載點為目標**必然**失敗（EBUSY）。若原樣實作,計數將**永遠寫不進去**,看門狗完全失效且可能不會有人察覺。

### 4.3 對 report/012 自身敘述的修正

Docker 官方文件此頁**只**載明 arming 門檻,**未**載明「10 秒重置 backoff」：

> "A restart policy only takes effect after a container starts successfully."
> "...starting successfully means that the container is up for at least 10 seconds and Docker has started monitoring it."
> —— docs.docker.com/engine/containers/start-containers-automatically/

因此 `report/012` 以「backoff 重置」立論的「緊迫重啟迴圈」**缺乏文件依據**。真正有文件支持的風險是**可能根本不會重啟**（容器停死、WebUI 一併消失）。

**此點 015 較 012 正確,一併更正。**

自然週期為啟動約 5s + 熔斷約 6s ≈ **11s**,僅略高於門檻,屬臨界值 —— 啟動加快或熔斷提前即落入 <10s 的最壞區間。015 的「退出前補 sleep 至 10.1s」是必要緩解；且因 trip 3 即鎖定,自動重啟上界為 **2 次**,不會形成無限迴圈。

---

## 5. 015 條件之外,仍缺的三項（條件式 ADOPT 的補充條件）

| # | 缺口 | 風險 | 最小補法 |
|---|---|---|---|
| **N1** | **health payload 無鎖定狀態欄位** —— 現有欄位僅 `uptime_s`／`cmd_queue_size`／`devices`／`quarantined`／`health_publish_failures_total`（`src/main.py:558`） | 鎖定後 listen 設備只是 `offline`,與「單純沒收到資料」外觀相同。操作者從 MQTT 側**無法區分**,只能翻 docker log | health payload 增 `listen_decoding_locked` 與 `listen_trip_count` 兩個布林／整數欄位 |
| **N2** | **無已定義的解鎖機制** —— WebUI 無對應端點；015 僅稱「由明確管理動作清除」 | 鎖定後 `_decoding_disabled` 使解碼停止 → 永遠收不到有效幀 → 穩定期 reset 永遠不觸發 → **需人工刪檔**,但刪哪個檔、怎麼刪未定義 | 至少在鎖定的 CRITICAL log 內逐字寫出檔案完整路徑與刪除指令；或加一個 WebUI 端點 |
| **N3** | **計數會被無關的維運重啟污染** | `trip_count` 持久化。操作者為其他原因重啟容器時若 `trip_count` 已為 2,之後任一次暫時性熔斷即達 3 → **永久鎖定** | 記錄 `last_trip` wall-clock,啟動時若距上次 trip 已超過（例如）24 小時即視為過期並歸零 |

---

## 6. 若採用：確切的最小檔案與變更

**採「既有 `./profile` bind mount」版本,可完全免除 Compose 變更。**

已確認 `src/` 內無任何 `listdir`／`glob`／`scandir` 目錄掃描,於 `profile/` 放置 dotfile **安全**；且 `.gitignore` 已有 `profile/config.yaml.bak`、`profile/config.yaml.lock` 之先例。

| 檔案 | 變更內容 | 約略行數 |
|---|---|---|
| **`src/listen_master.py`** | ① 狀態 store helper（讀／原子寫／flock,狀態檔 `/app/profile/.listen-watchdog.json` + `.lock`）<br>② `register_device()`（`:86`）回傳前加 `ha_manager.set_availability(False)`<br>③ `__init__` 啟動時讀狀態,`trip_count>=3` 或毀損 → `_decoding_disabled=True` + CRITICAL<br>④ 熔斷分支（`:236`–`:243`）**移除** `unregister_device()` 與 executor 重建,改為計數＋分流<br>⑤ guard 置於 `run_in_executor()`（`:211`）之前<br>⑥ 穩定期追蹤（有效幀設起點、任何 timeout 清空） | **約 90** |
| **`src/main.py`** | ① health payload（`:558`）增 `listen_decoding_locked`／`listen_trip_count`（缺口 N1）<br>② **僅在**要求「限 JK 幀」時,於 `:922` 掛載處傳入 `adapter_name` | 約 10 |
| **`.gitignore`** | 新增 `profile/.listen-watchdog.json`、`profile/.listen-watchdog.json.lock` | 2 |
| **`docker-compose.yaml`** | **不變更** | 0 |

**明確不做**：不改 `profile/config.yaml`、不清 adapter buffer、不新增 thread-kill、不 bind mount 單檔、不使用 `/tmp`、不改 `on-failure` 重啟策略（`on-failure:2` 會讓容器第三次停死,WebUI 一併消失,與目標相反）。

---

## 7. 工程建議：是否值得採用

以本專案的實際標準（單人維運、核心要求為「WebUI 不死、設定錯能進 WebUI 修」）評估：

| | report/012 uptime 守衛 | 本 Host 計數看門狗 |
|---|---|---|
| 程式碼量 | 約 35 行 | 約 90 行 + 狀態檔 + 鎖檔 |
| 新增持久狀態 | 無 | 有（含毀損／不可寫／過期三種新故障面） |
| 需要解鎖程序 | 否 | **是**（缺口 N2） |
| 自動重啟上界 | 1 次 | 2 次 |
| 跨主機 reboot 保持計數 | 否 | 是 |
| 外部可觀測行為 | 實體保留、暫時性自動恢復、持續性收斂鎖定 | **相同** |

兩者在**外部可觀測行為上等價**。Host 計數的額外價值僅為「多一次重啟嘗試」與「跨 host reboot 保持」,而代價是一整套持久狀態機制及其三種新故障面。

> **建議：優先採 report/012 的 uptime 守衛。** 若仍決定採用 Host 計數,015 的條件必須全數落實,並補上本報告 §5 的 N1–N3。

無論採哪案,以下三項為共同必要前提,應優先實作：
1. 熔斷分支移除 `unregister_device()`／Discovery cleanup（否則 HA 實體會被刪除）
2. `register_device()` 補 `set_availability(False)`（否則 stale `online` 競態）
3. guard 置於 `run_in_executor()` 之前（否則 worker 仍被燒光）

---

## 8. 未解的前提限制

**現行 `profile/config.yaml` 為 listen=0、active=4,`ListenMaster` 根本不會被建立。**

本方案在 py_ginlong **無法實機驗收**,本報告全部為靜態核對與隔離模型驗證。真正的實機驗收必須在具備 listen 設備的環境（py_jkbms）進行,建議順序：先讓該環境跑上現行版本並確認基準線 → 再實作 → 於該環境完成永久阻塞／延遲返回／MQTT 熔斷時不可用／正常資料流回歸四項注入驗收。

另注意 015 已正確指出的獨立邊界：listen driver 首次 `connect()` 失敗即 `sys.exit(1)`（`src/main.py:722`）,發生在 dispatcher／計數器建立**之前**,本方案完全不會改善該路徑,不得與「自動復原」混為一談。

---

## 9. Production 變更邊界（本輪嚴格遵守）

**未變更**：`src/`、`adapters/`、`profile/`、`Dockerfile`、`docker-compose.yaml` 下所有檔案。已以 `git status --porcelain` 對上述路徑確認**無任何變更**。

**未執行**：`docker restart`／`stop`／`up`／`down`、映像重建、實體 RS485／USB 連接、對實體 MQTT broker（192.168.106.5）的任何發布或訂閱。

**未執行 git 推送**：已知的推送方式為 gateway SSH key 搭配指定 remote（先前該次推送經使用者明示授權）；本輪**未執行任何 git 操作**,僅以 `git status` 唯讀確認工作區乾淨。

**臨時系統操作**：C／E 組於 `scratch/validation_016/mnt_test/` 及 `out/` 內建立臨時 bind mount（含一次 remount,ro）以取得 EBUSY 與 EROFS 實證,測試結束立即卸載；已以 `findmnt` 確認**無殘留掛載**。

**新增檔案僅限**：

```
scratch/validation_016/
├── harness.py              主驗證工具（A–I 共 9 組）
├── counter_worker.py       併發計數 worker（D 組多進程用）
├── sandbox/                production 逐字複製件（listen_master / ha_manager / listen_driver）
├── mnt_test/               C 組 bind mount 測試檔
└── out/
    ├── run.log / run.err   完整輸出
    └── results.json        結構化結果
```

以及本報告 `report/016_Host計數看門狗方案_RCL獨立重跑.md`。
