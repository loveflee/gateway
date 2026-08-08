# Master Modbus RTU 收包／解包唯讀審查

審查日期：2026-08-08
範圍：`src/driver.py`、`src/bus_master.py`、`adapters/generic_adapter.py`。
排除：Listen、JKBMS、USB 旁聽與 production 修改。
基準：commit `0b900a2`。

## 總評：FAIL

標準且完整的讀取回覆、前後雜訊、CRC 錯誤候選及不同 UID／FC 的重同步均可正確處理；不應重寫 Deep CRC Radar 或 Driver 架構。

但目前不只一個 Exception 小缺口：

1. **P1 — 讀取 Exception Response 無法被辨識。** `decode()` 僅搜尋 `[UID][expected_fc]`，不搜尋 `[UID][expected_fc | 0x80]`。因此合法的 `07 83 02 CRC_L CRC_H` 根本不會成為候選；第 342 行的 Exception 分支在此路徑不可達。
2. **P1 — 對未設定 `response_len` 的讀取，未驗證本次 request 的 count。** Radar 驗的是 ByteCount 與候選幀物理長度相符，不是 ByteCount 是否等於這次送出的 quantity。`profile/solis_inverter_map.yaml` 的 15 個 read command 沒有 `response_len`，所以同 UID、同 FC、CRC 正確但資料量不同的回覆可通過並發布部分資料。
3. **P2 — 實際寫入 ACK 不走 Adapter 的 UID／FC／echo／CRC 驗證。** `driver.write()` 只特判 5-byte Exception；任何其他 raw bytes 都回傳 `True`。後續 verify read 通常會攔住錯誤結果，故不是立即的錯誤寫入成功，但雜訊 ACK 會被視為已確認並多發一次 verify read。

因此不能下結論「只需要修 Exception」。最小安全修正應保留現有架構，補上 Exception、由既有 request 計算讀取預期長度，以及驗證 write ACK。

## 逐項裁決

| 項目 | 裁決 | 證據／說明 |
|---|---|---|
| Driver 收包 | 條件 PASS | 第一段以 `timeout` 等待，後續以 `idle_timeout=0.1s` 收集；可處理 30ms TCP 碎包。若兩段 TCP 資料間隔超過 100ms，driver 會把前段視為完整 raw response；這是正確性門檻，不只是效能取捨。對守規範 RTU 且 gateway 不產生 >100ms TCP chunk gap 的部署可保持不動。 |
| Deep CRC Radar | PASS（有兩項語意缺口） | 搜尋同 UID、同預期 FC，依 FC/ByteCount 推長度，CRC 失敗滑動一 byte 後繼續；可略過前置／後置垃圾、壞 CRC、錯 UID、錯 FC。Exception 與 request count 的問題見下。 |
| UID 驗證 | PASS（讀取） | header 先鎖 UID，`_verify_modbus_frame()` 再驗 UID。不同 UID 合法幀不會被採用。 |
| FC 驗證 | PASS（正常讀取） | header 先鎖預期 FC，第二層再驗。Exception FC 不被搜尋，故 Exception 分項 FAIL。 |
| ByteCount | PARTIAL / P1 | 對 FC01/02/03/04，驗證 ByteCount 等於選定幀的實際 data 長度；僅 profile 填 `response_len` 時才間接驗到 request 所需長度。無 `response_len` 時缺 request count 比對。 |
| CRC16 | PASS（Adapter read path） | Radar 與 `_verify_modbus_frame()` 都以 Modbus CRC16 little-endian 驗證。雙重驗證成本極低，屬合理 defense-in-depth，建議保留。寫 ACK 路徑除外。 |
| Exception Response | FAIL / P1 | 合法 FC03 `0x83`、FC04 `0x84` 回覆不能被 Radar 找到，最終為泛化的 DataDecodeError，而非 Modbus Exception。 |
| 抗前後雜訊 | PASS | 合法候選可由 raw bytes 中抽出；後置垃圾不影響已選幀。 |
| 錯位重同步 | PASS（正常 FC） | 壞 CRC 候選後以 `start_idx + 1` 繼續，能找到後方同 UID/FC 的合法幀。 |

## 協定逐項反查

### FC01、FC02、FC03、FC04

正常 response 為：`UID | FC | ByteCount | Data | CRC_L | CRC_H`。

- Radar 對 FC 1/2/3/4 用 `3 + ByteCount + 2` 計算候選長度：`adapters/generic_adapter.py:344-346`。
- 第二層驗 UID、FC、ByteCount 與 CRC：`adapters/generic_adapter.py:389-413`。
- 從協定 framing 角度，標準完整 response 為 PASS。
- 從「此 response 是否屬於這個 request」角度，只有 `expected_len` 有值時才完整；否則 response data 長度未被 request quantity 約束。

### FC05、FC06、FC15、FC16

正常 response 為：`UID | FC | Echo Start Address | Echo Value/Quantity | CRC_L | CRC_H`，固定 8 bytes。

- Adapter Radar 與 `_verify_modbus_frame()` 對這四個 FC 都以 8 bytes 處理：`adapters/generic_adapter.py:347-348,405-409`；隔離測試的 parser framing 為 PASS。
- 但 production 寫入使用 `driver.write()`，不呼叫 `adapter.decode()`。它只識別合法 5-byte Exception，其他任何回覆均回傳 `True`：`src/driver.py:271-287`。所以 active write path 並未驗 UID、FC、8-byte 固定長度、echo address/value 或 CRC，為 P2。

## Exception Response：已證明的缺口與影響

預期讀 FC03 時，合法 Exception 是：`UID | 0x83 | Code | CRC_L | CRC_H`。

目前 header 固定為：

