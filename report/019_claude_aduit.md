# 019 Master Modbus RTU 三項防線修正 — 獨立稽核

**稽核日期**：2026-08-08
**稽核標的**：`report/019_Master_Modbus_RTU_三項防線修正報告.md`（commit `3ae2fb4`）
**方法**：對現行原始碼靜態追蹤、獨立重跑作者測試、獨立掃描 profile、隔離沙箱對抗性測試、實機前後對照
**本輪變更**：**無**。未修改任何 production 程式、設定、Docker/Compose、容器、硬體或 MQTT。

---

## 裁決：**ADOPT（採用）**，附一項建議的後續小修

三項防線的實作**正確**，作者的驗證**紮實且誠實**（先寫失敗測試再改 production 的順序值得肯定）。已部署路徑（`driver.type: tcp` + `adapter: rtu`）未見回歸。

但獨立稽核另外查出 **1 項潛在回歸**與 **2 項行為變更**，作者報告均未載明。其中潛在回歸目前**未被啟用**，但應在有人切到 `adapter: tcp` 之前修掉。

| 項目 | 稽核結果 |
|---|---|
| Exception Response 精準分類 | ✅ 正確 |
| Request count → response length 約束 | ✅ 正確，且 profile 前置核對經獨立複驗 |
| Write ACK 完整驗證 | ✅ 正確（就 RTU 路徑而言） |
| TCP Driver 路徑一致性 | ⚠️ **對 `adapter: tcp`（MBAP）路徑構成潛在回歸** |
| 正常資料流／HA／MQTT 對外契約 | ✅ 未變更 |
| 作者宣稱的 20/20 | ✅ 獨立重跑通過 |

---

## 1. 事實核對

### 1.1 作者宣稱經獨立複驗，全部屬實

| 宣稱 | 複驗方式 | 結果 |
|---|---|---|
| 隔離測試 20/20 | `python3 scratch/master_rtu_audit_20260808.py` | ✅ `Ran 20 tests ... OK` |
| profile `response_len` 與推導值 0 不符 | 於容器內獨立掃描全部 `*.yaml` | ✅ **21 條 read_commands，0 不符** |
| `bus_master.py` 未修改 | `git show --stat 3ae2fb4` | ✅ 不在變更清單 |
| `main.py`／`ha_manager.py`／`mqtt_client.py`／`profile/*.yaml` 未修改 | 同上 | ✅ |
| 修改規模 91 新增／48 刪除 | 同上 | ✅ 相符 |

### 1.2 一項需要更正的敘述：部署狀態

報告 §「部署狀態」稱**尚未重啟容器或部署到現場設備**。

```
報告檔案時間           2026-08-08 20:39
production 檔案 mtime   2026-08-08 20:36
容器 StartedAt          2026-08-08 20:51（本地）
```

**報告撰寫當下屬實，但新程式碼已於 20:51 部署上線並正在執行。** 已於容器內確認：

```
docker exec ginlong grep -c "Response quantity mismatch" /app/src/adapters/generic_adapter.py → 1
docker exec ginlong grep -c "Write ACK"                  /app/src/driver.py                   → 6
docker exec ginlong sed -n '24,26p' /app/src/modbus_tcp_driver.py → return await super().write(payload)
```

閱讀者若據報告認為「尚可從容審查再決定是否部署」，前提已不成立。

### 1.3 實機前後對照（新碼於 20:51 上線）

| 觀察窗 | 輪詢 | 通訊失敗 | 失敗率 | 新型拒絕訊息 |
|---|---:|---:|---:|---:|
| 19:51–20:51（舊碼，60 分） | 283 | 130 | 45.9% | 0 |
| 20:51–21:00（新碼，9 分） | 93 | 45 | 48.4% | **0** |

失敗率同數量級，屬本現場雙 master 的既有基線（同窗另有 448 次 Flush／Frame 逾時）。
**`Modbus Exception`／`Response quantity mismatch`／`Write ACK` 三類新訊息出現次數皆為 0** —— 加嚴後未在實機上多拒絕任何一幀。

---

## 2. 對抗性驗證（本現場視角）

作者的 20 項測試涵蓋協定正確性，但未涵蓋**雙 master 噪音現場**這個本專案特有的前提。以下為獨立補做，隔離沙箱、production 逐字複製件、fake driver、無硬體／MQTT：

```bash
cd /root/py_ginlong/scratch/validation_019
timeout 120 python3 -u adversarial.py
```

**4/4 項發現全部成立。**

### X1 — 新失效模式：外來 Exception 幀會遮蔽我方合法回覆

```
單獨合法回覆           → {'v': 10}
[Exception][合法回覆]  → Modbus Exception: UID=7 FC=03 Code=02   ← 合法資料被丟棄
[合法回覆][Exception]  → 解析成功 {'v': 10}
```

