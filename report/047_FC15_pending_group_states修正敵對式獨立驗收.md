# 047 — FC15 `_pending_group_states` 修正敵對式獨立驗收

- 驗收者：Claude Code（獨立敵對驗收，非施工者）
- 日期：2026-08-12 23:32 – 23:45（CST）
- 受驗對象：report/046 宣稱完成的 `_pending_group_states` 生命週期修正（`get()` → `pop()`）
- 本輪 production code / profile / config 修改：**零**（SHA-256 前後比對證明）
- 新增檔案：`scratch/claude_047_pending_lifecycle_review.py`、`scratch/claude_047_pending_lifecycle_results.json`、本報告

---

## 人話結論（非工程師看得懂）

先直接回答十二個問題：

1. **046 說的 stale bug 是真的存在嗎？** **是，真的存在過。** 我用「把程式改回舊寫法」的方式重現：舊版在一次群組命令做完之後，還把上一次的目標留在記憶體裡，任何後續的驗證動作都能撿去用。
2. **`get()` 改成 `pop()` 真的修對了嗎？** **修對了。** 現在目標是「用完即銷毀」—— 建立驗證動作的當下就取走。同一支攻擊打舊版會成功、打現在的版本會被明確拒絕。
3. **重試安全嗎？** **安全。** 網關每重試一次都會重新產生一組全新的目標，不會沿用上一次的。我做了「故意讓重試不重建目標」的破壞版本來檢驗，測試確實抓得到。
4. **逾時、設備拒絕、解碼失敗這些狀況安全嗎？** **全部安全。** 我把七種結局各跑一遍（成功、不符重試、寫入逾時、設備拒絕、讀取逾時、解碼爆掉、重試耗盡），每一種結束後記憶體都是乾淨的，之後再想偷用一律被擋。
5. **舊目標真的不能再被利用了嗎？** **不能。** 全新的網關、失敗過的網關、跨群組去偷別人的目標 —— 十幾種試法全部被拒絕，沒有任何一種能撿到歷史目標。
6. **有沒有新的資料流黑洞？** **沒有找到。** 我列的八種黑洞逐一實測，全部封閉。
7. **FC15 封包還是標準的嗎？** **是。** 用業界標準函式庫當標準答案重新對照，2/3/4/8/9 路五種組合、RTU 與 TCP 兩種格式，每個位元組都一樣。
8. **舊功能有沒有被弄壞？** **沒有。** FC01、FC05、FC06、FC16 的封包逐位元組比對都跟以前一樣。
9. **Home Assistant 上的舊東西有沒有變？** **完全沒變。** 八個繼電器開關的設定卡片逐位元組相同，只多了兩個群組下拉選單。
10. **實機通過嗎？** **通過。** 全部從 Home Assistant 那條路（MQTT）下命令，讓正式網關自己去動繼電器，同時側錄網路線。11 次群組命令 = 線上剛好 11 筆 FC15，一次都沒退化成「連按好幾次單路開關」。
11. **最後有恢復原狀嗎？** **有。** 八路全關、兩個群組都顯示 all_off、網關正常、沒有重啟。
12. **最終：** **PASS。**

還有兩件值得你知道的事：

- 我順手驗證了上一輪（042）抓到的「顯示說謊」問題確實已經修好，而且**修得比我建議的更保守**：單獨開了一路之後，群組不會馬上假裝知道答案，而是先顯示「不明」，等下一次完整巡檢（15 秒內）才算出真正的值。實機時間軸完全符合。
- 有一個小地方值得記錄但不影響放行：程式在驗證時會把「這次要寫什麼」存進一個叫 `group_state` 的欄位，但**這個欄位從頭到尾沒有任何地方去讀它**。真正拿來比對的是 Home Assistant 原始命令。這表示就算目標真的殘留了，它也只能造成「不該放行卻放行」，不會造成「比對到錯的值」。這個結論比 046 自己的說法更精確。

---

