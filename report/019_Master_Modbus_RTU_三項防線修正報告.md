# Master Modbus RTU 三項防線修正報告

修正日期：2026-08-08

基準：`b930201 Add Traditional Chinese README`。
依據：[018 Master Modbus RTU 唯讀審查](018_Master_Modbus_RTU_唯讀審查.md)。

## 總裁決：ADOPT

三項協定防線已補齊；正常 Master 資料流、排程、HA／MQTT 對外資料契約維持不變。

| 驗收項目 | 結果 |
|---|---|
| Exception Response | PASS |
| Request count／response length 約束 | PASS |
| Write ACK validation | PASS |
| 正常 read regression | PASS |
| 正常 write + verify read regression | PASS |
| HA／MQTT 對外行為一致 | PASS（靜態路徑核對） |

## 先驗證、後修改的流程

本次沒有直接在現場設備試錯。

1. 先將隔離測試改為「修正後才應通過」的驗收條件，production 尚未修改時執行。
2. 基線共 18 個測試；正常 read、正常 ACK、TCP 碎包、前後 garbage、壞 CRC 重同步、正常 write + verify read 均通過。
3. 基線有 7 個預期失敗子案例，且只對應三項既知缺口：
   - FC03／FC04 Exception 均被誤報為「找不到合法 CRC」。
   - 未填 `response_len` 時，FC03 count=1 仍接受 ByteCount=4 的合法 CRC response。
   - UID、FC、CRC、echo 任一錯誤的 write ACK 都被當成成功。
4. 先以容器內 Python 唯讀掃描既有 profile，所有已宣告的 FC01–04 `response_len` 與 request count 推導值一致：`declared_response_len_mismatches=0`。
5. 僅在上述基線與 profile 前置核對完成後，才修改 production。
6. 因目前 `profile/config.yaml` 使用 `driver.type: tcp`，提交前再新增 TCP subclass 一致性測試；它證明 `AsyncModbusTcpDriver.write()` 仍會繞過 ACK 驗證。經授權擴大範圍至 `src/modbus_tcp_driver.py` 後，以父類委派修正。
7. 最終隔離驗收 20/20 通過。

## 修改清單與路徑

### 1. Exception Response 精準分類

檔案：[`adapters/generic_adapter.py`](../adapters/generic_adapter.py)