**成因**：Deep CRC Radar 新增 exception 候選後，以 `start_idx = min(candidates)` 取較前者。緩衝區中若先出現同 UID／同 FC 的合法 Exception 幀，本輪輪詢即失敗。

**修正前**：Exception 不匹配 normal header，radar 直接找到合法幀 → 成功。

**觸發條件**：另一 master 對同 UID／FC 取得 Exception 回覆，且該幀落在我方回覆之前。

**評價：不建議「修正」。** 反向做法（優先取 normal 幀）會引入更糟的結果 —— 當我方請求**真的**收到 Exception、而緩衝區殘留同指令的舊 normal 幀時，會把**陳舊資料當成當前值發布到 HA**。相較之下「本輪失敗、重試」只損失一次輪詢，資料不會出錯。**現行取法較安全，列為已知殘餘風險並監控即可。**

附帶：依 `Solis_Inverter_Modbus_Dev_Notes.md`，Solis 讀取未實作暫存器回傳 `0x0000` 而非 Exception，故實際觸發機率低。

### X2 — quantity 約束不誤殺正確幀

```
✅ 正確 count=2 → 4 bytes   → 接受
✅ 舊幀 count=1 → 2 bytes   → Response quantity mismatch
✅ 舊幀 count=6 → 12 bytes  → Response quantity mismatch
```

只拒絕「同 UID／FC 但 quantity 不符」的幀。配合 §1.1 的 21 條 read_commands 全數一致，**此項在本現場無誤殺風險**。

### X3 — 寫入 ACK 驗證缺乏容噪（與讀取路徑不對稱）

```
乾淨 ACK              → return True
ACK 前有 1 byte 雜訊  → DriverTimeoutError: Write ACK length mismatch: expected 8, received 9
ACK 後有 1 byte 雜訊  → DriverTimeoutError: Write ACK length mismatch: expected 8, received 9
合法 Exception ACK    → return False
```

讀取路徑有 Deep CRC Radar 可在雜訊中滑動尋找合法幀；**寫入 ACK 路徑是 `len(resp) != 8` 直接拒絕，無等價容噪**。本現場 40 分鐘內有 448 次 Flush／Frame 逾時，前置／後置雜訊是常態。

### X4 — 壞 ACK 的失敗帳務已改變（行為變更，非缺陷）

| 情境 | 帳務 |
|---|---|
| `ack is False`（合法 Exception） | `_record_success` → 不重試、設備維持 ONLINE |
| `DriverTimeoutError`（壞 ACK） | `physical_fault_count += 1` → 重試耗盡計 1 次失敗 |
| 回讀值不符 | `_record_success` → 設備維持 ONLINE |

**修正前**（TCP driver 盲回 `True`）：壞 ACK → 進 verify 回讀 → 值不符 → 設備**維持 ONLINE**。
**修正後**：壞 ACK → 物理故障 → 3 次重試耗盡計 1 次失敗 → 連續 5 次才離線。

**評價：方向正確。** 壞 ACK 本來就是物理故障，舊行為在遮蔽問題，與本專案「失敗要誠實浮出來」的既定姿態一致。寫入為使用者觸發、頻率低，加上 3 次重試與 5 次遲滯，風險有限但**確實存在，應載明**。

---

## 3. 潛在回歸（作者未察覺）

### 被移除的程式碼自帶設計說明

`git show 3ae2fb4 -- src/modbus_tcp_driver.py` 顯示被刪掉的註解逐字寫著：

> 原生 TCP 的 Exception 判斷會看 MBAP Header，長度不是 5，
> 所以直接盲發盲收，把解碼與 Exception 判定權力還給 Adapter。

亦即 `AsyncModbusTcpDriver.write()` 的「盲發盲收」**是刻意設計**，用於支援**原生 Modbus TCP（MBAP 框架、無 CRC）**。新版改為委派父類的 **Modbus RTU** ACK 驗證。

### 實證

`adapters/modbus_tcp_adapter.py`（`ADAPTER_NAME = "tcp"`）的 `encode_write()` 回傳 MBAP 框架封包：

```
_add_mbap_header(pdu, tx_id) → [txn_hi][txn_lo][0x00][0x00][len_hi][len_lo][unit][fc][addr][val]  = 12 bytes
```

新版 `driver.write()` 的守衛為：

```python
if len(payload) < 8 or payload[1] not in (5, 6, 15, 16):
    return True
```

對 MBAP 封包而言 `payload[1]` 是 **transaction ID 低位元組**，不是 FC。而 `_get_next_tx_id()` 為 `(self._tx_id + 1) & 0xFFFF` 逐次遞增，故低位元組循環 0–255：

| txn_lo | 行為 |
|---|---|
| ∉ {5,6,15,16}（252/256） | 提早 `return True` —— 與舊版盲收相同，**巧合可用** |
| ∈ {5,6,15,16}（4/256） | 進入 RTU 驗證 → MBAP 回應 12 bytes 無 CRC → `len(resp) != 8` → **`DriverTimeoutError`** |