## 1. 驗收方法與證據能力

### 1.1 硬性證據缺口（必須明說）

本機不是 git working tree（`git status` → `fatal: not a git repository`）。**本輪沒有、也不可能執行 `git diff`**，我不假裝做過。替代證據：

- **SHA-256 指紋**：12 個受驗檔案於驗收前後各算一次。
- **mtime 掃描**：`find -newermt` 界定 042 之後到底哪些檔案被動過。

### 1.2 不採信的來源

依任務要求，046 的 PASS 結論、測試結果、TX/RX 記錄、scratch 腳本結論、以及它對 production code 的描述，**一律當作待證命題**。本輪**沒有執行任何既有 scratch 腳本**（`fc15_046_*`、`fc15_043_*`、`fc15_041_*`、`claude_044_*` 全部未執行、未 import）。所有結論來自：

- 直接閱讀目前 production 原始碼；
- 全新撰寫的 `scratch/claude_047_pending_lifecycle_review.py`（**178 項檢查**）；
- 全新的實機閉環與唯讀封包側錄。

### 1.3 獨立 oracle

- PyModbus 3.13.0，隔離安裝於 `/tmp/fc15-pymodbus-lib`，**僅 scratch 使用**；已確認 `requirements.txt` 無 pymodbus、容器內 `import pymodbus` 失敗。
- 本檔自寫的 CRC16 / LSB-first packer / FC15 PDU builder —— **不引用 production 的 `calc_crc16` 去驗證 production 自己**。

```
PASS 178   FAIL 0   TOTAL 178
```

---

## 2. 第一節：受驗版本確認

### 2.1 042 之後實際被改動的檔案

| 檔案 | mtime | 對應輪次 |
|---|---|---|
| `profile/relay_8ch_map2.yaml` | 21:55:25 | 043／045（`__UNMATCHED__` 加入 select options） |
| `adapters/generic_adapter.py` | 23:19:17 | 043（poll 重算）+ **046（pop 生命週期）** |
| `AGENTS.md`、`README.md` | 23:25:50 | 045／046 文件同步 |

**沒有其他 production 檔案被改動。** 046 宣稱「唯一 production 修改是 `adapters/generic_adapter.py`」—— 就本輪範圍（042→047）而言屬實。

### 2.2 禁改模組指紋（與 042／043 記載比對）

| 檔案 | SHA-256（前 16 碼） | 判定 |
|---|---|---|
| `src/bus_master.py` | `831bfe98c534e5ad` | **未變** |
| `src/ha_manager.py` | `d02c5b3e76401db3` | **未變** |
| `src/driver.py` | `cc049da87d7bb02f` | **未變** |
| `src/modbus_tcp_driver.py` | `688e07ea49b4ab14` | **未變** |
| `src/map_validator.py` | `d4f256328b20ca6e` | **未變** |
| `src/main.py` | `ddc698fdecfce14b` | **未變** |
| `adapters/modbus_tcp_adapter.py` | `b08c6abbb85e1932` | **未變**（TCP adapter 完全靠繼承取得修正） |
| `profile/relay_8ch_map.yaml` | `8d9b093e3e4f49a6` | **未變** |
| `profile/config.yaml` | `c9ec5e0a9350bd10` | **未變** |

受驗版本：`adapters/generic_adapter.py` = `554ac9c6468a3d44…`（標頭版本 V2.9）、`profile/relay_8ch_map2.yaml` = `0116e41bd64abb86…`。

容器 `ginlong` 啟動於 `2026-08-12T15:21:12Z`（本機 23:21:12），**晚於** adapter 的 23:19:17 修改時間；`src/`、`adapters/`、`profile/` 均為 bind mount，因此**正在跑的就是本輪受驗的這份碼**。

---

## 3. 第二節：從 production code 重建真正的生命週期

### 3.1 靜態事實（逐行讀碼）

`_pending_group_states` 在整個 production 內**只出現 3 次**：

