# 052 — Gateway 系統級敵對式總驗收

- 驗收者：Claude Code（獨立敵對驗收者，非施工者）
- 日期：2026-08-13 02:08 – 02:30（CST）
- 範圍：南北向資料流、靜默失敗、熔斷、重連、鎖、佇列、資源洩漏、錯誤隔離、對外可觀測性
- 本輪 production 修改：**零**（19 個模組 SHA-256 前後逐一比對）
- 新增檔案：`scratch/claude_052_fake_device.py`、`claude_052_system_review.py`、`claude_052_soak_leak.py`、`claude_052_mqtt_soak.py`、三份 results JSON、本報告

---

## 人話結論

**整體體質很好，但我找到一個真的會讓資料無聲消失的洞，所以判 FAIL。**

先講那個洞。如果有人在地圖檔（profile）裡把 `command_id` 打錯一個字 —— 例如寫成 `read_coil` 而不是 `read_coils` —— 會發生這些事：

1. 驗證器說「檢查通過」；
2. 那個感測器**永遠不會被讀取**；
3. Home Assistant 上那個實體會被建立出來，但**永遠沒有數值**；
4. **從頭到尾不會有任何一行 log**，連 DEBUG 等級都沒有。

也就是說：打錯一個字，換來一個永遠空白的實體，而且系統完全不會告訴你。我實測驗證過了。修起來只要在驗證器加三行（把 sensor 的 `command_id` 跟 read_commands 對一次），不用動 adapter。**目前現場掛的兩份地圖都沒有這個問題**，所以不是現在就在出事，是「等著出事」。

其餘十二個問題的答案：

1. **南向資料流完整嗎？** 完整。我自己寫了一台假設備，讓**真的 driver、真的 adapter、真的排程器**去跟它對話，FC01、02、03、04、05、06、15、16 八個功能碼全部跑通，寫入還會回讀比對。
2. **北向資料流完整嗎？** 完整。從設備讀到的值一路進到 MQTT 的合併 JSON，命令也能從 MQTT 一路走回設備再驗證回來。
3. **有靜默失敗嗎？** 有一個（上面那個）。其餘 8 類黑洞我逐一注入測試，全部都會大聲失敗。
4. **熔斷照設計跑嗎？** 是。連 4 次失敗不熔斷、第 5 次熔斷並通知 HA、設備回來連 2 次成功就恢復 —— 一步不差。
5. **重連可靠嗎？** 可靠。我對**正在跑的現場網關**直接砍掉它跟設備的連線兩次，它都在**同一秒內**重連成功並繼續工作。MQTT 那邊我在容器裡砍了 30 次連線，30 次全部自動重連、訂閱恢復、命令真的收得到。
6. **鎖有問題嗎？** 沒有。鎖的順序是單向的，例外和取消之後都會正常釋放，不會卡死。
7. **佇列／排程正常嗎？** 正常。有上限、有防餓死機制、設備卸載後殘留命令會被丟掉。
8. **有記憶體／FD／task 洩漏嗎？** 沒有。跑 120 次故障循環 + 50 次強制斷線重連，檔案描述符從頭到尾都是 10 個，task 數 2 個，記憶體只動了 0.25MB 而且中後段完全不動。現場程序跑完兩次砍線 + 寫入 + 閒置，FD 18、執行緒 8、記憶體 51596KB **一個數字都沒變**。
9. **一台壞設備會拖垮別台嗎？** 不會。我讓 UID3 一直逾時到熔斷，UID1 全程 8 次輪詢全成功、狀態沒被誤改。
10. **FC01~FC16 有退步嗎？** 沒有。八個功能碼的封包、解碼、驗證、ACK 守衛全部重測過。
11. **HA/MQTT 對外約定一致嗎？** 一致。8 個開關的設定卡片逐位元組相同，只多了兩個群組選單。
12. **WebUI 正常嗎？** 正常。認證擋得住、選單載得出來、存檔讀檔都對，型別沒有被偷改。存檔時只會把檔尾的換行去掉，這是無害的差異。

---

## 1. 驗收方法與證據能力

### 1.1 硬性證據缺口

本機非 git working tree，**本輪沒有也無法執行 `git diff`**。改以 19 個模組的 SHA-256 於驗收前後各算一次。

