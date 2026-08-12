# FC15 多 Relay 活組功能 — 敵對式獨立審查（Claude 紅隊）

- 審查日期：2026-08-12
- 審查對象：report/037 所述之「FC15 多 Relay 原子寫入與 Profile 活組」
- 審查者立場：紅隊，不採信 report/037 的任何結論、測試結果與 PASS 判定
- 本輪未修改任何 production code；新增檔案只有 `scratch/claude_fc15_adversarial_verify.py` 與本報告

---

## 人話結論（給非工程師看）

**先講最重要的一件事：這次沒有東西可以驗收，因為 FC15 根本還沒做。**

上一份報告（037）不是「施工完成報告」，是「可行性評估報告」。它自己也寫了「production FC15 尚未施工」。我實際去翻了程式碼，確認屬實：**現在對繼電器下「一次控制多顆」的指令，程式會直接拒絕，不會做任何事。** 所以本輪不存在「FC15 寫得好不好」的問題，只有「037 的評估可不可信、可不可以照著做」的問題。

依這個前提回答你的十二個問題：

1. **這次 FC15 是否真的一包寫多 Relay？** 目前不是，因為還沒實作。但 037 提出的做法方向正確，我獨立重算的封包與國際標準工具完全一致。
2. **有沒有偷用 FC05 loop？** **沒有。** 我實測程式對 FC15 的反應是「明確拒絕」，不是偷偷拆成 8 次單顆指令。這點是乾淨的。
3. **UID3 實機是否真的支援？** **只能說「設備有正常回應，但沒有被真正證明」。** 037 做的測試是「把 8 顆全關的狀態，再寫一次全關」——結果當然是「前後都是全關」。這種測法無法分辨「設備真的照做了」和「設備隨口答應但什麼都沒做」。這是 037 最大的證據弱點，它的結論寫得比證據強。
4. **兩顆 Relay 是否已實機用同一指令改變？** **沒有。從來沒有做過任何真正改變繼電器狀態的測試。**
5. **ACK 是否正確？** 設備回的確認訊息格式正確，我重算過。但**現行程式對這台設備（走原生 TCP）的寫入確認訊息，實際上是完全不檢查、照單全收的** ——這是既有的漏洞，不是 FC15 帶來的。
6. **FC01 是否完整回讀驗證？** 現行程式對繼電器只會回讀**一顆**，不是一組。要做群組就必須補這塊。
7. **原 FC01 / FC05 是否完全不受影響？** **完全沒受影響**，因為根本沒改動。我逐一比對過封包位元組，全部一致。
8. **Profile 活組是否合理？** 方向合理，但 037 給的範例規格**有一個會出人命等級的漏洞**（見下面第 9 點）。
9. **修改是不是最小？** 037 說「BusMaster 不用改」——**這個判斷是錯的，而且危險。** 現行程式裡有一條規則：「如果回讀不出數值，就當作寫入成功」。套到群組上會變成：**繼電器實際上跑到一個不該有的組合（例如 ATS 兩路同時導通），程式卻回報「寫入成功」。** 037 完全沒有發現這一點。
10. **有沒有過度設計？** 沒有。037 的範圍算克制。
11. **哪些地方需要修？** 見下方「最小修正清單」，共 4 項，其中第 1 項是安全性阻擋項。
12. **最終裁決？** **FAIL（作為「可據以施工的定案」）。**
    不是因為方向錯，而是因為：**(a) 缺一次真正改變狀態的實機證據就宣告設備支援；(b) 漏掉一個會把失敗誤報成成功的致命路徑。** 這兩點補完後可以進入施工。

> 一句話：**方向對，封包算得對，但「設備真的會動」沒被證明，而且有一個把失敗當成功的坑沒被發現。不可以照現況直接施工。**

---

## 1. 本輪的證據能力與限制（必須先說清楚）

### 1.1 無法執行 `git diff`

任務要求「先做 `git status --short` / `git diff --stat` / `git diff` 分辨 Codex 改了什麼」。

```
$ git status --short
fatal: not a git repository (or any of the parent directories): .git
```

**本機不是 git working tree。** 因此本輪**無法**以 diff 區分「本輪 FC15 production 修改 / 使用者既有 dirty state / scratch / report」。這是硬性證據缺口，我不會假裝做過。

替代方法（已執行）：

- 以 mtime 分辨異動集合；
- 以 production code 實際內容判定 FC15 是否存在（比 diff 更強的證據：不論誰改過，現況就是現況）。

`report/037` 自述可確認的同步來源 HEAD 為 `9f253d0c…`，但本機無 `.git`，**該值本輪無法驗證**，僅照錄。