| 位置 | 行為 |
|---|---|
| `generic_adapter.py:67` | `__init__` 初始化為 `{}` |
| `generic_adapter.py:334` | `_prepare_coil_group_write()` —— **全部驗證通過後**才寫入 |
| `generic_adapter.py:347` | `_build_coil_group_verify_spec()` —— `pop(key, None)` **一次性消費** |

- **不存在** `.get(`、`.clear()`、`del`，也不存在第二條讀取路徑。
- `pop()` 位於 `verify_command_id` 查表與 re-validate **之前** —— 因此那兩處若拋錯，也**不會留下 residue**。

`bus_master.py::_process_write()`（禁改、未變）每個 attempt 的順序：

```text
for attempt in 1..3:
    write_payload   = adapter.encode_write(key, value)      ← 建立 pending
    read_cmd, ctx   = adapter.build_verify_read(key)        ← 消費 pending
    async with bus_lock:
        driver.write(...) → ACK → driver.read(...)
    decode → compare → success / 下一個 attempt
```

兩個關鍵靜態事實：

- **`encode_write` 與 `build_verify_read` 之間沒有任何 `await`** —— 在 asyncio 單執行緒模型下，這兩步之間不可能被其他 coroutine 插入。
- 兩者位於 `bus_lock` **之外**（如實記錄，不美化）—— 但因為上一點，這不構成 pending 被跨命令偷用的路徑。

### 3.2 十二問逐條回答（皆有實測證據）

| # | 問題 | 答案與證據 |
|---|---|---|
| 1 | `pop()` 在哪裡發生？ | `_build_coil_group_verify_spec()` 第一步。全 repo 僅此一處讀取。 |
| 2 | write timeout 前有沒有 consume？ | **有。** 實測 `driver.write()` 被呼叫的當下 pending 已是 `{}`（消費早於 TX）。 |
| 3 | ACK False 前有沒有 consume？ | **有。** 同上，消費發生在任何 I/O 之前。 |
| 4 | Modbus exception 前有沒有 consume？ | **有。** 同上。 |
| 5 | build verify 拋錯後是否殘留？ | **不會。** pop 在所有可能拋錯的步驟之前。 |
| 6 | FC01 timeout 後是否殘留？ | **不會。** 死亡案例 E 實測終局 `pending == {}`。 |
| 7 | decode exception 後是否殘留？ | **不會。** 死亡案例 F 實測 `pending == {}`。 |
| 8 | verify mismatch 後 retry 是否重新 encode？ | **是。** 追蹤到 3 個 attempt 各有一組 `SET → CONSUME`。 |
| 9 | retry 是否建立全新 target？ | **是。** 每次 consume 值都等於當次 command 值。 |
| 10 | 同 group 下一命令會取到前一個 target 嗎？ | **不會。** 案例 H 四連發，consume 序列 = `all_off, all_on, all_off, all_on`。 |
| 11 | 不同 group 會互染嗎？ | **不會。** 案例 I 交錯 + 主動跨群組攻擊皆被拒絕。 |
| 12 | concurrency / bus_lock 有偷用路徑嗎？ | **沒有。** encode→verify 之間無 `await`；且 `_arbitration_loop` 是 `await self._process_write(...)`，同一設備的寫入本來就序列化。 |

### 3.3 本輪額外查明（比 046 的說法更精確）

verify context 內的 `"group_state"` 欄位 **只被寫入、從未被任何地方讀取**（`generic_adapter.py:439`、`modbus_tcp_adapter.py:78` 寫入；全 repo 無 `get("group_state")` 或 `["group_state"]` 讀取）。真正的比對是 `bus_master` 的 `_values_equal(decoded.get(key), value)`，其中 `value` 是 **MQTT 原始命令值**。

