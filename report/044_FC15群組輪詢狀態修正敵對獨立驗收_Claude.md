# 044 — FC15 群組輪詢狀態修正（043 自驗）敵對式獨立驗收

- 驗收者：Claude Code（獨立敵對驗收，非施工者，未參與 043 的任何修改）
- 日期：2026-08-12 22:18 – 22:31（本機時區 CST）
- 受驗對象：report/043 宣稱對 report/042「黑洞 G」（群組狀態輪詢不重算、持續發布假值）的修復
- 立場：**不採信 043 的任何結論**。所有結論改為對目前 production code 直接讀碼、獨立隔離複測、與正式 Gateway 實機重跑得出。
- 本輪 production code 修改：**零**（SHA-256 前後比對，14 個檔案逐一相同，見第 7 節）
- 本輪新增檔案：`scratch/claude_044_independent_review.py`、`scratch/claude_044_independent_review_results.json`、`scratch/044_pcap_extract.py`、`scratch/044_live_capture.pcap`、`scratch/044_live_capture2.pcap`、`scratch/044_pcap_decoded.txt`、`scratch/044_mqtt_state.log`、`scratch/044_final_hashes.txt`、本報告

---

## 人話結論（非工程師看得懂）

**這一輪是真的修好了。我沒有相信任何人的說法，自己重打了一次「死亡案例」，結果乾淨。判 PASS。**

上一輪（042）我自己抓到一個問題：把一組繼電器（例如 3、4、5 路合稱「群組 234」）用群組指令關到全部關閉之後，如果有人單獨去開其中一路（例如只開第 3 路），畫面上的「群組」顯示還是死死地停在「全關」，而且每 15 秒巡檢一次都在重播這個假訊息，直到下一次有人再下群組指令才會更新。這對「一組繼電器要一起看、一起切」的場合是會出事的錯——操作員看畫面說安全，其實不安全。

這一輪我做了三件事，全部獨立進行，沒有一項直接採信別人的結論：

1. **重讀程式碼**，確認施工者真的加了兩個新邏輯：一是「平常巡檢也順手重算一次群組狀態」，二是「只開關單一路的指令，會先把牽涉到的群組標成『看不出來』，等下一次整批巡檢才恢復成正確值」。這兩塊邏輯我逐行看過，行為合理。

2. **自己重寫一整套隔離測試**（119 項，不是拿別人的腳本重跑），包含一個「證偽測試」：我把新邏輯故意關掉，重跑一次一模一樣的情境，結果**準確重現了上一輪抓到的那個假訊息 bug**；把新邏輯打開，同樣情境就不再出現假訊息。這證明我的測試真的有鑑別力，不是自己講自己聽。

3. **在正式 Gateway 上重打一次「死亡案例」**，不是我自己接線發指令，而是像家裡的 Home Assistant 一樣，全程走 MQTT 指令 → 正式網關 → 繼電器這條路，同時在網路線上錄封包對照。結果：

   - 先把「群組 234」關到全關。
   - 單獨打開第 3 路。**同一時間畫面上的「群組 234」立刻變成「看不出來」，不會停在「全關」說謊。**
   - 等下一次正常巡檢（15 秒後），畫面依然正確顯示「看不出來」，不會被巡檢覆寫回「全關」的舊值。
   - 把第 3 路關回去，畫面上「群組 234」還是先顯示「看不出來」（因為單路指令本來就只能證明那一路，證明不了整組），直到下一次整批巡檢才正確顯示「全關」。
   - 全程沒有任何一則 MQTT 訊息同時出現「第 3 路：開」跟「群組 234：全關」這種自相矛盾的組合——這正是上一輪判 FAIL 的唯一理由，這次沒有再發生。
   - 我也另外用群組指令把「群組 234」切成真實的通電圖案（3、5 路開、4 路關），跟把「群組 01」切成全開，兩次都在線路上看到單筆 FC15 封包送出、繼電器真的動、畫面正確更新，最後都復原成全關。

