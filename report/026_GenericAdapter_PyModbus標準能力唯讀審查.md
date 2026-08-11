# 026 — Generic Modbus Adapter 對照 PyModbus 標準能力唯讀審查

日期：2026-08-11  
範圍：唯讀程式審查、隔離封包／回覆驗證、最小升級設計；**未修改 production code、正式 profile、Docker 或現場設備。**

## 先講人話：結果與注意事項

**結果：目前系統「讀資料」與單一數值寫入是穩定且有防呆的；但它不是完整的通用標準 Modbus 寫入 Adapter。** 最明確的差距是：名稱為 FC16（寫多個暫存器），實際上永遠只會寫 **1 個** 16-bit 暫存器。因此遇到 32-bit 數字、浮點數或真正連續多格設定時，系統會送出不完整的封包，而不是設備所需的標準封包。

對目前已啟用的 Solis 設定沒有立即的行為改變：其五個可寫點都是 16-bit、FC16 count=1，現有封包已與 PyModbus 逐位元組一致。建議不要導入 PyModbus 到 production；把它保留成隔離測試的「標準答案」即可。若未來確有 32-bit／float 寫入需求，可只在 `generic_adapter.py` 補齊，毋須觸動 BusMaster、HA、雙 master 防護或 Driver。

注意事項：FC22 不是只改 Adapter 就能安全加入，因為目前 Driver 對 FC22 的錯誤 ACK 會盲目當成功；本輪不建議採用。雙 master 是指定的壓力情境，不列為缺失，也不以其 RX 雜訊推論 Adapter 有問題。

---

## 1. PASS / FAIL

**PASS WITH GAPS** — FC01–04 讀取與 FC05／FC06／FC16-count=1 的核心路徑符合已測標準封包與回覆契約，但 GenericAdapter 不具完整 FC16、多 coil、型別對稱與輸入範圍保護，不能稱為完整通用標準 Modbus Adapter。

## 2. 已驗證的現有能力

### 2.1 GitHub 與審查基準

| 項目 | 已確認結果 |
|---|---|
| 專案 main | 遠端與本機均為 `fc45e9e82dfb410ef82fd22a217a4156a580d864`（2026-08-09，`Refactor gateway configuration and device profile handling`）。|
| PyModbus oracle | 官方 `pymodbus-dev/pymodbus` 的 `dev` 分支，隔離快照 `14bff79ab5a0a6fd5fb04abf9d8dde6cc0ee1b4b`（2026-07-28）。未安裝至 production，也未加入 `requirements.txt`。|
| 比對層級 | RTU ADU：`UID + PyModbus PDU.encode() + 本專案 CRC16`。PyModbus 的 PDU encoder 不負責本專案 RTU CRC，因此 CRC 由同一個已審查的 CRC16 函式包裝；此層級可直接比對實際串列／RTU-over-TCP 位元組。|

