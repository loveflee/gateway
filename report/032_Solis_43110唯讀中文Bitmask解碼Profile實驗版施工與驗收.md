# Solis 43110 唯讀中文 Bitmask 解碼 Profile 實驗版施工與驗收

**日期：** 2026-08-11

## 人話結論

1. **本輪採 Profile-only，沒有新增專用 Adapter。** 現有 GenericAdapter 已可把一個 16-bit sensor 拆成多個 bit payload，HA Manager 也能把它們發成唯讀 binary sensor。
2. **原 `solis_inverter_map.yaml` 完全沒有修改。** map2 先由原圖複製，再只改新檔 `solis_inverter_map2.yaml`。
3. **43110 現在可看到中文 raw 與 bit 資訊。** map2 提供「儲能控制原始值」及 16 個中文唯讀 bit 診斷；bits 12–15 固定以「未知 Bit N」呈現。
4. **raw 1／33／35 均已正確逐 bit 拆解。** 結果分別為 `{0}`、`{0,5}`、`{0,1,5}`，不會再把 33 或 35 偽裝成單一模式 enum。
5. **未知 bit 不會消失。** `0x2001` 被驗證為 bit 0 與「未知 Bit 13」同時啟用。
6. **沒有新增任何 43110 寫入能力。** 實驗 map2 已移除 43110 的 setting 與 number entity；純編碼測試確認 `encode_write("set_storage_control", 1)` 會拒絕。
7. **map validator：PASS。**
8. **沒有實機 read-only 觀察。** 未改 `profile/config.yaml`、未 restart，因此沒有把任何正式 UID 切至 map2；所有驗證皆為 YAML、純記憶體 decode 與假 MQTT Discovery 組裝。
9. **結果：PASS WITH LIMITATIONS。** map2 的唯讀解碼與 HA Discovery 已驗證，但尚未在正式設備載入／觀察；bit 名稱依上游模型，並非本機 S6-EH1P6K firmware 的寫入認證。

---

## 1. 施工前基線與工作清單

### 基線

- Git HEAD：`0e4edef Add Solis map experiments and observations`
- 開工前已存在、且未碰觸的未追蹤檔：`report/031_Solis_43110儲能控制Bit定義與安全寫入敵對式審查.md`
- 穩定基準：`profile/solis_inverter_map.yaml`
- 031 的結論：43110 為 bitmask；現況整 word 寫入有風險；本輪只做讀取診斷，不施工寫入。

### 已確認的現有能力與架構

| 項目 | 實際證據 | 對本輪的結論 |
| --- | --- | --- |
| GenericAdapter bit decode | [generic_adapter.py:407](/root/py_ginlong/adapters/generic_adapter.py:407) 讀 1／2-byte value，逐一依 `bits[].bit` 產出 `bits[].id` 的 on/off payload。 | profile 可拆出 16 個 bit，不必修改 Adapter。 |
| HA Discovery | [ha_manager.py:151](/root/py_ginlong/src/ha_manager.py:151) 讀 `B3_STATUS_BITS`；[311 行](/root/py_ginlong/src/ha_manager.py:311) 的 binary_sensor payload 沒有 command topic。 | 可用現成 B3 產出唯讀中文 bit entity。 |
| profile 掛載 | [main.py:224](/root/py_ginlong/src/main.py:224) 從 `/app/profile/<name>.yaml` 載入；Docker 將 host `profile/` 掛載到 `/app/profile`。 | map2 可靜態驗證；不改 config 就不會影響現場。 |
| Adapter discovery | [main.py:122](/root/py_ginlong/src/main.py:122) 僅掃描頂層 `*_adapter.py`。 | 不需要也沒有新增 Adapter。 |
| 031 bit 定義限制 | [031 報告](/root/py_ginlong/report/031_Solis_43110儲能控制Bit定義與安全寫入敵對式審查.md:80) | 只用上游模型名稱做**唯讀診斷**；高位 bit 不命名。 |

### 施工清單與實際結果

| 預定項目 | 實際結果 |
| --- | --- |
| 先複製穩定圖為 map2 | 已執行 `cp profile/solis_inverter_map.yaml profile/solis_inverter_map2.yaml`；複製後兩檔 SHA-256 相同。 |
| 新增 profile-only bit decode | 已完成：一個 `storage_control_bits` backend sensor，bits 0–15。 |
| 保留 raw 值 | 已完成：`set_storage_control` 仍以 FC03 讀取，並以「儲能控制原始值」發佈為唯讀 sensor。 |
| 讓 bit 在 HA 可見 | 已完成：16 個 B3 binary_sensor，所有新增名稱／payload 均為繁體中文。 |
| map2 的 43110 改為唯讀 | 已完成：移除 43110 `settings` entry 與原 number entity。 |
| 單一合併文字 sensor | 未做：現有 profile schema／GenericAdapter 只能產出 individual payload key；以 16 個 bit entity 保留完整資訊，避免新增 Adapter。 |
| 新專用 Adapter、Driver、BusMaster、GenericAdapter、validator | 全部未修改。 |

