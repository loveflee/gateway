# FC15 實機真實切換與 RTU／MBAP 雙路補證

## 人話結論

1. UID3 的 FC15 **真的會改變 Relay**，不只是 ACK 後靜默丟棄。
2. 2、3、4、8 路均以**各一筆 FC15 request**完成實機切換；沒有使用 FC05 loop。A 測試的 coil 0、1 由同一筆 `start=0 quantity=2 packed=03` 變為 ON，之後以同一筆 FC15 恢復。
3. 2／3／4／8 路實機皆 PASS，8 路交錯向量得到預期 `0x55`。
4. 自行建立的 MBAP 與等價 RTU FC15 request，逐 byte 都與隔離 PyModbus 3.13.0 oracle 一致；RTU CRC 亦由專案 helper 與 scratch 獨立實作雙算一致。
5. 每筆 FC15 都收到正確 ACK（transaction、protocol、length、UID、FC、start、quantity），每次皆 FC01 回讀完整 8 路並逐 bit 比對。
6. 038 指出的 `None → success` **真實存在**：mock 實跑 BusMaster 後，只寫/讀一次便 publish 並記為成功。
7. `__UNMATCHED__` sentinel 可在不改 BusMaster 下阻止「寫入驗證成功」與 state publish：會重試 3 次、零 publish；但耗盡後仍會 `_record_success()` 保持設備 ONLINE，這是 transport-health 記帳，不是命令成功事件。
8. native TCP ACK 的 blind accept **真實存在**：現有 `AsyncModbusTcpDriver.write()` 對合法 ACK 以及錯 transaction/UID/FC/start/quantity/length/exception 共九種 MBAP response 都回 `True`。
9. 原 FC01、實機 FC05、isolated FC06/FC16、HA 原八個 switch observable contract 均 PASS，且本輪 production 雜湊不變。
10. 結論：**PASS WITH CHANGES**。可進入正式施工，但 FC15 encoder、群組逐 bit verify/sentinel、native-TCP ACK guard 與 `coil_groups` validator 必須作為同一組受測修改；本輪未施工。

## 1. 範圍、安全條件與不變性

- 先完整閱讀 `AGENTS.md`、[037 評估](/root/py_1f/report/037_FC15多Relay原子寫入與Profile活組評估實機驗證.md) 與 [038 敵對審查](/root/py_1f/report/038_FC15多Relay活組敵對式獨立審查_Claude.md)。
- 使用者已明確授權 UID3 空載 8CH Relay 實際 ON/OFF；測試只使用 `scratch/` 與本報告，未改 production、正式 profile 或設定。
- 2026-08-12 15:32:04 +08:00，`docker ps` 顯示 `ginlong` 運行；`ss` 唯一 `.190:502` connection 是 `ginlong`。15:32:08 停止 `ginlong` 後再次 `ss` 無任何 `.190:502` connection，才開始測試。`py_5f` 使用 `.191:502`。
- 本機不是 Git working tree，無法做 `git diff`；以 SHA-256 固定禁止修改檔案。測前/後相同：

```text
generic_adapter.py      918bf518cda0ae9c8e0c02363f1c236a10d66dbced9b97b5ac89a4aea166a08e
modbus_tcp_adapter.py   cf40e0784a728c6e3877ee2eaf5792bd558ce88a37ea240797a1f74ef199ee82
driver.py               cc049da87d7bb02fd5f6a413aa9a448029245960362b090dd108eea880842d66
modbus_tcp_driver.py    b9b9c23b3067f7b56c877fe91adff1f7d6305cd1b0928770de7b531c95cbfa7f
bus_master.py           831bfe98c534e5adc615e3565fc51c8f152199e9e99c82bd0a838da139e4661e
ha_manager.py           d02c5b3e76401db36c88ea49de3dc3ce2f0a0a5e83a4761e044344725dcbf530
map_validator.py        5045cf93e452d73c3965c6fc11681caa63e0014271196cf842342556d39366ca
relay_8ch_map.yaml      8d9b093e3e4f49a623a6c3203a3e7448c6ebf445391ffdb0dfc455ba2c27475c
config.yaml             6cc441be93277f003eafc968450f05939bdf4126022f3b5b5a0b634e546075c7
```

## 2. Wire framing 與 oracle