### 1.2 不採信的來源

041／049／050 的 PASS 只當基線參考。本輪**未執行任何既有 scratch 腳本**，全部重寫。

### 1.3 核心手法：真 stack + 假設備

`scratch/claude_052_fake_device.py` 是我自建的 Modbus TCP slave，支援 FC01/02/03/04/05/06/15/16，並可精準注入 `silent`／`fin`／`reset`／`garbage`／`exception`／`wrong_uid`／`wrong_fc`／`bad_mbap_len`／`short` 九種故障。上面接的是**未經修改的 production 類別**：

```
BusMasterScheduler ← AsyncModbusTcpDriver ← 真 TCP socket → FakeModbusTCPServer
        ↓                                                          ↑
   TcpAdapter (production)                              可控故障注入
        ↓
   HAManager (production) → FakeMQTT（只記錄，不改行為）
```

好處：能對整條鏈做破壞性測試，而**完全不接觸現場的 192.168.88.190**。現場只做唯讀觀測、兩次 socket 砍線與四筆已授權的空載繼電器寫入。

### 1.4 結果總計

| Harness | 結果 |
|---|---|
| `claude_052_system_review.py`（功能／熔斷／佇列／鎖／隔離／可觀測性） | **99 PASS / 1 FAIL / 3 NOTE** |
| `claude_052_soak_leak.py`（120 故障循環 + 50 重連，host） | **14 PASS / 0 FAIL** |
| `claude_052_mqtt_soak.py`（30 次 MQTT 非預期斷線，容器內） | **9 PASS / 0 FAIL** |
| 現場實機（砍線 ×2、寫入 ×4、重啟 ×1） | 全部 PASS |

唯一的 FAIL 就是第 6 節的缺陷 F1。

---

## 2. 模組清單與職責

| 模組 | 檔案 | 主要責任 | 本輪驗證 |
|---|---|---|---|
| Runtime wiring | `src/main.py` | 載入設定／Adapter／Driver／MQTT；MQTT 命令路由；生命週期 | 命令路由、關機、LWT、Discovery |
| Scheduler | `src/bus_master.py` | 輪詢排程、寫入快車道、熔斷、write→verify | 熔斷 6 案例、佇列、鎖、黑洞 |
| RTU-over-TCP Driver | `src/driver.py` | socket 管理、重連、幀間延遲、RTU ACK 守衛 | 50 次重連、FD、鎖釋放 |
| Native TCP Driver | `src/modbus_tcp_driver.py` | MBAP 封裝、write ACK 嚴格驗證 | 故障矩陣、ACK fail-closed |
| Local Serial Driver | `src/local_serial_driver.py` | USB/RS485 直連 | 靜態（本機無 usb 設備，未實跑） |
| Listen Driver / Master | `src/listen_driver.py`、`listen_master.py` | 被動監聽軌與保險絲 | 靜態（本機無 listen 設備） |
| Generic Adapter | `adapters/generic_adapter.py` | RTU codec、FC01-06/15/16、coil_groups | 八個功能碼 encode/decode/verify |
| TCP Adapter | `adapters/modbus_tcp_adapter.py` | MBAP framing、FC15、FC16 | 同上（實跑走這支） |
| HA Manager | `src/ha_manager.py` | Discovery／state／availability | 契約比對、節流、發布失敗回傳 |
| MQTT Client | `src/mqtt_client.py` | Paho 包裝、自動重連、重訂閱 | 容器內 30 次斷線重連 |
| Validator | `src/map_validator.py` | Profile schema | 合法 3、非法 3、**缺陷 F1** |
| Adapter Catalog | `src/adapter_catalog.py` | 外掛探索 | 壞外掛隔離、WebUI catalog |
| Web Admin | `src/web_admin.py`、`src/index.html` | 設定 API、認證 | 認證、catalog、存檔往返 |
| Runtime config/profile | `profile/*.yaml` | 現場設定 | 驗證器、orphan sensor 掃描 |

---

## 3. 南向完整資料流（八個功能碼）

真 stack 對假設備，逐段驗 `Scheduler → BusMaster → Adapter encode → Driver TX → Device → Driver RX → Adapter decode → Scheduler`：