### 1.2 mtime 異動範圍

`report/037_…md`（14:28）之後被修改的檔案只有 `CLAUDE.md`（本輪由我改寫）。`src/`、`adapters/`、`profile/` 全部停在 14:02，`scratch/test_uid3_fc15_probe.py` 為 037 本輪新增。

### 1.3 本機環境（`profile/config.yaml`）

| 項目 | 值 |
|---|---|
| node_id | `py_1f` |
| driver | `type: tcp`（原生 Modbus TCP／MBAP），`192.168.88.190:502` |
| inter_frame_delay | 0.18 |
| UID 1 | `temp_humid`，`adapter: tcp` |
| UID 3 | `relay_8ch`，`adapter: tcp`，`relay_8ch_map`，15s |

**本機沒有掛任何 Solis 設備**；UID 3 就是本案的 8 路繼電器，且走 `adapter: tcp`（不是 `rtu`）。

---

## 2. 第三節：Codex 到底改了什麼

### 結論：本輪 **production 修改量為零**

| 類別 | 內容 | 判定 |
|---|---|---|
| FC15 production 修改 | **無** | — |
| 既有 dirty state | 無法用 diff 判定（非 git repo） | 證據缺口 |
| scratch | `scratch/test_uid3_fc15_probe.py`（211 行，唯讀 + 一次 same-state write） | 合規（限於 scratch） |
| report | `report/037_…md` | 合規 |

`report/037` 是**評估報告**，不是施工報告。它自己在第 6 節寫「建議的最小正式施工」、在第 7 節寫「Production FC15 尚未施工」。**因此任務書中「Codex 本輪 FC15 production 修改」的前提不成立。**

### 逐檔說明

- `adapters/generic_adapter.py` — **未改**。FC15 仍拋 `NotImplementedError`。
- `adapters/modbus_tcp_adapter.py` — **未改**。FC15 仍拋 `NotImplementedError`。
- `src/driver.py` / `src/bus_master.py` / `src/ha_manager.py` / `src/map_validator.py` — **未改**。
- `profile/relay_8ch_map.yaml` — **未改**，無 `coil_groups`，8 個 setting 全部 `write_fc: 5`。
- `profile/relay_8ch_map2.yaml` — **不存在**（037 明說「未建立正式 map2」）。

→ **能否再縮小？不適用（修改量已為零）。**

---

## 3. 第六節：FC15 是不是 FC05 Loop —— 實測

這是硬性 FAIL 條件，我直接對 production adapter 注入 `write_fc: 15` 的 setting 實測：

```
[PASS] RTU generic_adapter: write_fc:15 拋 NotImplementedError    尚不支援功能碼 FC 15 組裝
[PASS] TCP modbus_tcp_adapter: write_fc:15 拋 NotImplementedError  TCP 尚不支援 FC 15 組裝
```

原始碼佐證 —— `adapters/generic_adapter.py::encode_write()` 只有 `fc == 6`／`fc == 5`／`fc == 16` 三個分支，其餘 `raise NotImplementedError`；`adapters/modbus_tcp_adapter.py::encode_write()` 只有 `fc == 6`／`fc == 5`。

**判定：沒有 FC05 loop 冒充 FC15。** 但原因是「FC15 完全不存在」，不是「FC15 實作得很乾淨」。

---

## 4. 第七／八節：Bit Packing 與 PyModbus Oracle（獨立重算）

我**未沿用** `scratch/test_uid3_fc15_probe.py` 的 packer 與 CRC，而是依 Modbus 規範在 `scratch/claude_fc15_adversarial_verify.py` 內重寫，再與隔離安裝的 **PyModbus 3.13.0** 對撞。

### 4.1 Bit packing（題目指定四組）

| 輸入 | 期望 | 實得 | 判定 |
|---|---|---|---|
| `[ON, ON]` | `0x03` | `0x03` | PASS |
| `[ON, OFF, ON]` | `0x05` | `0x05` | PASS |
| `[ON, ON, OFF, ON]` | `0x0B` | `0x0B` | PASS |
| `[ON,OFF,ON,OFF,ON,OFF,ON,OFF]` | `0x55` | `0x55` | PASS |

補充敵對向量：

- 3 coils 全 ON → `0x07`，**未使用高位為 0** — PASS
- 9 coils（僅第 9 顆 ON）→ `00 01`，跨 byte 正確 — PASS
- 反向偵測：若誤用 MSB-first，第四組會得 `0xAA` 而非 `0x55` —— 兩者可區分，確認本實作為 **LSB-first** — PASS

### 4.2 PyModbus PDU / RTU frame 對撞

