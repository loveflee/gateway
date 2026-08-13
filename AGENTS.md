# py_ginlong 開發與 AI 操作守則

本文件是進入此 repository 時的操作基線。這是一個連接 Modbus 設備、MQTT／Home Assistant 與 Web UI 的非同步工業閘道；它可能連到正在運轉的實體設備。先保護現場與既有可觀測行為，再談修改。

## 1. 真相來源與工作樹保護

- **以目前本機 working tree 為準。** 它可以包含尚未提交、尚未推送的 production、profile、測試與報告。判讀優先序為：實際 production code > runtime config > 可執行驗證 > reports > 本文件。
- 開始任何工作先執行 `git status --short`、`git diff --stat`，必要時讀取完整 `git diff`。辨認 HEAD、已修改檔與 untracked 檔；它們一律視為使用者資產。
- 未獲明確授權，不得 `git pull`、`reset`、`checkout`／`restore` 覆寫、rebase、merge、清理 untracked 檔、commit 或 push。不可用 remote 內容覆蓋本機現況。
- 只處理任務需要的檔案。發現與本任務無關的 dirty state 時，記錄並避開，不得「順手整理」。

## 2. 專案地圖

| 位置 | 用途 |
| --- | --- |
| `src/main.py` | Gateway 啟動入口與生命週期組裝；容器實際執行 `python /app/src/main.py`。 |
| `src/adapter_catalog.py` | Adapter 探索的共用實作。`load_adapters()` 供 main.py runtime 使用（保留原啟動日誌語意），`discover_adapter_catalog()` 供 WebUI 唯讀查詢；**掃描過程不建立 Gateway、Driver、MQTT 或 WebUI**。 |
| `src/` | 主動／監聽 Driver、BusMaster、MQTT、HA Discovery、Web UI、profile validator 等 production 程式。 |
| `adapters/` | 可載入的設備協定 Adapter；容器內掛載為 `/app/src/adapters`。 |
| `adapters/adapter_helper.py` | `StandardParser`：sensor 解析的共用實作（offset／length／datatype／scale／value map 的型別守衛與單點故障隔離）。**檔名不以 `_adapter.py` 結尾，故不會被 loader 當成 Adapter**；由 `generic_adapter` 與 `ampinvt_adapter` 匯入。 |
| `profile/config.yaml` | runtime 設定：driver、MQTT、bus 與裝置的 adapter／profile／mode。 |
| `profile/*_map.yaml` 等 | 裝置地圖：讀取命令、sensors、settings 與 HA 定義。它們不是 `config.yaml` 的替代品。 |
| `report/` | 審查、測試與決策紀錄；用來理解背景，不可凌駕實際 code。 |
| `scratch/` | 被 `.gitignore` 排除的探索、replay、隔離與一次性驗證工具；先讀用途與輸入，不能當成正式 CI。 |
| `old_data/` | 歷史材料，不是目前 runtime 或驗收依據。 |
| `build.sh`、`restart.sh`、`up.sh`、`down.sh`、`log.sh`、`it.sh` | 已有 Docker 操作捷徑；只在 repository root 執行。 |

`src/` 以 asyncio 為核心：`EdgeGateway` 載入設定、Adapter、Driver、MQTT 與 HA Manager；主動設備交給 `BusMasterScheduler` 輪詢與寫入，監聽設備交給 `ListenMasterDispatcher`。主動寫入鏈是 MQTT command → BusMaster → Adapter encode → Driver ACK → verify read → Adapter decode。HA Discovery、state、availability 與 health 均透過 MQTT 發布；`src/web_admin.py` 提供有 HTTP Basic 驗證的 Web UI、設定備份／還原、隔離清單與 traffic 查閱，並以 `GET /api/catalog` 回傳目前可用的 adapter 名稱與 profile 清單（含單一外掛載入失敗的 warning，不會讓整份 catalog 失效）。

## 3. Runtime、Docker 與重啟

`docker-compose.yaml` 的服務名稱是 `py_ginlong`，容器名稱是 `ginlong`，使用 host network 與 `restart: unless-stopped`。目前 bind mount 為：

- `./src:/app/src:ro`
- `./adapters:/app/src/adapters:ro`
- `./profile:/app/profile:rw`

