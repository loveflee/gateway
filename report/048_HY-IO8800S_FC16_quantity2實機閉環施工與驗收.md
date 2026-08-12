# 048 — HY-IO8800S FC16 quantity=2 實機閉環：手冊複核、TCP Adapter 施工與驗收

- 執行者：Claude Code（本輪同時是評估、施工與驗證者；非獨立敵對驗收）
- 日期：2026-08-13 00:00 – 00:25（CST）
- 依據文件：`HY-IOS 系列串口 IO 產品配置說明書` V1.0.1（2023/3/17）
- 目標設備：UID 3 @ `192.168.88.190:502`，空載繼電器
- production 修改：`adapters/modbus_tcp_adapter.py` V1.2 → **V1.3**（唯一一個）
- 文件更新：`CLAUDE.md`（三處已過期敘述）
- 測試用暫時 profile 已移出 `profile/`，改置於 `scratch/`

---

## 人話結論

**可行，做完了，實機通過。但你指定的暫存器位址要往後挪兩個，而且真正卡住的地方不是位址。**

三件事：

**第一，設備身分確認了。** 現場那台 8 路繼電器讀回來的型號字串是 `IO8800S` —— 就是這份手冊講的 HY-IO8800S，所以手冊適用，不是猜的。

**第二，40131 不是「參數1」，參數1 在 40133。** 手冊那張表自己前後矛盾：它說每組聯動規則佔 16 bytes（8 個暫存器），而且第 32 組在 40377 —— 這兩個數字只允許「模式、動作、DO編號、DI編號、參數1、參數2」照順序排，算出來參數1 在 40133。表格的位址欄少算了兩格。我不靠推理定案，直接把 8 組規則整塊讀回來看：第 3 組以後是原廠預設 `[0, 0, 1, 1, 1000, 1000]`，跟順序排法一個位元組都不差。**照原本的 40131 寫下去，實際會寫進「DO編號＝1、DI編號＝34464」**，DI 編號合法範圍只有 1~8，設備可能拒絕也可能夾限，回讀對不上的時候你會分不清是程式錯還是設備擋 —— 測試就失去意義了。

**第三，真正的阻斷點是程式，不是設備。** 這台走的 `tcp` adapter **根本沒有 FC16**，寫下去只會拿到 `NotImplementedError`。所以在動設備之前，我先把 FC16 補進去（只改這一支檔案，重用已經驗過的 32-bit 編碼器），隔離測試 41 項全過才上線。

然後實機三次寫入全部一次到位，包含最刁鑽的 `0x12345678`（四個位元組全不一樣，端序錯一點就會被抓到）：

```
寫 12 34 56 78 → 回讀 12 34 56 78 ✅
寫 00 01 86 A0 → 回讀 00 01 86 A0 ✅   (= 100000，你指定的值)
寫 00 00 00 00 → 回讀 00 00 00 00 ✅   (還原)
```

最後我把整塊 8 組聯動規則區重新讀一次跟測試前逐位元組比對：**64 個暫存器一個都沒變。** 繼電器全關、網關正常、設定檔還原成跟測試前完全相同。

順帶一提，你說「Gemini 把目的窄化成只驗端序講太窄」——我同意，而且要補一句：**這條鏈在 TCP adapter 那一環本來是斷的**，端序只是最後一關。

---

## 1. 手冊複核（我自己重讀，未採用他方推論）

### 1.1 確認為真

| 主張 | 手冊出處 | 判定 |
|---|---|---|
| 保持暫存器大端 | 3.1.5「保持寄存器中各参数均使用大端模式读写」 | ✅ |
| 支援 FC03／06／10 | 3.1.1「40001-49999 保持寄存器 03、06、10」 | ✅ |
| 協議位址 = PLC − 40001 | 手冊自己三個範例全部吻合（見下） | ✅ |
| 40129 = 0x0080 | 3.1.5.2 明寫「起始地址为 40129（0x0080）」 | ✅ |

手冊三個範例交叉驗證位址換算：

```
FC03  01 03 00 2C 00 02       0x2C=44  → 40045 運行時間      「读设备运行时间参数」✓
FC06  01 06 00 3E 00 01       0x3E=62  → 40063 設備地址      「写设备地址为 1」   ✓
FC16  01 10 00 57 00 02 04 00 01 C2 00
                              0x57=87  → 40088 波特率        「写设备波特率为 115200」✓
```

FC16 範例同時佐證：**quantity=2、byte count=4、資料大端** —— 與本輪要驗的格式完全一致。