| 功能碼 | 驗證內容 | 結果 |
|---|---|---|
| **FC01** | 線圈輪詢 → 8 個 switch 全部解出 | PASS |
| **FC02** | 離散輸入輪詢 → di_0=ON／di_1=OFF 與設備狀態一致 | PASS |
| **FC03** | 保持暫存器輪詢 → uint16 與 uint32（大端）同時解出 | PASS |
| **FC04** | 輸入暫存器輪詢 → 值與設備一致 | PASS |
| **FC05** | 寫線圈 → 設備狀態改變 → FC01 回讀驗證 → 發布 | PASS |
| **FC06** | 寫單暫存器 → holding 改變 → FC03 回讀驗證 | PASS |
| **FC15** | 群組寫入 → 兩個 coil 同時改變 → 整板 FC01 exact 比對 | PASS |
| **FC16** | quantity=2 寫入 → 兩個 register 皆改變 → **4-byte** exact 比對 | PASS |

錯誤路徑同時驗證：ACK 正常但**回讀不符**時，publish 次數為 **0**，不得 silent success。

---

## 4. 北向完整資料流

| 段落 | 成功去哪 | 失敗去哪 | 是否 publish | 是否 retry | availability |
|---|---|---|---|---|---|
| Device RX → decode | `result` dict | `DataDecodeError` → `_record_failure` | 否 | 下次排程 | 5 次後 offline |
| decode → BusMaster | `publish_state(decoded)` | 空 dict → 視為解碼無效 | 否 | 同上 | 同上 |
| BusMaster → HA Manager | 合併進 `_state_cache` | — | 200ms 節流內合併不丟 | — | — |
| HA Manager → MQTT | `_safe_publish` 回 True | rc≠0 → **回傳 False + WARNING** | — | — | — |
| MQTT → HA | 單一合併 JSON | — | — | — | 雙 topic `all` |

反向（命令）：

```
HA/MQTT command → _on_mqtt_message → _cmd_queue(500)
→ _mqtt_consumer_task → submit_write → pending_writes(200)
→ _arbitration_loop → _process_write
→ encode_write → build_verify_read → bus_lock{ driver.write → ACK → driver.read }
→ decode → _values_equal → publish_state + _record_success
```

每個失敗出口都有明確 log：adapter 編碼失敗（ERROR+traceback）、設備邏輯拒絕（WARNING）、物理 Timeout（WARNING）、回讀不符（WARNING）、耗盡（ERROR）。UID 未掛載／被隔離／純監聽模式三種丟棄情境也各有專屬 ERROR 訊息。

**實測節流不丟資料**：連續四種寫入後 state cache 仍同時保有 `sw_0`、`hold_u16`、`grp_01`、`hold_u32`，節流窗過後第一筆發布即帶全部歷史 key。

---

## 5. 熔斷驗證（只驗是否照現行設計跑）

先從 production code 重新推導現行設計，不採信舊報告：

```
_record_failure : timeout_count += 1；success_count = 0
                  timeout_count == 5 且 online → OFFLINE + set_availability(False)
                                              + 清掉 heap 內該 UID + 以 offline_time 重排
_record_success : success_count += 1；timeout_count = 0
                  success_count >= 2 且 not online → ONLINE + set_availability(True)
_reschedule     : not online 且 timeout_count >= 5 → offline_time，否則 poll_interval
寫入路徑        : 物理故障耗盡 → _record_failure；數值不符耗盡 → _record_success（維持 ONLINE）
```

| Case | 注入 | 期望（依現行設計） | 實測 | 結果 |
|---|---|---|---|---|
| 1 | 單次 timeout | 不熔斷，count=1 | online=True, count=1 | PASS |
| 2 | 連續 4 次（threshold−1） | 仍 ONLINE | online=True, count=4 | PASS |
| 3 | 第 5 次 | OFFLINE + availability=offline + 慢速重排 | 三者皆符合，heap 內該 UID 恰 1 筆 | PASS |
| 4 | 設備恢復 | 連續 2 次成功 → ONLINE + availability=online | count 歸零、online=True、發布 online | PASS |
| 5 | UID3 持續 timeout | UID1 不受影響 | UID3 OFFLINE；**UID1 8/8 輪詢成功且全程 ONLINE** | PASS |
| 6 | 寫入 verify 數值不符耗盡 | 視為硬體限制，維持 ONLINE、不計 transport fault | online 不變、timeout_count 不變 | PASS |

