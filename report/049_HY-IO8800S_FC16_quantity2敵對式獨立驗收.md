# 049 — HY-IO8800S FC16 quantity=2 敵對式獨立驗收

- 角色：Adversarial Independent Reviewer（未參與 048 施工）
- 範圍：讀碼、全新 isolated hostile harness、runtime 唯讀檢查；**未修改 production code / profile / config**。
- 裁決規則：本任一硬門檻未證實即 FAIL；不使用「PASS WITH LIMITATIONS」。

## 人話結論

1. 048 的 `40133 / 0x0084` 位址判斷：**UNPROVEN**。本輪未能取得 048 所稱原廠「產品配置說明書」的 register 章節，也未能以本輪正式 Gateway 做 FC03 交叉證實。
2. `40133 / 0x0084` 實機證實：**NO**。
3. TCP Adapter 的 FC16 程式碼：**isolated PASS**；但未獲本輪 production 閉環驗收。
4. quantity=2 與 4-byte strict verify：**isolated PASS**；實機 **UNPROVEN**。
5. `0x12345678` 實機 byte-for-byte 回讀：**NOT TESTED**。
6. `100000` 實機回讀：**NOT TESTED**。
7. int32 / float32 isolated：**PASS**。
8. FC01 / FC05 / FC06 / FC15 / FC16 legacy regression：**isolated PASS**。
9. 原 HA/MQTT observable：**isolated PASS**；現場 MQTT 穩定性不通過。
10. isolated 未發現 FC16 資料流黑洞；production closed-loop 未證實，故不能宣稱 NONE。
11. 本輪未發任何實機寫入，所以沒有新增狀態需要還原；但也未能完成要求的 64-register readback／全 OFF 最終確認。
12. **FINAL：FAIL**。

FAIL 的原因不是把 048 的結果倒過來猜，而是本輪獨立檢查確認：現行 UID3 正式 profile 根本沒有 `rule1_param1` 的 FC16 command route；而本輪又明確禁止修改 profile/config。以 raw socket 繞過 Gateway 或暫切 scratch profile 都會違反任務，故未做。

## 受驗版本與零修改證據

本機不是 Git working tree（`git status` 回覆 `fatal: not a git repository`），故以 SHA-256 前後比對取代 diff。驗收前後下列 11 檔完全相同；前後檔案清單在 `scratch/fc16_049_{pre,post}_sha256.txt`，其 diff 為空。

| 檔案 | SHA-256 |
| --- | --- |
| `AGENTS.md` | `b05f6f7098338916…c5f2581d22` |
| `adapters/generic_adapter.py` | `554ac9c6468a3d…4ffdb484` |
| `adapters/modbus_tcp_adapter.py` | `4103132532ddab…f84c83` |
| `src/bus_master.py` | `831bfe98c534e5…139e4661e` |
| `src/driver.py` | `cc049da87d7bb…880842d66` |
| `src/modbus_tcp_driver.py` | `688e07ea49b4ab…6b95a75e` |
| `src/ha_manager.py` | `d02c5b3e76401…e4661e` |
| `src/map_validator.py` | `d4f256328b20ca…dcbf530` |
| `profile/relay_8ch_map.yaml` | `8d9b093e3e4f…a2c27475c` |
| `profile/relay_8ch_map2.yaml` | `0116e41bd64a…4ffdb484` |
| `profile/config.yaml` | `c9ec5e0a9350…ae848c20a` |

本輪新增的只有 `scratch/fc16_049_independent_review.py`、其 stdout 以及本報告。沒有修改 Docker、production source、profile、config，沒有 restart，沒有 raw Modbus sender。

## 手冊、設備與安全靶點：未放行

