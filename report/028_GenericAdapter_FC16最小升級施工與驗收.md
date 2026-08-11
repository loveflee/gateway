# 028 — GenericAdapter FC16 最小升級施工與驗收

日期：2026-08-11  
依據：[026 唯讀審查](/root/py_ginlong/report/026_GenericAdapter_PyModbus標準能力唯讀審查.md) 與 [027 敵對式獨立複驗](/root/py_ginlong/report/027_026獨立敵對複驗.md)  
施工範圍：**只修改 `adapters/generic_adapter.py`。** 未修改 profile、Driver、BusMaster、Docker、MQTT／HA 或現場設備。

## 人話結論

1. **這次改了什麼？** 修正了超出 16-bit 範圍會被偷偷改值的問題，並讓 FC16 能在設定檔既有資訊足夠時寫入 32-bit 值。
2. **FC16 多了什麼？** 新增 `uint32`、`int32`、`float32` 的 quantity=2 寫入與完整回讀驗證；支援既有 `big`、`little`、`swap`／`word_swap`、`byte_swap` 定義。
3. **舊設備有影響嗎？** 沒有。修改前後的 legacy snapshot 完全相同；正式 Solis 五個設定仍是 FC16 quantity=1，沒有進入新路徑。
4. **測試結果？** **PASS WITH LIMITATIONS**。所有隔離、PyModbus oracle、ACK、verify、錯誤輸入與 027 指定的雙 master regression 測試均通過。
5. **刻意沒做什麼？** 沒有 FC15、FC22、FC23、64-bit、map validator 收緊、新 profile 欄位或 PyModbus production dependency。
6. **尚未實機驗證？** 沒有向實機寫入任何 32-bit register；因沒有已確認安全的 32-bit writable register，這一項為 **NOT TESTED**，不是失敗。
7. **是否建議 Claude Code 獨立完工驗收？** **YES**。

---

## 1. 動工前工作清單

| 項目 | 原計畫 | 實際結果 |
|---|---|---|
| Git / 前提 | 重讀 main、026、027、Adapter，確認敵對複驗的 legacy verify 限制。 | 完成。遠端與本機 main 均為 `fc45e9e82dfb410ef82fd22a217a4156a580d864`。|
| 修改檔案 | 僅 `adapters/generic_adapter.py`。 | 完成，production diff 僅此檔。|
| P0 | 將 overflow、NaN、`±Inf` 統一拒絕為 `ValueError`。 | 完成。|
| P1 | FC16 quantity=2：`uint32`／`int32`／`float32`；沿用 metadata。 | 完成。|
| 027 強制條件 | multi-register verify 才可 strict；legacy count=1 不可收緊。 | 完成，pre/post snapshot 完全相同。|
| 不修改 | Driver、BusMaster、TCP Driver、validator、HA/MQTT、profiles、Docker、timing／state machine。 | 完成，未修改。|
| 施工前基準 | 保存 legacy request bytes 與 1/2/6-register verify 行為。 | 完成：[prechange JSON](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/prechange_legacy_baseline.json>)。|

## 2. 實際修改

唯一 production 修改為 [generic_adapter.py](/root/py_ginlong/adapters/generic_adapter.py)。Public API 未變：

- `encode_write(key, value)`
- `build_verify_read(key)`
- `build_poll_read()`
- `decode(raw_data, context)`

新增 private helper 與目的：

| Helper | 用途 | 對應缺口 |
|---|---|---|
| `_find_setting()` | 集中原有 setting/address 查找。 | 最小化重複，不改 schema。|
| `_resolve_write_sensor()` | metadata 依 `link_sensor` 優先、同名 sensor fallback。 | 026 G2；027 要求涵蓋兩路。|
| `_resolve_fc16_codec()` | 只允許明確的 4-byte `uint32`／`int32`／`float32`；2-byte 或無 metadata 回 legacy。 | G1/G2。|
| `_coerce_legacy_16bit()` | 支援既有 `int16`/`uint16` 合法值，拒絕 wraparound、NaN/Inf。 | G3。|
| `_apply_write_word_order()` | 現有 32-bit read transform 的反向（各轉換為自反）。 | G2。|
| `_encode_fc16_32bit_value()`、`_build_fc16_request()` | 產生 quantity=2 / byte_count=4 的標準 FC16 RTU ADU。 | G1/G2。|

`build_verify_read()` 僅在新 codec 啟用時，於既有 internal context 放入 `strict_verify`、預期 data bytes、quantity 與 codec；`decode()` 只在該 context 做 exact byte-count 驗證。`_extract_data()` 在新路徑讀完整四 bytes 並用同一 codec 解回數值。