```text
Circuit Breaker Implementation: PASS
Current Policy Design:          NOT EVALUATED AS DEFECT
```

---

## 6. 資料流黑洞總掃描

| 類別 | 注入方式 | 觀測 | 判定 |
|---|---|---|---|
| **A. Command** | 送出未定義的 group state | ERROR + traceback，**零 TX、零發布** | 封閉 |
| **B. ACK** | wrong uid／wrong fc／bad MBAP len／short／exception | 全數拒絕，**零發布**且計入失敗 | 封閉 |
| **C. Decode** | garbage、byte-count 說謊 | `DataDecodeError`，零發布 | 封閉 |
| **D. Verify** | ACK 成功但回讀不符 | 零發布、retry 3 次、耗盡記 ERROR | 封閉 |
| **E. Queue** | 同 key 連發、400 筆灌爆 | 合併有 log，上限 200 不無限成長；卸載後丟棄有 WARNING | 封閉 |
| **F. MQTT** | publish 回 rc≠0 | `publish_state` **回傳 False** + WARNING（非靜默成功） | 封閉 |
| **G. Availability** | 熔斷／恢復 | offline/online 都有對應發布，與 log 一致無矛盾 | 封閉 |
| **H. Exception** | 逾時、解碼例外、取消 | 資料路徑上每個 except 都有 logger（靜態逐條確認） | 封閉 |
| **I. Scheduler** | 故障後排程 | 每條路徑都會 `_reschedule`，UID 不會從 heap 永久消失 | 封閉 |
| **F1（新發現）** | profile 的 `command_id` 打錯 | **validator 放行 + 永遠不解碼 + 零 log** | **FOUND** |

### 6.1 缺陷 F1（本輪唯一 FAIL）

**現象**：`sensors[].command_id` 若不指向任何 `read_commands[].id`：

```
validator 結果 : PASS（未攔截）
decode 結果    : {'good': 4369}          ← 'typo' 這個 key 永遠不存在
B1_INFO 宣告的 typo 實體 : 已送出 Discovery，但永遠收不到值
全程 log       : 只有 "Preparing to poll command: c1"，沒有任何警告
```

**根因**：

1. `src/map_validator.py` 的 `_check_backend()` **沒有**把 `sensors[].command_id` 與 `read_commands` 交叉比對（同一支檔案裡的 `coil_groups.verify_command_id` **有**做這件事，且 `command_by_id` 這個 dict 已經存在）。
2. `adapters/generic_adapter.py::_extract_data()` 的 poll 分支以 `sensor.get("command_id") != cmd['id']: continue` 略過不相干 sensor —— orphan sensor 對**每一個** command 都不相干，因此永遠被 `continue` 掉，而且不計入 `declared`，所以 V2.5 的「丟棄 N/M 個 sensor」彙總 WARNING 也**不會**觸發。

**影響面**：目前現場掛載的 `relay_8ch_map2.yaml` 與 `temp_humid_map.yaml` **都沒有 orphan sensor**（本輪已逐一掃描確認），因此此缺陷**尚未實際發作**。它是「一個 typo 就永久靜默」的體質問題。

**最小修正（只提方案，不施工）**：

- 檔案：`src/map_validator.py`
- 位置：`_check_backend()` 內，`command_by_id` 已可取用之處
- 內容：對每個 sensor，若 `command_id` 不在 `command_by_id`，`errors.append(...)`（約 3 行）
- 不需要動 `generic_adapter.py`、`bus_master.py`、任何 profile
- 附帶建議（非必要）：`_extract_data` 可在建構時把 orphan sensor 記一次 WARNING，作為第二道防線

---

## 7. 重連

### 7.1 Modbus TCP（隔離：50 次強制斷線）

| 注入 | 結果 |
|---|---|
| remote close (FIN)／connection reset（交替） ×50 | 每次下一筆請求都成功恢復（**50/50**） |
| FD | 10 → **10**（不得 +50） |
| asyncio task | 2 → **2** |
| `driver._bg_tasks`（socket 回收小精靈） | **0 殘留**（`add_done_callback` 自清理有效） |

