# 020 TCP 驅動 MBAP 潛在回歸修正（driver.py V1.8 → V1.9）

**日期**：2026-08-08
**依據**：`report/019_claude_aduit.md` §3 建議的最小修正
**修改範圍**：`src/driver.py` 一處守衛條件（+1 判斷式，+16 行註解）
**其他 production 檔案**：未變更

---

## 1. 背景：一個被推翻的前提

修正前曾考慮「改用 `/root/ginlong/src/modbus_tcp_driver.py` 的 V1.1 盲收版」來迴避問題。**該方案經實測否決** —— 前提有誤。

### 誤解：「RTU over TCP 本來就沒 CRC」

實測結果相反：

```
adapter: rtu 產生的寫入封包 = 07 06 06 03 00 01 B8 E4   (8 bytes)
                                              ^^^^^ 尾端 CRC16
_has_valid_modbus_crc(payload) = True
```

兩種傳輸必須分清楚：

| 設定 | 框架 | 尾端 CRC | 完整性由誰保證 |
|---|---|---|---|
| `driver.type: tcp` + **`adapter: rtu`**（本專案現行） | 完整 RTU 幀原樣穿過 TCP（串口伺服器隧道） | **有** | RTU CRC16 |
| `driver.type: tcp` + `adapter: tcp` | MBAP（7 bytes 標頭 + PDU） | **無** | TCP 本身 |

**沒有 CRC 的是原生 Modbus TCP，不是 RTU over TCP。** 這個區分決定了修正方向：正因為現行路徑**有** CRC，加上 CRC 檢查才會**維持驗證開啟**；若真的沒有，該守衛反而會把保護關掉。

### 為何不採用 V1.1 盲收版

實測對照（`adapter: rtu`，即目前實際部署路徑）：

| ACK 情境 | V1.1 盲收 | V1.8/V1.9 驗證 |
|---|---|---|
| 正確 ACK | `return True` | `return True` |
| **別台設備的 ACK（UID 不符）** | `return True` ❌ | `DriverTimeoutError: Write ACK UID mismatch` |
| 純垃圾 | `return True` ❌ | `DriverTimeoutError: Write ACK length mismatch` |

本現場 RS485 上有原廠 WiFi 採集棒作為第二 master，「別台設備的 ACK」那一列是實際會發生的情境。**換回 V1.1 等於拿掉正在生效的保護，去迴避一個尚未啟用的問題** —— 方向相反。

---

## 2. 修正內容

`src/driver.py` 的 `RobustAsyncTcpDriver.write()` 守衛：

```python
# V1.8
if len(payload) < 8 or payload[1] not in (5, 6, 15, 16):
    return True

# V1.9
if (len(payload) < 8
        or payload[1] not in (5, 6, 15, 16)
        or not _has_valid_modbus_crc(payload)):
    return True
```

語意：**只對「格式良好的 Modbus RTU 寫入請求」套用 RTU ACK 契約。**

`driver.type: tcp` 同時服務兩種語意不同的傳輸，而 driver 無從分辨 —— 只有 adapter 知道自己送的是哪一種。以「請求端是否帶合法 RTU CRC」作為判別依據，可讓兩條路徑各自走正確的邏輯，無須新增設定項或改動 adapter。

### 修正的缺陷

`adapters/modbus_tcp_adapter.py` 的 `encode_write()` 產出 MBAP 封包：

```
_add_mbap_header(pdu, tx_id) → [txn_hi][txn_lo][0x00][0x00][len_hi][len_lo][unit][fc][addr][val]
```

對 MBAP 而言 `payload[1]` 是 **transaction id 低位元組**，不是 FC。`_get_next_tx_id()` 為 `(self._tx_id + 1) & 0xFFFF` 逐次遞增，低位元組循環 0–255，故：