**027 修正已採納：** legacy context 沒有 `strict_verify`，仍走原本 `_verify_modbus_frame(expected_fc=...)` 與 `raw_data[3:5]` 第一格解碼，沒有把雙 master 長 frame 改判成 physical failure。

## 3. Diff 範圍與既有 dirty state

施工前 `adapters/`、`src/` 均無未提交差異。施工後：

- 本輪 production diff：`adapters/generic_adapter.py`，且 `git diff --check` 通過。
- 明確未修改：`src/driver.py`、`src/bus_master.py`、`src/modbus_tcp_driver.py`、`src/map_validator.py`、`requirements.txt`、所有 production profile、Docker files。
- 施工前既有、未碰觸的 dirty state：`profile/config.yaml`、`report/021...`；未追蹤的 `profile/6k2*.yaml` 與 022–027 報告。這些不屬於本輪 diff，未被覆蓋、清除或還原。
- 本輪新增的非 production 產物只在隔離工作目錄 `test/fc16_upgrade/`，以及本報告。

## 4. FC16 新能力

| 能力 | 結果 |
|---|---|
| `uint32` | FC16 quantity=2，PASS。|
| `int32` | FC16 quantity=2，PASS。|
| `float32` | FC16 quantity=2，PASS；含 scale=10 回讀除回 scale。|
| `big` / 預設 | PASS。|
| `little` | PASS，套用既有 32-bit read 的全 byte reverse 語意。|
| `swap` / `word_swap` | PASS，兩個 16-bit words 互換。|
| `byte_swap` | PASS，每一個 16-bit word 內 byte 互換。|
| `link_sensor` | PASS（以 `int32` + `word_swap` 顯式測試）。|
| 同名 fallback | PASS（`uint32`、`float32` 與正式 Solis 16-bit metadata）。|

metadata 無法明確推導時保持 legacy FC16 count=1；看到明確但不在本輪授權的 4-byte datatype（例如 `uint64`）或不支援 order（例如 32-bit `dcba`）時，以 `ValueError` 拒絕，不猜測、不靜默截斷。

## 5. Legacy Compatibility

修改前／後 baseline JSON 完全相同（檔案內容 byte-identical）：

| 項目 | 結果 |
|---|---|
| FC01–04 request / decode | 相同。|
| FC05 ON | `01 05 00 64 FF 00 CD E5`，相同。|
| FC05 OFF | `01 05 00 64 00 00 8C 15`，相同。|
| FC06 `0x1234` | `01 06 00 65 12 34 94 A2`，相同。|
| FC06 `int16 -2` | `01 06 00 65 FF FE 59 A5`，相同。|
| FC16 count=1 `0x1234` | `01 10 00 66 00 01 02 12 34 A2 E1`，相同。|
| legacy FC16 verify_count omitted / `=1` | 對合法 1、2、6-register reply 均仍接受，僅解第一 register，**UNCHANGED**。|

[修改後 capture](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/postchange_legacy_capture.json>) 與 [修改前 baseline](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/prechange_legacy_baseline.json>) 已由 `cmp -s` 驗證一致。

另以容器中的正式 `solis_inverter_map.yaml` 做唯讀、記憶體內靜態編碼檢查：五個現有 setting 均為 `quantity=1`、`byte_count=2`、`strict_verify=false`；沒有被新 codec 切換。結果在 [production Solis 靜態檢查 JSON](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/production_solis_static_encode.json>)。

## 6. PyModbus Oracle

PyModbus 只使用隔離官方 source snapshot `14bff79ab5a0a6fd5fb04abf9d8dde6cc0ee1b4b`，沒有加入 `requirements.txt` 或 production image。比較層為：

```text
UID + official PyModbus PDU.encode() + GenericAdapter RTU CRC16
```

| Case | GenericAdapter bytes | 結果 |
|---|---|---|
| FC05 ON/OFF、FC06、FC16 count=1 | 與 oracle 完全相同 | PASS |
| FC16 uint32 big `0x12345678` | `01 10 00 64 00 02 04 12 34 56 78 8F 40` | PASS |
| FC16 int32 `-2` + `word_swap` via link_sensor | `01 10 00 64 00 02 04 FF FE FF FF A4 20` | PASS |
| FC16 float32 `1.5` big | `01 10 00 64 00 02 04 3F C0 00 00 F8 5C` | PASS |
| uint32 little / swap / word_swap / byte_swap | `78563412` / `56781234` / `56781234` / `34127856` register payload | PASS |

測試程式：[run_fc16_upgrade_validation.py](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/run_fc16_upgrade_validation.py>)；完整 hex/result：[驗收 JSON](</root/Documents/Codex/2026-08-09/root-ginlong-master-1-2-report/test/fc16_upgrade/fc16_upgrade_validation.json>)。

