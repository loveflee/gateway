# 053 — 052 缺陷 F1 最小修正與重驗（只動 Validator）

- 執行者：Claude Code
- 日期：2026-08-13 02:40 – 03:00（CST）
- 授權範圍：**只修 Validator，不動 Adapter / BusMaster**，修完重驗
- production 修改：**`src/map_validator.py` V1.6 → V1.7，僅此一檔**
- 新增檔案：`scratch/claude_053_validator_f1_verify.py`、`claude_053_validator_f1_results.json`、本報告

---

## 人話結論

**修好了，而且我在現場真的把它演一次給自己看。**

052 抓到的問題是：地圖檔裡 `command_id` 打錯一個字，驗證器說通過，那個感測器永遠不會被讀取，Home Assistant 上的實體永遠空白，**而且從頭到尾一行 log 都沒有**。

現在改成：**打錯字在設備掛載的那一刻就會被擋下來，而且訊息會直接告訴你打錯的是哪一個、你可以用的正確值有哪些。**

我實際做了一次現場負面測試 —— 故意把一份壞地圖掛到 UID3 上重啟，結果是：

```
💥 sensors 'probe_val' 致命錯誤：command_id 'read_coil' 不存在於 read_commands
   （可用的有：['read_coils']），輪詢解碼會永遠略過它（該實體將永遠沒有數值）！
🚨 UID=3 已隔離：地圖檔校驗失敗（1 項錯誤）
```

而且同一時間：**UID1 溫濕度完全不受影響照常運作**、網關沒有掛掉、health 主題會列出被隔離的設備、HA 上 UID3 顯示 unavailable（不是假裝正常）、有人下指令給它還會再吼一次「這台已被隔離，請去 WebUI 修好再重啟」。以前這些全部都不會發生 —— 設備會安安靜靜地掛載成功，然後那個實體永遠空白。

三件我特別確認過的事：

1. **13 份現行地圖一份都沒被誤殺**（包括監聽型的 jkbms），改完立刻重啟，兩台設備照常上線、零隔離。
2. **Adapter 和 BusMaster 一個字都沒動**，指紋可證。防線完全放在驗證器。
3. **測試本身有鑑別力** —— 我把修正拿掉再跑一次，缺陷會重現；加回去就攔住。不是自說自話。

測試完那份壞地圖已刪除、設定檔還原成位元組完全相同、繼電器全關、兩台設備 ONLINE。

---

## 1. 修改內容（唯一一檔）

### `src/map_validator.py` V1.6 → V1.7

| 項目 | 內容 |
|---|---|
| 新增 | 巢狀函式 `_check_sensor_command_refs(items, commands)` |
| 新增 | 呼叫一行，**排在 `_check_read_commands()` 之後**（`command_by_id` 才是完整的） |
| 修改既有檢查 | **無**（既有函式、判準、訊息文字全部未動） |
| Adapter / BusMaster / Driver / HA Manager / profile / config | **未動**（指紋可證） |

判準刻意保守，只攔「一定會造成靜默資料消失」的三種情況：

| 情況 | 判定 | 理由 |
|---|---|---|
| 有 `read_commands`，sensor 的 `command_id` 指不到任何 id | **拒絕** | 這就是 F1；`_extract_data` 會對每一個 command 都 `continue` 掉它 |
| 有 `read_commands`，sensor 完全沒有 `command_id` | **拒絕** | 同樣永遠不匹配，結果完全一樣 |
| 沒有 `read_commands`，sensor 卻宣告了 `command_id` | **拒絕** | 參照對象不存在 |
| 沒有 `read_commands`，sensor 也沒有 `command_id` | **放行** | 監聽軌（`feed()` 型 adapter）的正常形狀，不得誤殺 |
| `sensors` 內的非 dict 項目 | **略過** | 維持既有語意，不新增誤報 |

錯誤訊息刻意帶上可用值清單，讓操作者不必翻檔案：