因此 host 上的 `src/`、`adapters/`、`profile/` 內容會提供給容器；**已在執行的 Python process 不會自動 reload**。依變更類型選擇最小且安全的動作：

```bash
# 只重啟既有 ginlong 容器，並追蹤日誌；適用於已掛載的程式或 profile 已核准變更
./restart.sh

# 重新建立 compose service，並追蹤日誌；適用於 compose 層級變更
./up.sh

# Dockerfile 或 requirements.txt 變更後的無快取 rebuild、重建與追蹤日誌
./build.sh

# 僅觀察、進入容器或停止
./log.sh
./it.sh
./down.sh
```

不要把 restart 當成無害動作：它會中斷現場輪詢與 Web UI，並觸發啟動時的 MQTT／HA Discovery 行為。任何 profile、Adapter、Driver 或 runtime config 變更都應先完成離線驗證與人工風險確認，再安排重啟和觀察。

## 4. Adapter 合約與 GenericAdapter 現況

Adapter 探索實作在 `src/adapter_catalog.py`；`src/main.py` 以 `from adapter_catalog import load_adapters` 取用，WebUI 則走 `discover_adapter_catalog()`。兩者共用同一套規則：只掃描 `/app/src/adapters` 的**頂層** Python module，忽略 package，且檔名必須以 `_adapter.py` 結尾。module 必須提供 `ADAPTER_NAME` 與 `Adapter` class；名稱以小寫註冊，重複名稱會拒絕載入。不要把可載入 Adapter 放進子目錄或使用不符命名的檔案。

- 主動模式 Adapter 必須實作 `encode_write(key, value)`、`build_verify_read(key)`、`build_poll_read()`、`decode(raw_data, context)`。
- 監聽模式另依 `feed()` 處理串流。Adapter 內應保持無阻塞、無網路 I/O 的解析／編碼邏輯，避免阻塞監聽解碼執行緒。
- 現有 Adapter 的 `ADAPTER_NAME`：`rtu`（`generic_adapter.py`）、`tcp`（`modbus_tcp_adapter.py`）、`jkbms`、`old_jkbms`、`st_inverter`、`solis_inverter`、`ampinvt`。新增或修改前先確認實際 `ADAPTER_NAME`、mode 與 profile 的組合 —— **檔名與註冊名不一致是常態**（例如 `st_inverter_adapter.py` 的註解寫 `generic > rtu` 但實際註冊為 `st_inverter`）。
- `old_jkbms_adapter.py` 檔名以 `_adapter.py` 結尾,**會被自動載入**；它以 `old_jkbms` 註冊,與 `jkbms` 不衝突。要停用某個舊 Adapter,改副檔名（如 `bak.` 前綴）而不是留在原地。

`adapters/solis_inverter_adapter.py` 是**疊加式 Adapter 的範本**：它繼承 `GenericAdapter`，只覆寫 `decode()`，在 `super().decode()` 之後、且僅當 context 為 poll 且 command id 相符時，替 43110 補上唯讀中文語意欄位。它**不實作** Modbus、CRC、transport、ACK、write、reconnect 或 scheduling。要為特定廠商加語意時沿用此模式：先讓 GenericAdapter 完成標準解碼，再疊加，不要複製一份協定實作。

目前 working tree 的 `adapters/generic_adapter.py` 可證實：讀取支援 FC01、FC02、FC03、FC04；寫入支援 FC05、FC06、**FC15**、FC16。