測完之後，8 路繼電器全部確認回到關閉，網關沒有重啟、沒有任何錯誤或警告，也沒有動到任何一個禁止修改的檔案（14 個檔案逐位元組核對完全相同）。

**一個過程中的意外要老實講**：我在啟動背景監聽指令時，用了 `ps aux` 去確認程序有沒有跑起來，結果那個指令把 MQTT 的帳號密碼用明文印在畫面上，被我不小心看到了。我沒有把這組帳密寫進這份報告、也沒有再重複同樣的指令，但這是一個真實發生過的資訊外洩事件，必須在這裡誠實揭露，而不是假裝沒發生過。

---

## 1. 方法與證據能力

### 1.1 無法執行 `git diff`（硬性證據缺口，如實揭露）

本機不是 git working tree（`git status` → `fatal: not a git repository`，AGENTS.md 與 CLAUDE.md 皆已明載）。本輪**沒有、也不可能**做過 `git diff`。改用：

- **SHA-256 指紋**：對照 report/042 記錄的雜湊值，逐一比對 14 個檔案，證明哪些真的被 043 改了、哪些完全沒動（第 7 節）。
- **mtime**：`stat` 確認 `generic_adapter.py` 的修改時間（22:03:20）早於本輪測試開始（22:18），確認我測到的是 043 施工後、我開始驗收前就已存在的版本，不是我自己或別人在驗收過程中又動過的版本。

### 1.2 不採信來源

本輪不採信 043 report 的任何 PASS 宣稱、不採信 043 自己寫的 `scratch/fc15_043_*.py` 的執行結果、不採信 043 內文列出的任何 TX/RX hex 表格。所有協定層封包重新用獨立 oracle 算一次、所有黑洞情境重新用真正的 production `BusMasterScheduler` 跑一次、所有實機行為重新用我自己發起的 MQTT 指令與封包側錄驗一次。

### 1.3 單 master 確認（測前與測後各一次）

```
測前 22:18:37 (以及測試過程中重複確認)：
ESTAB 192.168.88.36:36920 → 192.168.88.190:502  users:(("python",pid=47878))
ESTAB 192.168.88.36:56900 → 192.168.88.191:502  users:(("python",pid=1135))   ← py_5f，不同設備

測後 22:30:xx：
ESTAB 192.168.88.36:36920 → 192.168.88.190:502  users:(("python",pid=47878))
ESTAB 192.168.88.36:56900 → 192.168.88.191:502  users:(("python",pid=1135))
```

`pid=47878` 經 `docker top ginlong` 確認就是 `ginlong` 容器內的 `python /app/src/main.py`（`docker inspect` 顯示同一 PID、啟動時間 `2026-08-12T14:03:51Z`，與 `docker ps` 的 up-time 換算後一致）。**全程只有本網關一個 master 連到 `.190:502`**，`.191` 是另一台不同實體設備（py_5f），不影響本次判定。全程未使用任何 raw Modbus socket 對 `.190` 直接發幀；所有實機寫入都是 `mosquitto_pub → py_1f/relay_8ch/3/set/{key} → 正式 Gateway → UID3` 這條路。

### 1.4 意外事件揭露：MQTT 帳密經 `ps aux` 洩漏到本次對話紀錄

啟動背景 `mosquitto_sub` 監聽程序後，我執行了 `ps aux | grep mosquitto_sub` 以確認程序存活，該指令的輸出**把 `-u`/`-P` 帳密參數以明文列印**，因而出現在本次會話紀錄中。發現後我立即停止使用 `ps aux` 查該類程序，改用只印 PID、不印指令列參數的 `pgrep -f`，且後續所有指令都改用「在子 shell 內 `source .env` 取值、指令文字本身只帶 `$MQTT_USERNAME`/`$MQTT_PASSWORD` 變數名稱」的寫法，避免帳密字面值出現在任何工具呼叫文字或輸出中。**本報告與 `scratch/` 產物均未寫入該組帳密**，符合 AGENTS.md「禁止讀出、貼入 report／log／commit」的精神；但外洩到會話紀錄本身已經發生，如實揭露，不淡化處理。建議：MQTT broker 目前使用非常簡單的帳密組合，即使只是內部網段也建議之後找機會加強。