- `_prebuild_poll_cmds()` 保留既有 command context，沒有改 map 格式。
- Deep CRC Radar 保留原本的 `find → 推長度 → CRC → start_idx + 1` 滑動模式；只增加第二種候選 header：`[UID][expected_fc | 0x80]`。
- Exception 仍須通過同 UID、正確 Exception FC、固定 5 bytes 與 CRC16；通過後轉為：

  ```text
  Modbus Exception: UID=7 FC=03 Code=02
  ```

  位置：[`generic_adapter.py:346`](../adapters/generic_adapter.py#L346)–[`390`](../adapters/generic_adapter.py#L390)。

- 上層 `BusMasterScheduler` 未修改；Exception 仍沿既有 DataDecodeError failure／retry／availability 政策處理，只是日誌不再誤稱 CRC／找不到 frame。

### 2. Request count 對 response length 的約束

檔案：[`adapters/generic_adapter.py`](../adapters/generic_adapter.py)

- FC01／FC02 自動推導：`expected_data_bytes = ceil(count / 8)`、`expected_response_len = 5 + expected_data_bytes`。
- FC03／FC04 自動推導：`expected_data_bytes = count * 2`、`expected_response_len = 5 + expected_data_bytes`。
- 既有 profile 若有 `response_len`，繼續保留該值；未設定時才使用協定推導值。兩者在目前 map 的前置核對結果為一致。
- `_verify_modbus_frame()` 額外檢查 response ByteCount 是否等於本次 request 的 data byte count；不相符時明確拋出 `Response quantity mismatch`。

位置：[`generic_adapter.py:56`](../adapters/generic_adapter.py#L56)–[`91`](../adapters/generic_adapter.py#L91)、[`410`](../adapters/generic_adapter.py#L410)–[`450`](../adapters/generic_adapter.py#L450)。

### 3. Write ACK 完整驗證

檔案：[`src/driver.py`](../src/driver.py)

- 新增小型 Modbus CRC16 驗證函式，僅供 ACK 判斷使用。
- 對 FC05／FC06／FC15／FC16 的正常 ACK，必須同時通過：8-byte 長度、UID、FC、CRC16 little-endian、`response[2:6] == request[2:6]`。
  - FC05／FC06 的後四 bytes 是 address + value。
  - FC15／FC16 的後四 bytes 是 start address + quantity。
- 合法 5-byte Exception ACK（`FC | 0x80`）且 CRC 正確時維持既有 API：`write()` 回傳 `False`；壞 CRC／UID／FC／長度／echo 改為 `DriverTimeoutError`，絕不再回成功。

位置：[`driver.py:30`](../src/driver.py#L30)–[`38`](../src/driver.py#L38)、[`285`](../src/driver.py#L285)–[`311`](../src/driver.py#L311)。

### 4. TCP Driver 路徑一致性

檔案：[`src/modbus_tcp_driver.py`](../src/modbus_tcp_driver.py)

目前設定走 `driver.type: tcp`，此類別原本自己覆寫 `write()` 並盲目回 `True`。為避免重複第二套 CRC／echo 邏輯，改成一行委派：

```python
return await super().write(payload)
```

因此 TCP 與 RTU 傳輸路徑共用完全相同的 ACK 契約。位置：[`modbus_tcp_driver.py:24`](../src/modbus_tcp_driver.py#L24)–[`26`](../src/modbus_tcp_driver.py#L26)。

### 未修改的 production 路徑

- `src/bus_master.py`：未修改 polling 排程、timeout、retry、online/offline 規則。
- `src/main.py`、`src/ha_manager.py`、`src/mqtt_client.py`：未修改。
- `profile/*.yaml`、HA entity ID、MQTT topic、Discovery payload schema、正常 state payload：未修改。
- Driver idle 收包與 Deep CRC Radar 的滑動重同步演算法：未重寫。

## 驗證過程與結果

隔離測試檔：[`scratch/master_rtu_audit_20260808.py`](../scratch/master_rtu_audit_20260808.py)

執行：

```bash
python3 scratch/master_rtu_audit_20260808.py
```

最終結果：**20/20 OK**；其中的 TCP test 只建立暫時 `127.0.0.1` server，未連接任何現場設備。

| 測試群組 | 覆蓋內容 | 結果 |
|---|---|---|
| 正常 response | FC01、FC02、FC03、FC04、FC05、FC06、FC15、FC16；UID／FC／ByteCount／固定長度／CRC | PASS |
| Exception read | FC03 `0x83`、FC04 `0x84` 正確分類；CRC 錯 Exception 不接受 | PASS |
| quantity 約束 | count=1 收到兩個 register 拒絕；FC03 count=12 接受 24 data bytes；FC01／02 count=9 正確接受 2 data bytes | PASS |
| ACK normal | FC05、FC06、FC15、FC16 正常 echo ACK | PASS |
| ACK Exception | FC06 `0x86`、FC16 `0x90` 合法 Exception 回傳 `False` | PASS |
| ACK corruption | UID、FC、CRC、address、value、FC16 quantity 任一錯誤均拒絕 | PASS |
| Radar regression | 前後 garbage、壞 CRC candidate 後重同步、錯 UID、錯 FC、半幀／尾段、兩幀黏包 | PASS |
| Driver regression | 30ms TCP 碎包完整收取；130ms gap 的既有 idle 邊界行為維持 | PASS |
| 正常 write regression | 正常 write → verify read → `HA.publish_state()` | PASS |
| TCP subclass parity | `AsyncModbusTcpDriver` 正常 ACK、Exception、壞 ACK 與 RTU 父類一致 | PASS |

另以 `python3 -m py_compile` 編譯：`src/driver.py`、`src/modbus_tcp_driver.py`、`adapters/generic_adapter.py` 與隔離測試，通過；`git diff --check` 通過。

## 對外可觀測性核對

正常封包的 flow 未改：

```text
Driver raw response → Adapter decode → HAManager.publish_state → 原 state topic
```

本次條件僅在異常 response 觸發：

- 合法 Exception：錯誤訊息從泛化「找不到合法 CRC」改為明確的 `Modbus Exception`。
- quantity 不符：拒絕並留下 `Response quantity mismatch`。
- 寫入 ACK 異常：不再回 `True`，留下具體 `Write ACK ... mismatch` 原因。

因 `BusMasterScheduler`、`HAManager`、`RobustMQTTClient`、profile 與 Discovery 均未變更，正常設備的 HA entity、MQTT topic、Discovery、state payload、availability 邏輯與 polling interval 保持一致。

## 修改規模

| 檔案 | 新增 | 刪除 |
|---|---:|---:|
| `adapters/generic_adapter.py` | 47 | 10 |
| `src/driver.py` | 37 | 13 |
| `src/modbus_tcp_driver.py` | 7 | 25 |
| **production 合計** | **91** | **48** |

production diff churn 為 **139 行**，在 100–150 行上限內。`bus_master.py` 未改；隔離測試另有 185 新增、15 刪除。

## 部署狀態

本次只完成程式碼、隔離驗證與 Git 提交前檢查；**尚未重啟容器或部署到現場設備**。合併／拉取後，因 `src/` 為 bind mount，可由：

```bash
./restart.sh
```

載入新程式。部署後應以 `docker logs -f ginlong` 觀察正常輪詢，以及異常時的精準 Exception／ACK 訊息。