**含意**：即使 stale target 存在，它也只能造成「不該放行的 verify 被放行」（權限問題），**不可能造成「與錯誤的值比對成功」**（正確性問題）。046 把風險描述成「取得舊 target」是對的，但沒有指出這個界線。這讓修正的必要性不減（fail-open 面確實存在），但風險等級應如實記為「權限面」而非「比對面」。

---

## 4. 第三節：Mutation Control（測試鑑別力證明）

若測試抓不到故意損壞的版本，則其 PASS 無意義。本輪建立兩個 mutation：

### Mutation A —— 把 `pop()` 還原成舊版 `get()`

| 版本 | 命令結束後 pending | 直接 `build_verify_read("group_01")` |
|---|---|---|
| **Mutation A（舊行為）** | `{'group_01': 'all_on'}` | **接受，取得舊 target `all_on`** |
| **production（現行）** | `{}` | **`ValueError` 拒絕** |

→ 測試確實重現了 046 宣稱的 stale bug，也確實證明現行版本擋住它。**鑑別力成立。**

### Mutation B —— retry 未重新建立 pending

| 版本 | driver.write 次數 | 發布次數 | 結果 |
|---|---|---|---|
| **Mutation B（損壞）** | **1**（第 2 個 attempt 因無 target 中止） | 0 | **被抓到** |
| **production（現行）** | **3** | 0 | 3 組獨立 `SET/CONSUME` |

→ 若 `pop()` 真的破壞了 retry，本測試會立刻顯現。**鑑別力成立。**

---

## 5. 第四節：死亡案例 A–J

全部以**真的執行 production `BusMasterScheduler._process_write()`** 進行（真 adapter + 腳本化 driver + 計數 HA stub），RTU 與 TCP adapter 各驗。

| 案例 | 情境 | driver.write | 發布 | 終局 pending | 終局後 direct verify |
|---|---|---|---|---|---|
| **A** | 正常成功 | 1 | **1**（含正確 group state） | `{}` | `ValueError` 拒絕 |
| **B** | mismatch→retry→第 3 次成功 | 3 | **1** | `{}` | 拒絕 |
| **C** | write timeout | 3 | **0** | `{}` | 拒絕 |
| **D** | ACK False（設備拒絕） | 1 | **0**（且零 verify） | `{}` | 拒絕 |
| **E** | FC01 verify timeout | 3 | **0** | `{}` | 拒絕 |
| **F** | decode exception | 3 | **0** | `{}` | 拒絕 |
| **G** | retry 耗盡 | 3 | **0** | `{}` | 拒絕 |

**案例 B 的逐 attempt 追蹤**（本輪核心）：

```text
attempt1: SET all_on → CONSUME all_on → 回讀 00000000 → mismatch
attempt2: SET all_on → CONSUME all_on → 回讀 10000000 → mismatch
attempt3: SET all_on → CONSUME all_on → 回讀 11000000 → 相符 → 發布 ×1
```

每個 attempt 都是獨立的 `SET → CONSUME` 配對，**沒有任何一個 attempt 依賴上一個**。

**案例 H（同 group 快速連發 all_off→all_on→all_off→all_on）**：consume 序列精確為 `['all_off','all_on','all_off','all_on']`，每次發布的 group state 等於當次 target。

**案例 I（不同 group 交錯 group_01→group_234→group_01→group_234）**：consume 序列 `['all_on','pattern_101','all_off','all_off']`，零互染。另做主動攻擊：只對 `group_01` 呼叫 `encode_write`，卻去 `build_verify_read("group_234")` → **`ValueError: coil group 'group_234' 尚未建立 FC15 write，拒絕 verify`**，且 `group_01` 自己的 pending 仍完整保留（沒有被別的群組偷走）。

**案例 J（無 encode 直接 verify —— 核心 hostile test）**：全新 RTU adapter、全新 TCP adapter，對 `group_01`、`group_234`、未知 key 各試一次 —— **6/6 全部 `ValueError` fail-closed**，沒有任何一種能取得歷史 target。

---

## 6. 第五節：資料流黑洞 A–H