- **FC16**：既有單 register 路徑保持 quantity=1；當 setting 能以 `link_sensor` 優先、同名 sensor 次之，明確解析到 4-byte 的 `uint32`、`int32` 或 `float32` metadata 時，才走 quantity=2 的 32-bit 寫入與嚴格 verify read。32-bit write 的現有 order 為 `big`、`little`、`swap`／`word_swap`、`byte_swap`；沒有可明確推導的 metadata 時必須維持 legacy 16-bit 行為，不可猜測。
  - **quantity=2 已有實機閉環證據,不只是單元測試。** 見 report 048–050：驗證設備是 HY-IO8800S（`192.168.88.190:502`、UID3、原生 Modbus TCP／MBAP，**不是 Solis**），標的為 Rule1 Param1（PLC `40133` → protocol `0x0084`、`uint32` big-endian）。走完整的 `MQTT → FC16 → ACK → FC03 quantity=2 → MQTT state`，寫入 `0x12345678` 與 `100000` 皆完整回讀四個 byte，未發生只比對前半個 register 的退化，事後寫回 baseline `0` 並確認 Rule1、8 路 relay、兩個 group 全部還原。該輪 **production code 零修改** —— 既有實作原本就會為明確 4-byte metadata 建立 quantity=2。
  - 這條驗證是在 FC15（report 037–047）**之後**才補做的,且刻意換一台設備:Solis 沒有可安全寫入的 32-bit register（report 028／029 卡在此），因此 32-bit 路徑的端到端證據只在 HY-IO8800S 上取得。動 FC16 前先讀 050,不要以為它只有 mock 測試護著。
- **FC15**：由 profile 的 `coil_groups` 區塊驅動（見 §5）。`pack_coils_lsb_first()` 與 `build_fc15_pdu()` 是 **transport-neutral 的 PDU 產生器**，RTU 與 MBAP adapter 共用同一份實作；不要在子類重造一份 coil packing。群組寫入只建立一筆 FC15，verify 共用完整的 FC01 decoder。
- **群組狀態的生命週期是回歸敏感區**：`_pending_group_states` 於 verify 建立時**一次性消費**；正常 FC01 poll 會**重算** group state，避免 FC05 或外部主站改變 coil 後把舊快取重播給 HA；未命名組合以 `COIL_GROUP_UNMATCHED` 標示，不得猜測成任一已知 state。修改此處前先讀 report 043–047 的施工與驗收紀錄。

寫入 ACK 的驗證責任依 transport 分流,兩條路都不可再落回盲收：

- **RTU / RTU-over-TCP**：`src/driver.py` 驗 request 端 CRC 後，再驗 ACK 的長度、UID、FC、CRC 與 `resp[2:6]` echo。
- **原生 Modbus TCP（MBAP）**：`src/modbus_tcp_driver.py` 自行驗 transaction id、protocol id、length、UID、FC 與 echo（MBAP 無 CRC，故不能沿用父類的 RTU guard）。

修改 Adapter 時：

- 先以實際 profile、封包、decode 與 Driver ACK 契約驗證問題；能只改 Adapter 時，不得連帶改 Driver、BusMaster、profile schema 或 HA/MQTT。
- 既有 FC05／FC06／FC16 count=1 的合法 request bytes、legacy verify 行為與現有 profile 路徑是回歸邊界。不得為了收緊新功能而改變 legacy 對合法較長 verify frame 的處置。
- GenericAdapter 的 32-bit codec 需與既有 read codec 對稱。`dcba` 的 read/write 歷史語意尚未統一，沒有明確驗證前不可自行延伸。

## 5. Profile、設定與秘密資料