### 7.2 Modbus TCP（現場實機，兩次砍線）

對**正在跑的現場網關**直接 `ss -K` 砍掉它與 192.168.88.190:502 的連線：

```
02:23:11 [WARNING] 發送失敗（TCP 斷線）: [Errno 103] Software caused connection abort
02:23:11 [WARNING] 重連中，_io_lock 持鎖最長 5s，其他設備暫停
02:23:11 [INFO]    連線 192.168.88.190:502 → 連線成功 → 重連成功
02:23:11 [ERROR]   [1] 通訊失敗，累計 1 次        ← 只損失當下那一筆，未觸發熔斷
```

第二次（02:24:01）行為完全相同。兩次都在**同一秒內**恢復，之後的寫入與輪詢全部正常，`ss` 顯示只有一條新的 ESTAB 連線、無殘留。

### 7.3 MQTT（容器內，真實 broker，30 次非預期斷線）

以 `client._sock.close()` 模擬非優雅斷線（不是 graceful disconnect）。**不只看 connected 旗標**，每次都實際做一次 `publish → 訂閱回收` 往返：

| 項目 | 結果 |
|---|---|
| 斷線後自動重連 + 訂閱恢復 + command 真的收得到 | **30/30** |
| `on_connect` 回呼次數 | 31（初次 + 30 次重連） |
| FD | 7 → **7** |
| thread | 2 → **2** |
| `_subscriptions` 集合 | 維持 1 筆，未重複膨脹 |
| `msg_queue` | 無殘留，`disconnect()` 後清空 |

---

## 8. Lock 審查

| Lock | 取用點 | 用途 |
|---|---|---|
| `bus_master.bus_lock` | 2 | 序列化總線存取（poll 與 write 臨界區） |
| `bus_master.write_lock` | 2 | 保護 `pending_writes` dict |
| `driver._io_lock` | 1 | socket I/O 與重連 |

**鎖序**：`bus_lock → _io_lock`，單向。`driver.py` 內完全不出現 `bus_lock`，**不存在 lock order inversion**。`write_lock` 臨界區內只有 dict 操作，無任何 await I/O。

**釋放驗證（動態）**：

- 逾時例外後：`bus_lock` 與 `_io_lock` **皆已釋放**
- 任務被 `cancel()` 後：兩鎖**皆已釋放**，且之後仍能正常取得鎖完成輪詢（無死鎖）
- `bus_lock` 臨界區內**不呼叫 MQTT publish**（靜態確認）

**如實記錄，非缺失**：

- 重連發生在 `_io_lock` 內，連線期間其他設備暫停，最長 `connect_timeout`（5s）。production 註解已明示此設計，現場實測重連在同一秒完成。
- FC05/06/15/16 的**單次** write → ACK → verify 在同一個 `bus_lock` 內；**retry 之間鎖會釋放**，其他 poll/write 可插入。這是 Gateway 內部的原子操作循環，不是 Modbus 世界的 transaction。

---

## 9. Queue / Scheduler

| 項目 | 現行設計 | 實測 |
|---|---|---|
| 同 key 連發 4 次 | last-write-wins 合併 | 合併為 1 筆，生效值為最後一個 |
| 不同 key | 各自保留 | 3 個 key 全部實際寫入設備 |
| 上限 | 200 | 灌 400 筆後 `len ≤ 200`，**未無限成長** |
| poll 餓死 | 連續 5 次寫入強制插入 poll | 30 筆寫入洪水下 poll 仍被執行 |
| 設備卸載 | 待派發寫入丟棄且有 WARNING | 卸載後取不到該 UID 任務，不誤派 |
| 排程 heap | 每條路徑都 `_reschedule` | 120 循環後 heap ≤ 4 筆，無成長 |

---

## 10. 資源洩漏（加速 soak）

### 10.1 南向 120 次故障循環（每循環：正常輪詢 + 寫入 + 一種故障；每 10 次強制設備重開機）

| 取樣點 | FD | RSS (KB) | asyncio task | thread |
|---|---|---|---|---|
| baseline | 10 | 16896 | 2 | 1 |
| cycle 61 | 10 | 17152 | 2 | 1 |
| cycle 120 | 10 | 17152 | 2 | 1 |
| after GC | **10** | **17152** | **2** | **1** |