**即約 1.56% 的寫入會無故失敗，且呈間歇性、難以重現。**

### 影響範圍

**目前未啟用** —— `profile/config.yaml` 四台設備全部 `adapter: rtu`，走的是「TCP-to-RS485 串口伺服器隧道 RTU 幀」路徑，對此 RTU ACK 驗證**完全正確**。

風險在於：`driver.type: tcp` 同時服務兩種語意不同的傳輸（隧道 RTU／原生 MBAP），而 driver 無從分辨，只有 adapter 知道。日後若有人配置 `adapter: tcp`，寫入會間歇失敗。

### 建議的最小修正（約 1 行）

在套用 RTU ACK 規則前，先確認**請求本身是格式良好的 RTU 幀**：

```python
if len(payload) < 8 or payload[1] not in (5, 6, 15, 16) or not _has_valid_modbus_crc(payload):
    return True
```

MBAP 封包沒有尾端 CRC，幾乎必然（誤判機率 1/65536）落入提早 return，恢復舊有的盲收語意；隧道 RTU 路徑不受影響。**這比恢復 `AsyncModbusTcpDriver.write()` 的覆寫更小，且維持作者「兩條路徑共用同一 ACK 契約」的意圖。**

---

## 4. 對「是否有新風險」的總結回答

| # | 項目 | 性質 | 是否阻擋採用 |
|---|---|---|---|
| 1 | 外來 Exception 幀遮蔽合法回覆（X1） | 新失效模式，但替代做法更糟 | ❌ 不阻擋，列殘餘風險 |
| 2 | 寫入 ACK 無容噪（X3） | 與讀取路徑不對稱 | ❌ 不阻擋，需監控 |
| 3 | 壞 ACK 計入物理故障（X4） | 行為變更，方向正確 | ❌ 不阻擋，應載明 |
| 4 | `adapter: tcp`（MBAP）寫入間歇失敗 | **潛在回歸** | ⚠️ 目前未啟用；切換前必須先修 |

**正常資料流未受影響**：`Driver raw response → Adapter decode → HAManager.publish_state → 原 state topic` 全鏈路未變；`bus_master.py`、`ha_manager.py`、`mqtt_client.py`、profile、Discovery schema、HA entity ID、MQTT topic 皆未修改（已由 `git show --stat` 確認）。

---

## 5. 對作者流程的評價

**這份修正的作業品質明顯高於本專案先前數輪。** 值得記錄的優點：

1. **先寫「修正後才應通過」的失敗測試，再改 production** —— 基線 18 項、7 項預期失敗且精準對應三項既知缺口，這是正確的順序。
2. **改 production 前先做 profile 前置核對** —— `declared_response_len_mismatches=0` 這一步若省略，quantity 約束會在上線瞬間讓所有輪詢失敗。
3. **發現 TCP 路徑不一致後主動擴大範圍並說明** —— 沒有假裝沒看到。
4. **明確列出未修改的 production 路徑**，便於稽核。

需要改進的兩點：

1. **部署狀態未同步更新**（§1.2）。
2. **移除既有程式碼前未追究其註解所述的設計理由** —— §3 的潛在回歸即源於此。被刪掉的那段註解已經寫明「為什麼要盲收」。

---

## 6. 建議

**採用現行修正，並依下列順序處理：**

1. **（可延後）** 依 §3 加上 `_has_valid_modbus_crc(payload)` 守衛，關閉 MBAP 路徑的潛在回歸。目前未啟用，但這是 1 行成本。
2. **（監控）** 觀察 24–48 小時，留意日誌是否出現 `Write ACK ... mismatch`。若在使用者實際下達寫入時頻繁出現，代表 X3 的容噪不足在本現場已實體化，屆時再評估是否為 ACK 讀取加入等價的滑動搜尋。
3. **（文件）** 於 `report/019` 更正部署狀態，或以本報告作為補充。
4. **（同步）** py_jkbms 目前為純監聽（`mode: listen` × 3），不走 `bus_master`／`driver.write()` 路徑，本次修正對其無作用，**不需同步**。

---

## 7. 稽核邊界

**未變更**：`src/`、`adapters/`、`profile/`、`Dockerfile`、`docker-compose.yaml` 下所有檔案（已以 `git status --porcelain` 確認，僅 `scratch/` 有異動）。

**未執行**：`docker restart`／`stop`／`up`／`down`、映像重建、實體 RS485／USB 連接、對實體 MQTT broker 的任何發布或訂閱。

**新增檔案僅限**：`scratch/validation_019/`（對抗性測試與 production 逐字複製件，已被 `.gitignore` 排除）與本報告。

**驗證方式**：作者測試以原檔重跑；profile 掃描於容器內唯讀執行；對抗性測試全部使用 fake driver 與構造封包，未接觸任何實體設備。