## 2. 實際 profile 變更

只新增 [solis_inverter_map2.yaml](/root/py_ginlong/profile/solis_inverter_map2.yaml)，原圖未改。

### 2.1 唯讀 raw 與 bit sensor

- 保留既有 `read_storage_control`：FC03、43110、count=1；沒有新增讀取 request，因此不改 poll rotation。
- 保留 `set_storage_control` raw sensor，並在 `B1_INFO` 以「儲能控制原始值」呈現。
- 新增 `storage_control_bits`，同讀取 response 的 offset=3、length=2；它只會把同一 word 拆成 payload key，不產生任何 Modbus command。
- `B3_STATUS_BITS` 新增 16 個 binary_sensor；payload 值固定為「啟用」／「未啟用」，兩端設定一致。

這採用「方案 B：拆成數個唯讀 sensors」。現有 profile-only schema 不支援把任意 set bits 動態串成單一中文字串，但 16 個 entity 能保留完整 raw bitmask，不會藏掉未知 bit，也不需要污染 GenericAdapter。

### 2.2 中文 bit 對照與證據等級

| bit | map2 HA 名稱 | 證據限制 |
| ---: | --- | --- |
| 0 | 自用模式（上游定義） | 031：本機曾唯讀觀察到 raw=1；名稱來自上游 hybrid model。 |
| 1 | 分時模式（上游定義） | 上游 Time of Use；本機 firmware 未驗證。 |
| 2 | 離網模式（上游定義） | 上游 Off-Grid；本機 firmware 未驗證。 |
| 3 | 電池喚醒功能（上游定義） | 上游 modifier；本機 firmware 未驗證。 |
| 4 | 備援／保留模式（上游定義） | 上游 Reserve/Backup；本機 firmware 未驗證。 |
| 5 | 市電充電功能（上游定義，極性未在本機確認） | 031 記錄上游文件文字與 field capture 的極性矛盾。 |
| 6 | 饋網優先（上游定義） | 上游 Feed-In Priority；本機 firmware 未驗證。 |
| 7 | 電池 OVC（上游定義） | 只有上游名稱，不擴大解釋。 |
| 8 | 電池強制充電／削峰功能（上游定義） | 只有上游名稱，不擴大解釋。 |
| 9 | 電池電流校正（上游定義） | 只有上游名稱，不擴大解釋。 |
| 10 | 電池修復模式（上游定義） | 只有上游名稱，不擴大解釋。 |
| 11 | 削峰模式（上游定義） | 上游 Peak Shaving；本機未驗證可寫。 |
| 12–15 | 未知 Bit 12–15 | `UNKNOWN / UNVERIFIED`；不賦予語意，不隱藏。 |

本輪只呈現實際 set bit；即使同時出現上游理論上互斥的 mode bits，也只會並列顯示，絕不修正、清除或寫回。

### 2.3 map2 的 43110 寫入封閉

map2 刪除了下列兩個原圖中僅與 43110 有關的項目：

1. `settings[43110] = {key: set_storage_control, write_fc: 16}`
2. `B2_SETTING` 的「儲能模式 (33:自用 35:分時)」number entity。

故 map2 仍可從 FC03 response 解碼 raw／bits，但 GenericAdapter 找不到 `set_storage_control` 的 setting，無法建立 FC16／FC06／FC22 或任何其他寫入 request。這不改原 profile；原圖仍保持原本行為，map2 只在日後明確切換時才會套用此唯讀界線。

## 3. 驗證證據

### 3.1 原圖完整性

在複製前／後計算 SHA-256：

```text
f56103a67803a8ae945afdf03b68d373ab24fb6674a03264c8c0148faed4356f  profile/solis_inverter_map.yaml
f56103a67803a8ae945afdf03b68d373ab24fb6674a03264c8c0148faed4356f  profile/solis_inverter_map2.yaml  (剛複製時)
```

後續施工只寫入 map2。最終 Git diff 對原 `profile/solis_inverter_map.yaml` 為空；原檔 SHA-256 維持該值。

### 3.2 Validator

實際執行：

```bash
docker exec ginlong python /app/src/map_validator.py /app/profile/solis_inverter_map2.yaml
```

結果：

```text
✅ 檢查通過！地圖檔結構健康，無 Key 衝突，後端數值型別與 HA 語法完全正確。
```

沒有修改 `map_validator.py`。

### 3.3 Isolated RTU decode（零 I/O）

測試在既有 `ginlong` container 中載入 map2 與目前 `GenericAdapter`，以記憶體建立合法 RTU FC03 response（UID=1、byte-count=2、正確 CRC），再呼叫 `Adapter.decode()`。沒有呼叫 Driver、BusMaster、MQTT broker，也沒有連線設備。