```python
header = bytes([self.uid, expected_fc])
start_idx = raw_data.find(header, search_idx)
```

來源：`adapters/generic_adapter.py:325-335`。

所以 `07 83 02 ...` 不含 `07 03`，搜尋直接失敗；雖然隨後有 `if fc & 0x80`，但程式到不了該處：`adapters/generic_adapter.py:338-354`。

影響：

- **不是單純錯誤訊息。** 合法 Exception 被拋為「找不到合法 CRC 的封包」。
- **讀取輪詢：** `BusMasterScheduler._process_poll()` 捕捉 DataDecodeError 後呼叫 `_record_failure()`，連續 5 次可轉 OFFLINE／60 秒慢探測：`src/bus_master.py:360-375,398-420`。
- **寫入 verify read：** 同樣被視為實體失敗並在既有 write retry loop 中重試，最多三個 online 嘗試：`src/bus_master.py:234-315`。這會放大 Exception 本來應立即、明確回報的失敗。
- **直接 write Exception：** `driver.write()` 已能辨識 5-byte Exception 並回傳 `False`，因此直接寫入不走這個洞：`src/driver.py:275-285`。

### 最小修正建議（未實作）

不改滑動掃描架構；只把候選 header 由一種改為兩種，且 Exception 仍必須完整五 bytes、UID 正確、FC 必須等於 `expected_fc | 0x80`、CRC 正確：

```python
normal_header = bytes([self.uid, expected_fc])
exception_header = bytes([self.uid, expected_fc | 0x80])
# 每回合取兩個 find() 中最早的索引；其餘滑動與 CRC 流程不變。
```

在第二層驗證前或其中明確拋出：

```python
DataDecodeError(f"Modbus Exception: FC{expected_fc:02X} Code={raw_data[2]:02X}")
```

## ByteCount 與同 UID/同 FC 舊幀風險

`_prebuild_poll_cmds()` 只保留 `id`、`fc`、`req`、可選 `response_len`；沒有保留或推導 response 的預期 data byte count：`adapters/generic_adapter.py:54-74`。`_verify_modbus_frame()` 因而只能檢查「ByteCount 等於已選 frame 的資料長度」：`adapters/generic_adapter.py:397-403`。

這意味著以下 CRC 正確的 9-byte FC03 response，在本次 request 其實期望 7 bytes 時：

```text
07 03 04 12 34 56 78 CRC_L CRC_H
```

- profile 明填 `response_len: 7` 時：拒絕，PASS。
- 未填 `response_len` 時：接受，並可能只發布前兩個 bytes 對應的 sensor，FAIL。

這也是 Deep Radar 無法區分「前一筆殘留、同 UID、同 FC、不同 quantity」的主要條件。RTU response 不含起始地址，不能在 response 本身反查；正確最小防線是由這次 request 的 quantity 計算 expected response length：FC01/02 為 `5 + ceil(count/8)`，FC03/04 為 `5 + 2*count`。保留 profile `response_len` 作顯式覆寫／一致性檢查即可。

## Driver idle timeout 判斷

`_send_and_recv()` 先等第一段資料，之後每次 `reader.read(1024)` 等 `idle_timeout`，逾時就將目前 raw bytes 交給 Adapter：`src/driver.py:219-265`。

- 不會破壞單段回覆，亦能正確處理間隔 **小於 100ms** 的 TCP 碎包。
- 100ms 不是 Modbus RTU 規範保證；它是 TCP gateway 的傳輸行為假設。若 gateway 或網路在同一 response 的兩個 TCP chunks 間停超過 100ms，後段會留在 socket，下一交易前可能被 flush 丟棄。
- 未見目標 gateway 會產生此 gap 的實證，故不建議為此重構成 Driver 滑動 framing。建議維持目前設計並把 gateway 行為列為部署前提；若現場 trace 出現 >100ms chunk gap，再調大設定值或採精準收幀。

## 隔離測試

測試檔：`scratch/master_rtu_audit_20260808.py`。只建立 localhost 一次性 TCP server，未接觸實體設備。

```bash
python3 scratch/master_rtu_audit_20260808.py
```

結果：**12/12 OK**（測試以預期例外方式固定現況缺口）。涵蓋：

1. 正常 FC01、FC02、FC03、FC04、FC05、FC06、FC15、FC16 response。
2. 前置／後置 garbage。
3. 壞 CRC 候選後尋得後方合法幀。
4. 錯 UID、錯 FC 候選後只接受本次 UID／FC。
5. 前半截與後半截拒絕。
6. ByteCount 宣告截斷、以及 profile 有 expected length 時的錯 ByteCount 拒絕。
7. profile 無 expected length 時，CRC 正確但錯 request count 被接受（固定 P1）。
8. FC03 `0x83`、FC04 `0x84` Exception 未被辨識（固定 P1）。
9. 兩合法幀相連時選符合本次 UID／FC 的幀。
10. 30ms TCP 碎包完整收集；130ms gap 僅回傳前段（固定 Driver 邊界）。
11. 壞 CRC 的非 Exception write ACK 被 `driver.write()` 接受（固定 P2）。

## 結論與處置

**目前不可保持完全不動。**

禁止重構的項目：Deep CRC Radar 的滑動搜尋、雙重 CRC 驗證、Driver 的現有 idle 收包策略，均有正面防禦效益。

後續最小修改應依序：

1. 修讀取 Exception Response（P1）。
2. 由 poll request count 推導預期 response 長度，至少補足目前未設 `response_len` 的命令（P1）。
3. 在 `driver.write()` 或 BusMaster write path 驗完整 8-byte normal ACK（P2；可與第 1 項共用驗證函式，但不必重構）。
