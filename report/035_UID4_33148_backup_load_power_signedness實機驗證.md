# 人話結論

1. UID4 的 33148 實際 raw word 在 60 個有效樣本中皆為 `0xFF9C` 到 `0xFFC4`，不是一般正數功率的 `0x00xx`。
2. 現在 HA 顯示約 `6xxxx W`，直接原因是 map 將這個 raw word 當 `uint16`：例如 `0xFFC4` 會變成 `65476`。
3. 同一 raw 以 two’s-complement `int16` 解碼後是 `-100` 到 `-60 W`；60/60 筆都落在這個負值範圍。
4. `int16` 結果在本次保守的 ±20,000 W 觀察門檻內，且比 65,4xx W 更符合 6 kW inverter 的量級；這是強烈支持，不是原廠 firmware 規格證明。
5. 本輪沒有讀 UID1–UID3，因此**尚未證明** UID4 與另外三台有 firmware/datatype 差異，也無法判斷是共通 map bug 或 UID4 特例。
6. 目前**不建議立刻建立 UID4 專用 profile**；應先在已確認單 master 的條件下，分開對正常 UID 做 10–20 筆相同 raw 對照，再決定修共通 map 或做 UID4 特例。
7. 本輪只送 FC04 read；未寫 register、未改 production/profile/config/Adapter/Driver，也未重啟 Gateway。
8. **PASS**：已完成 UID4 raw、CRC、UID、FC、byte-count 與 signedness 的實機驗證；但「UID4 特例／共通問題」的範圍裁決仍是 INCONCLUSIVE。

## 1. 範圍與安全邊界

本輪目標僅為 UID4 的 33147–33150 raw 診斷。沒有修改任何 production code、profile、config、Adapter、Driver、BusMaster 或 Docker；唯一新增的是被 `.gitignore` 排除的：

- `scratch/test_uid4_33148_signedness.py`
- `scratch/uid4_33148_signedness_20260811_2323.log`
- `scratch/uid4_33148_signedness_20260811_2323.json`

primary capture 前與完成後的 `docker ps --filter name='^/ginlong$'` 檢查均未見運行中的 `ginlong` 容器。腳本要求 `--confirm-single-master` 才會允許送出第一個 request；本次以該參數執行。此確認不等於能以主機端證明原廠 WiFi master 的內部狀態，故下文另列 transport 限制。

報告定稿時的最後安全檢查則看到 `ginlong` 已是 `Up` 狀態；這是在本 agent 停止實機操作後的外部狀態變化。本 agent 沒有執行 Docker restart、compose、config 儲存或任何 write request。

`profile/config.yaml` 在本輪開始前已有未提交修改（inter-frame delay、idle timeout、四台 poll interval）；本輪只唯讀使用其中的 host、port 和 timeout，沒有寫入或格式化此檔。

## 2. 施工前確認的讀取契約

目前 `profile/solis_inverter_map2.yaml` 定義：

| Register | key | 現行 datatype |
| --- | --- | --- |
| 33147 | `household_load_power` | `uint16` |
| 33148 | `backup_load_power` | `uint16` |
| 33149–33150 | `battery_power` | `int32` |

其 command 是 `read_meter_battery_b`：FC04、`start_addr: 33147`、`count: 4`。`adapters/generic_adapter.py` 對 read command 直接將 map 的 `start_addr` 打包，並使用 `calc_crc16()`。本次腳本直接 import 同一個 `calc_crc16()`，並使用同一地址語意；沒有位址減一或改 function code。

現行 config 的連線目標：`192.168.106.14:502`，timeout 1.0 s、idle timeout 0.11 s、max frame time 1.0 s。雖然 config 的 driver type 名稱是 `tcp`，目前 `AsyncModbusTcpDriver` 繼承的實作仍傳輸 raw RTU frame，因此此次採用與 GenericAdapter 相同的 RTU CRC framing。

### 實際 TX