- `profile/config.yaml` 是 runtime config；裝置的 `profile:` 值由 Gateway 在 `/app/profile` 以 `<name>.yaml` 載入（找不到 YAML 才嘗試同名 Python module）。地圖檔不是可任意交換的範例資料。
- **雙向量必須宣告為有號型別。** 現行 Solis 地圖的既定慣例是：可正可負的量（`active_power`／`meter_active_power`／`battery_power` 用 `int32`，`battery_current`／`inverter_temp`／`backup_load_power` 用 `int16`），只增不減的量才用 `uint16`。把有號暫存器誤宣告為 `uint16` 的症狀是 HA 出現 `6xxxx` 這種物理上不可能的數值（`0xFFxx` 被當正數）。新增功率／電流／溫度點位時先確認雙向性，不要預設 unsigned。
- 同一組地圖可能存在多份變體（例如 `solis_inverter_map.yaml` 與 `solis_inverter_map2.yaml`、`6k2*.yaml`、`relay_8ch_map2.yaml`）。修正某個 sensor 的 datatype 時，要確認**目前 `config.yaml` 實際掛載的是哪一份**；只改其中一份會讓其他變體保留舊錯誤，日後切換 profile 即復發。
- **`profile/` 內存在已入庫但未啟用的地圖。** `relay_8ch_full_map.yaml` 是 HY-IO8800S 依 report 051 盤點建立的完整點位版（176 個 sensor、96 個 MQTT state key），只收錄手冊列出且經實機唯讀回讀確認的點位。它**沒有被任何 `config.yaml` 引用**，驗證機 UID3 目前跑的仍是 `relay_8ch_map2`。注意兩件事：(1) 放在 `profile/` 就會被 `web_admin._discover_profiles()` 掃到而出現在 WebUI 下拉選單，屬「可選但未選」；(2) 它**不新增任何寫入目標** —— `settings` 只有原本 8 顆 FC05 relay，`B2_SETTING` 只有兩個 FC15 group select，手冊標為 RW 的 holding register（裝置位址、broadcast mode、輸出保持、pulse 邊緣／防抖、心跳、Rule 參數）一律唯讀。要開放其中任何一個寫入,必須另過一輪寫入安全審查:configured device address 會改變通訊目標、output state hold 會改變斷電後的 DO 行為,重啟與恢復出廠寄存器則永不映射到 HA。
- **`coil_groups` 是 FC15 專用的 profile 區塊**，也是目前唯一在既有 `read_commands`／`sensors`／`settings` 之外新增的 schema：

  ```yaml
  coil_groups:
    group_01:
      start_addr: 0            # FC15 起始 coil 位址
      count: 2                 # 必須等於 members 長度
      members: [switch_0, switch_1]   # 對應 settings 的 key，順序即 coil 順序
      verify_command_id: read_coils   # 回讀所用的 FC01 command
      states:                  # 具名狀態 → 各 member 的 ON/OFF 序列
        all_off: [OFF, OFF]
        all_on:  [ON, ON]
  ```

  `map_validator.py` 已涵蓋此區塊：dict 結構、group key 非空字串、`start_addr` 合法、`count` 為 1–2000 的非 bool 整數、`start_addr + count ≤ 65536`、`members` 為非空 list、且 `count == len(members)`。它同時拒絕 `settings` 內重複的 key（重複會使 `coil_groups` 無法安全對應位址）並檢查 `sensors` 的 `command_id` 是否存在。**通過 validator 不等於位址、實機 coil 對應或寫入安全已獲證明。**
- 地圖靜態驗證使用現有 validator。容器運行時可用：

  ```bash
  docker exec ginlong python /app/src/map_validator.py /app/profile/<map>.yaml
  ```

  這只驗證目前 validator 已涵蓋的 YAML／HA／基本數值結構；通過不等於寄存器位址、功能碼、codec 或實機寫入安全已獲證明。
- Web UI 寫入的是 `config.yaml`，會使用 `config.yaml.lock`、`config.yaml.bak` 與原子替換。不得手動刪除、覆蓋或把這些保護檔當雜物清理；要改 runtime config 時，優先理解其驗證與備份流程。
- `.env` 是秘密資料，包含 MQTT 與 Web UI 所需的環境變數。禁止讀出、貼入 report／log／commit，也不要將 MQTT 帳密寫回 `config.yaml`。需要欄位名稱時只參考 `.env.example` 或程式中的環境變數名稱。
- 不可為測試任意改正式 profile、縮短 timeout／retry／inter-frame delay、放寬 ACK／CRC／offline 防線，或改變 poll rotation／write budget。

## 6. 驗證與現場安全

本 repository **沒有已發現的根目錄 `tests/`、專案 CI 設定或單一通用 `pytest` 指令**。目前可用的驗證分三層，不能混稱：

1. `src/map_validator.py`：可重跑的地圖靜態檢查。
2. `scratch/validation_*` 與 `scratch/master_rtu_audit_20260808.py`：隔離／半自動 harness。它們各自可能使用 sandbox、基準副本或 replay 資料；執行前先閱讀腳本，確認它測的是目前 source 還是歷史 copy，也不可讓它寫入未知實機暫存器。
3. 實機觀察：用於傳輸、實際地圖、HA 上報、ACK／verify 與長時間穩定性，但只可對已確認安全的點位進行。沒有安全測試條件時，寫 `NOT TESTED`，不可把 isolated PASS 宣稱成實機 PASS。