| 黑洞 | 情境 | 證據 | 判定 |
|---|---|---|---|
| **A** | pending 建立但永不 consume | SET 次數 == CONSUME 次數，終局 `{}` | **封閉** |
| **B** | consume 後 retry 未重建 | 3 attempt = 3×(SET,CONSUME)；Mutation B 對照 | **封閉** |
| **C** | 失敗後歷史 target 被下一命令使用 | 耗盡失敗（all_on）後下一命令 all_off，consume 僅 `['all_off']`、發布 `all_off` | **封閉** |
| **D** | group A 的 target 被 group B 使用 | 跨群組攻擊 `ValueError` | **封閉** |
| **E** | verify 無 target 卻默默繼續 | `ValueError`，非默默繼續 | **封閉** |
| **F** | FC15 成功但 FC01 不符，HA 仍收成功 | ACK True + 回讀 `10` vs 目標 `11` → **零發布** | **封閉** |
| **G** | 單路 FC05 改 member 後 group 保留假值 | FC05 verify 後立即 `group_234=__UNMATCHED__`，非 `all_off`；FC05 wire bytes 不變 | **封閉** |
| **H** | 正常 FC01 poll 未重算 group | RTU/TCP 各驗：`10101000` → `group_01=__UNMATCHED__`、`group_234=pattern_101`；全 OFF → 兩者 `all_off` | **封閉** |

額外兩項本輪自行加測：

- **poll 重算絕不參考 pending**：故意留下 `pending={'group_01':'all_on'}` 再跑一次全 OFF 的 poll → 結果為 `all_off`（**不是** `all_on`）。poll 只看實體 coil。
- **`__UNMATCHED__` 不可當寫入命令**：RTU/TCP 皆 `ValueError`，且被拒後**不留 pending**（避免 sentinel 反而製造殘留）。

```text
DATA FLOW BLACK HOLE: NONE
```

---

## 7. 第六節：FC15 協定重新獨立驗證

五組向量，production encoder 與 **PyModbus oracle byte-for-byte 相同**（RTU 與 MBAP 各一份），並逐欄位重驗：

| 向量 | RTU 幀（含 CRC） | MBAP | FC/start/qty/bytecount/LSB/未用高位 |
|---|---|---|---|
| 2 coils @0 = `11` | `03 0F 00 00 00 02 01 03 1F 4F` | oracle 相同 | 全部正確 |
| 3 coils @2 = `101` | `03 0F 00 02 00 03 01 05 B7 4D` | oracle 相同 | 全部正確 |
| 4 coils @4 = `1101` | `03 0F 00 04 00 04 01 0B 0F 48` | oracle 相同 | 全部正確 |
| 8 coils @0 = `10101010` | `03 0F 00 00 00 08 01 55 BF 73` | oracle 相同 | 全部正確 |
| 9 coils @0（跨 byte） | PDU `0F 00 00 00 09 02 55 01` | oracle 相同 | 未用高位 = 0 |

**Native TCP ACK Guard**（`src/modbus_tcp_driver.py`，本輪未變，仍需重驗）：

- 合法 FC05/06/15/16 ACK → **4/4 回 `True`**
- 惡意 ACK（wrong txid / protocol / MBAP length / UID / FC / address / quantity / short / empty / truncated / overlong）→ **11/11 全部拒絕**
- FC05/06/15/16 Modbus exception → **4/4 回 `False`**

---

## 8. 第七節：舊功能與對外可觀測性