### 1.2 手冊內部矛盾（本輪最重要的書面發現）

3.1.5.2 的位址欄與同節的其他敘述互相衝突：

- 錨點 A：`40377 = 聯動規則 32` → (40377−40129)/31 = **8 register／組**
- 錨點 B：內文「每组参数为 16 字节，包含联动模式、联动动作、输出编号、输入编号、参数 1、参数 2 组成」→ 2+2+2+2+4+4 = **16，剛好，無 padding**
- 錨點 C：40129 / 40137 / 40145 三組列舉，stride = 8 ✓

三個錨點只允許順序排列：

```
40129 模式(2B)  40130 動作(2B)  40131 DO編號(2B)  40132 DI/AI編號(2B)
40133 參數1(uint32 4B)          40135 參數2(uint32 4B)
```

但表格的位址欄標的是 `40130 = DO編號/DI編號`、`40131 = 參數1`、`40133 = 參數2` —— 那樣只湊得出 12 bytes、6 個暫存器，下一組會落在 40135 而不是 40137，也推不出 40377。同節另有一處明顯 typo（`40153+(6*(n-1))`，stride 應為 8），而修訂歷史 V1.0.1 的說明正是「修改规则寄存器说明」。

**結論：表格位址欄不可信，以 16 byte 順序排列為準。參數1 在 40133（協議 0x0084）。**

---

## 2. Phase 0：實機唯讀探測（走正式 Gateway，未新增 master）

方法：暫時把 UID3 的 profile 換成唯讀 probe（= `relay_8ch_map2` + 三筆 FC03 讀取命令，**零新增 settings**，探測 sensor 不宣告 HA 實體，因此不產生任何新 Discovery），重啟後觀察，測完還原。單 master 全程成立。

### 2.1 設備身分（決定手冊是否適用）

```
TX  00 02 00 00 00 06 03 03 00 00 00 10          (FC03 40001, 16 registers)
RX  bytecount=32
    49 4F 38 38 30 30 53 00 00 00 ... 00
    -> ASCII: 'IO8800S'
```

**UID 3 = HY-IO8800S**（8 DO／8 DI）。手冊適用，不是型號推測。

### 2.2 FC03 quantity=2 + uint32 大端讀路徑（唯讀，零風險）

以 40045 運行時間（RO uint32）為靶：

```
TX  00 03 00 00 00 06 03 03 00 2C 00 02
RX  bytecount=4  data=00 00 9B B2
    big-endian    = 39858     ← Gateway 解出的值
    little-endian = 2996502528 ← 若端序錯會是這個荒謬值
```

連續觀察，Gateway 發布的 `hy_uptime_s` 為：

```
39858 → 39878 → 39898 → 39918 → 39938 → 39958 → 39978
```

**每輪固定 +20，與 4 個命令 × 5 秒的輪詢週期完全一致。** 這不只證明 4 bytes 解對，還證明它在物理語意上就是「秒」。**FC03 quantity=2 + uint32 大端的讀路徑，實機成立。**

### 2.3 聯動規則 layout 定案（一次讀 8 組）

```
TX  00 04 00 00 00 06 03 03 00 80 00 40          (FC03 40129, 64 registers)
RX  bytecount=128
```

解出的 64 個暫存器，從第 17 個（規則 3）開始出現清晰的 8 個一組重複樣式：

| 規則 | 8 個 register 內容 |
|---|---|
| 規則 1 | `[0, 0, 0, 0, 0, 0, 0, 0]` |
| 規則 2 | `[0, 0, 0, 0, 0, 0, 0, 0]` |
| 規則 3～8 | `[0, 0, 1, 1, 0, 1000, 0, 1000]` |

對照手冊預設值（DO編號 1、DI編號 1、參數1 1000、參數2 1000）：

```
offset +0  模式      = 0      ✓ 預設 0
offset +1  動作      = 0      ✓ 預設 0
offset +2  DO 編號   = 1      ✓ 預設 1
offset +3  DI/AI編號 = 1      ✓ 預設 1
offset +4,+5  參數1 uint32 = 0x000003E8 = 1000  ✓ 預設 1000
offset +6,+7  參數2 uint32 = 0x000003E8 = 1000  ✓ 預設 1000
```

**順序排列讀法獲得實機證實，表格位址欄確認為錯。參數1 = 規則起點 + 4 registers。**

### 2.4 安全前置條件（你提的那一條，已確認）

