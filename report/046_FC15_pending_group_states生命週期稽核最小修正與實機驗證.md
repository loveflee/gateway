# 046 — FC15 `_pending_group_states` 生命週期稽核、最小修正與實機驗證

## 人話結論

1. **有 stale risk。** 舊程式成功或失敗後都留下上一次群組目標。
2. **可以實際重現。** 先成功寫入 `group_01=all_on`，再違規直接呼叫 `build_verify_read("group_01")`，舊程式會接受舊的 `all_on`。
3. **只修改了 Generic Modbus Adapter**：`adapters/generic_adapter.py`。
4. **是最小修改。** 讀取 pending 時由 `get()` 改成一次性 `pop()`；沒有改 BusMaster、Driver、HA、Validator、profile 或 config。
5. **stale state 已無法重用。** 成功、mismatch 耗盡、timeout、ACK 拒絕、decode 例外後都沒有 pending；直接 verify 明確拒絕。
6. **FC15 與 FC01/05/06/16 regression 均 PASS**，TCP ACK guard、validator、HA/MQTT observable 也均 PASS。
7. **實機 PASS。** UID3 經 MQTT → Gateway → FC15 → ACK → FC01 verify → MQTT，快速同群組 A→B→A 與另一群組皆正確，最後 8 路 OFF。
8. **沒有發現 stale pending 資料流黑洞。**
9. 本報告與 AGENTS/README 在所有驗證完成後才更新。

```text
FINAL: PASS
```

## 施工前模組清單與可達性證據

| 模組 | 檔案 | 稽核結果 | 結論 |
| --- | --- | --- | --- |
| Generic Modbus Adapter | `adapters/generic_adapter.py` | `_pending_group_states` 初始化；`_prepare_coil_group_write()` 寫入；舊 `_build_coil_group_verify_spec()` 以 `get()` 讀取且從不清除 | **有可達 stale risk** |
| BusMaster（唯讀） | `src/bus_master.py` | 每個 attempt 都同步呼叫 `encode_write()`，再立刻呼叫 `build_verify_read()`；retry 回到下一個 attempt 再 encode | 一次性 consume 安全，不需修改 |
| TCP Adapter（唯讀） | `adapters/modbus_tcp_adapter.py` | 群組寫入與 verify 都呼叫 GenericAdapter 的 inherited pending helpers | 自動獲得同一修正 |
| 其他 production/profile | Driver、HA、validator、map、config | 不在問題資料流 | **NO CHANGE** |

舊版完整資料流如下；星號處是問題：

```text
MQTT group command
→ BusMaster attempt
→ Adapter.encode_write()
→ _prepare_coil_group_write(): pending[group] = target
→ build_verify_read(): pending.get(group) (*)
→ FC01 verify / decode / success or retry or fail
→ pending 沒有清除 (*)
→ 後續非法直接 build_verify_read(group) 可重用舊 target
```

`scratch/fc15_046_pending_lifecycle_audit.py --expect current-risk` 在未修改版本得到 **28 PASS / 0 FAIL**，證明而非假設下列情境均遺留 `{"group_01": "all_on"}`：正常成功、verify mismatch 耗盡、driver timeout、ACK `False`、decode exception。RTU 與 TCP 各自都可在終局後直接 build verify 並得到舊的 `group_state="all_on"`。

## 最小施工與生命周期契約

唯一 production 修改在 `adapters/generic_adapter.py`：

```python
state_name = self._pending_group_states.pop(key, None)
if state_name is None:
    raise ValueError("...尚未建立 FC15 write，拒絕 verify")
```

新契約：

```text
每個 attempt: encode_write → 建立 pending → build_verify_read → pop/consume
retry:         重新回到 encode_write，建立新的 pending
所有終局:      dict 已無該 key，直接 verify fail closed
```

`src/bus_master.py` 的呼叫順序證實每次 retry 先執行新的 `encode_write()`，故 pop 不會遺失 retry target；它反而涵蓋 ACK 拒絕、timeout、decode exception、mismatch 耗盡與成功這些 Adapter 無法得知的終局。

## 施工後對照

| 模組 | 預計修改 | 實際修改 | 驗證 | 結果 |
| --- | --- | --- | --- | --- |
| Generic Adapter | stale pending 一次性 consume | `.get()` → `.pop()`；補 V2.9 lifecycle 註解 | lifecycle 38/38、FC15 regression、實機 | PASS |
| BusMaster | NO CHANGE | 無 | SHA-256 `831bfe98…` | PASS |
| HA Manager | NO CHANGE | 無 | SHA-256 `d02c5b3…`、observable regression | PASS |
| RTU Driver | NO CHANGE | 無 | SHA-256 `cc049da8…` | PASS |
| TCP Driver | NO CHANGE | 無 | SHA-256 `688e07ea…`、hostile ACK regression | PASS |
| TCP Adapter | NO CHANGE | 無 | SHA-256 `b08c6abb…`；繼承 lifecycle contract | PASS |
| Validator | NO CHANGE | 無 | SHA-256 `d4f25632…` | PASS |
| Relay profile / config | NO CHANGE | 無 | map SHA-256 `0116e41b…`；config `c9ec5e0a…` | PASS |

## 隔離驗證

