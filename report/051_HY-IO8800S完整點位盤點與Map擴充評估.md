# 051 — HY-IO8800S 完整點位盤點與 Map 擴充評估

- 範圍：原廠手冊、最新 production code、UID3 的 Gateway 唯讀 FC01／FC02／FC03 實測。
- 設備：`192.168.88.190:502`，UID3，原生 Modbus TCP（MBAP）；測試未使用 raw socket sender。
- 原廠依據：[HY-IOS 串口 IO 系列說明書](/root/.codex/attachments/42081d8f-bbfd-46b3-977c-9ea170c15adf/HY-IOS串..V1.pdf)，§1.3、§3.1、§3.2。

# 人話結論

1. 手冊找到 6 類點位：8 DO、8 DI、裝置／通訊參數、聯動 Rule、DI 脈衝計數與危險特殊寄存器；HY-IO8800S 沒有 AI。
2. 實機用正式 Gateway 唯讀確認 94 個實體／設定資料點；加上既有兩個推導 FC15 group state，MQTT state 共 96 key。
3. DO、DI、身分／通訊參數、Rule 1–8 和 DI1–8 計數都可直接納入「唯讀」full map；每一類都有手冊與實機證據。
4. DI、型號、版本、序號與運行資料只能讀；Rule／通訊／裝置參數雖可寫，但本輪**不開放寫入**。
5. Rule、通訊與輸出保持的寫入可能立即影響設備；pulse counter 只可寫 `0`；重啟／恢復出廠寄存器永不映射到 HA。
6. 現有 FC01、8 個 FC05 switch、FC15 `group_01`／`group_234` 完全保留；測試全程無 Modbus 寫入。
7. Generic Adapter、TCP Adapter、Driver、BusMaster、HA Manager、Validator 都已足夠，**production code 不需要修改**。
8. 建議下一輪建立唯讀正式 `relay_8ch_full_map.yaml`；可施工草案在 [relay_8ch_full_map_draft.yaml](/root/py_1f/scratch/relay_8ch_full_map_draft.yaml)，尚未放入 `profile/` 或啟用。
9. 本輪唯讀盤點：**PASS**。任何新增 holding-register 寫入：**UNPROVEN／未授權**。

## 施工前盤點清單與結論

| 模組 | 查核結果 | 結論 |
| --- | --- | --- |
| Device Protocol / Manual | §3.1 明定 DO=FC01/05/0F、DI=FC02、holding=FC03/06/10；§1.3 指 HY-IO8800S 是 8 DI、8 DO、0 AI。 | 地址和型別有一手來源；不猜未知位址。 |
| Generic Adapter | 已預建 FC01/02 bit byte-count、FC03/04 register byte-count；`_extract_data()` 可解 bits、ASCII string、`uint16`、`uint32`、value map。 | 唯讀 map 所需 datatype 已齊。 |
| TCP Adapter / Driver | TCP Adapter 將相同 PDU 加 MBAP 並驗 transaction ID／length；TCP Driver 維持既有讀取與 ACK contract。 | FC01/02/03 不需 transport 修改。 |
| BusMaster | 每次 poll 在既有 `bus_lock` 內 TX／RX，decode 後發給 HA state。 | 可安全做正式 Gateway 唯讀輪詢。 |
| HA Mapping | HA Manager 支援 `sensor`、`binary_sensor`、`switch`、`select`；state 是合併 JSON cache。 | DO 保持 switch、DI 用 binary_sensor、其餘用 sensor；Rule action 不做 select。 |
| Validator | 檢查 `read_commands`、sensor offset／length／datatype、HA domain、settings、coil_groups。 | 草案與原 map 均 validator PASS；它不代替實機位址證明。 |

## 地址約定與證據標準

手冊的 PLC 位址為 1-based；本 Gateway 使用 PDU 0-based address。holding 例：手冊 §3.2.2.6 讀 PLC `40045` 時 PDU 是 `0x002C`，因此 `protocol = PLC - 40001`。線圈與 DI 同理分別為 `PLC - 00001`、`PLC - 10001`。

`MANUAL CONFIRMED` 是原廠文件列出；`HARDWARE CONFIRMED` 是本輪 MBAP TX/RX 回讀且由 production Adapter 成功解碼；`BOTH CONFIRMED` 才列入 map 草案。所有 scale 都是 `1`，除非表內明列單位；holding register 依手冊為 big-endian。

## 完整點位表

### DO：8 個輸出線圈

| PLC | Protocol | FC | 型別／長度 | 實機狀態 | 存取／安全 | 證據 |
| --- | ---: | --- | --- | --- | --- | --- |
| 00001–00008（DO1–DO8） | `0x0000`–`0x0007` | 01 read；05 single write；0F multi write | coil，1 bit／路，LSB-first | 八路均 `OFF` | 現有 FC05 與 profile FC15 group 保持；繼電器接點安全仍以現場硬體為準。 | BOTH CONFIRMED |