- **8 組規則的模式全部 = 0**（未啟用），規則 1 整塊為 0。
- 因此寫入規則 1 的參數1 **不會觸發任何 DO 動作**。
- 原值 = `0`，還原目標明確且可精確驗證。

靶點定為：**PLC 40133 / 協議 `0x0084`，uint32 大端，規則 1 參數1。**

---

## 3. Phase 1：施工（`adapters/modbus_tcp_adapter.py` V1.2 → V1.3）

### 3.1 為什麼非改不可

施工前實測（production class，非推論）：

```
[rtu/generic] encode_write(FC16, 100000) -> 03 10 00 84 00 02 04 00 01 86 A0 C2 5C
              build_verify_read          -> codec=True  strict=True
[tcp/MBAP]    encode_write(FC16, 100000) -> NotImplementedError: TCP 尚不支援 FC 16 組裝
              build_verify_read          -> codec=False strict=None
```

`modbus_tcp_adapter.py` 自己重寫了 `encode_write()` 與 `build_verify_read()`，只有 FC05／FC06。所以：

1. `write_fc: 16` 一律 `NotImplementedError` —— **閉環根本起不了頭**；
2. 就算把 `verify_count: 2` 硬塞進去讀回 4 bytes，context 沒有 codec，decode 會走 legacy 分支 `chunk = raw_data[3:5]`，**只比對前 16 bits** —— 那正是「部分驗證」黑洞。

### 3.2 實際修改（最小、只此一支）

| 位置 | 修改 |
|---|---|
| 檔頭 | V1.2 → V1.3，補修復歷程說明 |
| `build_verify_read()` legacy 分支 | `write_fc == 16` 時呼叫**繼承來的** `_resolve_fc16_codec()`；有 codec 就把 `reg_count` 改為 2，並在 context 補 `strict_verify`／`expected_data_bytes`／`expected_count`／`codec` |
| `encode_write()` | 新增 `elif fc == 16:`，重用**繼承來的** `_encode_fc16_32bit_value()`（32-bit）與 `_coerce_legacy_16bit()`（legacy），自行組 MBAP PDU |
| `decode()` | **未修改** —— 它本來就把 `strict_verify` 與 `codec` 透傳給父類的 `_verify_modbus_frame()` 與 `_extract_data()` |

未修改：`generic_adapter.py`、`bus_master.py`、`driver.py`、`modbus_tcp_driver.py`、`ha_manager.py`、`map_validator.py`、任何既有 profile。

判準與 RTU 版完全一致：**只有 `link_sensor`（次之為同名 sensor）能明確解析出 4-byte `uint32`／`int32`／`float32` 且 `word_order` 合法時才走 quantity=2；metadata 不明確一律維持 legacy 16-bit，不猜測。**

### 3.3 上線前隔離驗證

`scratch/claude_048_tcp_fc16_isolated.py` —— **41 項，全數 PASS**（`..._results.json`）。PyModbus 3.13.0 僅作隔離 oracle，未進 `requirements.txt`。

| 類別 | 內容 | 結果 |
|---|---|---|
| 32-bit 編碼 | 5 個值（0／1／1000／100000／0xFFFFFFFF）：TCP PDU == RTU PDU == PyModbus `WriteMultipleRegistersRequest` | PASS |
| MBAP 欄位 | protocol=0、length、UID、FC=0x10、addr、quantity=2、byte count=4 | PASS |
| verify 請求 | FC03 quantity=2，context 帶 strict_verify／codec／expected_* | PASS |
| decode | 4-byte 回讀 exact（0／100000／0xFFFFFFFF） | PASS |
| **嚴格性** | 回讀只有 1 個暫存器時**必須拋錯**，不得只比前 16 bits | PASS |
| 不符判別 | 回讀 99999 不會被當成 100000 | PASS |
| **端序守衛** | big／little／word_swap／byte_swap 各產生不同且正確的 register bytes | PASS |
| **Regression** | FC05／FC06／FC16-legacy 的 bytes 與 verify context **完全未變**；FC16 legacy 仍 quantity=1 且無 strict | PASS |
| 邊界 | `settings` 內 `write_fc: 15` 仍拒絕；uint32 超範圍（2³²、−1）拒絕 | PASS |

另外重跑 047 的完整敵對套件：**176／178 PASS**。兩個 FAIL 都是預期的指紋比對（`config.yaml` 當時指向 probe profile、`modbus_tcp_adapter.py` 就是本輪施工的檔案），**所有行為類檢查（FC15 生命週期、8 種資料流黑洞、ACK guard、FC01/05/06/16 regression、HA 可觀測性）全數維持 PASS**。

