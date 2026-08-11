# 人話結論

1. UID1 的 33148 raw 是 `0x0078–0x008C`（120–140 W）。
2. UID2 的 raw 是 `0x0050–0x005A`（80–90 W）。
3. UID3 的 raw 是 `0x0050–0x005A`（80–90 W）。
4. 三台各 30 筆、合計 90 筆合法 frame 中，**沒有任何一筆**出現 `0xFFxx` 或其他 sign bit set 的 raw。
5. 因為正 raw 用 `uint16` 與 `int16` 解出的數字完全相同，這**沒有證明** 33148 的共用 datatype 是 `int16`。
6. 它也還不足以證明 UID4 firmware 特例：UID1–UID3 當下可能沒有處於會輸出負 backup power 的狀態。
7. 建議目前**先不改**共用 map，也**不建立** UID4 專用 map；應等待正常 UID 在已知負 backup power 狀態的唯讀 raw 對照。
8. 最終裁決：**INCONCLUSIVE**。

## 1. 範圍與安全邊界

本輪只讀 UID1、UID2、UID3 的 33147–33150：

```text
FC04 / start address 33147 / count 4
```

沒有修改 production code、profile、config、Adapter、Driver、BusMaster 或 Docker；沒有 FC05、FC06、FC16、FC22 或其他寫入。唯一變動在 `.gitignore` 排除的 `scratch/`：

- 將既有 `test_uid4_33148_signedness.py` 泛化為受限的 `--uid {1,2,3,4}`，仍只允許 FC04。
- UID1／UID2／UID3 各自的 `.log` 與 `.json` raw capture。

每次實機 capture 前以：

```bash
docker ps --filter name='^/ginlong$'
```

確認無輸出；腳本也會在送出第一個 frame 前再次檢查。三台依序、非交錯執行：UID1 → UID2 → UID3，UID1→UID2 間隔約 39 秒、UID2→UID3 間隔約 18 秒，均超過要求的 2–3 秒。

## 2. 固定讀取契約

沿用 [035 UID4 raw 實機驗證](035_UID4_33148_backup_load_power_signedness實機驗證.md) 已確認的正式 map 語意：

| Register | 解碼（僅供觀察） |
| --- | --- |
| 33147 | `household_load_power` / uint16 |
| 33148 | 同時顯示 uint16 與 int16 |
| 33149–33150 | `battery_power` / big-endian int32 |

每台的 request 都由 GenericAdapter 現用 `calc_crc16()` 產生；只有 UID 改變：

| UID | FC04 RTU request |
| --- | --- |
| 1 | `01 04 81 7B 00 04 A9 EC` |
| 2 | `02 04 81 7B 00 04 A9 DF` |
| 3 | `03 04 81 7B 00 04 A8 0E` |

每筆有效 RX 都必須同時滿足：target UID、FC04、byte count=8、CRC 正確。每份 JSON 都保存 timestamp、UID、TX/RX hex、33147 raw/uint16、33148 raw/uint16/int16、33149–33150 int32。

## 3. 實機結果

### UID1

時間：2026-08-11 23:37:37 至 23:38:06；30/30 valid response。

| 項目 | 結果 |
| --- | --- |
| 33148 raw 範圍 | `0x0078–0x008C` |
| uint16 / int16 | 120–140 / 120–140 |
| raw 分布 | `0x0078` ×1、`0x0082` ×21、`0x008C` ×8 |
| 33148 sign bit set | 0 / 30 |
| 33147 household | 6013–7125 W |
| 33149–33150 battery int32 | 1422–1730 W |

### UID2

時間：2026-08-11 23:38:45 至 23:39:14；30/30 valid response。

| 項目 | 結果 |
| --- | --- |
| 33148 raw 範圍 | `0x0050–0x005A` |
| uint16 / int16 | 80–90 / 80–90 |
| raw 分布 | `0x0050` ×7、`0x005A` ×23 |
| 33148 sign bit set | 0 / 30 |
| 33147 household | 0 W |
| 33149–33150 battery int32 | 1429–1926 W |

### UID3

時間：2026-08-11 23:39:32 至 23:40:01；30/30 valid response。

