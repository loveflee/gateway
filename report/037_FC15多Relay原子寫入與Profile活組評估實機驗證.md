# FC15 多 Relay 原子寫入與 Profile 活組評估實機驗證

## 人話結論

目前 Gateway 已能用 FC01 一次讀回 8 路 Relay，也已有 FC15 的 RTU ACK 防線與 FC01 verify 分流，但 **RTU GenericAdapter 與 UID3 實際使用的 Modbus TCP Adapter 都還不能建立 FC15 request**。本輪在停止 production Gateway、確保對 `192.168.88.190:502` 只有單一 master 後，對 UID3 執行「讀目前狀態 → FC15 寫回完全相同狀態 → 再讀驗證」。結果為 **DEVICE FC15 SUPPORTED**：8 路原本全 OFF，單一 FC15 request 寫回全 OFF，設備正常回 ACK，前後 bit-for-bit 相同；自行建立的 MBAP 與等價 RTU bytes 均與 PyModbus 3.13.0 oracle 完全一致。FC15 可讓多個 coils 存在於同一筆 Modbus command，不需要也不得使用 FC05 loop；但不能證明機械接點在同一微秒動作，ATS 仍需硬體互鎖。既有 FC05 單路控制可完整保留，群組可由 Profile 活組。建議下一輪施工，但須補齊 TCP/RTU encoder、原生 TCP ACK 驗證、群組 verify 與 validator；BusMaster scheduler 不需重構。總裁決：**PASS WITH CHANGES**。

## 1. 範圍、基準與安全條件

- 依本機 `AGENTS.md`、production code、`profile/config.yaml` 與 `profile/relay_8ch_map.yaml` 審查。
- 本機不是 Git working tree；可確認的同步來源 HEAD 為 `9f253d0c956e4802a5bbb2fd4f6491b8c2ef65f0`（2026-08-12 01:52:24 +0800）。
- 本輪未修改 `src/`、`adapters/`、正式 profile、設定或 HA entity；只新增 `scratch/test_uid3_fc15_probe.py` 與本報告。
- Probe 前 `ss` 只見 `ginlong` 連到 `.190:502`；`py_5f` 使用 `.191:502`。停止 `ginlong` 後再次確認 `.190:502` 無連線，才執行一次 same-state write。
- 測後立即 `docker compose start`；UID1、UID3 均恢復 ONLINE，MQTT/Discovery 正常，容器 restart count=0。

## 2. 施工前架構查證

### GenericAdapter 與 TCP Adapter

`adapters/generic_adapter.py`：

- `encode_write()` 已支援 FC05、FC06、FC16；其他 FC（包含 FC15）拋 `NotImplementedError`。
- `build_verify_read()` 對 `write_fc in (5, 15)` 選 FC01。
- FC01/02 的 expected bytes 為 `(count + 7) // 8`。
- poll decode 對 `bits` 逐一以 `val >> bit` 拆解，因此 `relay_8ch_map.yaml` 的單一 data byte會同步產生 `switch_0`～`switch_7`。

UID3 使用 `adapter: tcp`，所以實際 encoder 是 `adapters/modbus_tcp_adapter.py`。它目前僅支援 FC05/FC06，FC15 同樣缺失；這表示只改 RTU GenericAdapter 不足以讓 UID3 上線。

### Driver ACK contract

`src/driver.py::write()` 已將 RTU FC05/06/15/16 納入 guard。正常 ACK 必須是 8 bytes、UID/FC 正確、CRC 正確，且 `response[2:6] == request[2:6]`。對 FC15，這四 bytes正是 start address＋quantity，不要求 echo byte count 或 coil data，契約正確。合法 `FC=0x8F` exception 為 5-byte RTU frame，CRC 正確時回 `False`。

但 UID3 是原生 Modbus TCP：wire frame 使用 MBAP、無 RTU CRC。現有 guard 主動讓 MBAP 落回 `return True`，故 **production native-TCP FC15 ACK 尚未被 Driver 驗證**。本輪 probe 自己嚴格驗了 transaction ID、protocol ID、MBAP length、UID、FC、start 與 quantity；這不能冒充 production 已具備同一防線。