---

## 4. Phase 2：Production 實機閉環

路徑：`MQTT command → EdgeGateway → BusMaster bus_lock → TcpAdapter FC16 → AsyncModbusTcpDriver ACK guard → HY-IO8800S → FC03 quantity=2 verify → 32-bit decode → exact compare → MQTT state`。**無 raw sender，無第二 master。** `tcpdump` 為 host `br0` 唯讀側錄。

寫入前經正式 Gateway 讀得原值 `rule1_param1 = 0`。

### 交易 1 —— `0x12345678`（端序判別力最強的向量）

```
00:18:35.351 TX FC16  00 05 00 00 00 0B 03 10 00 84 00 02 04 12 34 56 78
00:18:35.418 RX ACK   00 05 00 00 00 06 03 10 00 84 00 02
00:18:35.679 TX FC03  00 06 00 00 00 06 03 03 00 84 00 02
00:18:35.708 RX FC03  00 06 00 00 00 07 03 03 04 12 34 56 78
```
target 305419896 / 回讀 305419896 / 耗時 **357.4 ms** / **PASS**

### 交易 2 —— `100000`（你指定的值）

```
00:18:43.355 TX FC16  00 08 00 00 00 0B 03 10 00 84 00 02 04 00 01 86 A0
00:18:43.421 RX ACK   00 08 00 00 00 06 03 10 00 84 00 02
00:18:43.682 TX FC03  00 09 00 00 00 06 03 03 00 84 00 02
00:18:43.713 RX FC03  00 09 00 00 00 07 03 03 04 00 01 86 A0
```
target 100000 / 回讀 100000 / 耗時 **357.9 ms** / **PASS**

### 交易 3 —— 還原 `0`

```
00:18:51.359 TX FC16  00 0A 00 00 00 0B 03 10 00 84 00 02 04 00 00 00 00
00:18:51.426 RX ACK   00 0A 00 00 00 06 03 10 00 84 00 02
00:18:51.687 TX FC03  00 0B 00 00 00 06 03 03 00 84 00 02
00:18:51.717 RX FC03  00 0B 00 00 00 07 03 03 04 00 00 00 00
```
target 0 / 回讀 0 / 耗時 **357.5 ms** / **PASS**

Gateway log：

```
00:18:35 [Command] UID=3 key=rule1_param1 value=305419896 → 寫入驗證成功
00:18:43 [Command] UID=3 key=rule1_param1 value=100000    → 寫入驗證成功
00:18:51 [Command] UID=3 key=rule1_param1 value=0         → 寫入驗證成功
```

MQTT state 依序出現 305419896 → 100000 → 0，與線路一致。

**逐項確認：**

- ACK 由 V1.3 driver 嚴格驗過（transaction id、protocol、length、UID、FC=0x10、echo address+quantity 全部相符才回 True）
- verify 是 **FC03 quantity=2**，回應 byte count = 4，**整整 4 個 bytes 都參與比對**
- 三筆交易的 `FC16 TX → ACK → FC03 TX → FC03 RX` 四幀**連續、中間無任何插入**（UID1 的輪詢從未插進來）
- 設備接受 quantity=2 的 FC16，沒有夾限、沒有例外

---

## 5. 最終復原

### 5.1 整塊聯動規則區逐位元組比對

測試後重新掛 probe profile，再讀一次 40129 起 64 個 registers，與測試前的 128 bytes 直接比對：

```
測試前 128 bytes == 測試後 128 bytes    ->  True
有差異的 register index                ->  無
規則1  [0,0,0,0,0,0,0,0]        -> [0,0,0,0,0,0,0,0]
規則3  [0,0,1,1,0,1000,0,1000]  -> [0,0,1,1,0,1000,0,1000]
```

**64 個暫存器全部與測試前完全相同。** 線路側錄也佐證：整段期間對 UID3 只出現 **3 筆 FC16**，全部指向 `0x0084` quantity=2，沒有任何其他寫入。

### 5.2 系統狀態