| 測試 | 結果 | 覆蓋 |
| --- | --- | --- |
| `fc15_046_pending_lifecycle_audit.py --expect fixed` | **38/38 PASS** | A 成功、B mismatch、C timeout、D ACK exception、E decode exception、F 同群組 A/B/A、G 群組隔離、H 無 encode direct verify / unknown key；RTU/TCP 各跑一次 |
| `fc15_041_production_isolated.py` | **73/73 PASS** | FC15 2/3/4/8/9 coils、PyModbus oracle、CRC、exact match、`__UNMATCHED__`、FC01/05/06/16、ACK guard、validator、HA observable |
| `fc15_043_poll_group_selftest.py` | **9/9 PASS** | FC01 group poll、FC05 sentinel、042 死亡案例、RTU/MBAP 與原 FC05 bytes |
| `claude_044_independent_review.py` | **119/119 PASS** | hostile MBAP ACK、profile mutation、legacy regression、HA discovery 與群組 poll fail-closed |
| `map_validator.py profile/relay_8ch_map2.yaml` | PASS | host 與 container 各一次 |
| `py_compile` | PASS | 修改 Adapter、Adapter/Driver/BusMaster/HA/validator modules |

修後 lifecycle 的關鍵結果：所有終局的 `pending={}`；三次 retry 的 `verify_states` 都是本 attempt 的 `all_on`；終局後 direct verify 一律 `ValueError`。同群組 `path_a → path_b → path_a` 與不同群組 `group_01/group_234` 都逐次取到當前 target，且 consume 後不留 residue。

## Build、部署與健康度

依既有 `build.sh` 流程執行：

```text
docker compose build --no-cache      PASS
image: sha256:bad84c868cf4fe56674ee36fc39a713dd32b16d13b0f4ae3857300bc578a2541
docker compose down                  PASS
docker compose up -d                 PASS
```

容器 `ginlong`：`running`、`RestartCount=0`、啟動時間 `2026-08-12T15:21:12Z`。日誌確認 MQTT connected、UID1/UID3 ONLINE、兩台設備無隔離，測試窗口沒有 ERROR／CRITICAL／Traceback。UID3 `192.168.88.190:502` 只有 Gateway Python 的單一 TCP connection。

## UID3 實機閉環（空載、MQTT 路徑）

測試全程是 MQTT command → production Gateway → UID3；沒有 raw Modbus sender。`scratch/fc15_046_live_pending_lifecycle.pcap` 是唯讀 `tcpdump` 側錄；下列為完整 FC15 write/ACK/verify 關鍵幀：

| 動作 | FC15 TX | ACK RX | FC01 verify TX / RX | MQTT 結果 |
| --- | --- | --- | --- | --- |
| group_01 A `all_off` | `00 21 … 03 0F 0000 0002 01 00` | `00 21 … 03 0F 0000 0002` | `00 22 … 03 01 0000 0008` / data `00` | 8 路 OFF、group_01=all_off |
| group_01 B `all_on` | `00 23 … 03 0F 0000 0002 01 03` | `00 23 … 03 0F 0000 0002` | `00 24 …` / data `03` | switch_0/1 ON、group_01=all_on |
| group_01 A `all_off` | `00 25 … 03 0F 0000 0002 01 00` | `00 25 … 03 0F 0000 0002` | `00 26 …` / data `00` | 8 路 OFF、group_01=all_off |
| group_234 `pattern_101` | `00 27 … 03 0F 0002 0003 01 05` | `00 27 … 03 0F 0002 0003` | `00 28 …` / data `14` | switch_2/4 ON、group_234=pattern_101 |
| group_234 restore | `00 29 … 03 0F 0002 0003 01 00` | `00 29 … 03 0F 0002 0003` | `00 2A …` / data `00` | 8 路 OFF、兩群組 all_off |

`group_01` 的 A→B→A 以 verified MQTT state 相隔約 0.58 秒；每次都是獨立 FC15/ACK/FC01 transaction，未發生舊 target、假成功或群組互染。最後 state 為 `switch_0..7=OFF`、`group_01=all_off`、`group_234=all_off`。

## 資料流黑洞檢查

production 搜尋 `_pending_group_states` 只有三處：初始化、`_prepare_coil_group_write()` 的合法寫入、`_build_coil_group_verify_spec()` 的一次性 `pop()`。不存在第二條 `get()`、其他讀取、silent stale reuse 或跨群組 fallback。所有沒有本次 encode 的 direct verify 都明確拋 `ValueError`，不是 success。 

```text
DATA FLOW BLACK HOLE: NONE
```

## 文件更新

本報告與 `AGENTS.md`、`README.md` 都在 isolation、build、deployment、實機、final restore 全部 PASS 後才更新。文件新增了 FC15 pending target 的一次性 consume / retry 契約；未宣稱實體 relay 機械同步，ATS 仍須硬體 interlock。

## 最終裁決

```text
Stale risk reproduced:             PASS
Minimal GenericAdapter-only fix:    PASS
Pending cannot be reused:           PASS
Retry / timeout / exception:        PASS
Group isolation:                    PASS
FC15 regression:                    PASS
FC01 / FC05 / FC06 / FC16:          PASS
TCP ACK guard / validator:          PASS
HA / MQTT observable:               PASS
Production closed loop:             PASS
Final restore:                      PASS
Documentation after validation:     YES
DATA FLOW BLACK HOLE:               NONE

FINAL: PASS
```