- V1.8：低位元組恰為 5/6/15/16 時（4/256 ≈ **1.56%**）誤入 RTU 驗證 → MBAP 回應 12 bytes 且無 CRC → 必然 `DriverTimeoutError`，呈間歇性、難以重現
- V1.9：MBAP 無尾端 CRC，自動落回盲收（誤判機率 1/65536），等同 `modbus_tcp_driver` V1.1 的既有行為

---

## 3. 驗證

### 3.1 兩條路徑對照（隔離沙箱，production 逐字複製件）

```bash
cd /root/py_ginlong/scratch/validation_019
timeout 90 python3 verify_fix.py
```

**A. 隧道 RTU（`adapter: rtu`）—— 現行部署路徑，保護必須完全不變**

```
請求封包 = 07 06 06 03 00 01 B8 E4   CRC 合法=True

ACK 情境                V1.8（修正前）          V1.9（修正後）
正確 ACK                return True            return True
別台設備 ACK(UID 不符)   DriverTimeoutError     DriverTimeoutError
純垃圾                  DriverTimeoutError     DriverTimeoutError
合法 Exception          return False           return False
```

四種情境**行為逐字相同**，保護未被削弱。

**B. 原生 Modbus TCP（`adapter: tcp`，MBAP 無 CRC）—— 潛在回歸是否關閉**

```
掃描 256 個連續 transaction id：
  盲收放行 = 256/256    誤入 RTU 驗證而失敗 = 0/256
  ✅ 潛在回歸已關閉
（V1.8 在此掃描下為 4/256 ≈ 1.56% 失敗）
```

### 3.2 原有 20 項回歸測試

```bash
timeout 180 python3 scratch/master_rtu_audit_20260808.py
→ Ran 20 tests in 3.755s / OK
```

作者於 `report/019` 建立的全部驗收條件維持通過。

### 3.3 實機

```bash
cd /root/py_ginlong && ./restart.sh
```

```
21:29:49  ✅ 掛載 [主動] 設備: UID=1 / UID=2 / UID=3 / UID=4 adapter=rtu
21:29:49  🚀 Edge Gateway V3.9 啟動完成
21:29:49  ✅ 全部 4 台設備掛載正常，無隔離
21:29:49  [MQTT] connected 192.168.106.5
21:29:55  [Discovery] 全部 4 台送出完畢（間隔 2.0s）

Task exception   : 0
driver traceback : 0
Write ACK 拒絕   : 0
容器內版本       : #version driver.py - V1.9 工業封存版
```

---

## 4. 殘餘風險（沿用 `report/019_claude_aduit.md`，本次未改變）

| # | 項目 | 狀態 |
|---|---|---|
| 1 | 外來 Exception 幀在前時會遮蔽我方合法回覆 | 已知殘餘風險。反向做法會把陳舊資料當現值發布，更糟 —— **不建議修**，監控即可 |
| 2 | 寫入 ACK 為精確長度比對，無讀取路徑那種 Deep CRC Radar 容噪 | 本次未處理。建議觀察實際寫入時是否頻繁出現 `Write ACK ... mismatch` |
| 3 | 壞 ACK 由「值不符（無害）」改計為「物理故障」 | 行為變更，方向正確（壞 ACK 本就是物理故障），已載明 |

第 1、2 項與本次修正正交，未受影響。

---

## 5. 變更邊界

**已變更**：`src/driver.py`（V1.8 → V1.9，守衛條件 +1 判斷式、+16 行說明註解、檔頭修復歷程 +4 行）。

**未變更**：`src/modbus_tcp_driver.py`（維持 V1.2 委派父類）、`adapters/`、`profile/`、`src/` 其餘檔案、`Dockerfile`、`docker-compose.yaml`。

**未採用**：`/root/ginlong/src/modbus_tcp_driver.py` 的 V1.1 盲收版（理由見 §1）。該檔為 20:23 的修改前快照，保留作參考，未回填。

**新增檔案**：`scratch/validation_019/verify_fix.py`、`compare.py` 與 sandbox 複製件（已被 `.gitignore` 排除）、本報告。