### BusMaster、HA 與 validator

`BusMaster._process_write()` 在同一個 `async with self.bus_lock` 內依序執行 write/ACK 與 verify read，decode/publish 在 lock 外。因此：

> 對本 Gateway 而言，FC15 寫入與後續驗證可屬於同一個受 bus_lock 保護的操作循環，中間不允許本 Gateway 自己的其他 poll/write 插入。

這不是整個 RS485/TCP 世界的 transaction，也擋不住外部 master。現有 `pending_writes[(uid, key)]` 可用不同 group key 區分命令。HA Manager 已有 `select`，可把命名狀態送到既有 `/set/<key>` command topic；無需建立大型 command framework。現有 validator 不認識 `coil_groups`，也不會驗連續地址、count、state 長度、重複狀態或 overlap。

## 3. PyModbus 標準參照

使用隔離安裝於 `/tmp/fc15-pymodbus-lib` 的 PyModbus 3.13.0，未加入 `requirements.txt`。官方 `WriteMultipleCoilsRequest.function_code` 為 15；`encode()` 產生 start、quantity、byte count 與 packed bits，最大 count 檢查為 2000。`pack_bitstring()` 按每個 byte 的低位到高位對應先到後的 coils；不足 8 bits 的高位補 0。正常 response 只 encode start＋quantity。參照：[PyModbus 3.11.3 API](https://pymodbus.readthedocs.io/en/v3.11.3/source/library/pymodbus.html)、[PyModbus 3.6.9 FC15 說明](https://pymodbus.readthedocs.io/en/v3.6.9/source/library/pymodbus.html)、[PyModbus 官方 repository](https://github.com/pymodbus-dev/pymodbus)。

隔離 byte tests 對 2、3、4、8 coils 均得到 `self-built MBAP == oracle` 與 `self-built RTU == oracle`。另注意 PyModbus packing 會對短 list 就地補齊；probe 傳入副本，避免污染安全用的 before-state。

## 4. UID3 實機 FC15 Probe 原始數據

實際線路是 **Modbus TCP/MBAP**，所以 wire frame 沒有 CRC。為滿足 RTU codec 審查，另列同一 PDU 的等價 RTU frame 與 CRC。

### FC01 Before

```text
TX: FC 01 00 00 00 06 03 01 00 00 00 08
RX: FC 01 00 00 00 04 03 01 01 00
States coil 0..7: OFF OFF OFF OFF OFF OFF OFF OFF
```

解析：transaction=`FC01`、protocol=`0000`、length=`0006/0004`、UID=`03`、FC=`01`、byte count=`01`、data=`00`。

### FC15 same-state request

```text
Start Address: 0
Quantity:      8
Byte Count:    1
Packed Data:   00 (coil 0 位於 bit0，LSB-first)

Gateway MBAP:
FC 15 00 00 00 08 03 0F 00 00 00 08 01 00
PyModbus MBAP oracle:
FC 15 00 00 00 08 03 0F 00 00 00 08 01 00
byte-for-byte: SAME

Gateway 等價 RTU:
03 0F 00 00 00 08 01 00 7F 4C
PyModbus RTU oracle:
03 0F 00 00 00 08 01 00 7F 4C
byte-for-byte: SAME
RTU CRC (wire order): 7F 4C
```

### FC15 Response

```text
RX: FC 15 00 00 00 06 03 0F 00 00 00 08
Transaction ID: FC15
Protocol ID:    0000
Length:         0006
UID:            03
FC:             0F
Start Address:  0000
Quantity:       0008
```

這是正常 FC15 ACK，不是 `8F 01` exception。Probe 完整驗證 MBAP identity/length、UID、FC、start 與 quantity。RTU CRC 不適用於這條 MBAP wire response。

### FC01 After

```text
TX: FC 02 00 00 00 06 03 01 00 00 00 08
RX: FC 02 00 00 00 04 03 01 01 00
States coil 0..7: OFF OFF OFF OFF OFF OFF OFF OFF
before == after: YES
```

```text
DEVICE FC15 SUPPORTED = YES
```

符合 PASS 的 request/oracle、正常 ACK、identity/start/quantity 與 before/after 全部條件。測試只能證明多 coils 在同一筆 FC15 request 中送達設備；不能證明機械 relay 同一微秒吸合。ATS electrical interlock 必須由硬體保障。

## 5. Profile 活組方案裁決

### Value 表示法比較

- **A：bool array**：最通用，直接對應 FC15；但目前 MQTT consumer 傳入 scalar string，HA 也沒有原生 bool-array control，verify/publish 需定義 canonical JSON，操作安全性較差。
- **B：Profile 命名狀態**：最適合 ATS。HA 可沿用 `select`；未列出的 `[ON, ON]` 根本不暴露。Adapter 只理解連續 coils 與 bool vector，不寫死 ATS 規則。
- **建議：B 為正式 UI/API，A 僅作 Adapter 內部標準形態與 isolated test input。** 不建立新 command framework。

建議最小 schema（報告範例，未建立正式 map2）：

```yaml
coil_groups:
  ats_group:
    start_addr: 0
    count: 2
    members: [switch_0, switch_1]
    verify_command_id: read_coils
    states:
      all_off: [OFF, OFF]
      path_a:  [ON,  OFF]
      path_b:  [OFF, ON]

B2_SETTING:
  - key: ats_group
    name: ATS 路徑
    ha:
      type: select
      options: [all_off, path_a, path_b]
      state_key: ats_group
```

規則：`count == len(members) == len(each state.values)`；members 必須依地址連續排列，合法長度至少涵蓋 2、3、4、8；values 僅接受 bool 或明確 ON/OFF。Adapter 將命名狀態解析成 bool vector，一次建立 FC15，禁止 FC05 loop，也禁止隱性 read-modify-write。`verify_command_id` 指向既有完整 8-coil read；decode 共用原本 bit decoder、更新全部單路 switch，再從 group slice反查命名狀態供 BusMaster 比對。

### 四種配置

以下只替換 `coil_groups`；原有八個 FC05 `settings` 全部保留。

配置一：01 群組，其餘單路 FC05：

```yaml
coil_groups:
  group_01: {start_addr: 0, count: 2, members: [switch_0, switch_1], states: {off: [OFF, OFF], on: [ON, ON]}}
```

配置二：01／23／45／67 四組：

```yaml
coil_groups:
  group_01: {start_addr: 0, count: 2, members: [switch_0, switch_1], states: {off: [OFF, OFF], on: [ON, ON]}}
  group_23: {start_addr: 2, count: 2, members: [switch_2, switch_3], states: {off: [OFF, OFF], on: [ON, ON]}}
  group_45: {start_addr: 4, count: 2, members: [switch_4, switch_5], states: {off: [OFF, OFF], on: [ON, ON]}}
  group_67: {start_addr: 6, count: 2, members: [switch_6, switch_7], states: {off: [OFF, OFF], on: [ON, ON]}}
```

配置三：0～7 一組：

```yaml
coil_groups:
  all_relays:
    start_addr: 0
    count: 8
    members: [switch_0, switch_1, switch_2, switch_3, switch_4, switch_5, switch_6, switch_7]
    states: {all_off: [OFF, OFF, OFF, OFF, OFF, OFF, OFF, OFF], all_on: [ON, ON, ON, ON, ON, ON, ON, ON]}
```

配置四：012／3456，7 維持單路：

```yaml
coil_groups:
  group_012: {start_addr: 0, count: 3, members: [switch_0, switch_1, switch_2], states: {off: [OFF, OFF, OFF], on: [ON, ON, ON]}}
  group_3456: {start_addr: 3, count: 4, members: [switch_3, switch_4, switch_5, switch_6], states: {off: [OFF, OFF, OFF, OFF], on: [ON, ON, ON, ON]}}
```

離散的 `0,3,7` 不能表示成只寫三顆的 FC15；若 start=0、quantity=8，就必須明確提供中間全部 coils 的目標狀態。

### Overlap

技術上不同 group key 可進入 `pending_writes`，BusMaster 會依序執行，最後一筆影響重疊 coils；完整 FC01 verify 可同步八個單路 HA state。但 group A 的狀態可能被 group B 改成未命名組合，且跨 publisher 的 command collision 無法由 bus lock 消除。第一版沒有必要承擔此模糊性，建議 validator **禁止 group overlap**；後續若真有需求，再另案定義 priority、UNKNOWN state及跨群組同步，不應默許。

## 6. 建議的最小正式施工

1. `adapters/generic_adapter.py`：加入純粹的 LSB-first coil packer、RTU FC15 encoder、group schema解析、完整板 FC01 verify context與 group state反查；保留所有 FC05 行為。
2. `adapters/modbus_tcp_adapter.py`：以同一 PDU/packer 建立 MBAP FC15；不能只改 RTU GenericAdapter，因 UID3 走此 override。
3. `src/modbus_tcp_driver.py`：只在 native TCP subclass 加嚴格 MBAP write ACK/exception驗證（transaction/protocol/length/UID/FC/start/quantity）。`src/driver.py` 的 RTU guard **NO CHANGE**。
4. `src/map_validator.py`：驗 `coil_groups` 型別、唯一 key、連續 members、count/state長度、bool token、state vector唯一性、verify command存在、地址範圍與禁止 overlap。
5. `profile/relay_8ch_map2.yaml`：由原圖複製，保留八個 FC05，另加群組與對應 HA select；原 `relay_8ch_map.yaml` 不動。
6. 測試：2/3/4/8 packing、跨 byte、非法/離散/overlap schema、RTU/MBAP oracle、正常/exception/錯 tx/UID/FC/start/quantity/length ACK、完整 FC01 verify、FC05 byte regression與 BusMaster lock時序。

`src/bus_master.py = NO CHANGE`：既有 lock、retry、write budget 與 publish flow 足夠；Adapter 應讓 group verify decode 同時回傳八個 switch state及 group命名狀態，使現有 `_values_equal(decoded[key], value)` 成立。`src/ha_manager.py = NO CHANGE`：採既有 select 與 command topic。不得用 FC05 loop 模擬 FC15。

## 7. 限制與未測項

- 本輪只做全 8 路「寫回原狀」一次；沒有故意切換 relay，也未測 2/3/4 實機 group，後者目前只有 byte oracle PASS。
- 未量測機械接點 timing；Protocol 無法提供此保證。
- 沒有測外部 master 同時競爭；probe 刻意建立單一 master。
- Production FC15 尚未施工，故 HA group command、BusMaster production verify及 native TCP production ACK guard 都是 NOT TESTED。
- Probe 的 MBAP response無 CRC；報告所列 `7F 4C` 是同一 PDU 的等價 RTU oracle，不是線上 response CRC。

## 8. 最終裁決

1. UID3 是否實機支援 FC15？ **YES**
2. FC15 request 是否與 PyModbus byte-for-byte 一致？ **YES**（MBAP 與等價 RTU 均一致）
3. FC15 ACK 是否完整驗證？ **YES（本輪 probe）／NO（現有 production native-TCP path）**
4. FC01 verify 是否 bit-for-bit 成功？ **YES**
5. 是否需要 FC05 loop？ **NO**
6. 是否可以建立 Profile 活組？ **YES**
7. GenericAdapter 是否需要修改？ **YES**
8. Driver 是否需要修改？ **YES，僅 native `modbus_tcp_driver.py`；RTU `driver.py` NO CHANGE**
9. BusMaster 是否需要修改？ **NO**
10. 是否建議進入下一輪正式施工？ **YES**

總裁決：**PASS WITH CHANGES**。先完成上述最小實作與 isolated regression，再建立 `relay_8ch_map2.yaml`；未經另輪批准不得切換 production profile。