---

## 2. 施工存在性與指紋

### 2.1 043 宣稱修改的檔案

| 檔案 | 本輪核對 mtime | 本輪核對 SHA-256（前 24 碼） | 判定 |
|---|---|---|---|
| `adapters/generic_adapter.py` | 22:03:20（早於本輪測試開始 22:18） | `2d2c27070f9921ec8c6c349d` | **確實已改**（與 042 基線 `9fd38c3b0a1669e4f8b34846` 不同） |
| `profile/relay_8ch_map2.yaml` | 21:55:25 | `0116e41bd64abb864ea058e2` | **確實已改**（與 042 基線 `5f8daff1b7e8df058091b59b` 不同，新增 `__UNMATCHED__` 選項） |

### 2.2 043 宣稱未修改的檔案（獨立核對，全部相同）

| 檔案 | SHA-256 | 判定 |
|---|---|---|
| `src/bus_master.py` | `831bfe98c534e5ad…` | 與 042/043 基線相同 |
| `src/ha_manager.py` | `d02c5b3e76401db3…` | 與 042/043 基線相同 |
| `src/driver.py` | `cc049da87d7bb02f…` | 與 042/043 基線相同 |
| `profile/relay_8ch_map.yaml` | `8d9b093e3e4f49a6…` | 與 042/043 基線相同 |
| `profile/config.yaml` | `c9ec5e0a9350bd10…848c20a` | 與 043 記錄的基線相同，UID3 仍指向 `relay_8ch_map2` |
| `adapters/modbus_tcp_adapter.py` | `b08c6abbb85e1932…` | 與 042 基線相同（16 碼核對） |
| `src/map_validator.py` | `d4f256328b20ca6e…` | 與 042 基線相同（16 碼核對） |

**六個未列入施工清單的檔案全部逐位元組相同，043 沒有夾帶未宣告的修改。**

### 2.3 一個與判決無關但值得記錄的小發現：042 報告內部表格有一處抄寫誤差

042 report 第 94 行以 24 碼記錄 `src/modbus_tcp_driver.py` 為 `688e07ea49b4ab14d27c48b7`，但同一報告第 573 行以 16 碼記錄為 `688e07ea49b4ab14`。本輪重算該檔案目前的 SHA-256 為 `688e07ea49b4ab14d27c42d2be769a9db41be63d5707e015e1838f576b95a75e`——與 16 碼版本一致，但與 24 碼版本在第 17 碼起不符（`d27c48b7` vs `d27c42d2`）。由於檔案 mtime（16:08:23）自 042 驗收當下至今從未變動，且與另外兩個獨立記錄點（16 碼版本、以及 043 report 完全沒有動過此檔的宣告）互相印證，判斷這是 **042 report 自己第 94 行的抄寫/取值誤差**，不是檔案被竄改的證據。列此存查，不影響本輪任何裁決。

### 2.4 config.yaml 仍指向正式群組 profile

`profile/config.yaml` UID3：`adapter: tcp`、`profile: relay_8ch_map2`、`mode: active`、`poll_interval: 15`——與測試前、測試後、以及 042/043 記錄的狀態一致，全程未被本輪驗收改動。

---

## 3. 043 claimed 修復邏輯：逐行讀碼確認

讀取目前 `adapters/generic_adapter.py`（V2.8，2026-08-12 22:03:20 mtime），確認新增兩段邏輯，均只存在於 `generic_adapter.py`，`modbus_tcp_adapter.py` 未被修改但透過繼承 `_extract_data()` 自動獲得（TCP adapter 的 `decode()` 明確呼叫 `GenericModbusAdapter._extract_data(self, ...)`，UID1/UID3 實際使用的正是 `tcp` adapter）：