```
[PASS] PDU 2 coils start=0   mine=0f 00 00 00 02 01 03   oracle=0f 00 00 00 02 01 03
[PASS] PDU 3 coils start=2   mine=0f 00 02 00 03 01 05   oracle=0f 00 02 00 03 01 05
[PASS] PDU 4 coils start=4   mine=0f 00 04 00 04 01 0b   oracle=0f 00 04 00 04 01 0b
[PASS] PDU 8 coils start=0   mine=0f 00 00 00 08 01 55   oracle=0f 00 00 00 08 01 55
[PASS] RTU 2 coils  03 0f 00 00 00 02 01 03 1f 4f
[PASS] RTU 3 coils  03 0f 00 02 00 03 01 05 b7 4d
[PASS] RTU 4 coils  03 0f 00 04 00 04 01 0b 0f 48
[PASS] RTU 8 coils  03 0f 00 00 00 08 01 55 bf 73
```

CRC 由本檔獨立重算（poly `0xA001`、init `0xFFFF`、wire order little-endian），非引用 037 的值。

**判定：PyModbus Oracle 一致 — PASS。**（注意：這證明的是「037 提出的目標封包格式正確」，不是「production 產生了這些封包」——production 產不出來。）

---

## 5. 第二十一節：037 實機 raw log 獨立重算

我不引用 037 算好的任何數值，全部自行重建：

| 項目 | 037 記載 | 我獨立重算 | 判定 |
|---|---|---|---|
| FC15 MBAP TX | `FC 15 00 00 00 08 03 0F 00 00 00 08 01 00` | 相同 | PASS |
| 等價 RTU + CRC | `03 0F 00 00 00 08 01 00 7F 4C` | 相同 | PASS |
| ACK protocol id | 0 | 0 | PASS |
| ACK MBAP length | 6 | 實際載荷 6 | PASS |
| ACK UID / FC | 03 / 0x0F | 03 / 0x0F | PASS |
| ACK start / quantity | 0 / 8，共 12 bytes | 相同，且**不含 byte_count/data** | PASS |
| FC01 before | 8 路全 OFF | `01 00` → `[0]*8` | PASS |
| FC01 after | 8 路全 OFF | `01 00` → `[0]*8` | PASS |

ACK 契約正確：FC15 正常回應只 echo start + quantity，**不要求設備回傳 byte_count / packed data**。037 對此描述正確。

### 5.1 ⚠️ 但這組數據證明力嚴重不足

`before == target == after == 全 OFF`。

**「設備確實把 8 顆 coil 寫成 OFF」與「設備回了 ACK 但什麼都沒做」，在這組數據下完全無法區分。**

這正是本 repo `Solis_Inverter_Modbus_Dev_Notes.md` 記載的既有教訓：寫入未實作的暫存器會**被靜默丟棄卻仍回正常 ACK**。同一個懷疑必須套用在 FC15 上。

可採信的部分：設備回的是**正常 FC15 ACK**，不是 `0x8F` illegal-function exception。若設備完全不認得 FC15，標準行為應回 exception code 01。所以「設備接受 FC15 這個功能碼」是**成立的**。

不可採信的部分：「設備會依 FC15 內容實際驅動 coil」**未被證明**。

→ 037 人話結論寫「結果為 **DEVICE FC15 SUPPORTED**」，**超出其證據強度**。正確表述應為：「FC15 功能碼被接受且 ACK 合法；實際寫入效力 UNPROVEN」。

---

## 6. 第十二／十七節：BusMaster 契約敵對檢查 —— 發現 037 遺漏的致命路徑

### 6.1 bus_lock 原子性：037 描述正確但不完整

`src/bus_master.py::_process_write()` 的臨界區確實是：

```
async with self.bus_lock:
    Write-TX → driver.write(ACK) → Verify-TX → driver.read(Verify-RX)
```

`decode` 與比對在鎖外 —— 不影響總線獨佔，可接受。

**但 037 沒有講的是：`for attempt in range(...)` 迴圈在 `async with self.bus_lock` 的外層。**

```
[PASS] retry 迴圈在 bus_lock 外層 → 兩次嘗試之間可被其他 poll/write 插入
```

→ 原子性**僅限單次嘗試**，不涵蓋整個 retry 週期。第 1 次嘗試失敗後、第 2 次嘗試前，本 Gateway 自己的 poll 或其他 write 可以插進來。對群組寫入而言，這代表「群組處於半途狀態時，中間可能夾雜其他總線操作」。

037 的表述「FC15 寫入與後續驗證可屬於同一個受 bus_lock 保護的操作循環」在**單次嘗試**層級正確，但未揭露 retry 邊界。應補上這句限定。