UID3 的實際線路是 native Modbus TCP：MBAP wire frame **沒有 RTU CRC**。因此實機 TX/RX 以 MBAP 為準；每個 PDU 同時建構等價 RTU frame，RTU CRC 均由 `adapters.generic_adapter.calc_crc16()` 與 scratch 的獨立 `0xA001` implementation 交叉驗證。

PyModbus 3.13.0 僅安裝於 `/tmp/fc15-pymodbus-lib` 作 oracle，未加入 `requirements.txt`。本輪每一筆 FC15（target、group restore、final restore）都檢查：

```text
self-built MBAP == PyModbus MBAP: YES
self-built RTU  == PyModbus RTU:  YES
project CRC == independent CRC:    YES
```

FC15 normal ACK 只含 `UID FC start quantity`；不含 byte count/data。下文的 RTU ACK 是由已驗 MBAP PDU 加 CRC 的等價表示，並不是原生 TCP wire response。

## 3. 實機 FC01 基線

```text
Timestamp: 2026-08-12T15:32:11.879+08:00
MBAP TX:   39 01 00 00 00 06 03 01 00 00 00 08
MBAP RX:   39 01 00 00 00 04 03 01 01 00
States:    [OFF, OFF, OFF, OFF, OFF, OFF, OFF, OFF]
RTU TX:    03 01 00 00 00 08 3C 2E
RTU RX:    03 01 01 00 50 30
```

`BEFORE_STATE` 是完整 8-bit baseline。所有測試均先 FC01 讀整板、實際寫入、再讀整板；每組先回到 baseline，最後再用完整 8-coil FC15 安全復原一次。

## 4. FC15 真實狀態改變：2／3／4／8 路

每列 target 的 `after` 都是 FC01 start=0/count=8 回讀結果，而不是 ACK 推論。耗時是 write TX→ACK RX；FC01 讀取單次約 26.6–28.7 ms。

| Case | Before | FC15 target | Packed | MBAP TX / RX | RTU request / ACK | FC01 after | Target / restore |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 0–1, 2 coils | `00000000` | `11000000` | `03` | `39 03 00 00 00 08 03 0F 00 00 00 02 01 03` / `39 03 00 00 00 06 03 0F 00 00 00 02` | `03 0F 00 00 00 02 01 03 1F 4F` / `03 0F 00 00 00 02 D5 E8` | `11000000` (`RX data=03`) | PASS / restore PASS |
| B 2–4, 3 coils | `00000000` | `00101000` | `05` | `39 08 00 00 00 08 03 0F 00 02 00 03 01 05` / `39 08 00 00 00 06 03 0F 00 02 00 03` | `03 0F 00 02 00 03 01 05 B7 4D` / `03 0F 00 02 00 03 B5 E8` | `00101000` (`RX data=14`) | PASS / restore PASS |
| C 4–7, 4 coils | `00000000` | `00001101` | `0B` | `39 0D 00 00 00 08 03 0F 00 04 00 04 01 0B` / `39 0D 00 00 00 06 03 0F 00 04 00 04` | `03 0F 00 04 00 04 01 0B 0F 48` / `03 0F 00 04 00 04 14 2B` | `00001101` (`RX data=B0`) | PASS / restore PASS |
| D 0–7, 8 coils | `00000000` | `10101010` | `55` | `39 12 00 00 00 08 03 0F 00 00 00 08 01 55` / `39 12 00 00 00 06 03 0F 00 00 00 08` | `03 0F 00 00 00 08 01 55 BF 73` / `03 0F 00 00 00 08 55 EF` | `10101010` (`RX data=55`) | PASS / restore PASS |

表中 bit string 的左到右是 coil 0→7，故 Case D 的 LSB-first wire byte 為 `0x55`。A 已直接證明兩個 coils 從 OFF 變為 ON 的唯一 write 是一筆 `FC15 start=0 quantity=2`，不是兩次 FC05；這可保證它們被同一個 Modbus multiple-coils request 送入設備，不能據此宣稱機械接點同一微秒動作。

### 每一筆 group restore 與 final restore