1. **`_append_polled_coil_group_states(command, decoded)`**：在 `_extract_data()` 的 `ctx_type == "poll"` 分支、既有 bit decoder 填完 `switch_0..7` 之後呼叫。對每個 `verify_command_id == 本次 poll command id` 的 group，用既有的 `_match_coil_group_state()`（精確比對，非新邏輯）重算一次，寫回 `decoded[group_key]`。**沒有新增比對邏輯，只是把既有 exact matcher 從「只在群組寫入 verify 時呼叫」擴大到「每次正常巡檢也呼叫」。**

2. **`_mark_partial_verify_groups_unmatched(key, decoded)`**：在 `_extract_data()` 的 `ctx_type == "verify"`、`read_fc in (0x01, 0x02)` 且**沒有** `coil_group` context（也就是既有的單路 FC05 verify 路徑，count=1）分支呼叫。對任何 `members` 包含該 `key` 的 group，強制設為 `__UNMATCHED__`，不嘗試比對——因為單路 verify 只讀得到一顆 coil，物理上無法證明整組向量，所以在下一次整批 FC01 巡檢重算之前，誠實地標成「不知道」而不是保留舊值。

3. `profile/relay_8ch_map2.yaml` 的兩個 `select`（`group_01`、`group_234`）都在 `options` 加入 `"__UNMATCHED__"`，確保 Home Assistant 收到這個哨兵值時不會因為選項未宣告而報 schema 錯誤。

**這兩段新邏輯都沒有動到 `bus_master.py`、`ha_manager.py`、`driver.py`——與 043 report 的宣稱一致，且已用 SHA-256 獨立核對這三個檔案真的沒被動過（第 2.2 節）。**

---

## 4. 獨立隔離複測（全新 harness，非重跑 042/043 的腳本）

新增 `scratch/claude_044_independent_review.py`：完全重新撰寫，不 import、不執行 042 或 043 遺留的任何 scratch 腳本邏輯；只 import 目前正在執行的 production 模組（`adapters/generic_adapter.py`、`adapters/modbus_tcp_adapter.py`、`src/map_validator.py`、`src/modbus_tcp_driver.py`、`src/bus_master.py`、`src/ha_manager.py`），CRC16／FC15 PDU 打包／LSB-first 邏輯全部獨立重寫，並交叉核對 PyModbus 3.13.0（`/tmp/fc15-pymodbus-lib`，隔離安裝，未進 `requirements.txt`，未進容器）。

```
PASS 119   FAIL 0   NOT-TESTED 0   TOTAL 119
```