循環統計：`poll_ok=120, poll_fail=120, write_ok=120, 假設備收到 57 次新連線`。

判定：FD 完全不動；task 不動；RSS 僅 +256KB 且**在後半段完全持平**（非單調成長）；`pending_writes=0`、heap 未成長、state cache 未爆量。故障循環結束後設備仍能恢復 ONLINE（熔斷可逆）。`driver.disconnect()` 後 socket 與背景任務全部釋放。

### 10.2 現場程序（真實 gateway，含兩次砍線 + 4 筆寫入 + 60 秒閒置）

| 取樣點 | FD | thread | RSS (KB) |
|---|---|---|---|
| baseline | 18 | 8 | 51596 |
| 砍線後 | 18 | 8 | 51596 |
| 4 筆寫入後 | 18 | 8 | 51596 |
| 二次砍線後 | 18 | 8 | 51596 |
| 60 秒閒置後 | **18** | **8** | **51596** |

**一個數字都沒有變。**

---

## 11. 錯誤隔離

| 情境 | 結果 |
|---|---|
| UID3 持續 timeout 至熔斷 | UID1 **8/8** 輪詢成功、全程 ONLINE、狀態未被誤改 |
| 單一 adapter import 崩潰 | 只降級為 warning，`rtu`／`tcp` 等其餘 adapter 全部仍可載入；移除後 catalog 完全復原 |
| 非法 profile（count=0／fc 非整數／settings key 重複） | validator 全數攔截 |
| 現行 profile（relay_8ch_map、map2、temp_humid_map） | 全數通過 |

WebUI 的 `/api/catalog` 實機回傳 7 個 adapter、13 個 profile、`warnings: []`。

---

## 12. ONLINE / OFFLINE / Availability 狀態機

實測轉移（與 log 逐筆對照，無矛盾）：

```
UNKNOWN --(連續 2 次成功)--> ONLINE
ONLINE  --(連續 5 次失敗)--> OFFLINE + set_availability(False) + 慢速探測(offline_time)
OFFLINE --(連續 2 次成功)--> ONLINE  + set_availability(True)  + 恢復 poll_interval
```

MQTT 側：`connected → (socket 斷) → 自動重連 → connected + 重新訂閱`，30/30。

**沒有出現「log 說 OFFLINE 但 HA available=true」這類矛盾**：熔斷當下即發布 `offline`，恢復當下即發布 `online`，兩者都經 MQTT 實際捕獲。

---

## 13. Shutdown / Restart

`docker restart` 實測：

```
02:25:56 收到終止訊號，開始優雅關機
02:25:56 BusMaster 調度器已安全停止 / MQTT Consumer 任務已安全中止 / Health Monitor 任務已安全中止
02:25:56 ✅ 設備已卸載: UID=1 / UID=3      ← 各自發空 retained payload 清除 Discovery
02:25:56 發送最終 Gateway Offline 遺言 → 成功
02:25:56 Driver 已斷線
02:25:56 💤 系統已安全停止，記憶體/Task 已徹底清理
```

MQTT 側的 availability 序列與 Discovery 生命週期（以 `switch_0/config` 為例）：

```
02:25:53  payload_len=782   ← 訂閱當下收到的 retained 快照
02:25:56  payload_len=0     ← 關機清除（避免殭屍實體）
02:25:59  payload_len=782   ← 重啟後重送，恰好一次
02:25:56  3/status offline → py_1f/status offline
02:25:57  py_1f/status online
02:26:12  3/status online
```

**沒有重複 Discovery、沒有重複 poll task、沒有殘留。** 重啟後 FD 18／thread 7／RSS 51116KB，ERROR/CRITICAL/Traceback **0 行**，`RestartCount=0`。

---

## 14. FC01～FC16 Regression