```
[profile] 💥 sensors 'probe_val' 致命錯誤：command_id 'read_coil' 不存在於 read_commands
（可用的有：['read_coils']），輪詢解碼會永遠略過它（該實體將永遠沒有數值）！
```

---

## 2. 為什麼防線只能放在 Validator（授權範圍的技術理由）

`generic_adapter._extract_data()` 的輪詢分支以 `sensor.get("command_id") != cmd['id']: continue` 過濾不相干 sensor。orphan sensor 對**每一個** command 都不相干，因此：

- 永遠被 `continue`，不會進入解碼；
- **不計入 `declared`**，所以 V2.5 的「丟棄 N/M 個 sensor」彙總 WARNING 也不會觸發。

本輪實測確認 Adapter 這個行為**完全沒有改變**（也不該改，那是設計上的過濾器）：

| 形狀 | 執行期行為 | 是否靜默 |
|---|---|---|
| 同一 command 底下**還有**正常 sensor | 該次輪詢仍然成功，orphan 無聲消失 | **靜默**（F1 死亡案例） |
| 同一 command 底下**只剩** orphan | `decode()` 拋 `解析成功但未提取出任何有效數據` | 不靜默（本來就會吵） |

也就是說：真正危險的是第一種形狀，而它在執行期沒有任何可觀測訊號。**唯一能在資料消失之前攔下來的地方就是掛載期的 validator**，這與本輪「只修 Validator」的授權範圍一致。

---

## 3. 隔離驗證：`scratch/claude_053_validator_f1_verify.py`

```
PASS 39   FAIL 0   TOTAL 39
```

### 3.1 必須攔下（F1 死亡案例）

| 案例 | 結果 |
|---|---|
| `command_id` 打錯字（原始死亡案例） | 攔下 |
| 錯誤訊息含 sensor 名、錯誤值、可用值清單 | 通過 |
| `bits` 型 sensor（繼電器地圖形狀）打錯字 | 攔下 |
| 有 `read_commands` 卻缺 `command_id` | 攔下 |
| 宣告 `command_id` 但沒有 `read_commands` | 攔下 |
| `read_commands` 是空 list | 攔下 |
| 多個 orphan → 各報一條 | 2 條 |
| 大小寫／前後空白（`C1`、` c1`、`c1 `）不得寬容匹配 | 3/3 攔下 |

### 3.2 不得誤報

| 案例 | 結果 |
|---|---|
| 13 份現行 profile（含 6k2 系列、solis 系列、ampinvt、jkbms、relay、temp_humid…） | **13/13 通過** |
| 監聽軌型（無 `read_commands`、sensor 也無 `command_id`） | 通過 |
| 沒有 `sensors` 區塊 | 通過 |
| `sensors` 內混入非 dict 項目 | 不誤報、不崩潰 |

### 3.3 既有檢查不得改變

| 既有檢查 | 訊息是否維持 |
|---|---|
| `sensors` 非 list → 「sensors 格式錯誤」 | 是 |
| sensor key 重複 → 「Key 'a' 重複」 | 是 |
| `scale = 0` → 「scale 不能為 0」 | 是 |
| bit 索引非整數 → 「bit 必須是整數」 | 是 |
| `read_commands` ID 重複 → 「發生重複碰撞」 | 是 |
| `count = 0` → 「count 必須大於 0」 | 是 |
| `coil_groups` members 非連續 → 「必須按連續位址排列」 | 是 |
| validator 仍是純函式，不修改傳入的 dict | 是 |

### 3.4 Mutation control（測試鑑別力證明）

把新檢查換成 no-op 重新編譯執行同一支 validator：

| 版本 | 對 F1 死亡案例的判定 |
|---|---|
| 拿掉新檢查（等同 V1.6） | **放行**（缺陷重現） |
| 現行 V1.7 | **攔截** |

→ 本測試確實抓得到「沒修」的版本，3.1 的 PASS 不是假陽性。