## 7. Multi-register Verify

隔離 loopback 以實際 GenericAdapter + 現有 Driver 執行：

```text
FC16 float32 write quantity=2
→ Driver 正確 ACK（address + quantity echo）
→ FC03 verify read quantity=2
→ strict exact byte count=4
→ float32 完整 decode
→ 1.5
```

關鍵 frame：

```text
write            01 10 00 64 00 02 04 3F C0 00 00 F8 5C
ACK              01 10 00 64 00 02 00 17
verify request   01 03 00 64 00 02 85 D4
verify response  01 03 04 3F C0 00 00 F6 1B
```

錯 quantity ACK 由既有 Driver 拒絕；沒有修改 ACK guard。multi-register verify 的 wrong byte count（短與長）、wrong UID、wrong FC、bad CRC、Modbus Exception 全部成為 `DataDecodeError`。

## 8. Negative Tests

| 項目 | 實際結果 |
|---|---|
| `70000` / `-70000`（FC06、legacy FC16） | `ValueError`，不再 `& 0xFFFF`。|
| `NaN`、`+Inf`、`-Inf` | `ValueError`。|
| 4-byte `uint64` metadata | `ValueError`（本輪不支援）。|
| 32-bit `dcba` metadata | `ValueError`（現有 32-bit read 沒有此定義，不猜測）。|
| strict verify short/long byte count | `DataDecodeError`。|
| wrong UID / FC / CRC / Exception | `DataDecodeError`。|
| wrong verify value | 正確 decode 為不同值；float case 差異大於 BusMaster `0.01` tolerance。|

以實際 `BusMasterScheduler._process_write()` 搭配 fake driver/HA 驗證 `ValueError`：現有 [bus_master.py:239](/root/py_ginlong/src/bus_master.py:239) 的 exception handler 只記錄並 return，不會送出 driver request，也不改變 online/offline state。

## 9. 雙 master Regression

**UNCHANGED**

027 指定的 `verify_count` 未設／`=1` regression 已以前後 snapshot 重現：

| 合法 FC03 verify reply | 修改前 | 修改後 |
|---|---|---|
| 1 register | 接受，解第一格 | 相同 |
| 2 registers | 接受，解第一格 | 相同 |
| 6 registers | 接受，解第一格 | 相同 |

因此原本在刻意雙 master 壓力下會落入「回讀值不符、設備維持 ONLINE」的 legacy path，沒有被本輪新 strict 檢查轉成 physical failure。strict validation 僅存在於新 32-bit context。

## 10. 實機驗證

| 項目 | 結果 |
|---|---|
| Legacy 實機 Modbus write/read | **NOT TESTED** — 本輪未對 `192.168.106.14` 或任何設備傳送封包；只做 profile 靜態編碼。|
| 新 FC16 32-bit 實機 write/read | **NOT TESTED — 無已確認安全的 32-bit writable register。** 未為追求結果而寫未知 register。|

所有新能力已有 PyModbus byte oracle、實際本機 Driver ACK loopback 與完整 verify codec 證據。未來取得明確安全的 32-bit writable device/register 後，才可另行授權實機驗收。

## 11. 對外可觀測性

| 項目 | 結果／理由 |
|---|---|
| HA entity / MQTT topic / Discovery / state / availability / command payload | 不變；未改 HA、MQTT、profile key 或 BusMaster。|
| value map / scale | legacy 不變；新 float32 scale=10 已測 write raw 值與 verify 回復使用者值。|
| poll rotation / polling | 不變；未改 poll path。|
| timeout / retry / write budget / offline-online | 不變；未改 Driver／BusMaster。無效寫入只走既有 encode exception handler。|
| write → ACK → verify 順序 | 不變；新 path 僅使 verify count/codec 正確。|
| 正常 INFO logging / traffic log / CRC / ACK guard / inter-frame delay | 不變；未修改相應模組。|
| 雙 master | legacy verify 處置 UNCHANGED；雙 master 本身仍是刻意壓力測試，不列缺失。|

## 12. 未納入項目

- map_validator 收緊：下一輪獨立處理。
- `uint64` / `int64` / `float64`：不做。
- FC15：不做。
- FC22：不做。
- FC23、FC43、vendor-specific FC：不做。
- 新 production profile metadata：不新增。
- PyModbus production dependency：不加入。

## 13. 最終裁決

**PASS WITH LIMITATIONS**

P0/P1 的授權範圍已完成，legacy requests 與 027 指定的 verify 行為保持不變，所有隔離驗收通過。限制僅是未對實機寫入 32-bit register，原因是沒有已確認安全的測試地址。

目前是否建議將本次實際修改交給 Claude Code 進行敵對式獨立完工驗收？

**YES**