### 6.2 🚨 致命發現：`decoded[key] is None` 會被判定為寫入成功

`src/bus_master.py::_process_write()`：

```python
if decoded.get(key) is None:
    logger.warning(f"[{uid}] 寫入驗證回讀 key={key} 值為 None (可能不支援該資料型別)，無法比對，視為成功")
    ha_mgr.publish_state(decoded)
    self._record_success(uid)
    return
```

這條後門原本是給「adapter 因 datatype 不支援而解不出值」用的。

**套到 037 提出的群組設計上，會產生 fail-open：**

037 的設計是「adapter 從 group slice **反查命名狀態**供 BusMaster 比對」。反查天然存在「查無此組合」的情況 —— 例如群組目標是 `path_a = [ON, OFF]`，但實際寫入後回讀是 `[ON, ON]`（未定義／禁止的組合）。

自然的 adapter 實作會對「查不到對應命名狀態」回傳 `None` → **BusMaster 直接判定寫入成功、發布 state、記 success**。

**後果：ATS 兩路同時導通這種最危險的狀態，會被系統回報為「寫入驗證成功」。**

037 第 6 節寫「`src/bus_master.py = NO CHANGE`：既有 lock、retry、write budget 與 publish flow 足夠」。**此判斷在未加額外約束下是錯誤且不安全的。**

### 6.3 `_values_equal` 是純量比較，不是 bit vector 比較

```python
def _values_equal(a, b, tolerance: float = 0.01) -> bool:
    if type(a) == type(b) and isinstance(a, (int, str)):
        return a == b
    ...
```

BusMaster 只做**單一純量**比對 `_values_equal(decoded.get(key), value)`。第十五節要求的「全部目標 bits 逐一比對」**不會由 BusMaster 執行**，必須完全由 adapter 的反查邏輯保證。

→ 這代表「多 bit verify 的正確性」是一個**單點依賴**：只要 adapter 反查稍微寬鬆（例如部分匹配、就近匹配），就會產生 partial verify 假 PASS，而 BusMaster 沒有任何第二道防線。037 未指出此風險集中點。

---

## 7. 第十四節：FC01 Verify 方案裁決

### 現況實測

```
[PASS] RTU build_verify_read(fc15) 走 FC01                    read_fc=0x01
[PASS] RTU fc15 verify 只讀 1 個 coil（無群組 multi-bit verify 能力）  quantity=1
```

`build_verify_read()` 確實已把 `write_fc in (5, 15)` 對映到 FC01，但 `reg_count = int(setting.get("verify_count", 1))` 預設為 **1**。群組需要 `quantity == count`。

### 方案 A（只讀 group range） vs 方案 B（沿用整板 FC01 start=0 count=8）

實測整板路徑可直接共用既有 decoder：

```
[PASS] FC01 poll 仍為 start=0 count=8
[PASS] FC01 一次 decode 出 switch_0..7（可共用作群組 verify）
       got={'switch_0':'ON','switch_1':'OFF','switch_2':'ON','switch_3':'OFF',
            'switch_4':'OFF','switch_5':'ON','switch_6':'OFF','switch_7':'ON'}
```

（以 data byte `0xA5` 注入驗證，8 路 bit 展開全部正確。）

**裁決：採方案 B。** 理由：

1. 既有 `bits:` decoder 已能一次解出 `switch_0..switch_7`，**不需要寫第二套 bit decoder**（符合第十四節要求）；
2. 一次 FC01 即可同步 HA 上原本 8 個 switch 實體，**不需要 `publish_fc15_states()`**（符合第十六節要求）；
3. 修改面更小。

037 的方案 B 選擇正確。

---

## 8. 第九／十／十八節：Profile 活組評估

### 9.1 是否真的 Generic — 通過（但僅止於紙上）

037 提出的 `coil_groups` schema 以 `start_addr` + `count` + `members` 表達，能表示 `01`、`234`、`4567`、`0~7`，以及 `01 / 234 / 567` 等任意合法連續群組，**未把群組寫死在 Python**。四種配置範例逐一檢查無 hard-code。

⚠️ 但這只是報告中的 YAML 範例，**尚未有任何 production code 消費它，也未有 validator 認識它**。「活組」目前是設計意圖，不是已驗證的能力。

### 9.2 非連續群組 — 037 立場正確

037 明確拒絕把 `0,3,7` 展開成 start=0/quantity=8 的隱性 read-modify-write。這是正確的，理由與第十節一致：RMW 會改動 command 未指定的 coils。

**最小且合理的攔截層裁決：`map_validator.py`。**