| 區塊 | 項目數 | 內容 |
|---|---|---|
| 0-fingerprints | 9 | 禁改檔案雜湊核對（見第 2 節） |
| 1-protocol-oracle | 34 | FC15 RTU/MBAP 七組向量對 PyModbus + 獨立重算 CRC，非法輸入 fail-closed |
| 2-mutation-control | 1 | 證明協定層 oracle 本身能偵測到刻意損壞的封包 |
| 3-ack-guard | 19 | Native TCP `AsyncModbusTcpDriver.write()` 敵對測試：3 種合法 ACK 必須 `True`、13 種惡意/畸形 ACK 必須非 `True`、3 種 Modbus exception 必須 `False` |
| 4-bitflip-matrix | 7 | FC01 群組 verify 精確比對：`group_01`、`group_234` 逐 bit 翻轉全部必須 `__UNMATCHED__`，精確命中必須回傳正確 state 名稱 |
| 4b-mutation-control | 1 | 故意把比對邏輯換成「只比第一個 bit」，證明寬鬆版本會誤判——第 4 節的 PASS 不是套套邏輯 |
| **5-poll-recompute** | **9** | **本輪核心**：直接呼叫 production `decode()`/`build_poll_read()`，驗證平常巡檢會重算 `group_01`/`group_234`；`switch_2=ON` 時巡檢絕不會同時回報 `group_234=all_off`（042 黑洞 G 的直接反例）；FC05 單路 verify 立即把牽涉的群組標成 `__UNMATCHED__`，且不誤傷不相關的群組；並附一個「證偽測試」：把兩個新方法монkey-patch 成空操作後，同一巡檢情境確實不再產生 `group_234` 這個 key（證明沒有修復邏輯就會退回沉默） |
| **6-black-hole-scheduler-repro** | **4** | **本輪核心**：用真正的 `BusMasterScheduler`（非重新實作）+ 腳本化 driver（無網路）+ 會做「合併快取」的 HA stub，完整跑一次「群組全關 → 巡檢 → 單路開 → 巡檢 → 單路關 → 巡檢」六步驟情境，核對全程**沒有任何一次 `publish_state()` 同時出現 `switch_2=ON` 與 `group_234=all_off`**；並且用同一套情境對「新方法被停用的相同 adapter」重跑一次，**確實重現 042 report 的原始矛盾**——這是本輪最重要的鑑別力證明：不是我自己寫的測試自己一定會過，而是同一份測試在「有修復」與「沒修復」兩種狀態下給出不同、且方向正確的結果 |
| 7-validator | 18 | `relay_8ch_map.yaml`／`relay_8ch_map2.yaml` 合法通過；15 種對 `coil_groups` 的惡意變異（不連續 members、count 不符、非法 token、重複 vector、`verify_command_id` 亂指、群組互相 overlap、位址超界、重複 member、未知 member、缺 `states`、`count:0`、`coil_groups` 非 dict、verify command 非 FC01、member 非 FC05、群組超出 verify FC01 範圍）全部被拒絕 |
| 8-regression | 11 | FC05 CH1/CH8 ON/OFF bytes 在「無群組的舊 profile」與「有群組的新 profile」兩邊完全相同；FC01 巡檢封包與 8 個 switch 解碼結果兩邊逐一相同；舊 profile 永遠不會冒出 `group_01`/`group_234` 這兩個 key；FC06／FC16 32-bit legacy 路徑（合成 profile）bytes 不變 |
| 9-ha-observability | 6 | 用 production `HAManager` 對兩份 profile 各產生一次完整 Discovery，逐 topic 比對：**沒有任何舊 topic 消失**，**8 個既有 switch（含 connectivity）payload 逐位元組相同**，新增的兩個 group select 都含 `__UNMATCHED__` 選項 |

第 5、6 節的「證偽測試」是本輪方法論上最重要的一步：**我沒有假設 043 的修復邏輯正確再去測，而是先確認「如果沒修好，我的測試會不會抓到」**。結果是會——把 `_append_polled_coil_group_states`/`_mark_partial_verify_groups_unmatched` monkey-patch 成空操作後，同一份腳本、同一套情境，精確重現了 042 report 記錄的矛盾（`switch_2: "ON"` 與 `group_234: "all_off"` 同時出現在同一則發布）。這證明第 119 項 PASS 不是自問自答。

完整結果：`scratch/claude_044_independent_review_results.json`。

---

## 5. 實機黑洞 G 重現（正式 Gateway，全程走 MQTT）

側錄方式：host `br0` 介面對 `192.168.88.190:502` 做唯讀 `tcpdump`（不介入總線），另開 `mosquitto_sub` 訂閱 `py_1f/relay_8ch/3/state`。所有指令經 `mosquitto_pub` 打 `py_1f/relay_8ch/3/set/{key}`，未使用任何 raw socket。測前板況：全 8 路 OFF、`group_01=all_off`、`group_234=all_off`（22:26:22、22:26:36 兩次自然巡檢已確認，未下任何指令）。

### 5.1 死亡案例逐步重放