```text
04 04 81 7B 00 04 A9 B9
│  │  │    │    │  └─ Modbus RTU CRC（little-endian）
│  │  │    │    └──── count = 4 registers
│  │  └────┴───────── start address = 0x817B = 33147
│  └────────────────── FC04 Read Input Registers
└───────────────────── UID4
```

預期合法 RX 為 13 bytes：`UID=04`、`FC=04`、`byte_count=08`、8 bytes data、2 bytes CRC。payload 分割為 33147（2B）、33148（2B）、33149–33150（4B）。

## 3. 實機 primary capture

執行時間：2026-08-11 23:23:25 至 23:24:25（Asia/Taipei）

```bash
python scratch/test_uid4_33148_signedness.py \
  --count 60 --interval 1 --confirm-single-master \
  --output scratch/uid4_33148_signedness_20260811_2323.json
```

結果：在 60 次 request 中取得 **60/60** 合法 target frame；沒有 timeout、CRC 錯誤、UID 錯誤、FC 錯誤、byte-count 錯誤或未分類 RX。

代表性樣本：

```text
TX: 04 04 81 7B 00 04 A9 B9
RX: 04 04 08 00 00 FF C4 00 00 04 4D 12 EA

CRC = PASS | UID = 4 | FC = 0x04 | byte_count = 8
33147 = 0x0000 | uint16 = 0
33148 = 0xFFC4 | uint16 = 65476 | int16 = -60
33149-33150 = 0x0000044D | int32 = 1101
```

每一筆完整 TX／RX、CRC 結果及三個數值欄位均保存於 scratch log；JSON 包含完整 60 筆 raw records，供後續獨立重算。

## 4. 統計結果

| 項目 | 結果 |
| --- | ---: |
| request 次數 | 60 |
| 有效 response | 60 |
| timeout | 0 |
| bad CRC | 0 |
| wrong UID | 0 |
| wrong FC | 0 |
| wrong byte count | 0 |
| 未分類 RX | 0 |
| 33148 raw 最小／最大 | `0xFF9C` (65436) / `0xFFC4` (65476) |
| 33148 uint16 最小／最大 | 65436 / 65476 |
| 33148 int16 最小／最大 | -100 / -60 |
| 33148 raw ≥ 32768 | 60 / 60 |
| int16 落入 ±20,000 W 觀察門檻 | 60 / 60 |
| 33147 household load | 全部 0 W |
| 33149–33150 battery power | 1066–1818 W |

33148 的前十個不同 raw value（本次只有五種）：

| raw | uint16 | int16 | 次數 |
| --- | ---: | ---: | ---: |
| `0xFFC4` | 65476 | -60 | 20 |
| `0xFFBA` | 65466 | -70 | 16 |
| `0xFFB0` | 65456 | -80 | 7 |
| `0xFFA6` | 65446 | -90 | 12 |
| `0xFF9C` | 65436 | -100 | 5 |

## 5. 獨立離線複驗

對 primary JSON 的 60 筆 `rx_hex` 重新使用目前 scratch 腳本的 frame classifier 與 decoder 逐筆檢查：

```text
Offline primary-capture audit: PASS
valid_frames = 60
raw_counts = {'FF9C': 5, 'FFA6': 12, 'FFB0': 7, 'FFBA': 16, 'FFC4': 20}
```

每一筆再次通過：UID4、FC04、byte count=8、GenericAdapter 同一 CRC helper、33148 uint16/int16 解碼與 JSON 紀錄的一致性。

## 6. 已證明、強烈支持與未證明

### 已證明

- UID4 在這段 60 秒 capture 中，收到了 60 個 CRC 正確的 FC04/4-register frame。
- 33148 raw 60/60 皆為 `0xFFxx`，範圍 `0xFF9C–0xFFC4`。
- 同一 raw 的 `uint16` 是 65436–65476；two’s-complement `int16` 是 -100 至 -60。
- 現行 `datatype: uint16, scale: 1` 必然會把這些 raw 上報為 HA 的約 6xxxx W。