理由：validator 是**啟動時**檢查，失敗只隔離單台設備（skip + log），符合 repo 既有的 fail-loud 姿態；若放到 command building 才拒絕，錯誤要等到使用者按下按鈕才浮現，且已經進入 write 路徑。放 profile loading 與放 validator 實質同層，但 validator 是既有機制，無需新增管道。

### 9.3 Overlap 群組 — 建議禁止（同意 037，但理由更強）

037 建議 validator 禁止 overlap。我同意，且理由比 037 更強：

除了 037 列出的 race / command collision / HA state ambiguity 之外，**overlap 會直接放大 6.2 節的 fail-open 缺陷**：group A 與 group B 重疊時，B 的寫入會把 A 推入未命名組合 → A 的後續反查回 `None` → 被判成功。禁止 overlap 能顯著降低 `None` 出現機率，但**不能取代 6.2 的修正**（單一群組被外部 master 或硬體卡住時仍會落入未命名組合）。

### 9.4 ATS 語意 — 037 立場正確

037 主張 ATS 的「禁止 `[ON, ON]`」由 profile 的 `states` 白名單表達，`GenericAdapter` 只理解 multiple coils，不寫入 ATS 安全策略。符合第十一節要求。

⚠️ 但必須強調：**白名單只限制「使用者能下什麼指令」，不限制「硬體實際會落在什麼狀態」。** ATS 的電氣互鎖必須由硬體保證 —— 037 有寫這句，正確且必須保留。

---

## 9. 第二十節：對外可觀測性 Regression（實測）

現況 production 未改動，故 regression 應為全等。實測確認：

### FC05 encoder（RTU）

| key / value | 封包 | CRC |
|---|---|---|
| `switch_0=ON` | `03 05 00 00 FF 00` | `8D D8` ✅ |
| `switch_0=OFF` | `03 05 00 00 00 00` | `CC 28` ✅ |
| `switch_7=ON` | `03 05 00 07 FF 00` | `3C 19` ✅ |
| `switch_7=OFF` | `03 05 00 07 00 00` | `7D E9` ✅ |

CRC 全部由本檔獨立重算比對。

### FC05 encoder（TCP／MBAP，本機實際路徑）

```
[PASS] FC05 TCP switch_0=ON   00 01 00 00 00 06 03 05 00 00 FF 00
[PASS] FC05 TCP switch_0=OFF  00 02 00 00 00 06 03 05 00 00 00 00
[PASS] FC05 TCP switch_7=ON   00 03 00 00 00 06 03 05 00 07 FF 00
[PASS] FC05 TCP switch_7=OFF  00 04 00 00 00 06 03 05 00 07 00 00
```

PDU 與 RTU 版逐位元組一致，僅換 MBAP 框架，protocol id = 0，UID = 3。

### FC01 / FC06 / FC16 / HA

- FC01 poll 仍為 `start=0 count=8` — PASS
- FC01 整板 decode 出 `switch_0..switch_7` — PASS
- FC06 / FC16 未觸及（`encode_write` 分支未改）— PASS
- HA entity id / command topic / state topic / payload ON-OFF / availability — profile 與 `ha_manager.py` 均未改 — PASS

**判定：對外可觀測性零變動 — PASS**（因為修改量為零，此項為 trivially true）。

---

## 10. 第二十二節：真正狀態改變的實機測試 —— **未執行，標記 UNPROVEN**

**本輪未進行任何會改變繼電器狀態的實機操作。** 理由：

1. **未取得授權。** 使用者本輪未授權對 UID 3 做狀態改變測試；第二十二節本身的前提是「如果使用者已授權，而且設備處於安全空載／測試條件」。
2. **不知道 8 路繼電器接了什麼。** `relay_8ch_map.yaml` 只有 `繼電器 CH1`～`CH8` 這種通用名稱，無負載資訊。在不知負載的情況下切換現場繼電器是不可逆的外部動作。
3. **FC15 在 production 不存在**，任何 FC15 實機測試都只能由 scratch 腳本自行對總線送幀 —— 那等同於在生產網關旁另開一個 master。
4. 現況 `ginlong` 正在運行並持續輪詢 `.190:502`（`ss` 確認只有它連該位址，`py_5f` 走 `.191`）。要做單 master 測試必須先停容器，這會中斷現場。

已執行的**非侵入式**確認（不碰總線，只讀既有日誌與 socket 狀態）：

```
UID=3 adapter=tcp 已掛載，Discovery 已送出
bus_master: [3] 連續通訊成功，恢復 ONLINE
容器 RestartCount = 0，Up 24 分鐘
ss: 只有 ginlong (pid 19298) 連 192.168.88.190:502
```