```text
A restore  15:32:12.022  start=0 qty=2 packed=00
MBAP TX/RX 39 05 00 00 00 08 03 0F 00 00 00 02 01 00
            39 05 00 00 00 06 03 0F 00 00 00 02
RTU TX/ACK  03 0F 00 00 00 02 01 00 5F 4E / 03 0F 00 00 00 02 D5 E8

B restore  15:32:12.168  start=2 qty=3 packed=00
MBAP TX/RX 39 0A 00 00 00 08 03 0F 00 02 00 03 01 00
            39 0A 00 00 00 06 03 0F 00 02 00 03
RTU TX/ACK  03 0F 00 02 00 03 01 00 77 4E / 03 0F 00 02 00 03 B5 E8

C restore  15:32:12.314  start=4 qty=4 packed=00
MBAP TX/RX 39 0F 00 00 00 08 03 0F 00 04 00 04 01 00
            39 0F 00 00 00 06 03 0F 00 04 00 04
RTU TX/ACK  03 0F 00 04 00 04 01 00 4E 8F / 03 0F 00 04 00 04 14 2B

D restore  15:32:12.460  start=0 qty=8 packed=00
MBAP TX/RX 39 14 00 00 00 08 03 0F 00 00 00 08 01 00
            39 14 00 00 00 06 03 0F 00 00 00 08
RTU TX/ACK  03 0F 00 00 00 08 01 00 7F 4C / 03 0F 00 00 00 08 55 EF

Final restore 15:32:12.882 start=0 qty=8 packed=00
MBAP TX/RX   39 23 00 00 00 08 03 0F 00 00 00 08 01 00
              39 23 00 00 00 06 03 0F 00 00 00 08
RTU TX/ACK    03 0F 00 00 00 08 01 00 7F 4C / 03 0F 00 00 00 08 55 EF
```

九筆 FC15 write 的 ACK 均為：correct transaction ID、protocol `0000`、length `0006`、UID `03`、FC `0F`、以及 request 相同的 start/quantity。每筆耗時為 30.699–31.642 ms。

## 5. 全部 FC01 before／verify／restore 原始資料

下表保留所有實機 FC01 讀取。所有為 start=0/count=8；RTU equivalent request 固定為 `03 01 00 00 00 08 3C 2E`。每個 response 的等價 RTU CRC 都由兩種 implementation 驗過。