| 時間 | 動作 | MQTT `state` payload（節錄關鍵欄位） | 線路 TX/RX |
|---|---|---|---|
| 22:27:05 | 指令 `group_234=all_off`（FC15，建立已知基線） | `switch_2..4: OFF`, `group_234: "all_off"` | TX `00 64 … 03 0F 00 02 00 03 01 00` / verify RX `03 01 01 00` |
| 22:27:07 | 指令 `switch_2=ON`（FC05 單路） | `switch_2: "ON"`, `group_234: "__UNMATCHED__"` ← **不是 all_off** | TX `00 67 … 03 05 00 02 FF 00` / verify TX `03 01 00 02 00 01`（只讀 1 顆 coil）/ RX `03 01 01 01` |
| 22:27:22 | （無指令）自然巡檢 15 秒後 | `switch_2: "ON"`, `group_234: "__UNMATCHED__"` ← **巡檢仍正確，未被覆寫回 all_off** | TX `00 69 … 03 01 00 00 00 08`（整板）/ RX `03 01 01 04`（`0x04`=bit2=switch_2 ON） |
| 22:27:42 | 指令 `switch_2=OFF`（復原） | `switch_2: "OFF"`, `group_234: "__UNMATCHED__"` ← 單路 verify 誠實標「不知道」，不猜測 | TX `00 6B … 03 05 00 02 00 00` / verify RX `03 01 01 00` |
| 22:27:52 | （無指令）自然巡檢 | `switch_2: "OFF"`, `group_234: "all_off"` ← **整批巡檢正確重算回全關** | TX `00 6D … 03 01 00 00 00 08` / RX `03 01 01 00`（`0x00`=全 0） |

**全程 5 筆狀態發布中，沒有任何一筆同時出現 `switch_2: "ON"` 與 `group_234: "all_off"`。** 042 report 判 FAIL 的唯一理由（21:27:09／21:27:12 兩次自相矛盾發布）在本輪相同情境、相同 group、相同 member 下**沒有重現**。

完整封包側錄：`scratch/044_pcap_decoded.txt`（含測試窗口內全部 UID1、UID3 流量，共 70 行）；完整 MQTT 狀態序列：`scratch/044_mqtt_state.log`（15 則，含精確 ISO 時間戳）。

### 5.2 與 043 report 自己聲稱的死亡案例對照

043 report 第 59-96 行也記錄了一次幾乎相同的序列，但依任務指示**不採信**該報告的自我陳述。本節第 5.1 的每一筆 MQTT payload 與線路 hex 都是本輪獨立發起指令、獨立側錄取得，時間戳（22:27:xx）與 043 report 記錄的時間戳（21:2x-22:0x，多輪）完全不同，證明是獨立的一次重放，不是複製貼上 043 的證據。

---

## 6. 實機 FC15 真實群組切換與復原

### 6.1 群組 234（3 路）：切換成真實通電圖案

```
22:28:17.133  Write TX   00 6F 00 00 00 08 03 0F 00 02 00 03 01 05
22:28:17.163  Write RX   00 6F 00 00 00 06 03 0F 00 02 00 03
22:28:17.424  Verify TX  00 70 00 00 00 06 03 01 00 00 00 08
22:28:17.451  Verify RX  00 70 00 00 00 04 03 01 01 14
```

`0x05` = `00000101`（3-bit 群組向量 `[ON,OFF,ON]`）→ `0x14` = `00010100`，bit2/bit4 為 1 → `switch_2=ON, switch_3=OFF, switch_4=ON`。MQTT 同步顯示 `group_234: "pattern_101"`。**PASS。**

復原：

```
22:28:19.139  Write TX   00 71 00 00 00 08 03 0F 00 02 00 03 01 00
22:28:19.170  Write RX   00 71 00 00 00 06 03 0F 00 02 00 03
22:28:19.431  Verify TX  00 72 00 00 00 06 03 01 00 00 00 08
22:28:19.457  Verify RX  00 72 00 00 00 04 03 01 01 00
```