### DI：8 個離散輸入

| PLC | Protocol | FC | 型別／長度 | 實機狀態 | 存取／安全 | 證據 |
| --- | ---: | --- | --- | --- | --- | --- |
| 10001–10008（DI1–DI8） | `0x0000`–`0x0007` | 02 read | discrete input，1 bit／路，LSB-first | 八路均 `OFF` | 唯讀；草案不指定 device class，避免猜接入的是門磁、乾接點或其他訊號。 | BOTH CONFIRMED |

### 裝置與通訊 holding parameters

| PLC | Protocol | 名稱 | FC／型別／長度 | 實機解碼值 | 存取、power-on effect、注意 | 證據 |
| --- | ---: | --- | --- | --- | --- | --- |
| 40001 | `0x0000` | device model | FC03，string，32 B | `IO8800S` | RO | BOTH |
| 40017 | `0x0010` | firmware version | FC03，string，32 B | `V1.0.3` | RO | BOTH |
| 40033 | `0x0020` | serial number | FC03，string，20 B | `00600423030700010193` | RO | BOTH |
| 40043 | `0x002A` | running slave address | FC03，uint16，2 B | `3` | RO；由 configured address + DIP offset 組成 | BOTH |
| 40044 | `0x002B` | DIP offset | FC03，uint16，2 B | `2` | RO，範圍 0–31 | BOTH |
| 40045 | `0x002C` | uptime | FC03，uint32，4 B，s | `3544` | RO | BOTH |
| 40047 | `0x002E` | configured device name | FC03，string，32 B | `HY-IO8800S-4NN` | RW；文件說明裝置位址／serial 參數要重啟才生效，其餘參數多為立即生效。唯讀草案不開 write。 | BOTH |
| 40063 | `0x003E` | configured device address | FC03，uint16，2 B | `1` | RW，1–255；重啟後才生效，會改通訊目標。禁止 HA write。 | BOTH |
| 40064 | `0x003F` | broadcast mode | FC03，uint16，2 B | `0` / `off` | RW enum：0 off、1 receive/respond、2 receive/no-response；勿遠端改。 | BOTH |
| 40065 | `0x0040` | output state hold | FC03，uint16，2 B | `2` / `hold_after_soft_restart_and_power_loss` | RW enum；會改變重啟／上電後 DO 行為。禁止 HA write。 | BOTH |
| 40066 | `0x0041` | pulse count edge | FC03，uint16，2 B | `0` / falling edge | RW enum；會改變後續計數語意。 | BOTH |
| 40067 | `0x0042` | pulse debounce | FC03，uint16，2 B，ms | `50` | RW，5–255 ms；影響 DI 計數。 | BOTH |
| 40068 | `0x0043` | boot info enabled | FC03，uint16，2 B | `0` / off | RW enum；僅觀察。 | BOTH |
| 40069 | `0x0044` | boot info content | FC03，string，16 B | `HY-IO8800S` | RW；僅觀察。 | BOTH |
| 40077 | `0x004C` | serial heartbeat period | FC03，uint16，2 B，s | `0` | RW，0 off、1–65535；可能改變串口 traffic。 | BOTH |
| 40078 | `0x004D` | serial heartbeat content | FC03，string，16 B | `HY-IO8800S` | RW；僅觀察。 | BOTH |
| 40086 | `0x0055` | RTC Unix time | FC03，uint32，4 B，s | `3544` | RW；RTC 在型號表標示為可定制，讀值已確認但不據此推定 wall-clock 正確性。 | BOTH |
| 40088 | `0x0057` | serial baud rate | FC03，uint32，4 B，baud | `9600` | RW，600–230400；改後會中斷通訊。禁止 HA write。 | BOTH |
| 40090 | `0x0059` | serial data bits | FC03，uint16，2 B | `8` | RW，8/9；改後重啟生效。 | BOTH |
| 40091 | `0x005A` | serial stop bits | FC03，uint16，2 B | `1` | RW，1/2；改後重啟生效。 | BOTH |
| 40092 | `0x005B` | serial parity | FC03，uint16，2 B | `0` / none | RW enum：0 none、1 odd、2 even；改後重啟生效。 | BOTH |
| 40093 | `0x005C` | serial packet time | FC03，uint16，2 B，ms | `0` | RW，0–255（0 adaptive）；改後影響通訊切包。 | BOTH |

### Rule 1–8：固定 16-byte block