| 項目 | 方法 | 結果 |
|---|---|---|
| FC01 poll 幀 | map vs map2 逐位元組 | **相同** |
| FC01 八路解碼 | 同一回應兩份 profile | **八個 switch 值完全相同**；新增 key 僅 `group_01`、`group_234` |
| FC05 CH1/CH8 ON/OFF | bytes + 標準重算 | **未變**（`03 05 00 0X FF00/0000`） |
| FC05 verify | 是否被群組邏輯污染 | **仍是單路 FC01 count=1**，未變 |
| FC06 | encoder bytes | **未變** |
| FC16 16-bit legacy | encoder bytes | **未變** |
| FC16 32-bit codec | bytes + verify context | **未變**（FC03 quantity=2、`strict_verify`） |
| 無 `coil_groups` 舊 profile | 是否誤入群組分支 | **不會** |
| Validator | map / map2 / 非連續 members | 合法 2/2 通過、非法被拒 |
| HA Discovery | map vs map2 全 topic JSON 比對 | **既有 8 switch + connectivity byte-for-byte 相同**；無 topic 消失 |
| 新增實體 | — | 只有兩個 group `select`，options 含 display-only 的 `__UNMATCHED__` |
| MQTT 契約 | state_topic / command_topic / payload_on/off / availability_mode | **全部不變** |

**沒有任何舊 observable 因本次 pending lifecycle 修正而改變。**

---

## 9. 第八～十節：Production 實機敵對驗證

全程 `MQTT → production Gateway → TCP/MBAP FC15 → Relay → ACK → FC01 verify → Gateway decode → MQTT state`。**未使用任何 raw Modbus sender**；`tcpdump` 為 host `br0` 唯讀側錄。

測前／測後單 master 皆成立（`192.168.88.190:502` 只有 Gateway Python 一條連線）。基線：8 路全 OFF、`group_01=all_off`、`group_234=all_off`。

### 9.1 Test 1 —— `group_01` all_on → all_off → all_on → all_off

| 時間 | MQTT 命令 | FC15 TX | ACK RX | FC01 TX / RX | MQTT state |
|---|---|---|---|---|---|
| 23:39:18 | `all_on` | `00 66 …03 0F 0000 0002 01 03` | `00 66 …03 0F 0000 0002` | `00 67 …03 01 0000 0008` / `…01 03` | switch_0/1 ON、`group_01=all_on` |
| 23:39:24 | `all_off` | `00 68 …01 00` | `00 68 …` | `00 69 …` / `…01 00` | 全 OFF、`all_off` |
| 23:39:30 | `all_on` | `00 6B …01 03` | `00 6B …` | `00 6C …` / `…01 03` | switch_0/1 ON、`all_on` |
| 23:39:36 | `all_off` | `00 6D …01 00` | `00 6D …` | `00 6E …` / `…01 00` | 全 OFF、`all_off` |

### 9.2 Test 2 —— `group_234` pattern_101 → all_off

| 時間 | 命令 | FC15 TX | FC01 RX | MQTT state |
|---|---|---|---|---|
| 23:39:42 | `pattern_101` | `00 6F …03 0F 0002 0003 01 05` | `…01 14`（= `00010100`） | switch_2 ON、switch_3 OFF、switch_4 ON、`group_234=pattern_101` |
| 23:39:48 | `all_off` | `00 72 …03 0F 0002 0003 01 00` | `…01 00` | 全 OFF、`all_off` |

### 9.3 Test 3／4 —— 042 死亡案例重打（本輪最重要的實機檢驗）

```text
23:39:54  group_234=all_off (FC15)      → 全 OFF, group_234=all_off
23:40:00  switch_2=ON      (FC05)       → switch_2=ON, group_234=__UNMATCHED__   ★
          TX 00 77 …03 05 0002 FF00 / verify 00 78 …03 01 0002 0001 → 01
23:40:13  正常 FC01 整板 poll            → switch_2=ON, group_234=__UNMATCHED__   ★
          TX 00 79 …03 01 0000 0008 / RX 01 04
23:40:20  switch_2=OFF     (FC05)       → switch_2=OFF, group_234=__UNMATCHED__
23:40:28  正常 FC01 整板 poll            → 全 OFF, group_234=all_off              ★
          TX 00 7C …03 01 0000 0008 / RX 01 00
```