### 強烈支持

**STRONG EVIDENCE — 33148 應以 signed int16 解讀於 UID4 這個現場。**

理由是 60/60 個完整、CRC 正確的 raw word 均設有 sign bit，而二補數解碼穩定落在 -60 至 -100 W；這比 65,4xx W 更符合本設備量級。33147=0 與 battery power=1066–1818 W 是同期觀察值，能用於理解現場狀態，但不足以單獨定義寄存器的官方物理語意。

### 未證明

- Solis 對 UID4 firmware 的官方 33148 datatype 定義。
- 負值是否表示特定方向、反送、離網狀態或其他物理意義。
- UID1–UID3 是否也會在相同操作情境回傳 signed-looking raw。
- 因而尚未能裁決「共通 map 應從 uint16 改 int16」或「僅 UID4 需要專用 profile」。

## 7. transport 限制與停止處置

primary capture 完成後，為使 scratch 腳本更貼近 `src/driver.py`，加入與 driver 相同的 pre-TX input-buffer flush（只改 `scratch/`，不改 production）並進行額外短測。

該**後續、未納入 primary 統計**的測試，在 20 次 request 中看到：

```text
pre-TX flush events = 19
pre-TX flush bytes  = 104
wrong FC            = 8
unclassified RX     = 12
valid target frame  = 0
```

這些 bytes 不能從 host 端安全歸因為原廠 WiFi master、serial-server buffer 殘留或其他來源；但它們表示當時 transport 並非可乾淨證明的單一 Modbus 對話。依本輪停止原則，發現後已停止後續實機 request，沒有嘗試用更多輪詢「沖掉」資料。

這不改變 primary capture 的 60 個完整 target frame 及 raw 解碼事實，但限制了我們對「只有本腳本為 master」的獨立證明強度。未來若要做 UID1–UID3 對照，必須先處理此 transport 隔離問題。

## 8. 對照與後續建議

本輪沒有對 UID1–UID3 送 request，符合「UID4 為主要目標、不得交錯輪詢」的邊界。因此：

| 問題 | 裁決 |
| --- | --- |
| 是否有證據證明 UID4 與其他三台 datatype/firmware 不同？ | **NO — NOT TESTED** |
| 是共通 map bug 還是 UID4 特例？ | **INCONCLUSIVE** |
| 現在是否建立 UID4 專用 profile？ | **NO** |

建議下一輪（需先重新授權）：確認外來 bytes 的來源並取得可證明單 master 的連線，再**分開**對一個正常 UID 執行 10–20 筆相同 UID-specific FC04 read。若其 raw 主要是 `0x00xx`、UID4 持續 `0xFFxx`，才有足夠依據建立 UID4 專用實驗 profile；若其他 UID 也同樣 `0xFFxx`，則應檢討共通 map 的 `backup_load_power` datatype。

## 9. Diff 與安全驗收

- Production code：**NO DIFF**。
- `profile/solis_inverter_map2.yaml`：**NO DIFF**。
- `profile/config.yaml`：本輪 **NO WRITE**；僅保留施工前既有 dirty state。
- Modbus write：**0 次**；腳本唯一 `sendall()` payload 是固定 UID4 FC04 read request。
- Gateway restart：本 agent／測試腳本 **0 次**。primary capture 前與完成後 `ginlong` 均未運行；報告定稿時發現容器已由外部流程恢復為 `Up`，未再對它做任何操作。
- UID1–UID3 read：**0 次**。

## 10. 最終裁決

**PASS**（UID4 33148 raw/signedness 實機驗證）

同時保留此範圍限制：**INCONCLUSIVE**（UID4-specific 與共通 map 問題的判定）。

目前不應修改 map，也不應建立 UID4 專用 profile；先保留這份 raw 證據，等取得乾淨的 UID1–UID3 對照後再開下一輪最小 profile-only 實驗。