| 項目 | 結果 |
|---|---|
| `config.yaml` | `diff` 與測試前基線**無差異**，SHA-256 `c9ec5e0a9350bd10…` 相同 |
| UID3 profile | 已還原為 `relay_8ch_map2` |
| 繼電器 | `switch_0..7` 全部 OFF、`group_01=all_off`、`group_234=all_off` |
| 容器 | `running`、`RestartCount=0` |
| MQTT | `py_1f/status=online`、`py_1f/relay_8ch/3/status=online` |
| 錯誤 | 還原後日誌 ERROR／CRITICAL／Traceback／隔離 = **0 行** |
| 單 master | `.190:502` 只有 Gateway 一條連線 |
| 暫時 profile | 已移出 `profile/`，改置 `scratch/`（不會出現在 WebUI 下拉選單） |

### 5.3 production 指紋

```
4103132532ddab2f  adapters/modbus_tcp_adapter.py   ← 本輪唯一修改（V1.2→V1.3）
554ac9c6468a3d44  adapters/generic_adapter.py      未變
831bfe98c534e5ad  src/bus_master.py                未變
cc049da87d7bb02f  src/driver.py                    未變
688e07ea49b4ab14  src/modbus_tcp_driver.py         未變
d02c5b3e76401db3  src/ha_manager.py                未變
d4f256328b20ca6e  src/map_validator.py             未變
8d9b093e3e4f49a6  profile/relay_8ch_map.yaml       未變
0116e41bd64abb86  profile/relay_8ch_map2.yaml      未變
c9ec5e0a9350bd10  profile/config.yaml              未變（測試中暫改，已還原）
```

---

## 6. 文件同步

在全部驗證通過後才更新 `CLAUDE.md` 三處已過期敘述：

1. native TCP「寫入 ACK 在 driver 層完全沒有被驗證」→ 已於 report/041 修補（V1.3 guard），改為記錄現況與 18 種惡意 ACK 的復驗結果。
2. 「FC15 目前不存在」→ 改為「FC15 只能經 profile `coil_groups` 使用，`settings` 內硬寫 `write_fc: 15` 仍拒絕」。
3. 「TCP adapter 沒有 FC16 32-bit codec 路徑」→ 改為記錄 V1.3 已補齊，並保留「下一個要加的功能碼仍必須在這支檔案裡各補一次」這條會咬人的性質。

`AGENTS.md` 第 4 節目前只描述 `generic_adapter` 的能力邊界（仍然正確），建議由施工流程的擁有者決定是否補一行 TCP adapter FC16。本輪未動 `AGENTS.md`。

---

## 7. 已知限制與後續項目

1. **本輪不是獨立敵對驗收。** 我同時是評估者、施工者與測試者。若要比照 041→047 的流程，這份 048 應該再經一輪獨立敵對驗收（重算封包、mutation control、重打死亡案例）。
2. **只驗證了 uint32 + `word_order: big`。** `int32`、`float32`、`little`／`swap`／`byte_swap` 只有隔離層證據，無實機證據。要實機驗這些需要另一個安全靶點。
3. **只驗證了 quantity=2。** quantity>2（例如整組 8 register 的規則寫入）未實作也未測試。
4. **靶點語意提醒**：`0x0084` 是聯動規則 1 的參數1。今天它安全是因為**規則 1 模式 = 0**。若日後有人啟用規則 1，這個位址就不再是惰性參數 —— 不要把它當成通用的測試暫存器。
5. `profile/` 內仍有 042／044／047 記錄的兩個未引用檔（`relay_8ch_map2_4ch_test.yaml`、`relay_8ch_map2_8ch_test.yaml`），本輪未處理（非我建立）。我自己產生的兩個暫時 profile 已移至 `scratch/`。

---

## 8. 結論

```text
手冊複核（大端／功能碼／位址換算）:        PASS
手冊位址欄矛盾:                            FOUND（參數1 應為 40133，非 40131）
設備身分（IO8800S）:                       PASS
FC03 quantity=2 + uint32 大端 讀路徑:      PASS（實機，運行時間每輪 +20 秒佐證）
聯動規則 layout 實機定案:                  PASS（規則 3~8 = [0,0,1,1,1000,1000]）
安全前置（全規則模式 = 0）:                PASS
TCP Adapter FC16 施工（V1.3）:             PASS（隔離 41/41）
既有功能 regression:                       PASS（FC05/06/16-legacy bytes 未變；047 套件 176 項行為檢查全過）
FC16 quantity=2 實機閉環:                  PASS（3/3，含 0x12345678 端序強判別向量）
Verify 為完整 4-byte exact compare:        PASS
最終復原（64 個暫存器逐一比對）:           PASS
系統健康與零殘留:                          PASS

FINAL: PASS
```

你原本缺的「安全的 32-bit writable register 實機閉環」這一塊，現在補上了。