- **整段 23 筆 MQTT state 發布中，沒有任何一筆同時出現 `switch_2=ON` 與 `group_234=all_off`。** 042 的假狀態未再現。
- FC05 單路 verify 只讀一顆 coil（`00 78 …03 01 0002 0001`），無法證明整組，因此**立即** fail-closed 為 `__UNMATCHED__` —— 符合任務第九節「合法應為 `group_234=__UNMATCHED__`」。
- `switch_2=OFF` 之後仍維持 `__UNMATCHED__`（單路 verify 依舊不能證明整組），**直到 23:40:28 的完整 FC01 poll** 才重算回 `all_off` —— 符合任務第九節 Test 4。

### 9.4 額外 hostile —— `__UNMATCHED__` 當寫入命令

```text
23:40:42  [Command] UID=3 key=group_01 value=__UNMATCHED__
23:40:42  [ERROR] adapter 編碼失敗，跳過此次寫入
          ValueError: coil group 'group_01' 不支援 state '__UNMATCHED__'
```

側錄顯示 23:40:28 至 23:40:43 之間**除排程輪詢外零 UID3 幀** —— sentinel 命令產生 **零 FC15 TX**，大聲失敗、fail-closed。

### 9.5 額外 hostile —— 同群組不等待的快速連發

23:40:48.399 / 48.703 / 49.007 連送 `all_on`、`all_off`、`all_on`（間隔 0.3 秒）。線上結果：

```text
00 7E FC15 01 03 → 00 7F FC01 → 01 03   (all_on)
00 80 FC15 01 00 → 00 81 FC01 → 01 00   (all_off)
00 82 FC15 01 03 → 00 83 FC01 → 01 03   (all_on)
```

三筆命令各自完整、各自 verify、順序正確，MQTT state 依序 `all_on → all_off → all_on`。**沒有任何一次 verify 用到別次的 target。**

### 9.6 第十節 —— 真正的單筆 FC15

側錄期間 UID3 的 TX 統計：

```text
FC15 = 11    FC05 = 2    FC01 = 24
```

本輪共下 **11 次群組命令** → 線上剛好 **11 筆 FC15**。`group_234` 的 3 路是**一筆** `03 0F 0002 0003 01 05`，**沒有**退化成 3 筆 FC05。2 筆 FC05 對應 `switch_2` 的 ON/OFF 兩次單路命令。

另做原子性機器檢查：11 筆 FC15 交易的 `Write TX → ACK → FC01 TX → FC01 RX` 四幀**全部連續，插入違規 0 次**（UID1 的溫濕度輪詢從未插進任何 write→verify 區間）。耗時 318.6–320.4 ms，離散度極低。

> 界線（不誇大）：FC15 只證明多個 coil 在**同一筆 Modbus request** 提交，且本 Gateway 內部不會自己插隊；**不**等於實體繼電器接點在同一微秒動作，**不**構成 RS485 世界的 global transaction。ATS 等安全切換仍須硬體互鎖。

---

## 10. 第十一節：最終復原

| 項目 | 證據 | 結果 |
|---|---|---|
| `switch_0..7 = OFF` | 最終 state 八路全 OFF | PASS |
| `group_01 = all_off` | 最終 state | PASS |
| `group_234 = all_off` | 最終 state | PASS |
| Gateway ONLINE | `py_1f/status = online` | PASS |
| MQTT ONLINE | `py_1f/relay_8ch/3/status = online` | PASS |
| UID1／UID3 ONLINE | health：`{"1":{"online":true,"timeout_count":0},"3":{"online":true,"timeout_count":0}}`、`quarantined: []` | PASS |
| RestartCount | `restarts=0`、`status=running` | PASS |
| 無 ERROR/CRITICAL/Traceback | 測試窗口內僅 1 筆 —— 23:40:42 我**刻意注入**的 sentinel 故障測試，屬預期 | PASS |
| 單 master | 測前／測後皆只有 1 條 `.190:502` 連線 | PASS |

---