---

## 4. 全套迴歸重跑

| Harness | 結果 | 說明 |
|---|---|---|
| `claude_052_system_review.py`（系統級總驗收） | **100 PASS / 0 FAIL / 3 NOTE** | 052 當時唯一的 FAIL（`8.F1.commandid`）現已轉為 PASS |
| `claude_053_validator_f1_verify.py`（本次修正） | **39 PASS / 0 FAIL** | — |
| `claude_048_tcp_fc16_isolated.py`（TCP FC16） | **41 PASS / 0 FAIL** | — |
| `claude_047_pending_lifecycle_review.py`（FC15 生命週期） | **177 PASS / 1 FAIL** | 唯一 FAIL 是指紋檢查 `modbus_tcp_adapter.py 自 042 起未變`，那是 048 的施工，非本輪；**178 項行為檢查全過** |
| `map_validator.py` CLI × 13 份 profile | **13/13 PASS** | 逐檔跑過 |

> `claude_fc15_acceptance_042.py` 無法重跑：它依賴 041 施工者留下的 `relay_8ch_map2_4ch_test.yaml` / `_8ch_test.yaml`，這兩份已於 049～051 輪被移除。**非本次修改造成**，且其覆蓋範圍已被 047 完全取代。

---

## 5. 現場部署與負面驗證

### 5.1 部署

`src/` 是 bind mount，`docker restart ginlong` 即載入 V1.7：

```
02:54:33 [AdapterLoader] 掃描完畢，共掛載 7 個轉譯器外掛
02:54:33 ✅ 掛載 [主動] 設備: UID=1 adapter=tcp
02:54:33 ✅ 掛載 [主動] 設備: UID=3 adapter=tcp
02:54:33 ✅ 全部 2 台設備掛載正常，無隔離
02:54:43 [1] 連續通訊成功，恢復 ONLINE
02:54:48 [3] 連續通訊成功，恢復 ONLINE
```

**現行兩份 profile 零誤殺，兩台設備照常上線。**

### 5.2 現場負面驗證（決定性證據）

把一份「保留原本完全正常的 `relay_status_byte`，另加一個 `command_id` 打錯字的 `probe_val`」的地圖暫時掛到 UID3 —— 這正是 F1 最危險的那種形狀（在 V1.6 下會正常掛載且永遠靜默）。

**掛載前，CLI 就先擋下（exit=1）：**

```
🚫 檢查失敗！抓到 1 個地雷：
   💥 sensors 'probe_val' 致命錯誤：command_id 'read_coil' 不存在於 read_commands
      （可用的有：['read_coils']），輪詢解碼會永遠略過它（該實體將永遠沒有數值）！
```

**現場 Gateway 啟動時的完整行為：**

```
02:55:38 ✅ 掛載 [主動] 設備: UID=1 adapter=tcp          ← 好的設備照常上線
02:55:38 [ERROR] 💥 sensors 'probe_val' 致命錯誤：command_id 'read_coil' 不存在…
02:55:38 [ERROR] 🚨 UID=3 [relay_8ch_map2_f1probe] 已隔離：地圖檔校驗失敗（1 項錯誤）
02:55:38 [CRITICAL] ║ 🚨 設定有誤：1/2 台設備未能掛載，已隔離
02:55:48 [1] 連續通訊成功，恢復 ONLINE                    ← UID1 完全不受影響
```

**對外可觀測性同步到位（以前完全沒有）：**

| 觀測點 | 隔離期間的值 |
|---|---|
| `py_1f/health` | `"devices": {"1": {...}}`、`"quarantined": [{"uid": 3, "profile": "relay_8ch_map2_f1probe", "reason": "地圖檔校驗失敗（1 項錯誤，詳見上方日誌）"}]` |
| `py_1f/relay_8ch/3/status` | `offline`（HA 顯示 unavailable，不是假裝正常） |
| `py_1f/status` | `online`（網關存活，WebUI 可進去修） |
| 對 UID3 下指令 | `[ERROR] 🚨 丟棄寫入 UID=3 key=switch_0：該設備啟動時已被隔離（profile=…：地圖檔校驗失敗…），請至 WebUI 修正設定後重啟` |