本輪自行搜尋並檢視可公開取得的原廠 [HY-IOS 產品資料](https://webapi.huayuniot.com/static/upload/file/20241122/1732267630548963.pdf)。它可確認 HY-IO8800S 是該原廠的 8 DI / 8 DO 系列產品並使用標準 Modbus RTU，但**不是** 048 所引用、可供核對 3.1.5.2 register layout 的「產品配置說明書」。對 `40129`、每組 16 bytes、`40377`、參數 1 地址與 FC03/06/16 register 規格，本輪沒有取得可獨立引用的一手章節；精確搜尋也沒有找到該文件。

因此不能接受「手冊表格有 typo」作為本輪結論。雖然 `40133 - 40001 = 0x0084` 與每 8 registers 的數學推導彼此相容，沒有原廠 register 表加上本輪實機 FC03 8-rule readback，仍非唯一證據。手冊 layout、UID3 型號 ASCII、規則 1 mode/action/DO/DI/param1/param2、規則 1 是否 mode=0、及 64-register baseline 全部是 **UNPROVEN**。

## 為何正式實機閉環不可執行

現行 `profile/config.yaml` 的 UID3 固定為 `adapter: tcp`、`profile: relay_8ch_map2`。現行 map 的 `settings` 僅有 coil `0x0000`–`0x0007` 的 FC05 switch，`read_commands` 也只有 FC01 0..7；沒有 FC03 holding-register command、`rule1_param1` sensor、FC16 setting 或 HA number entity。直接以目前 TcpAdapter 載入這份 map 執行 `encode_write("rule1_param1", 1)` 明確 `ValueError`，isolated evidence 已收錄於本輪 harness。

故下列要求不能同時成立：

```text
禁止修改 profile/config
        +
必須 MQTT → 正式 Gateway → TcpAdapter → UID3 FC16 @ 0x0084
```

`scratch/` 中雖有舊輪留下的暫時 profile，但它不是目前 runtime；啟用它必然修改 config/profile，且本輪不得採信其地址、讀值或先前結果。未使用它。

另外，唯讀 runtime 健康檢查顯示 `ginlong` running、`RestartCount=0`，且 UID3 僅有 Gateway Python 一條 TCP connection；但最近日誌持續出現 `MQTT unexpected disconnect rc=Unspecified error`，約兩秒後 reconnect。這使 MQTT command/state capture 亦不具可接受的穩定性。沒有為此重啟或修復。

## TCP FC16 重新讀碼與獨立 isolated 證據

目前 V1.3 的 TCP Adapter 在 `encode_write()` 僅在 `_resolve_fc16_codec()` 能從 `link_sensor`／同名 sensor 明確得到 4-byte `uint32`、`int32` 或 `float32` 與合法 word order 時，才組 FC16 quantity=2；否則維持 legacy FC16 quantity=1。`build_verify_read()` 對同一 codec 建 FC03 quantity=2，帶 `strict_verify=True`、`expected_data_bytes=4`、`expected_count=2`；`decode()` 再交給 GenericAdapter 的嚴格 byte-count 驗證。因此短回應或錯 byte count 不能退化成 16-bit 成功。見 [TCP Adapter](/root/py_1f/adapters/modbus_tcp_adapter.py:101)、[strict decode](/root/py_1f/adapters/modbus_tcp_adapter.py:233)。

本輪全新 `scratch/fc16_049_independent_review.py` 直接載入目前 production class，不 import 或執行 048 的 scratch。結果：**308 PASS / 0 FAIL**。

- uint32 `0, 1, 1000, 100000, 0x12345678, 0xFFFFFFFF`：production RTU、MBAP、獨立 Big-Endian packer 與 isolated PyModbus 3.13.0 全部 byte-for-byte 相同；CRC 再由獨立 CRC 重算。
- int32 `0, 1, -1, -12345678, INT32_MIN, INT32_MAX`，float32 `0.0, 1.0, -1.0, 12.5, 123.456`：每個 codec byte 與 strict 4-byte decode 均 PASS；測得 big/little/swap/byte_swap 的 device-order 皆正確。uint32/int32 overflow 均拒絕。
- `float('nan')` 以 `ValueError` 拒絕；`float('inf')` 在 TCP Adapter 的既有早期 `int(round(...))` 產生 `OverflowError`，同樣 fail-closed（零 frame），但與 GenericAdapter 的 exception class 不一致。這是可改善的一致性問題，非 silent success。
- Mutation A（把 TCP FC16 改回 `NotImplementedError`）、B（verify quantity 偷降為 1）、C（只比 `0x12345678` 前 16 bits 而接受 `0x12340000`）均被 harness 抓到。
- strict hostile vectors `0x12340000`、`0x00005678`、`0x78563412`、`0x56781234`、2-byte short response 與 byte-count 不符均不能成功；完全相等的 4 bytes 才通過。

Native TCP ACK guard 也由全新 mock 走 production `AsyncModbusTcpDriver.write()` 重驗：FC05、FC06、FC15、FC16 quantity=1、FC16 quantity=2 的合法 ACK 全部 True；wrong transaction/protocol/length/UID/FC/address/quantity、short、truncated、overlong 全部拒絕；FC16 exception `0x90` 回 False。這與 [ACK guard](/root/py_1f/src/modbus_tcp_driver.py:70) 的契約一致。

## 資料流黑洞與 regression

全新 scripted driver 實際跑 `BusMasterScheduler._process_write()`，而非只讀碼。isolated 結果如下：

| 項目 | 結果 |
| --- | --- |
| A FC16 被移除 | adapter encode loud failure、0 write/0 publish |
| B 壞 ACK | 拒絕，0 publish |
| C 成功寫入卻不 verify | 抓到；正確路徑必有 write + FC03 verify |
| D verify quantity=1 | mutation 被抓到；production 強制 quantity=2 |
| E 前/後 16-bit mismatch | 3 retries、0 publish |
| F 2-byte／byte-count 回讀 | decoder reject、0 publish |
| G decode exception | 0 publish |
| H wrong word order | mismatch，0 publish |
| I timeout | retries exhausted、0 stale publish |
| J 舊路徑 | 未退化，見下列 regression |

`BusMaster` 的 write → ACK → verify 仍在同一 `bus_lock` 中，見 [write loop](/root/py_1f/src/bus_master.py:238)。本輪 **isolated DATA FLOW BLACK HOLE = NONE**；因未能走實機 closed-loop，production 層面的 NONE 只能標 **UNPROVEN**。

Byte-for-byte regression：FC01 0..7 未變；FC05 CH1/CH8 ON/OFF 仍為 FC05；FC06 `03 06 00 10 00 2A`；FC15 `group_01` 與 `group_234` 仍各只有單一 FC15；legacy FC16 仍為 `10 00 20 00 01 02 00 2A`（quantity=1）。兩份正式 relay map 都通過 host 與 container validator。HAManager fake MQTT discovery 也證明 8 個既有 switch/connectivity topic 和 JSON payload 逐位元組相同，未出現 FC16 entity；現行 map 只有原本兩個 group select。

## 未執行的實機項目與最終復原

沒有對 UID3 直接開 socket、沒有 MQTT command、沒有 FC03/FC16 實機 TX/RX、沒有 tcpdump。因而以下皆為 **NOT TESTED / UNPROVEN**：設備型號 ASCII、40129 讀值、8 個 rule block、0x0084 FC16 ACK、`0x12345678`／`100000` readback、MQTT state、64-register before/after byte comparison、UID1/UID3 online state、8 relay 全 OFF 與 group state final restore。

這不是遺漏，而是遵守「禁止 profile/config 修改、禁止 raw sender、所有 live test 必走正式 MQTT path」的結果。未曾寫入，所以也沒有因本輪產生的裝置狀態需要復原；但不能把「沒動」替代為「完整 restore PASS」。

## 根因與下一輪最小施工／驗收清單（本輪不施工）

根因是驗收部署面缺少一條可在正式 Gateway 安全指向已驗證靶點的 FC16 profile route；而本輪禁止建立或啟用它。MQTT 連線不穩定是第二個 closed-loop 阻斷。

下一輪必須先取得明確授權，並依序：

1. 取得並存檔原廠完整 HY-IOS configuration manual，核對 register layout；先以臨時**唯讀** profile 由正式 Gateway 讀型號、40129 起 64 registers，且紀錄 mode/action/DO/DI/兩個 param。
2. 修復並觀察 MQTT 連線穩定，確認正式 command/state topic 可用。
3. 建立經 validator 通過的受控 FC16 test profile，僅增加 `rule1_param1` 的 FC03 2-register sensor 和 FC16 setting；完整 SHA snapshot 後短暫切換、restart、測後還原 config/profile SHA。
4. 僅在 rule 1 mode=0、手冊與 FC03 layout 均唯一證實、保存完整 128-byte before snapshot 後，走 MQTT 實測 `0x12345678`、100000、原值 restore；側錄 FC16 ACK、FC03 quantity=2、MQTT state 與耗時。
5. 讀回 64 registers 逐 byte 比較，確認 relay/group、UID1/UID3、MQTT、container 與 single-master，再由新的獨立驗收者裁決。

## 最終裁決

```text
手冊 layout：                         UNPROVEN
FC03 實機 layout / 型號 / safe target：NOT TESTED
TCP FC16 encode：                      PASS (isolated)
quantity=2 / 4-byte strict verify：   PASS (isolated)
PyModbus / RTU / MBAP oracle：         PASS (isolated)
int32 / float32 codec：                PASS (isolated)
TCP ACK guard：                        PASS (isolated)
FC01 / FC05 / FC06 / FC15 / legacy16：PASS (isolated)
HA/MQTT existing observable：          PASS (isolated)
資料流黑洞：                           NONE (isolated), UNPROVEN (production)
Production MQTT closed loop：          NOT TESTED
完整 64-register restore：             NOT TESTED
Production 零修改：                    PASS

FINAL：FAIL
```