| 項目 | 結果 |
| --- | --- |
| 33148 raw 範圍 | `0x0050–0x005A` |
| uint16 / int16 | 80–90 / 80–90 |
| raw 分布 | `0x0050` ×15、`0x005A` ×15 |
| 33148 sign bit set | 0 / 30 |
| 33147 household | 0 W |
| 33149–33150 battery int32 | 1506–1826 W |

## 4. Transport 與 frame 品質

| Counter | UID1 | UID2 | UID3 |
| --- | ---: | ---: | ---: |
| valid target frame | 30 | 30 | 30 |
| pre-TX flush events / bytes | 0 / 0 | 0 / 0 | 0 / 0 |
| timeout | 0 | 0 | 0 |
| bad CRC | 0 | 0 | 0 |
| wrong UID | 0 | 0 | 0 |
| wrong FC | 0 | 0 | 0 |
| wrong byte count | 0 | 0 | 0 |
| unclassified RX | 0 | 0 | 0 |

這比 035 後段補充測試中的外來／殘留 bytes 乾淨得多；但它只表示此 90 個 frame 的 transport 驗證乾淨，並不能以主機端證明原廠 WiFi master 永遠不存在。

## 5. 離線逐筆複驗

對三份 JSON 的 90 筆 `rx_hex` 逐筆使用目前 scratch 腳本重新執行：

1. request bytes 必須等於該 UID 的固定 FC04 vector；
2. frame classifier 必須判為 `valid`；
3. decoder 重算的 household、backup uint16/int16、battery int32 必須與 JSON 相同。

結果：

```text
UID1: OFFLINE AUDIT PASS (30 frames)
UID2: OFFLINE AUDIT PASS (30 frames)
UID3: OFFLINE AUDIT PASS (30 frames)
```

三份 log 各有 30 個逐筆輸出，三份 JSON 各有 30 records。

## 6. 與 UID4 的對照及正確判讀

035 的 UID4 60 筆 raw 都是 `0xFF9C–0xFFC4`，解為 int16 是 -100 至 -60；本輪的 UID1–UID3 90 筆都是 `0x00xx`。

| 設備 | 樣本 | 33148 raw | int16 結果 |
| --- | ---: | --- | --- |
| UID1 | 30 | `0x0078–0x008C` | 120–140 |
| UID2 | 30 | `0x0050–0x005A` | 80–90 |
| UID3 | 30 | `0x0050–0x005A` | 80–90 |
| UID4（035） | 60 | `0xFF9C–0xFFC4` | -100～-60 |

這個對照已證實「當下 raw 值型態不同」，但**不能**從正 raw 反推 uint16 是正確的 protocol datatype：`0x005A` 用 uint16 與 int16 都是 90。也不能證明 UID1–UID3 的 firmware 在負 backup power 時不會回 `0xFFxx`。

因此，本輪沒有達成下列任何一種可施工門檻：

- **COMMON MAP BUG**：未看到 UID1–UID3 的 signed-looking raw，不能用本輪資料證明共用 map 必須改成 int16。
- **UID4-SPECIFIC STRONGLY SUPPORTED**：雖有正／負 raw 的當下差異，但沒有已確認「同類負 backup power 狀態」下的正常 UID 對照。

## 7. 建議與未納入項目

目前建議：**先不改**。

下一輪若需提高證據強度，應等待 UID1、UID2 或 UID3 處於可獨立確認的負 backup power 情境，並在同樣的單 master／FC04-only 條件下重做 10–20 個 raw samples。裁決規則如下：

- 正常 UID 也出現 `0xFFxx` 且 int16 合理 → 可判 **COMMON MAP BUG**，再另開 profile-only 修改。
- 正常 UID 在可確認同類負功率情境仍持續正 raw，而 UID4 持續 `0xFFxx` → 可提升 **UID4-SPECIFIC BEHAVIOR** 的可信度。

本輪不修改 `solis_inverter_map2.yaml`、不新增 UID4 map，也不改 Home Assistant 顯示。

## 8. 最終裁決

**INCONCLUSIVE**

理由：已取得高品質的 UID1–UID3 正 raw 對照，但沒有任何正常 UID 的 sign-bit 樣本；正數的 uint16/int16 相同，不能決定共用 map datatype，也不足以將 UID4 定性為 firmware 特例。