| 功能碼 | 驗證方式 | 結果 |
|---|---|---|
| FC01 | 假設備輪詢 + 現場實機輪詢 + 8 bit 解碼 | PASS |
| FC02 | 假設備輪詢，離散輸入解碼 | PASS |
| FC03 | uint16 + uint32(大端) 同幀解碼 | PASS |
| FC04 | 輸入暫存器解碼 | PASS |
| FC05 | encode bytes + FC01 count=1 verify + 現場實機 ON/OFF | PASS |
| FC06 | encode bytes + FC03 verify | PASS |
| FC15 | 單筆群組寫入 + 整板 FC01 exact + 現場實機 `pattern_101` | PASS |
| FC16 | quantity=2 + 4-byte exact verify（strict_verify + codec） | PASS |
| ACK 守衛 | wrong uid/fc/addr/qty/len/short/exception 全數 fail-closed | PASS |

---

## 15. HA / MQTT 對外可觀測性

- 既有 discovery topic **一個都沒消失**
- 8 個 switch + connectivity 的 payload **byte-for-byte 相同**（unique_id、object_id、state_topic、command_topic、payload_on/off、state_on/off、device identifiers、availability 雙 topic、`availability_mode: all` 逐欄比對）
- 新增僅 profile 宣告的兩個 group `select`
- availability 走 retained 的設備 status topic
- `publish_state` 在 MQTT 失敗時**回傳 False**，非靜默成功
- 卸載時對 discovery topic 發空 retained payload

---

## 16. WebUI

| 項目 | 結果 |
|---|---|
| 未帶認證 | **401** |
| 錯密碼 | **401** |
| `/api/catalog` | 7 adapters、13 profiles、`warnings: []` |
| `/api/config` GET | 回傳 `yaml_content` 原文 |
| `/api/config` POST（原內容回存） | `{"status":"ok"}`，原子替換 + 自動 `.bak` |
| 存檔後型別 | 逐欄比對 **無任何 silent cast**（int/float/str 全部保持；`inter_frame_delay` 仍是 float 0.18） |
| 存檔後解析結果 | 與存檔前 `yaml.safe_load` **完全相等** |
| 唯一差異 | 檔尾換行被 `.strip()` 去掉（605 → 604 bytes），解析語意不變 |

存檔測試後我已把 `config.yaml` 還原為原始位元組（SHA-256 `c9ec5e0a…848c20a`）。

```text
WebUI Runtime Function:                 PASS
Frontend/Backend Type Difference:       TECHNICAL DEBT（僅檔尾換行正規化，無型別差異）
Type Difference counted as defect:      NO
```

---

## 17. 故障注入矩陣

| Fault | Expected | Actual | Observable | Result |
|---|---|---|---|---|
| Modbus timeout | 計失敗、不發布、重排 | 相同 | WARNING「設備無回應」+ ERROR 累計 | PASS |
| wrong UID | 拒絕 | `DataDecodeError` Slave ID 不符 | 計失敗 | PASS |
| wrong FC | 拒絕 | `DataDecodeError` FC 不符 | 計失敗 | PASS |
| bad CRC | 拒絕 | Deep CRC Radar 找不到合法幀 | 計失敗 | PASS |
| bad MBAP length | 拒絕 | `DataDecodeError` MBAP 長度不符 | 計失敗 | PASS |
| Modbus exception | 拒絕（write 回 False，read 拋錯） | 相同 | WARNING/ERROR | PASS |
| verify mismatch | retry 3 次後維持 ONLINE、零發布 | 相同 | WARNING「回讀值不符」 | PASS |
| socket reset | 重連並恢復 | 同秒重連 | WARNING「接收失敗（TCP 斷線）」 | PASS |
| device reboot | 重連並恢復 | 57 次重連全恢復 | WARNING + 重連成功 | PASS |
| MQTT disconnect | 自動重連 | 30/30 | `unexpected disconnect` WARNING | PASS |
| MQTT reconnect | 重訂閱且 command 可達 | 30/30 往返成功 | `connected` INFO | PASS |
| bad adapter | 降級 warning，不拖垮 | 相同 | AdapterLoader warning | PASS |
| bad profile | validator 攔截 | 3/3 攔截 | 💥 訊息 | PASS |
| **profile command_id 打錯** | **應攔截** | **放行且永遠靜默** | **無任何 log** | **FAIL（F1）** |
| queue pressure | 上限 200 | 400 → ≤200 | — | PASS |
| cancellation | 鎖釋放、可繼續 | 相同 | — | PASS |
| shutdown | task cancel、socket close、LWT、清除 discovery | 相同 | 完整關機日誌 | PASS |

---

## 18. 資料流黑洞最終裁決