手冊 §3.1.5.2 說明每個 Rule 是 16 B（8 registers），按 `mode, action, output, input, Param1, Param2` 排列；HY-IO8800S 有 8 DO，故手冊上限是 16 Rule。Rule 9–16 雖為 `MANUAL CONFIRMED`，本輪未讀，故 `HARDWARE UNPROVEN`，草案只收錄 1–8。

| Rule | PLC block start | Protocol start | mode/action/output/input/Param1/Param2（相對 offset） | 本輪值 `mode/action/output/input/Param1/Param2` | 證據 |
| --- | ---: | ---: | --- | --- | --- |
| 1 | 40129 | `0x0080` | `+0/+1/+2/+3/+4/+6`；各為 `u16/u16/u16/u16/u32/u32` | `0/0/0/0/0/0` | BOTH |
| 2 | 40137 | `0x0088` | 同上 | `0/0/0/0/0/0` | BOTH |
| 3 | 40145 | `0x0090` | 同上 | `0/0/1/1/1000/1000` | BOTH |
| 4 | 40153 | `0x0098` | 同上 | `0/0/1/1/1000/1000` | BOTH |
| 5 | 40161 | `0x00A0` | 同上 | `0/0/1/1/1000/1000` | BOTH |
| 6 | 40169 | `0x00A8` | 同上 | `0/0/1/1/1000/1000` | BOTH |
| 7 | 40177 | `0x00B0` | 同上 | `0/0/1/1/1000/1000` | BOTH |
| 8 | 40185 | `0x00B8` | 同上 | `0/0/1/1/1000/1000` | BOTH |

`Param1` of Rule 1 is therefore PLC `40133` / PDU `0x0084`; this matches the verified FC16 target in report 050. Rule mode enum is profile-mapped (0 disabled; 1 DI follow; 2 pulse; 3 delay; 4 timed; 5 cycle; 6 button; 7 schedule once; 8 schedule cycle; 9 daily; 10–13 AI threshold). `action` cannot safely be one universal enum because its meaning changes with `mode`; it remains an observable raw `uint16`.

All Rule fields are documented RW and may immediately control a DO. Existing values happen to have mode `0` (disabled), but that is an observed state, not permission to make Rule write entities public.

### DI pulse counters and excluded points

| PLC | Protocol | Name | FC／type | Actual | Access／safety | Evidence |
| --- | ---: | --- | --- | --- | --- | --- |
| 41281, 41283, 41285, 41287, 41289, 41291, 41293, 41295 | `0x0500`, `0x0502`, `0x0504`, `0x0506`, `0x0508`, `0x050A`, `0x050C`, `0x050E` | DI1–DI8 pulse count | FC03, uint32, 4 B each | all `0` | RW in manual but **only write zero** to reset. Draft exposes sensor only. | BOTH |
| 42049 | `0x0800` | restart / factory reset | special, 1 B, write-only | not read | `0x5500` restart; `0x0055`/`0x5555` reset + restart. Never map. | MANUAL only |
| 30001 onward | `0x0000` onward | AI input registers | FC04, per-channel uint32 | not queried | HY-IO8800S model table says AI quantity `0` / not supported; no FC04 map. | Manual confirms absence for this model |

## 實機唯讀證據

測試以暫態 `hyios_051_readonly_inventory` profile 經正式 Gateway 輪詢；它沒有新增 FC06／FC16 setting，也沒有 MQTT command。profile 先後通過 host/container validator，輪詢結束後 `config.yaml` 已與測前快照 byte-for-byte 相同，並已重啟回 `relay_8ch_map2`。

以下為完整 MBAP frames，格式是 `transaction-id protocol-id length UID PDU`。本輪是 Modbus TCP，故不含 RTU CRC。