→ **實機 FC15（真實狀態改變）：UNPROVEN。**
→ 037 的 same-state probe：只能支撐「FC15 功能碼被接受、ACK 合法」，**不能支撐「設備會依內容驅動 coil」**。

---

## 11. 第二十四節：對 037 的十二項錯誤假設挑戰

| # | 挑戰項 | 判定 |
|---|---|---|
| 1 | 是否把 FC15 說成多 register？ | **否。** 037 全程正確使用 coils，PDU 格式正確 |
| 2 | 是否其實用 FC05 loop？ | **否。** 實測 production 對 FC15 直接拒絕，無 loop 路徑 |
| 3 | 是否只驗 ACK 沒驗 coils？ | **否。** 037 有做 FC01 before/after。但 before==after==全 OFF，證明力不足 |
| 4 | 是否只驗第一個 bit？ | **否**（037 驗了 8 bit）。**但**現行 `build_verify_read` 對 fc15 只讀 1 coil，且 BusMaster 只做純量比對 —— 未來實作若照抄現況就會變成只驗一顆。**這是實作階段的高風險點** |
| 5 | 是否群組被寫死？ | **否。** schema 由 profile 表達，四種配置皆可 |
| 6 | 是否偷偷 RMW？ | **否。** 037 明確拒絕非連續群組的隱性 RMW |
| 7 | 是否為了 FC15 大改 Driver？ | **否，且 037 判斷精準** —— 只動 `modbus_tcp_driver.py` 子類，`driver.py` RTU 守衛 NO CHANGE。我獨立確認 MBAP 封包因無 CRC 必然落回 `return True` 盲收，037 對此描述正確 |
| 8 | 是否 BusMaster 被不必要重構？ | **否，但 037 走向另一個錯誤** —— 它宣告 BusMaster 完全不用改，而未發現 `None → 視為成功` 的 fail-open（見 6.2）|
| 9 | 是否 HA entity/topic 改變？ | **否。** 037 主張沿用既有 `select` 與 `/set/<key>`，正確 |
| 10 | 是否 PyModbus 被加成 dependency？ | **否。** 隔離於 `/tmp/fc15-pymodbus-lib`，`requirements.txt` 無此項，容器內 `import pymodbus` 失敗（已實測）|
| 11 | 是否實機測試數據不足卻判 PASS？ | **是 —— 這是 037 最主要的問題。** 見第 5.1 節 |
| 12 | 是否「同一 FC15 frame」被誇大成機械接點完全同步？ | **否。** 037 明確寫「不能證明機械接點在同一微秒動作，ATS 仍需硬體互鎖」。這點誠實且正確 |

---

## 12. 最小修正清單

### 修正 1 🚨（阻擋項）— 群組 verify 不得回傳 `None`

**問題**
`src/bus_master.py::_process_write()`：`if decoded.get(key) is None:` → 記 warning、`publish_state`、`_record_success`、`return`。

**為什麼錯**
037 提出的群組設計要求 adapter 「從 group slice 反查命名狀態」。反查必然存在「查無此組合」的情況。若 adapter 對此回 `None`，一次**實際失敗**（coils 落在未命名／禁止組合，例如 ATS 雙路導通）會被 BusMaster 判定為**寫入成功**並發布。這是 fail-open，與 repo「寫入 ACK 什麼都不能證明、所以要回讀驗證」的既有安全姿態直接矛盾。

**最小修正**
不改 `bus_master.py`。在 adapter 的群組 verify decode 路徑中：反查失敗時**必須回傳一個不會等於任何合法群組狀態的字串哨兵**（例如 `"__UNMATCHED__"`），**絕不回傳 `None`**。`_values_equal` 對 `str` 走 `a == b`，必然為 `False` → 正常進入既有 retry / 失敗路徑。

**不該動的地方**
`src/bus_master.py` = **NO CHANGE**（此哨兵約定使 037 的 `BusMaster NO CHANGE` 判斷得以成立，但這是**條件成立**，不是無條件成立，必須寫進 adapter 契約文件）。
`src/driver.py`、`src/ha_manager.py` = NO CHANGE。

**修後驗證**
scratch 向量：群組 `[ON, OFF]` 目標，注入回讀 `[ON, ON]` → adapter 回 `"__UNMATCHED__"` → `_values_equal("__UNMATCHED__", "path_a")` 必須為 `False`。並斷言 adapter 在任何輸入下對群組 key **永不回 `None`**。

---

### 修正 2（阻擋項）— 補一次真正改變狀態的實機測試

**問題**
037 只做 same-state（全 OFF → 全 OFF）write，卻在人話結論寫 `DEVICE FC15 SUPPORTED`。