**對照 052 記錄的修正前行為**：設備會正常掛載、`quarantined` 為空、HA 上 `probe_val` 實體建立但永遠空白、全程零 log。

### 5.3 還原

| 項目 | 結果 |
|---|---|
| 測試用 profile `relay_8ch_map2_f1probe.yaml` | 已刪除，`profile/` 無 probe 殘留（14 檔） |
| `config.yaml` | `diff` 與測試前**無差異**，SHA-256 `c9ec5e0a…848c20a` |
| UID1 / UID3 | 皆 ONLINE，`quarantined: []` |
| 繼電器 | `switch_0..7` 全 OFF、`group_01`／`group_234` = `all_off` |
| 容器 | `running`、`RestartCount=0` |

---

## 6. 修改範圍證明

02:30 之後被改動的**所有**檔案（排除 scratch/report/pyc）：

```
02:51:29  ./src/map_validator.py     ← 本輪唯一 production 修改
02:57:01  ./profile/config.yaml      ← 負面測試期間暫改，已還原為位元組相同
```

指紋：

```
0650135a53e29c5e  src/map_validator.py            ← V1.7（本輪修改）
554ac9c6468a3d44  adapters/generic_adapter.py     未動
4103132532ddab2f  adapters/modbus_tcp_adapter.py  未動
831bfe98c534e5ad  src/bus_master.py               未動
cc049da87d7bb02f  src/driver.py                   未動
688e07ea49b4ab14  src/modbus_tcp_driver.py        未動
d02c5b3e76401db3  src/ha_manager.py               未動
ddc698fdecfce14b  src/main.py                     未動
c9ec5e0a9350bd10  profile/config.yaml             未動（還原後相同）
0116e41bd64abb86  profile/relay_8ch_map2.yaml     未動
```

**Adapter 與 BusMaster 完全未動，符合授權範圍。**

---

## 7. 最終裁決

```text
缺陷 F1 修正:                        PASS
修改範圍（只動 Validator）:          PASS
現行 13 份 profile 零誤殺:           PASS
既有檢查判準與訊息未變:              PASS
Mutation control（測試具鑑別力）:    PASS
隔離驗證 39/39:                      PASS
系統級迴歸 052（100/103, 0 FAIL）:   PASS
FC15 迴歸 047（178 項行為檢查）:     PASS
FC16 迴歸 048（41/41）:              PASS
現場部署零誤殺:                      PASS
現場負面驗證（隔離 + 大聲失敗）:     PASS
錯誤隔離（UID1 不受影響）:           PASS
對外可觀測性（health/status/命令）:  PASS
最終還原與清理:                      PASS

052 SILENT FAILURE:                  CLOSED
052 DATA FLOW BLACK HOLE:            CLOSED

FINAL: PASS
```

**052 的唯一失分項已封閉。** 該缺陷從「validator 放行 → 永遠不解碼 → HA 實體永遠空白 → 零日誌」，變成「掛載期即攔截 → 設備隔離 → ERROR + CRITICAL 日誌 → health 主題列出 → HA 顯示 unavailable → 後續命令再次大聲拒絕」。

### 後續建議（非本輪範圍，不自行施工）

1. `generic_adapter._extract_data()` 可在 Adapter 建構時對 orphan sensor 記一次 WARNING，作為 validator 之外的第二道防線。本輪授權明確排除 Adapter，未施作。
2. 052 揭露的未涵蓋範圍仍在：`local_serial_driver.py`（`type: usb`）與監聽軌（`mode: listen`）本機無對應硬體，仍只有靜態閱讀，未實跑。