| raw | hex | 預期／實際啟用 bit | 結果 |
| ---: | --- | --- | --- |
| 1 | `0x0001` | `[0]` | PASS：自用模式 bit 啟用 |
| 33 | `0x0021` | `[0, 5]` | PASS：bit 0 與 bit 5 同時存在 |
| 35 | `0x0023` | `[0, 1, 5]` | PASS：三個 bit 均存在，沒有縮成「分時模式」單值 |
| 2048 | `0x0800` | `[11]` | PASS：削峰模式（上游定義）bit 啟用 |
| 2080 | `0x0820` | `[5, 11]` | PASS：bit 5、11 同時存在 |
| 8193 | `0x2001` | `[0, 13]` | PASS：自用模式與未知 Bit 13 同時存在 |

測試也逐一確認 16 個 key 的 state 僅為「啟用」或「未啟用」。因此 unknown bit 沒有被 raw mapping、default value 或 decoder 靜默丟棄。

### 3.4 無 43110 寫入路徑

同一 pure test 驗證：

```text
43110 encode_write=REJECTED PASS
```

也就是 map2 內 `adapter.encode_write("set_storage_control", 1)` 拋出既有 `ValueError`，而不是產生任何 request bytes。這是設定／編碼層驗證，沒有嘗試寫到 inverter。

### 3.5 HA Discovery 唯讀驗證（假 MQTT）

以 in-memory fake MQTT client 呼叫目前 `HAManager.send_discovery()`，結果：

```text
HA read-only discovery: raw sensor=PASS, bit binary sensors=16 PASS, command_topic=NONE PASS
```

具體斷言：

- raw `set_storage_control` 發佈為 `sensor`，沒有 `command_topic`。
- 16 個 `storage_bit_*` 發佈為 `binary_sensor`，每個都有 `payload_on=啟用`、`payload_off=未啟用`，沒有 `command_topic`。
- 不存在 map2 的 `number/.../set_storage_control/config` Discovery。

fake MQTT 只收集 Python method calls，不連接 broker；因此此測試不會改變 HA retained discovery 或現場 observable behavior。

## 4. 對外可觀測性與實機邊界

### 原穩定 profile

`solis_inverter_map.yaml` 沒有任何本輪 diff；`profile/config.yaml` 也沒有修改。因此正式 UID 仍使用原地圖，entity、MQTT topic、Discovery、state、availability、poll rotation、timeout、retry、offline／online 與原 43110 write path 都沒有被本輪變更。

### 實驗 map2

若未來經另行授權把特定測試 UID 切換到 map2，它會：

- 保留同一 FC03 43110 polling request；
- 額外發佈一個 raw diagnostic sensor 與 16 個 bit diagnostics；
- 移除 map2 的 43110 number command Discovery／setting；
- 不會把 mode／modifier 重新合併成 enum，也不會隱藏 bit 12–15。

### 實機 read-only 觀察

**NOT TESTED。** 本輪沒有變更 runtime config、沒有 restart，且沒有把任何 UID 切換到 map2。這是刻意的安全邊界，不是失敗；isolated decode 與 Discovery 組裝均 PASS，但不可宣稱 HA broker／實機已看到新 entity。

## 5. Diff 範圍與禁止項目檢查

本輪新增／修改的檔案只有：

- `profile/solis_inverter_map2.yaml`（新實驗 profile）
- 本報告 `report/032_Solis_43110唯讀中文Bitmask解碼Profile實驗版施工與驗收.md`

本輪沒有修改：

- `profile/solis_inverter_map.yaml`
- `profile/config.yaml`
- `adapters/generic_adapter.py` 或任何 Adapter
- `src/driver.py`
- `src/bus_master.py`
- `src/map_validator.py`
- MQTT／HA Manager、Docker、timeout、retry、poll rotation

沒有發送 FC16、FC06、FC22、RMW 或任何對 43110 的實機 write。

## 6. 已知限制與後續建議

1. map2 以 16 個獨立 diagnostics 顯示 bit，不提供單一「自用模式｜市電充電功能」合併字串。這是現有 profile-only 能力的刻意最小解；資訊沒有遺失。
2. bit 0–11 的中文名稱仍是上游模型；特別是 bit 5 已明示極性尚未在本機確認。這些 entity 不提供控制。
3. 12–15 只能顯示「未知 Bit N」，不能推測用途。
4. 若日後要在實機載入 map2，需另行授權、僅切換明確測試 UID，並預先處理 HA retained Discovery 的 entity 新增／舊 43110 number cleanup；不可直接切換全部正式設備。
5. 任何 43110 寫入、RMW、FC22 或 select／switch 控制仍是另一輪獨立工作，必須延續 031 的雙 master 安全審查。

## 7. 最終裁決

**PASS WITH LIMITATIONS**

### 1. 原穩定 profile 是否完全未修改？

**YES**

### 2. 新 map2 是否可以正確唯讀解碼 43110 bitmask？

**YES**

### 3. 是否存在任何新的 43110 寫入路徑？

**NO**

### 4. 是否建議將 map2 交給 Claude Code 做敵對式獨立驗收？

**YES**