**為什麼錯**
`Solis_Inverter_Modbus_Dev_Notes.md` 已記載本 repo 的既有教訓：不支援的寫入會被靜默丟棄卻仍回正常 ACK。same-state write 無法區分「照做」與「ACK 後丟棄」。

**最小修正**
不改任何 code。在**取得使用者明確授權**、確認目標 coil 為安全空載、且已停止 `ginlong` 確保單 master 後，對一個已知安全的 2-coil 群組執行一次真實切換（例如 `[OFF, OFF] → [ON, ON]`），完整記錄：command timestamp、FC15 TX、ACK RX、FC01 verify TX/RX、最終 8 coil states、操作耗時。必須確認**兩顆 coil 在同一筆 FC15 request 中改變**。測後復原原狀並重啟容器。

在此之前，037 的結論必須降級表述為：「FC15 功能碼被接受且 ACK 合法；寫入效力 UNPROVEN」。

**不該動的地方**
全部 production = NO CHANGE。測試腳本只放 `scratch/`。

---

### 修正 3 — 群組 verify 必須讀滿 `count`，且必須逐 bit 比對

**問題**
`build_verify_read()` 的 `reg_count = int(setting.get("verify_count", 1))` 預設為 1；實測 `write_fc: 15` 的 verify request `quantity == 1`。BusMaster 只做純量比對，不會逐 bit 檢查。

**為什麼錯**
第十五節要求全部目標 bits 相符才 PASS。若實作階段沿用現況，會退化成「只驗第一顆」的 partial verify 假 PASS。

**最小修正**
群組走**方案 B**：verify 沿用既有整板 `FC01 start=0 count=8`（`verify_command_id: read_coils`），共用既有 `bits:` decoder。adapter 在 decode 後對 group slice **逐 bit 與目標向量比對**，全等才回傳命名狀態，任一 bit 不符即回修正 1 的哨兵。

**不該動的地方**
`src/bus_master.py`、`src/ha_manager.py` = NO CHANGE；不得新增第二套 bit decoder；不得新增 `publish_fc15_states()`。

**修後驗證**
scratch 向量：群組 `0~3` 目標 `[ON, OFF, ON, ON]`，逐一注入 4 種單 bit 不符的回讀，全部必須判 FAIL；完全相符才判 PASS。

---

### 修正 4 — FC15 必須同時實作在 `modbus_tcp_adapter.py`

**問題**
`adapters/modbus_tcp_adapter.py` **自行重寫**了 `encode_write()` 與 `build_verify_read()`，並未繼承 `generic_adapter` 的完整實作（它甚至沒有 RTU 版的 FC16 32-bit codec 路徑）。

**為什麼錯**
本機 UID 3（`relay_8ch`）的 `adapter: tcp`。**只改 `generic_adapter.py` 對本機完全無效。** 037 有指出這點（第 2 節），此處確認並升級為施工必要條件。

**最小修正**
把 coil packer 與 FC15 PDU 建構抽成 `generic_adapter` 的共用函式，`modbus_tcp_adapter` 只負責套 MBAP —— 與現有 FC05/FC06 的分工方式一致，不要複製第二份 packer。

**附帶（不列入本輪阻擋項）**
`modbus_tcp_driver.py` 的原生 TCP 寫入 ACK 目前**完全未驗證**（MBAP 無 CRC → `driver.py::write()` 守衛必然 `return True` 盲收）。這是**既有缺陷，非 FC15 引入**。037 建議在 `modbus_tcp_driver.py` 加嚴格 MBAP ACK 驗證，方向正確，但應**另案處理**，不要與 FC15 綁在同一輪 —— 它會影響 UID 1 與 UID 3 現有的所有 FC05/FC06 寫入路徑，風險面遠大於 FC15 本身。

**不該動的地方**
`src/driver.py` 的 RTU 守衛 = **NO CHANGE**（037 判斷正確）。
`src/map_validator.py` = 本輪 NO CHANGE，`coil_groups` schema 驗證（連續性、count/state 長度、bool token、禁止 overlap、禁止非連續）列為施工輪的一部分。

---

## 13. 第二十八節：逐項裁決