```text
FC01 DO 0..7
TX 00 07 00 00 00 06 03 01 00 00 00 08
RX 00 07 00 00 00 04 03 01 01 00

FC02 DI 0..7
TX 00 02 00 00 00 06 03 02 00 00 00 08
RX 00 02 00 00 00 04 03 02 01 00

FC03 identity (40001..40042, quantity 42)
TX 00 03 00 00 00 06 03 03 00 00 00 2A
RX 00 03 00 00 00 57 03 03 54 49 4F 38 38 30 30 53 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 56 31 2E 30 2E 33 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 30 30 36 30 30 34 32 33 30 33 30 37 30 30 30 31 30 31 39 33

FC03 ordinary parameters (40043..40093, quantity 51)
TX 00 04 00 00 00 06 03 03 00 2A 00 33
RX 00 04 00 00 00 69 03 03 66 00 03 00 02 00 00 0D D8 48 59 2D 49 4F 38 38 30 30 53 2D 34 4E 4E 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 02 00 00 00 32 00 00 48 59 2D 49 4F 38 38 30 30 53 00 00 00 00 00 00 00 00 48 59 2D 49 4F 38 38 30 30 53 00 00 00 00 00 00 00 00 0D D8 00 00 25 80 00 08 00 01 00 00 00 00

FC03 Rule 1..8 (40129..40192, quantity 64)
TX 00 05 00 00 00 06 03 03 00 80 00 40
RX 00 05 00 00 00 83 03 03 80 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8 00 00 00 00 00 01 00 01 00 00 03 E8 00 00 03 E8

FC03 DI pulse counters (41281..41296, quantity 16)
TX 00 06 00 00 00 06 03 03 05 00 00 10
RX 00 06 00 00 00 23 03 03 20 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

原始合併 MQTT state、每個 traffic line 與 key completeness 在 [hyios_051_inventory.json](/root/py_1f/scratch/hyios_051_inventory.json)。它顯示 `required=96`、`received=96`、`missing=[]`。實際 production Adapter 再以這六個 RX frame 重新 decode，結果為 `10 + 8 + 3 + 19 + 48 + 8 = 96` 個 key，無 OOB／null／exception drop：`MAP_DRAFT_PRODUCTION_DECODE_AUDIT=PASS`。

## Map 草案與 HA 設計

完整可驗證草案：[relay_8ch_full_map_draft.yaml](/root/py_1f/scratch/relay_8ch_full_map_draft.yaml)。它有 6 read commands、80 sensor declarations、8 個原 FC05 settings、2 個原 FC15 group、94 個 B1 entities 與 2 個原 B2 selects；`src/map_validator.py` 的 host/container invocation 都 PASS。

- 原 `switch_0`–`switch_7`、names、ON/OFF payload、FC05 settings、`group_01`／`group_234` 和 FC01 0..7 完整保留。
- DI 用 `binary_sensor`；身分、通訊參數、Rule fields 與 counter 用 `sensor`。Rule mode 有 value map；mode-dependent action 只顯示 raw 值，不錯誤地做成通用 select。
- 所有新 holding register 都只有 `sensors`，沒有 `settings`、`number`、`select` 或 `button` 寫入 route。RW 是手冊裝置能力，不是本輪 Gateway 開放權限。
- 以單一 FC03 `quantity=64` 解 8 個同形 Rule block，重複是 YAML 宣告而非第二套 decoder／新 framework；每欄都沿用 GenericAdapter 的既有 `offset`＋datatype 路徑。

## 資料流黑洞與相容性審查

資料流為 `FC01/02/03 RX → TCP Adapter MBAP validation → GenericAdapter _extract_data → decoded key → HAManager merged JSON state → MQTT`。本輪以實幀重跑草案，六段都解出預期數量；bit 位元全部明確產生 key。對 enum，GenericAdapter 對未知 numeric value 回傳 `str(value)`，不會 silent drop；對 action 則不施加可能錯誤的 enum。

現有 `relay_8ch_map2.yaml`、`relay_8ch_map.yaml`、Adapters、Drivers、BusMaster、HA Manager、Validator 的測前／測後 SHA-256 相同；`config.yaml` 已和 `scratch/hyios_051_config_before.yaml` 精確相同。最終 ginlong `running`、restart count `0`，日誌顯示 UID1／UID3 都 ONLINE。沒有 production code、正式 profile 或 config 的持久修改。

還原後另由 MQTT 正常 UID3 FC01 poll 收到 `switch_0`–`switch_7 = OFF`、`group_01 = all_off`、`group_234 = all_off`。這是測前既有 relay baseline，且本輪沒有 FC05／FC06／FC0F／FC10 寫入。

## 後續建議與限制

1. 若批准正式唯讀觀測，先把草案複製為 `profile/relay_8ch_full_map.yaml`，再獨立比較 discovery／MQTT observable 並短暫切 UID3 profile 驗收；不要覆寫 `relay_8ch_map.yaml` 或 `relay_8ch_map2.yaml`。
2. 切換後會多出新 sensor/binary_sensor discovery；原 8 switch 與兩個 group entity 必須逐項比對不變。
3. Rule 9–16 需另做 FC03 `0x00C0` count 64 的唯讀確認後才能加。AI 與 special register 不應加。
4. Rule、輸出保持、位址／串口參數的 write enable 必須是另一份安全設計：逐點手冊範圍、readback、rollback、實機風險與 HA authorization 都要另案驗證。尤其 ATS／DO 安全不可由 Modbus 或 profile 取代硬體互鎖。

```text
READ-ONLY INVENTORY: PASS
FULL-MAP DRAFT / VALIDATOR: PASS
PRODUCTION CODE CHANGE REQUIRED: NO
EXISTING FC01 / FC05 / FC15 CONTRACT: UNCHANGED
NEW HOLDING-REGISTER WRITES: UNPROVEN / NOT ENABLED
FINAL (scope of this assessment): PASS
```