```text
Southbound Black Hole:   NONE FOUND
Northbound Black Hole:   FOUND        ← F1：orphan sensor 永遠不解碼且零 log
Write-path Black Hole:   NONE FOUND
Poll-path Black Hole:    FOUND        ← F1（同一缺陷，發作於輪詢解碼路徑）
Reconnect Black Hole:    NONE FOUND
MQTT Black Hole:         NONE FOUND
Scheduler Black Hole:    NONE FOUND
```

---

## 19. 未涵蓋範圍（如實揭露）

- `local_serial_driver.py`（`type: usb`）、`listen_driver.py`／`listen_master.py`（`mode: listen`）本機**沒有對應硬體與設定**，本輪僅做靜態閱讀，**未實跑**。這兩條軌不在現行 `config.yaml` 的資料路徑上，但也因此不能宣稱它們已被本輪驗證。
- soak 為加速循環（120 + 50 + 30 次），非連續 24 小時長跑。
- 現場實機只做兩次砍線與四筆空載繼電器寫入；破壞性故障全部在假設備上進行。

---

## 20. 最終固定輸出

```text
SOUTHBOUND DATA FLOW:            PASS
NORTHBOUND DATA FLOW:            PASS
SILENT FAILURE:                  FOUND
DATA FLOW BLACK HOLE:            FOUND
CIRCUIT BREAKER:                 PASS
RECONNECT:                       PASS
LOCK SAFETY:                     PASS
QUEUE / SCHEDULER:               PASS
MEMORY LEAK:                     NONE
FD / SOCKET LEAK:                NONE
ASYNC TASK LEAK:                 NONE
FAULT ISOLATION:                 PASS
ONLINE/OFFLINE STATE MACHINE:    PASS
FC01 REGRESSION:                 PASS
FC02 REGRESSION:                 PASS
FC03 REGRESSION:                 PASS
FC04 REGRESSION:                 PASS
FC05 REGRESSION:                 PASS
FC06 REGRESSION:                 PASS
FC15 REGRESSION:                 PASS
FC16 REGRESSION:                 PASS
HA OBSERVABILITY:                PASS
MQTT OBSERVABILITY:              PASS
WEBUI RUNTIME:                   PASS
WEBUI TYPE DIFFERENCE:           TECHNICAL DEBT
WEBUI TYPE DIFFERENCE COUNTED AS DEFECT: NO
SOAK TEST:                       PASS
FINAL RESTORE / CLEANUP:         PASS
FINAL: FAIL
```

**判定理由**：除缺陷 F1 之外，本輪要求的每一項都取得正面證據 —— 八個功能碼的南北向完整流通、九類故障注入全部 fail loud、熔斷六個案例完全符合現行設計、TCP 與 MQTT 重連在隔離與現場都可靠且零資源殘留、鎖序單向且例外/取消後必釋放、佇列有界且不餓死、120+50+30 次循環後 FD/task/thread/RSS 全部持平、單一壞設備與壞外掛都被隔離、FC01～FC16 無退步、HA/MQTT 契約 byte-for-byte 一致、WebUI 功能正常且無 silent corruption、關機重啟乾淨無重複 Discovery。

但依第六節與第二十九節的硬性規則：**發現任何 silent failure 即 `DATA FLOW BLACK HOLE = FOUND`，FINAL = FAIL**。F1 是一個確實存在、可重現、零日誌的靜默資料消失路徑，因此最終判 **FAIL**。

需要強調的是：**F1 不是本輪任何改動造成的，也沒有在現行設定下發作**（兩份掛載中的 profile 都乾淨）。修正範圍是 `src/map_validator.py` 的三行交叉檢查，等待下一輪施工批准。

---

## 21. 本輪零修改證據

19 個 production 模組於驗收前後各算一次 SHA-256，**逐一相同**（harness 第 10 節 19/19 PASS）。`config.yaml` 在 WebUI 存檔測試後已還原為原始位元組。現場繼電器最終全 OFF、兩個群組 `all_off`、UID1/UID3 ONLINE、`RestartCount=0`、無 ERROR/CRITICAL/Traceback。

本輪只新增 `scratch/` 測試與本報告，**未修改任何 production code、profile 或 config**，未自行施工修復。