修改 production 前至少建立與風險相稱的 isolated／replay 測試；修改 profile 前先跑 validator；修改 write、codec、Driver 或 BusMaster 時，要同時驗證 request、response／ACK、verify decode 與既有 regression。實機驗收應保留 TX/RX、HA／MQTT 與 log 證據。雙 master 是刻意的壓力環境；它本身造成的 collision、RX noise 或 timeout 不可直接列為系統缺失，但任何改動導致原本處置退化則是 regression。

## 7. 修改與驗收原則

### 先驗證再修改

涉及 Driver、BusMaster、Adapter、Profile 或 HA/MQTT 對外行為時，先讀現況、提出可驗證的假設、取得封包／isolated／實機證據，再動手。report 是背景與證據索引，不是把計畫自動變成功能的依據。

### 最小修改

能在 Adapter 解決，不改 Driver 或 BusMaster；能由既有 metadata 推導，不新增 profile schema。不得為重構美觀改 public contract、輪詢順序、timeout、retry、offline／online 狀態機、ACK／CRC 防線或 traffic log。

### 對外可觀測性一致

既有設備若需求未明確要求改變，必須保持 HA entity／unique ID、MQTT topic、Discovery payload、state、availability、scale 與 value map 結果、poll rotation、write → verify 流程、timeout、retry、offline／online 行為與正常 INFO logging 的語意一致。新增能力應有明確 profile／功能碼界線，不能讓舊設備無聲改走新路徑。

### Live OT 安全

禁止猜測 writable register、對未知 register 實機寫入、為了測試修改正式 profile、或把高壓力雙 master 環境誤診為普通 bug。對會影響現場的重啟、Docker、設定或寫入，先取得明確授權與回復策略；禁止以「測試全部 PASS」為由冒險。

## 8. 程式、報告與 Git 慣例

- Python 依現有檔案使用 4 空白縮排、`snake_case` 函式／變數、`PascalCase` 類別。既有 production 檔有版本標頭、修復歷程與防禦性 log；修改既有檔時沿用其局部風格，不要無關地重排或重寫歷史。
- 本輪報告放在 `report/`，以遞增編號加明確中文主題命名。報告要先給非工程師的人話結論，再寫範圍、證據、限制、未測項與結果；不得以 report 取代可重跑驗證。
- `scratch/` 可放隔離實驗或暫存資料，但因被忽略，不是預設可交付測試。需要長期回歸的測試，應在取得授權後規劃可版本控制的位置與執行方式。
- 提交前重看 `git diff --check` 與目標檔 diff，確認沒有夾帶 profile、secret、產物或其他人的變更。沒有明確指令就停在人工審查，不自行 commit／push。

## 9. 已知邊界（提醒，不在未授權任務中順手修）

- `map_validator.py` 已針對 `coil_groups`、重複 settings key 與 sensor `command_id` 參照收緊；其餘欄位（read FC 白名單與數量上限、write FC 白名單、`verify_count`、datatype／word_order 值域）仍未全面對齊 GenericAdapter 的能力,後續可另案處理。
- `dcba` 的 read/write 歷史語意尚有不對稱：read 對 4-byte 不處理 `dcba`（落到 `big`），write 則明確拒絕。現行 profile 無此組合，但未經實機驗證前不可自行延伸。
- FC22、FC23 與 64-bit write 仍不是已授權的必要能力（FC15 已於 report 037–047 完成施工與驗收，FC16 quantity=2 已於 report 048–050 取得實機閉環證據）。
- **驗證機在遠端,不是這台 VM。** HY-IO8800S（UID3）與其 relay／IO 點位的實機證據來自遠端設備,本目錄只留下報告、地圖與測試腳本。這代表:任何宣稱「已實機驗證」的結論,**證據鏈只存在於文件裡**,本機無法重跑。因此 report 與 AGENTS/README 的對齊不是文書工作,是唯一可稽核的來源 —— 發現文件與程式碼不一致時,優先修文件而不是憑印象改程式碼。
- `local_serial_driver.py`（`type: usb`）與監聽軌（`mode: listen`）本機無對應硬體。report 052 的系統級總驗收已在「未涵蓋範圍」明確排除這兩條軌,它們只做過靜態閱讀,**不得宣稱已驗證**。
- PyModbus 的定位是標準 reference／isolated test oracle；不得加入 `requirements.txt` 或作為 production runtime dependency，除非另有明確設計與授權。