MQTT：`group_234: "all_off"`，8 路全 OFF。**PASS。**

### 6.2 群組 01（2 路）：全開後復原

```
22:30:25.106  Write TX   00 7C 00 00 00 08 03 0F 00 00 00 02 01 03
22:30:25.138  Write RX   00 7C 00 00 00 06 03 0F 00 00 00 02
22:30:25.398  Verify TX  00 7D 00 00 00 06 03 01 00 00 00 08
22:30:25.426  Verify RX  00 7D 00 00 00 04 03 01 01 03
```

`0x03` = `00000011` → `switch_0=ON, switch_1=ON`，`group_01: "all_on"`。**PASS。**

復原：

```
22:30:27.110  Write TX   00 7E 00 00 00 08 03 0F 00 00 00 02 01 00
22:30:27.142  Write RX   00 7E 00 00 00 06 03 0F 00 00 00 02
22:30:27.402  Verify TX  00 7F 00 00 00 06 03 01 00 00 00 08
22:30:27.429  Verify RX  00 7F 00 00 00 04 03 01 01 00
```

全 OFF。**PASS。**

兩次真實群組切換都在線路上只看到**一筆** FC15（`03 0F`），驗證用一筆 FC01 整板讀回，與 042 report 記錄的「單筆 FC15、不是 FC05 迴圈」架構特徵一致，本輪獨立側錄再次確認未退化。

### 6.3 側錄期間 UID1 未受干擾

`scratch/044_pcap_decoded.txt` 顯示 UID1（`temp_humid`，FC03，10 秒週期）在整段測試期間持續正常輪詢（`01 03 00 00 00 02` 起始、`01 03 04 ...` 回應），未曾與 UID3 的任何 write→verify 區間交錯——`bus_lock` 序列化在本輪也維持正常。

---

## 7. 最終復原與健康度

| 項目 | 證據 | 結果 |
|---|---|---|
| 8 路繼電器全部回到 OFF | 22:30:xx 之後 `mosquitto_sub` 取得的最新 `state`：`switch_0..7` 全 `"OFF"` | PASS |
| `group_01` / `group_234` 回到 `all_off` | 同上 payload | PASS |
| 單 master 測前測後皆成立 | 第 1.3 節、本節重複 `ss -tnp` 核對 | PASS |
| Gateway 容器健康 | `docker ps` → `Up`；`docker inspect` → `Status=running, RestartCount=0` | PASS |
| 測試窗口內無 ERROR/CRITICAL/Traceback | `docker logs --since 22:26:00`／`--since 22:30:00` grep 皆為空 | PASS |
| MQTT／HA 可用性正常 | `py_1f/status = online`；`py_1f/relay_8ch/3/status = online`（retained topic 讀取確認） | PASS |
| **本輪未修改任何 production 或 profile 檔案** | 14 個檔案 SHA-256 測前測後逐一相同（`diff` 空輸出，見指令記錄） | PASS |
| 全程未使用 raw Modbus socket | 所有寫入指令均為 `mosquitto_pub` 經正式 Gateway 轉譯 | PASS |
| 全程未重啟容器 | `RestartCount=0`、`StartedAt` 全程未變（本輪測試不需要、也沒有重啟） | PASS |

---

## 8. 未解決但非阻擋事項（如實記錄，不影響本輪裁決）

以下是 042 report 第 14 節已記錄、043 report 未提及修復、本輪核對後**確認依然存在**的項目。它們與本輪受驗的「黑洞 G」修復無關，不影響本輪 PASS 判定，但獨立驗收有責任如實揭露，不能因為主要問題已修好就略過：