| 項目 | 判定 | 說明 |
|---|---|---|
| FC15 encoder | **FAIL** | production 不存在（RTU/TCP 皆 `NotImplementedError`）。037 的**設計**封包經 PyModbus oracle 驗證正確，但那不是 encoder 實作 |
| FC15 ACK | **PASS（契約）／FAIL（production 防線）** | ACK 契約描述正確（只 echo start+quantity）；但原生 TCP 路徑在 driver 層盲收，未驗證 |
| FC01 multi-bit verify | **FAIL** | 現況 `write_fc:15` 的 verify quantity=1，且 BusMaster 只做純量比對，無 multi-bit 能力 |
| BusMaster 原子操作循環 | **PASS WITH CAVEAT** | write+ACK+verify 確在同一 `bus_lock`；但 retry 迴圈在鎖外，原子性僅限單次嘗試。037 未揭露此邊界 |
| Profile 活組 | **PASS（設計）／FAIL（實作）** | schema 非 hard-code、可表達各種合法連續群組；但無 code 消費、無 validator 認識 |
| FC05 Regression | **PASS** | RTU 與 TCP 封包逐位元組一致，CRC 獨立重算通過 |
| HA 對外可觀測性一致 | **PASS** | entity / topic / payload / availability 零變動 |
| PyModbus Oracle | **PASS** | 2/3/4/8 coils 的 PDU 與 RTU frame 全部 byte-for-byte 一致；未成為 production 依賴（容器內 import 失敗，已實測）|
| 實機 FC15 | **UNPROVEN** | 只有 same-state write；從未做過真正改變狀態的測試 |
| 修改是否最小 | **YES（trivially，修改量為零）** | 037 提出的施工範圍亦克制，無過度設計 |

**建議：最小修正後重驗。**

**最終裁決：FAIL**

---

## 14. 裁決理由與定位說明

必須精確理解這個 FAIL 的意義：

**這不是「Codex 把 FC15 做壞了」**——它根本沒做，而且沒有偷用 FC05 loop、沒有動 Driver、沒有動 BusMaster、沒有改 HA、沒有把 PyModbus 塞進 production。就「不破壞現有系統」而言，037 是乾淨的。

**FAIL 的依據是任務書第二十七節的硬性條件**，其中兩條未成立：

1. **「實機數據支持設備真的 FC15 supported」— 不成立。** same-state write 無法區分「照做」與「ACK 後丟棄」，而本 repo 自己的硬體筆記正好記載了後者確實會發生。037 卻在人話結論寫下 `DEVICE FC15 SUPPORTED`，**結論強度超出證據**。
2. **「FC01 multi-bit verify 正確」— 不成立。** production 無此能力，且 037 宣告的「BusMaster NO CHANGE」在未加 adapter 哨兵約定的前提下會導致 `None → 視為成功` 的 fail-open。**這是 037 完全未發現的安全缺陷**，且正好落在 ATS 這種「錯誤組合有實體後果」的應用上。

若把 037 重新定位為「可行性評估」而非「可據以施工的定案」，它的裁決 `PASS WITH CHANGES` 是合理的。但依本輪任務書的驗收標準，**現況不可進入施工**。

補完修正 1 與修正 2 後，本案可升級為 **PASS WITH CHANGES** 並進入施工輪。

---

## 15. 限制與未測項（誠實揭露）

- **無法執行 `git diff`** —— 本機非 git repo。「誰改了什麼」僅以 mtime 與 code 現況推定。037 引用的 HEAD `9f253d0c…` 本輪無法驗證。
- **未做任何實機狀態改變測試** —— 未獲授權、負載未知。實機 FC15 效力標記 UNPROVEN。
- **未對總線送出任何幀** —— 本輪所有實機資訊來自既有容器日誌與 `ss`，未新增 master。
- **未驗證 037 的 probe 腳本執行過程** —— 只重算其報告記載的 raw log。若該 log 本身失真，本輪無法察覺。
- **037 引用的 PyModbus 文件連結未逐一開啟核對** —— 改以本機隔離安裝的 PyModbus 3.13.0 實際執行結果作為 oracle。
- **修正 1 的哨兵方案未經實作驗證** —— 為靜態分析導出的最小修正，需在施工輪以 scratch 向量證明。
- **未評估外部 master 競爭** —— 現場 `.190` 目前只有 `ginlong`，但無法排除未來加入其他 master。

---

## 附錄：本輪驗證腳本

`scratch/claude_fc15_adversarial_verify.py` —— 45 項檢查全數通過（TOTAL 45 / PASS 45 / FAIL 0）。

執行方式（host，PyModbus 走隔離路徑，不碰 production）：

```bash
cd /root/py_1f && python3 scratch/claude_fc15_adversarial_verify.py
```

涵蓋：bit packing（2/3/4/8 coils、高位補 0、跨 byte、LSB/MSB 可區分）、PyModbus PDU/RTU oracle 對撞、CRC 獨立重算、037 raw log 重建、FC15 ACK 契約、production FC15 缺席證明、FC05 RTU/TCP encoder regression、FC01 整板 decode、BusMaster 契約靜態分析。

腳本**不對總線送出任何幀**，全部為本機純計算與 production 模組唯讀呼叫。