| Label | MBAP TX | MBAP RX | coil 0→7 | RTU RX equivalent |
| --- | --- | --- | --- | --- |
| A before | `39 02 00 00 00 06 03 01 00 00 00 08` | `39 02 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| A target verify | `39 04 00 00 00 06 03 01 00 00 00 08` | `39 04 00 00 00 04 03 01 01 03` | `11000000` | `03 01 01 03 10 31` |
| A restore verify | `39 06 00 00 00 06 03 01 00 00 00 08` | `39 06 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| B before | `39 07 00 00 00 06 03 01 00 00 00 08` | `39 07 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| B target verify | `39 09 00 00 00 06 03 01 00 00 00 08` | `39 09 00 00 00 04 03 01 01 14` | `00101000` | `03 01 01 14 50 3F` |
| B restore verify | `39 0B 00 00 00 06 03 01 00 00 00 08` | `39 0B 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| C before | `39 0C 00 00 00 06 03 01 00 00 00 08` | `39 0C 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| C target verify | `39 0E 00 00 00 06 03 01 00 00 00 08` | `39 0E 00 00 00 04 03 01 01 B0` | `00001101` | `03 01 01 B0 51 84` |
| C restore verify | `39 10 00 00 00 06 03 01 00 00 00 08` | `39 10 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| D before | `39 11 00 00 00 06 03 01 00 00 00 08` | `39 11 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| D target verify | `39 13 00 00 00 06 03 01 00 00 00 08` | `39 13 00 00 00 04 03 01 01 55` | `10101010` | `03 01 01 55 90 0F` |
| D restore verify | `39 15 00 00 00 06 03 01 00 00 00 08` | `39 15 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| Final before restore | `39 22 00 00 00 06 03 01 00 00 00 08` | `39 22 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |
| FINAL RESTORE verify | `39 24 00 00 00 06 03 01 00 00 00 08` | `39 24 00 00 00 04 03 01 01 00` | `00000000` | `03 01 01 00 50 30` |

`FINAL RESTORE = PASS`：final state 與 `BEFORE_STATE` 均為全 OFF。

## 6. FC05 實機 Regression

這四筆是刻意的單路 FC05 regression，不是 FC15 模擬。每筆 ACK echo 與後續整板 FC01 都正確，最後已由 final FC15 restore 再確認 baseline。

| Operation | MBAP TX / RX | RTU request / ACK | write ms | FC01 target result |
| --- | --- | --- | ---: | --- |
| switch_0 ON | `39 17 00 00 00 06 03 05 00 00 FF 00` / same | `03 05 00 00 FF 00 8D D8` / same | 29.080 | `10000000` PASS |
| switch_0 OFF | `39 1A 00 00 00 06 03 05 00 00 00 00` / same | `03 05 00 00 00 00 CC 28` / same | 29.095 | `00000000` PASS |
| switch_7 ON | `39 1D 00 00 00 06 03 05 00 07 FF 00` / same | `03 05 00 07 FF 00 3C 19` / same | 28.943 | `00000001` PASS |
| switch_7 OFF | `39 20 00 00 00 06 03 05 00 07 00 00` / same | `03 05 00 07 00 00 7D E9` / same | 28.994 | `00000000` PASS |

## 7. Native TCP ACK 實證：現況 blind accept

以 `AsyncModbusTcpDriver.write()` 的真實 production method 加 mock `_send_and_recv()`，request transaction low byte 特意為 `0F`，確認不是 transaction ID 偶然避開 guard。結果如下，九例全 `returned=True`、無 exception：

| MBAP response case | 現有 `write()` 結果 |
| --- | --- |
| 合法 FC05 ACK | accepted |
| 合法 FC15 ACK | accepted |
| 錯 transaction ID | **accepted** |
| 錯 UID | **accepted** |
| 錯 FC | **accepted** |
| 錯 start address | **accepted** |
| 錯 quantity | **accepted** |
| 錯 MBAP declared length | **accepted** |
| `FC=8F, exception=01` | **accepted** |

原因已由實際呼叫證實：MBAP request 無 RTU CRC，父類 guard 進入早退 `return True`。這不是對 FC15 特有的問題，卻是任何新增 native-TCP FC15 寫入的安全阻擋項。

## 8. `None → success` 與 sentinel 實證

使用真實 `BusMasterScheduler._process_write()`、mock adapter/driver/HA：

| Verify decoded group value | write/read calls | HA `publish_state` | 結果 |
| --- | ---: | ---: | --- |
| `None` | 1 / 1 | 1 | **立即成功路徑 confirmed**，`success_count=1` |
| `__UNMATCHED__` | 3 / 3 | 0 | 三次 retry，無 state publish，最後記「回讀值不符」error |

因此 038 的 fail-open 是真實的。群組 adapter 的正式契約必須保證：對每個合法 group key，任何未完全符合命名 state 的回讀都回 `__UNMATCHED__`（或同等不可能合法 state），絕不回 `None`。

`_values_equal("__UNMATCHED__", "path_a") == False` 已實測。目標 `[ON, OFF, ON, ON]` 的四個逐 bit flip 都映射 `__UNMATCHED__`，只有 exact vector 映射 `path_a`。這可讓 BusMaster 維持 NO CHANGE：它不會 publish 群組成功或寫入成功 log。注意耗盡時現有 code 呼叫 `_record_success()` 是為了「回讀不符但 transport 仍通」保持 ONLINE；不是驗證成功，卻也不會讓 availability 變 failure。

## 9. FC01 multi-bit verify、舊功能與 HA observable

方案 B 可直接沿用既有整板 FC01：isolated TCP poll 是 `00 01 00 00 00 06 03 01 00 00 00 08`；注入 data `A5` 後既有 decoder 一次得到：

```text
switch_0 ON, switch_1 OFF, switch_2 ON, switch_3 OFF,
switch_4 OFF, switch_5 ON, switch_6 OFF, switch_7 ON
```

所以正式群組 verify 應走既有 `read_coils`（start 0/count 8）：以群組 slice逐 bit exact match，再將既有八個 switch state 一次 publish；不要新增第二套 bit decoder。

未施工下的 isolated regression：

```text
FC06 RTU:       03 06 00 10 00 2A 08 32
FC16 16-bit:    03 10 00 20 00 01 02 00 2A 39 8F
FC16 32-bit:    03 10 00 30 00 02 04 01 02 03 04 5A 0C
FC16 verify:    03 03 00 30 00 02 C5 E6 (count=2, strict 4 data bytes)
```

每一幀 CRC 都與 project helper/independent implementation 一致。HA discovery 仍為八個 `py_1f_relay_8ch_3_switch_0..7`，state topic `py_1f/relay_8ch/3/state`、command topic `py_1f/relay_8ch/3/set/switch_n`、payload `ON/OFF` 與兩層 availability 皆不變。

```text
HA observable regression = PASS
FC01 / FC05 / FC06 / FC16 regression = PASS
```

## 10. 活組 schema 與正式施工範圍

正式 `coil_groups` 仍應由 profile 定義，不把 01/23/45/67 或 ATS 規則寫死在 GenericAdapter。validator 必須拒絕：非連續 members、`count != len(members)`、state length錯、非 bool/ON/OFF token、重複 state vectors、不存在的 `verify_command_id`、overlap groups。ATS 若不允許 `[ON, ON]`，只是不將它列入 `states`；硬體 electrical interlock 仍不可由 Modbus 取代。

最小施工裁決：

| 檔案 | 裁決 | 理由 |
| --- | --- | --- |
| `adapters/generic_adapter.py` | YES | 共用 LSB-first packer、RTU FC15、exact group/sentinel verify contract |
| `adapters/modbus_tcp_adapter.py` | YES | UID3 走 native TCP，需以共用 PDU 包 MBAP FC15 |
| `src/modbus_tcp_driver.py` | **YES，同一施工輪** | FC15 若走 native TCP，不能讓錯 ACK/exception blind accept；mock 已實證風險 |
| `src/driver.py` | NO CHANGE | RTU CRC ACK guard 對 FC15 start/quantity echo 已正確 |
| `src/bus_master.py` | NO CHANGE（有 sentinel 契約） | 同一 attempt 的 write→ACK→FC01 已在同一 `bus_lock`；retry 間仍可能插入其他工作，必須如實記錄 |
| `src/ha_manager.py` | NO CHANGE | 使用既有 select、topics、state publish |
| `src/map_validator.py` | YES | 只新增 `coil_groups` 安全 schema 驗證 |
| `profile/relay_8ch_map.yaml` | NO CHANGE | 作穩定 FC05 基線 |
| `profile/relay_8ch_map2.yaml` | 後續新建 | 僅在施工批准後建立，加入 group select |

`modbus_tcp_driver.py` 不應再另案延後：雖然會覆蓋現有 FC05/06 native TCP write path，但本輪已取得全九類 mock ACK、實機 FC05及產物 regression 基線；隨 FC15 一起做最小 MBAP parser/validator 才能讓新的 write path有完整 ACK 防線。必須先以這些 case 做 isolated regression，且不要改 RTU `driver.py`。

## 11. 測後恢復與最終裁決

測後 `ginlong` 已在 15:32:25 重啟。日誌證據：兩台設備正常掛載、MQTT connected、Discovery 兩台送畢、UID1 在 15:32:36 與 UID3 在 15:32:41 恢復 ONLINE；容器 `running`，restart count=0。原始八個 HA switch discovery 已重新發出。實機 final FC01 亦為 baseline 全 OFF。

1. UID3 是否實機支援 FC15？ **YES**
2. FC15 request 是否與 PyModbus byte-for-byte 一致？ **YES，MBAP/RTU 2/3/4/8及 restore 均一致**
3. FC15 ACK 是否正確？ **YES（實機 probe parser）；NO（現有 production native-TCP guard）**
4. FC01 multi-bit verify 是否成立？ **YES，整板逐 bit實機驗證；正式功能尚待實作**
5. 是否需要 FC05 loop？ **NO**
6. 是否可建立 Profile 活組？ **YES**
7. GenericAdapter 是否需修改？ **YES**
8. Driver 是否需修改？ **YES，僅 `modbus_tcp_driver.py`；`driver.py` NO CHANGE**
9. BusMaster 是否需修改？ **NO，前提是 sentinel contract；retry atomicity 僅限單次 attempt**
10. 是否建議進入正式施工？ **YES**

最終：**PASS WITH CHANGES**。

## 附錄：可重跑產物

- [實機測試工具](/root/py_1f/scratch/test_uid3_fc15_real_switch_039.py)
- [完整實機 JSON TX/RX log](/root/py_1f/scratch/fc15_039_live_results.json)
- [隔離 mock/回歸工具](/root/py_1f/scratch/fc15_039_isolated_contracts.py)
- [隔離 27/27 結果](/root/py_1f/scratch/fc15_039_isolated_results.json)

實機工具要求 Gateway 已停止且 `.190:502` 無其他 master；它只在本輪已明確授權的空載 Relay 條件下可執行。