PyModbus 官方原始碼中，讀取 request 會驗證位址與數量；FC15 的最大數量為 2000 coils，FC16 伺服端接受 1–123 registers，FC22 的標準 RTU frame 為 10 bytes；本次以此作為實作參考，而不是作為 production dependency。[官方 FC01/02/05/15 原始碼](https://github.com/pymodbus-dev/pymodbus/blob/14bff79ab5a0a6fd5fb04abf9d8dde6cc0ee1b4b/pymodbus/pdu/bit_message.py)／[官方 FC03/04/06/16/22/23 原始碼](https://github.com/pymodbus-dev/pymodbus/blob/14bff79ab5a0a6fd5fb04abf9d8dde6cc0ee1b4b/pymodbus/pdu/register_message.py)。

### 2.2 Read FC

| FC | 現況 | request／RX 驗證 | 隔離結果 |
|---|---|---|---|
| FC01 Read Coils | 已實作 | `UID, FC, addr, quantity, CRC`；以 `(count+7)//8` 驗 byte count、CRC、UID、FC、例外回覆。 | 與 PyModbus 完全相同：`01 01 00 0A 00 09 DC 0E`。錯 UID、FC、截斷、CRC、Exception、quantity 全拒絕。|
| FC02 Read Discrete Inputs | 已實作 | 同 FC01。 | 完全相同：`01 02 00 0A 00 09 98 0E`；六類負向測試全拒絕。|
| FC03 Read Holding Registers | 已實作 | byte count 必須為 `count*2`；Deep CRC Radar 尋找同 UID/FC 的完整 CRC frame。 | 完全相同：`01 03 00 0A 00 01 A4 08`；六類負向測試全拒絕。|
| FC04 Read Input Registers | 已實作 | 同 FC03。 | 完全相同：`01 04 00 0A 00 01 11 C8`；六類負向測試全拒絕。|

實作位置：[預建 request 與 quantity 推導](/root/py_ginlong/adapters/generic_adapter.py:56)、[Deep CRC Radar 與 exception](/root/py_ginlong/adapters/generic_adapter.py:339)、[RX byte count／CRC 檢查](/root/py_ginlong/adapters/generic_adapter.py:410)。這也保留了先前連續讀取測試所驗證的「錯 quantity 不可被當成正確舊幀」行為；參考既有 [023 報告](/root/py_ginlong/report/023_連續暫存器合併讀取可行性與隔離驗證.md)。

### 2.3 Write FC 與 ACK 契約

| FC | 「功能碼存在」 | 是否完整實作 | 已驗證 RX / ACK 行為 |
|---|---|---|---|
| FC05 Write Single Coil | 是 | 是（單 coil） | 產生的 RTU bytes 與 PyModbus 一致；Driver 對正常 ACK 成功、合法 Exception 回傳 `False`，錯 UID／FC／地址／value／CRC 全拒絕。|
| FC06 Write Single Register | 是 | 僅限一個 16-bit register；合法範圍保護不足 | 正常 ACK 契約完整；但 `70000` 被靜默壓為 `0x1170`，見第 3 節。|
| FC15 Write Multiple Coils | 否 | 否 | PyModbus 標準 request 可產生；GenericAdapter 對 scalar 為 `NotImplementedError`，對 coil list 先為 `ValueError`。Driver 本身對人工構造的標準 FC15 ACK 已完整驗證。|
| FC16 Write Multiple Registers | 是 | **部分實作：固定 quantity=1、byte count=2。** | count=1 bytes 與 PyModbus 一致；count=2／4 的 list 無法編碼。Driver 對人工構造的標準 FC16 ACK 已完整驗證。|
| FC22 Mask Write Register | 否 | 否 | GenericAdapter 無 encoder；Driver 的 RTU ACK guard 未涵蓋 FC22，連 `not-a-modbus-ack` 都回傳成功，不能只改 Adapter。|
| FC23 Read/Write Multiple Registers | 否 | 否 | GenericAdapter 無 encoder／回覆流程；不應在本輪擴張。|

目前 Driver 的 RTU ACK 驗證是適當的單一 transport 責任點：對 FC05／06／15／16 檢查 request CRC、回覆長度 8、UID、FC、CRC 與 `resp[2:6]` echo。對 FC05/06 那是 address/value；對 FC15/16 那是 address/quantity。因此完整 FC15/16 **不需要**在 GenericAdapter 重複造一份 ACK parser。[Driver ACK guard](/root/py_ginlong/src/driver.py:289)。隔離 loopback 已對每個 FC05／06／15／16 實測：正確 ACK 接受、合法 Exception 回傳 `False`、錯 UID、FC、address、value/quantity、CRC 均拒絕。

### 2.4 Data type、byte order 與回讀能力

| 能力 | Read | Write 現況 | 隔離證據 |
|---|---|---|---|
| `uint8` / `int8` | 支援 | 沒有型別宣告；只是被當數值壓到 16-bit register。 | read `254`／`-2` 正確。|
| `uint16` / `int16` | 支援 | FC06、FC16-count=1 支援；`int16=-2` 的 two's complement bytes 正確。 | 兩者與 PyModbus PDU 完全相同。|
| `uint32` / `int32` / `float32` | 支援 | 不支援。設定中的 `datatype`／`word_order` 完全未被 write path 讀取。 | 預期 FC16 quantity=2；實際固定 quantity=1，分別只留下低 16-bit 或浮點轉整數。|
| `uint64` / `int64` / `float64` | 支援 | 不支援。 | 預期 quantity=4；實際仍為 quantity=1。|
| `string` | 支援 ASCII 解碼、去除 NUL／空白 | 一般字串寫入不支援；僅 `ON/OFF/TRUE/FALSE` 特例及 `link_sensor` value map。 | 讀取 `ABC\0` 得 `ABC`。|
| bits / coil | FC01／02 byte count、bit extraction 支援 | FC05 單一 coil 支援；FC15 多 coil 不支援。 | FC01/02 正反向測試通過。|
| big endian | 預設支援 | 16-bit 固定 network order；多字無 codec。 | read/write 16-bit bytes 正確。|
| little endian、`swap` / `word_swap`、`byte_swap` | read 支援 | write 忽略。 | 32-bit read 成功；`uint32 word_swap` 寫入與 PyModbus 的兩個 register bytes 不等價。|
| `dcba` | 目前僅 64-bit read 的自訂 word 反轉支援 | write 不支援。 | 64-bit read 成功。|

Read codec 位於 [_unpack_value](/root/py_ginlong/adapters/generic_adapter.py:94)。Write path 在 [encode_write](/root/py_ginlong/adapters/generic_adapter.py:171) 先一律做 `int(round(float(value)*scale))`，而 FC06／FC16 使用 `int_val & 0xFFFF`；這是讀寫 codec 不對稱的直接原因。

## 3. 已驗證缺口

| 編號 | 代碼位置／實際行為 | PyModbus／標準對照 | isolated test 結果 | 是否影響實際能力 |
|---|---|---|---|---|
| G1 | [generic_adapter.py:218](/root/py_ginlong/adapters/generic_adapter.py:218) 將 FC16 寫死 `count=1, byte_count=2`。 | PyModbus `WriteMultipleRegistersRequest` 可正確產生多個連續 registers；FC16 的標準上限為 123。 | FC16 count=1 完全相同；count=2 oracle `01 10 00 64 00 02 04 12 34 56 78 8F 40`、count=4 oracle 均存在，但 GenericAdapter 對 list 為 `ValueError`。 | **是。** profile 使用者看到 FC16 容易誤認為可寫多格。|
| G2 | [generic_adapter.py:183](/root/py_ginlong/adapters/generic_adapter.py:183)–[220](/root/py_ginlong/adapters/generic_adapter.py:220) 不讀 `datatype`／`word_order`，並遮罩為 16-bit。 | 多字值必須被拆成連續 big-endian registers，再按裝置 word order 重排。 | `uint32/int32/float32/uint64/int64/float64` 與 word swap 全部不等價；例如 `uint32 0x12345678` 實際只寫 `0x5678`。 | **是。** 新設備的 32/64-bit／float 寫入會錯。|
| G3 | [generic_adapter.py:206](/root/py_ginlong/adapters/generic_adapter.py:206)–[220](/root/py_ginlong/adapters/generic_adapter.py:220) 對溢位靜默 `& 0xffff`；`Inf` 會未捕捉地拋 `OverflowError`。 | PyModbus PDU 以無號 16-bit packing，超範圍不可默默改寫為另一值。 | FC06 `70000` 實際送 `... 11 70 ...`；`NaN` 為 `ValueError`，`Inf` 為未統一的 `OverflowError`。 | **是。** 錯誤指令可能寫到非使用者輸入的值。|
| G4 | [generic_adapter.py:222](/root/py_ginlong/adapters/generic_adapter.py:222) 沒有 FC15。 | PyModbus 有 FC15 bit packing、count 與 byte count。 | 官方 oracle：`01 0F 00 64 00 03 01 05 3E 9C`；GenericAdapter 無法產生。Driver FC15 ACK 全通過。 | **是，但目前 profiles 無實際需求。**|
| G5 | GenericAdapter 無 FC22；[driver.py:307](/root/py_ginlong/src/driver.py:307) 的 ACK guard 只含 5/6/15/16。 | FC22 正常 ACK 為 10 bytes，且必須 echo address/AND/OR masks。 | 發送合法 FC22 request 後，以 `not-a-modbus-ack` 回覆，現有 Driver 回傳 `True`。 | **是。** 若只加 Adapter，會把錯 ACK 報成成功。|
| G6 | GenericAdapter 無 FC23；其既有 write 後 verify flow 也不是 FC23 response contract。 | FC23 正常 response 是 read-register data，不是 FC15/16 的 8-byte echo。 | 官方 oracle 可產生；GenericAdapter 無 encoder。 | **是，但無現場需求與最小修正理由。**|
| G7 | [map_validator.py:171](/root/py_ginlong/src/map_validator.py:171) 只驗 `write_fc` 可轉整數；[map_validator.py:194](/root/py_ginlong/src/map_validator.py:194) 只拒絕 count ≤ 0。 | 標準 read count 需依 FC 限制，且 GenericAdapter 已宣告的 datatype／word order 應可被 profile validator 限定。 | `write_fc=999`、`verify_count=0`、未知 datatype／word order、FC03 `count=126` 全被 validator 接受；PyModbus 對 FC03 `count=126` 拒絕。`scale=0` 則已正確拒絕。 | **是。** 壞 profile 可在載入後才造成非標準封包或靜默錯誤。|
| G8 | [generic_adapter.py:237](/root/py_ginlong/adapters/generic_adapter.py:237)–[240](/root/py_ginlong/adapters/generic_adapter.py:240) 的 verify read 永遠只取前兩 bytes。 | 多 register FC16 必須回讀完整值再以相同 codec 解碼。 | `verify_count=2` 的 verify request 確實為 `01 03 00 64 00 02 85 D4`，但回覆 `12 34 56 78` 只解成 `0x1234`。 | **是。** 若補 FC16 多格卻不補 verify，會出現假驗證。|

## 4. 不應修改的部分

以下已實際讀取且確認本次不必動：

- [BusMaster write budget、排程、write → ACK → verify read → HA publish 流程](/root/py_ginlong/src/bus_master.py:220)：既有 public adapter contract 足夠；FC15／完整 FC16 的標準 ACK 已由 Driver 正確驗證。
- [Driver reconnect、inter-frame delay、max frame／response 限制與 RTU ACK 防線](/root/py_ginlong/src/driver.py:289)：P1 FC16 的 request 只從 11B 增至 13B（32-bit）或 17B（64-bit），ACK 仍為 8B，遠低於 `max_response_bytes=2048`。不改 idle 判幀、CRC、防線或 traffic log。
- [原生 Modbus TCP Driver](/root/py_ginlong/src/modbus_tcp_driver.py:16)：它繼承父類，並保留 MBAP 與 RTU-over-TCP 的既有分流行為；本輪 GenericAdapter 仍只處理 RTU ADU，不觸碰 MBAP 語意。
- MQTT / HA Manager、offline/online 狀態機、雙 master 防護、重試數、timeout、poll rotation、現有 profile key 與 Solis 地圖。

## 5. 建議升級項目

### P0 — 應先修正的「名義能力與安全」差距

1. **完整化 FC16 的能力宣告與 encoder。** 在完成實作前，文件與註解應明確標示「FC16 count=1 only」，不能稱作完整 Write Multiple Registers。G1、G2、G8 已有逐 byte 和 verify 實證。
2. **拒絕溢位與非有限數值，不得遮罩改值。** 對現有合法 16-bit 值輸出完全不變；僅把先前會錯寫或不一致例外的無效輸入改成明確 `ValueError`。G3 已實證。

### P1 — 低風險、確有標準價值，可在 GenericAdapter 內完成

1. **FC16 quantity=2 的 `uint32`／`int32`／`float32` write。** 用目前 sensor 的 `datatype`、`length`、`word_order`，優先以 `link_sensor` 找 sensor，沒有 `link_sensor` 時以同名 key 找 sensor；未找到時保留既有單一 `uint16` 行為。G1/G2/G8 的證據完整，且 Driver 的 FC16 ACK 已驗證可直接沿用。
2. **同步補完整 verify read codec。** 將預期 register count 和 codec 放入既有內部 `context`，由 `decode()` 驗 exact byte count，再用同一 codec 解回完整值。這不改 `build_verify_read(key)`／`decode(raw_data, context)` 的 public signature。
3. **加強 profile validator 的標準邊界。** 只拒絕明顯無效值：read FC 僅 1/2/3/4 與其各自數量上限、write FC 只允許已宣告支援集、`verify_count >= 1`、顯式 `datatype` 與 `word_order` 必須被支援且 length 相符。既有沒有填 `word_order` 的 profile 視為預設 `big`，不可造成舊地圖失效。G7 已實證。

### P2 — 有設備需求才採用

1. **FC16 quantity=4 的 `uint64`／`int64`／`float64` write。** 共用 P1 codec helper，但要另加 4-register order／範圍／實機設備測試。隔離封包差距已證明，尚無目前 profile 需求。
2. **未關聯 sensor 的多字寫入 metadata。** 目前 schema 對有 `link_sensor` 或同名 sensor 的設定足夠；若日後出現無對應 sensor 的 32/64-bit writable setting，才考慮 `write_datatype`、`write_word_order` 或 `write_count`。本輪**不新增**這些欄位，避免 profile 膨脹。

### 不採用（本輪）

- **FC15 Multiple Coils：暫不採用。** 其標準封包及 Driver ACK 都已被驗證，但目前 `encode_write(key, value)` 是單一 HA command value，現有 profile 也沒有可無歧義描述「多個 coils」的 input contract；硬塞 list／bitmask 會改變公開 command 語意。等有實際設備 profile 與 payload 語意後再做。
- **FC22 Mask Write Register：暫不採用。** G5 證明必須連動 Driver ACK 驗證（10B、三個 echo fields），違反本輪「能在 Adapter 解決就不動 Driver」原則。
- **FC23 Read/Write Multiple Registers：不採用（P3）。** 無需求，且會擴張現在 write 後 verify 的 transaction contract。
- FC43、vendor-specific FC：不採用（P3）。沒有實際需求與證據。

## 6. 最小修正範圍（僅為核准後方案，不是本輪變更）

| 檔案 | 精確範圍 | 內容 |
|---|---|---|
| `adapters/generic_adapter.py` | `encode_write()`、`build_verify_read()`、`_extract_data()`；可新增私有純 helper | 加入 codec resolver、有限值／範圍驗證、canonical bytes → device word order、FC16 list of registers encoder，以及完整 verify decode。保留四個 public methods：`encode_write`、`build_verify_read`、`build_poll_read`、`decode`。|
| `src/map_validator.py` | `_check_backend()`、`_check_settings()`、`_check_read_commands()` | 只加入已列 P1 schema 限制；不用新欄位。|
| `test/modbus_adapter_audit/` 或正式 tests | 新增／保留 oracle 與 regression tests | 將本次 failing gap tests 轉成實作後必須 PASS 的回歸測試。|
| 文件／report | capability matrix | FC16 寫清楚已完整或 count=1 only，避免能力誤解。|

**不修改** `src/driver.py`、`src/bus_master.py`、`src/modbus_tcp_driver.py`、MQTT／HA、Docker、現有 profile／map。本輪的 schema 足以從 `link_sensor` 或同名 sensor 推導目前需要的 P1 資訊；不新增 profile 欄位。

## 7. 對外可觀測性一致性

核准後的 P0/P1 實作必須以以下規則驗收；未修改 profile 的既有設備行為應維持等價。

| 可觀測項目 | 保持方式 |
|---|---|
| HA `entity_id`、MQTT topic、Discovery/state/availability payload、profile key、value map | 不改 HA／MQTT／profile key；Adapter 的 read decode 對舊 profile 不變。|
| scale 後結果與 word order | 對舊 16-bit path 使用相同 scale、相同 bytes；新多字 codec 只在 profile 能明確導出的多字 setting 啟用。|
| Poll rotation、輪詢順序、timeout、retry、offline/online | 不改 BusMaster／Driver，poll path 不改。|
| write → verify read | 保留同一順序與呼叫介面；P1 僅使新多字 setting 的 verify count 和解析長度正確。|
| RTU bytes（既有有效寫入） | FC05、FC06、FC16-count=1 必須以 snapshot byte tests 證明完全相同；本次 oracle 已確認其合法值相同。|
| 正常 INFO log | 不增加正常輪詢／寫入 log；只允許無效 datatype、overflow、NaN/Inf、非法 quantity 的明確錯誤。|
| RTU、RTU-over-TCP、原生 Modbus TCP | P1 的 RTU payload 合理增加但不超 Driver 限制；RTU ACK 仍固定 8B。原生 TCP／MBAP 不改。|
| 雙 master、ACK/CRC、traffic log、inter-frame delay | 不改 Deep CRC Radar、Driver ACK guard（P1 僅 FC16）、traffic log 與 delay；雙 master 仍為刻意壓力情境。|

目前 Solis 三張候選與正式 `solis_inverter_map` 的所有 settings 都是 FC16 count=1、對應 `uint16` 讀點；因此上述 legacy branch 能維持現有封包。先前 P1/P2 合併讀取實機觀察的 RX 差異屬於讀取地圖壓力測試，與本次不改 poll path 的 GenericAdapter 寫入升級無關；參考 [025 報告](/root/py_ginlong/report/025_6k2p1_P1實測與P2升級門檻報告.md)。

## 8. 驗證計畫（核准後才實作）

1. **Byte-for-byte PyModbus oracle：** 保留本次 RTU ADU 比對，讓 FC05、FC06、FC15（若未來採用）、FC16 count=1/2/4、uint16/int16/uint32/int32/float32/uint64/int64/float64，以及 big/little/word swap/byte swap/DCBA 都有同一輸入的精確 bytes 比較。
2. **既有行為回歸：** FC01–04、FC05、FC06、現有 FC16 count=1；原 Solis map 每一 writable setting 的 request snapshot、verify request snapshot、scale 回讀結果及 poll rotation 都必須 PASS。
3. **新能力：** FC16 count=2、count=4（僅 P2 核准時）、uint32/int32/float32 write，以及完整 verify 回讀的 exact quantity/decoded value。
4. **負向測試：** invalid datatype、malformed word order、overflow、NaN/Inf、scale=0、quantity=0、各 FC 標準上限外、payload byte count 不一致、Exception、bad CRC、wrong UID、wrong FC、wrong address、wrong quantity。P1 不採用 FC15/22/23 時，不讓它們靜默變成「宣稱支援」。
5. **必要時實機：** 只在使用者明確授權的測試 profile／測試時段，先以一個不影響現場控制的 32-bit setting 做 FC16 count=2，檢查 TX/RX frame、ACK、verify value、HA/MQTT before/after snapshot、timeout/retry/availability；雙 master 結果作壓力觀察，不列成缺失。

本輪已完成的可再現隔離產物：

- [測試程式](/root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/modbus_adapter_audit/run_generic_adapter_audit.py)
- [測試結果 JSON](/root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/modbus_adapter_audit/generic_adapter_audit_results.json)
- [官方 PyModbus source 快照](/root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/modbus_adapter_audit/pymodbus_official)

測試涵蓋 FC01–04 的 request bytes 及六種 RX 負向情境、FC05／06／16-count=1 oracle 一致性、FC16 count=2/4 和多型別不等價證據、FC15/22/23 缺口、schema 邊界，以及本機隔離 TCP 的 FC05／06／15／16 ACK contract。全部斷言通過；結果 JSON 內保留每一個 oracle／實際 hex。

## 9. 最終裁決

**ADOPT WITH LIMITS**

值得把 PyModbus 當成隔離測試的 reference oracle，用來完善目前 GenericAdapter；**不值得**把 PyModbus 納入 production dependency 或以它取代現有架構。

採用界線是：先完成 P0，接著只在 `generic_adapter.py` 與必要 validator 補 P1 的 FC16 32-bit codec／verify；維持 Driver、BusMaster、HA/MQTT、輪詢與雙 master 防線不動。FC15、FC22、FC23 和 64-bit 寫入均等待明確設備需求與相應驗證。

## 審查完整性與未變更聲明

本輪重新讀取 GitHub main 最新 commit，以及 `adapters/generic_adapter.py`、`src/driver.py`、`src/bus_master.py`、`src/modbus_tcp_driver.py`、`requirements.txt`、`src/map_validator.py`、現有隔離測試及近期 Modbus／Adapter reports。正式程式檔 `adapters/generic_adapter.py`、`src/driver.py`、`src/bus_master.py`、`src/modbus_tcp_driver.py`、`src/map_validator.py`、`requirements.txt` 在審查結束時均無本輪 diff；新增內容只有本報告與隔離工作目錄的測試／官方 source snapshot。