## 11. 第十二節：Production 零修改證據

12 個受驗檔案於**驗收前**與**驗收後**各算一次 SHA-256，逐一相同：

```text
554ac9c6468a3d44  adapters/generic_adapter.py
b08c6abbb85e1932  adapters/modbus_tcp_adapter.py
831bfe98c534e5ad  src/bus_master.py
cc049da87d7bb02f  src/driver.py
688e07ea49b4ab14  src/modbus_tcp_driver.py
d02c5b3e76401db3  src/ha_manager.py
d4f256328b20ca6e  src/map_validator.py
ddc698fdecfce14b  src/main.py
8d9b093e3e4f49a6  profile/relay_8ch_map.yaml
0116e41bd64abb86  profile/relay_8ch_map2.yaml
c9ec5e0a9350bd10  profile/config.yaml
b05f6f7098338916  AGENTS.md
```

本輪只新增 `scratch/` 測試與本報告，**未修改任何 production code、profile 或 config**，未重啟容器，未變更 runtime 設定。

---

## 12. 觀察事項（不影響裁決，供後續參考）

1. **`context["group_state"]` 是 dead metadata**（第 3.3 節）。建議：若無預定用途，於下次觸及該檔時移除或加註「僅供除錯」，避免日後有人誤以為它參與比對而據此放寬邏輯。**本輪不建議為此單獨動 production。**
2. **同群組快速連發存在既有的 pending_writes 合併語意**（`bus_master` 以 `(uid, key)` 為鍵，未派發前重複寫入會合併成最後一個值）。本輪 0.3 秒間隔下三筆都各自派發，未觸發合併；此為既有設計，不是本次修正引入。
3. **042／044 記錄的兩項既有缺口仍在**：`profile/` 內未被引用的 `relay_8ch_map2_4ch_test.yaml`、`relay_8ch_map2_8ch_test.yaml` 仍會出現在 WebUI profile 下拉選單。與本輪主題無關，如實延續揭露。

---

## 13. 最終裁決

```text
Lifecycle（12 問逐條實證）:            PASS
Stale prevention:                      PASS
Mutation control（A 與 B 皆有鑑別力）: PASS
Retry:                                 PASS
Timeout:                               PASS
Exception:                             PASS
Group isolation:                       PASS
Direct verify fail-closed:             PASS
FC15 RTU oracle:                       PASS
FC15 MBAP oracle:                      PASS
CRC:                                   PASS
ACK guard（4 合法 / 11 惡意 / 4 exception）: PASS
FC01:                                  PASS
FC05:                                  PASS
FC06:                                  PASS
FC16:                                  PASS
Validator:                             PASS
HA / MQTT observability:               PASS
黑洞 A～H:                             NONE
Production MQTT closed loop:           PASS
Real FC15 single transaction（11/11）: PASS
Final restore:                         PASS
Production zero modification:          PASS

隔離檢查：178 PASS / 0 FAIL
實機：11 次群組命令 = 11 筆 FC15，0 次插入違規

FINAL: PASS
```

**判定理由**：046 宣稱的 stale 風險經 Mutation A 獨立重現屬實；`pop()` 修正經 178 項全新敵對檢查與兩組具鑑別力的 mutation control 證明有效且未破壞 retry；七種終局全部 `pending == {}` 且終局後 direct verify 一律 fail-closed；跨群組與同群組連發皆無互染；八種資料流黑洞逐一實測封閉；FC15 協定與 PyModbus oracle byte-for-byte 相同；FC01/05/06/16 與既有 HA/MQTT 契約 byte-for-byte 未變；實機經正式 MQTT 閉環完成 Test 1–4 與兩項額外 hostile，042 的自相矛盾狀態未再現，且 `__UNMATCHED__` 的暫態→整板 poll 重算時間軸完全符合規格；最終全部復原、網關健康；本輪 production 零修改。

沒有任何一項落入 FAIL / UNPROVEN / NOT TESTED。