1. **`profile/relay_8ch_map2_4ch_test.yaml`、`profile/relay_8ch_map2_8ch_test.yaml` 仍殘留於 `profile/` 目錄**（042 report 2.4 節已指出）。這兩個檔案不是 `config.yaml` 引用的正式 profile，但仍會出現在 WebUI 的 profile 下拉選單中，存在被誤選的風險。本輪核對其 SHA-256 與 042 record 相同，確認自 042 以來沒有再被觸碰，也沒有被清理。
2. **`_pending_group_states` 字典從不清除**（042 report 14.4 節已指出）。讀碼確認 `_prepare_coil_group_write()` 寫入該 dict 後，成功或失敗路徑都沒有 `pop`/清除呼叫。目前不可利用——`build_verify_read()` 只在 `_process_write()` 內、同一次 attempt 中緊接 `encode_write()` 之後呼叫，且 `bus_lock` 序列化保證了呼叫順序——但這是一個潛在的 fail-open 面，若未來新增任何獨立呼叫 `build_verify_read(group)` 的路徑，會拿到上一次的 pending state 而非拒絕。

以上兩項建議留給下一輪施工清單，本輪不代為修復（依任務授權範圍，僅驗收）。

---

## 9. 最終裁決

```text
獨立隔離複測（全新 harness，119 項）:      PASS 119 / FAIL 0 / NOT-TESTED 0
  含 mutation control 證明鑑別力（4b, 5.4, 6 節）:  PASS

FC15 RTU/MBAP 協定 oracle（PyModbus 交叉核對）:   PASS
Native TCP ACK Guard（3 合法 + 13 惡意 + 3 exception）: PASS
FC01 群組 verify 精確比對（bit-flip matrix）:       PASS
Validator（2 合法 + 15 惡意 profile）:              PASS
FC01/05/06/16 REGRESSION:                          PASS
HA/MQTT OBSERVABILITY（8 個舊 switch byte-for-byte）: PASS

黑洞 G 實機重現（正式 Gateway, 全程 MQTT）:         未重現 → PASS
  - 全程 5 次狀態發布，無一次 switch_2=ON 與 group_234=all_off 同時出現
  - FC05 單路 verify 立即 fail-closed 為 __UNMATCHED__
  - 下一次正常 FC01 巡檢正確重算回真實值（both directions）

FC15 實機真實群組切換（group_234 3路、group_01 2路）: PASS
FINAL RESTORE:                                      PASS
單 Master 確認（測前/測後）:                        PASS
Production Code 零修改（14 檔 SHA-256 比對）:       PASS

FINAL: PASS
```

**判定理由**：043 report 宣稱的兩處修復（`_append_polled_coil_group_states` 巡檢重算、`_mark_partial_verify_groups_unmatched` 單路 verify 立即 fail-closed）經獨立讀碼確認存在且邏輯正確；經全新撰寫、附帶證偽測試的隔離 harness 119 項全通過，且證偽測試證明「若無修復會重現原始 bug、有修復則不會」；經正式 Gateway 全程走 MQTT 路徑的實機重放，042 report 記錄的自相矛盾狀態（`switch_2=ON` 同時 `group_234=all_off`）**在本輪相同情境下未再出現**；未修改任何 production 或 profile 檔案；測試前後單 master 成立；最終所有繼電器復原為 OFF，網關健康運行無錯誤。

依「若有任何一項未被證實，即判 FAIL」的原則檢視：本輪要求驗證的每一項——RTU/MBAP oracle、hostile ACK guard、verify/sentinel、validator、FC01/05/06/16 regression、8-switch 可觀測性、黑洞 G 實機重現（含立即 verify 與後續巡檢兩個觀測點）、實際 FC15 群組切換與復原（2 路與 3 路各一次）、最終復原——均已用獨立方法產生正面證據，沒有遺留「未測」項目。第 8 節列出的兩項屬於**既有、非本輪範圍**的次要缺口，已於 042 report 記錄在案，本輪如實延續揭露，不影響本輪「黑洞 G 是否修好」的判定。

**本輪未修改任何 production code 或 profile**（SHA-256 前後相同，14 檔逐一核對）。**未自行施工**，僅提供獨立證據與裁決。
