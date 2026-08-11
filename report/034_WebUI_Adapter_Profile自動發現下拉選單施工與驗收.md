# 人話結論

1. `solis_inverter` 原本看不到，是因為 WebUI 把 Adapter 選項寫死在 HTML；Gateway 實際上早已可動態載入它。
2. 現在 Adapter 下拉會讀取與 Gateway **同一套**外掛 discovery 規則；新增合格 `*_adapter.py` 後不必再改 HTML。
3. Profile 已改為讀取目前 `/app/profile` 的正常 `.yaml` 地圖檔；新增地圖後不必手打檔名，設定仍保存既有的不含 `.yaml` 名稱。
4. 已存在但目前找不到的舊 Adapter／Profile 不會被下拉選單吃掉，會保留為「目前未發現」；catalog 失敗時 Raw YAML 仍可編輯。
5. 隔離驗收與容器內實際 catalog 均 PASS。沒有儲存 `config.yaml`、沒有重啟 Gateway、沒有碰 Modbus／MQTT／HA 行為。
6. 本輪沒有實際重啟並開啟正式 WebUI，因為目前 `config.yaml` 有施工前既有的四台設備實驗 map 變更；重啟會影響現場。故整體裁決為 **PASS WITH LIMITATIONS**，建議交 Claude Code 做敵對式獨立驗收。

## 1. 動工前工作清單與根因

施工前完成 Git working tree、`src/main.py`、`src/web_admin.py`、`src/index.html`、`adapters/`、`profile/`、`profile/config.yaml` 與 `docker-compose.yaml` 的唯讀盤點。

已確認的根因：

- Gateway 在 `main.py` 只掃描 `adapters/` 頂層的 `*_adapter.py`，import 成功且具 `ADAPTER_NAME`、`Adapter` class 才會加入 `ADAPTER_FACTORY`。
- WebUI 的 `index.html::addDevice()` 則有另一份固定選項：`rtu`、`tcp`、`jkbms`、已不在 runtime catalog 的 `ampinvt`；`solis_inverter` 不在其中。
- Profile 原本是純文字 input；runtime 設定中的 `profile: solis_inverter_map2` 不含 `.yaml`。

規劃並實際執行的最小範圍：共用 discovery helper、唯讀 catalog API、動態 select 與隔離測試／本報告。沒有修改 `_validate_config()`，也沒有用 catalog 限制 Raw YAML 或既有 config。

## 2. 實際修改與架構決策

| 檔案 | 修改 | 原因 |
| --- | --- | --- |
| `src/adapter_catalog.py`（新增） | 封裝 runtime 既有的頂層 `*_adapter.py` 掃描、import、必備欄位、class、名稱衝突與單一失敗略過規則。 | 避免 WebUI 再維護第二份 Adapter 真相。僅掃描／import，不建立 Gateway、Driver、MQTT 或 WebUI。 |
| `src/main.py` | 原 `load_adapters()` 改為呼叫 helper，保留 `ADAPTER_FACTORY` 與啟動日誌語意。 | 讓 runtime 與 WebUI 使用完全同一實作，而非複製規則。沒有重構 Gateway 生命週期。 |
| `src/web_admin.py` | 新增受 HTTP Basic auth 保護的唯讀 `GET /api/catalog` 與 Profile discovery。 | 供表單讀取實際可選的 Adapter／Profile；錯誤回 warning，不改設定、不重啟。 |
| `src/index.html` | 移除 Adapter 寫死 option；Profile 改為 select；加入 catalog 載入、未知值暫存 option 與失敗警告。 | 新增 Adapter／Profile 不再需要更新 HTML，救援設定不會被選單覆寫。 |
| `scratch/validation_webui_catalog_20260811.py`（隔離測試） | 建立 temporary fake plugins／profiles、loopback WebUI API 驗證。 | 驗證 discovery、篩選、auth、故障退化與前端 contract；不接觸實機。 |

### 2.1 API schema

`GET /api/catalog`（與既有 `/api/config` 相同的 HTTP Basic auth）回傳：

```json
{
  "adapters": ["jkbms", "rtu", "solis_inverter", "st_inverter", "tcp"],
  "profiles": ["...不含 .yaml 的地圖名稱..."],
  "warnings": []
}
```

此 API 是唯讀：不驗證／不修正 Profile、不寫 `config.yaml`、不觸發 restart。單一 Adapter import 失敗只產生 warning，其他有效項目仍會回傳。

### 2.2 Profile discovery 規則

- 來源：`/app/profile`（容器實際設定目錄）。
- 僅取實體、非隱藏、以 `.yaml` 結尾的檔案，回傳去除 `.yaml` 的名稱。
- 排除目前 runtime config `config.yaml`，以及 `.bak.`／`.tmp.` 命名的暫存／備份 YAML。
- 不在 catalog 時執行 `map_validator`，不隱藏 validator 尚未接受、但使用者原本可指定的實驗 profile。

### 2.3 前端相容性與退化

- `waitAndLoad()` 先讀 catalog 再讀 config；既有值會正確選中。
- 若 config 的值不在 catalog，動態加入同 value 的 `（目前未發現）` option。表單輸出仍寫入原字串。
- catalog HTTP 失敗或 schema 不合法時，畫面顯示持續 warning，但不清空 select；後續載入的既有值仍會成為暫存 option。Raw YAML 流程不依賴 catalog。
- `collectDevices()` 仍讀 select 的 `.value`；`formToRaw()`／`rawToForm()` 的既有資料流未改變，因此未知值不會因轉換而改成第一個 option。

## 3. 驗收證據

### 3.1 容器內實際 runtime catalog（唯讀）

在既有 `ginlong` 容器以 `PYTHONPATH=/app/src` 直接執行 catalog 函式；沒有重啟、沒有載入 Gateway：

```text
adapters = ["jkbms", "rtu", "solis_inverter", "st_inverter", "tcp"]
profiles = ["6k2", "6k2p1", "6k2p2", "inverter_map", "jkbms_map",
            "relay_8ch_map", "solis_inverter_map", "solis_inverter_map2",
            "temp_humid_map", "water_level_map"]
warnings = []
```

因此 `solis_inverter` 與 `solis_inverter_map2` 已能由實際容器 catalog 自動發現。

### 3.2 隔離回歸

在容器 `/tmp` 執行 `scratch/validation_webui_catalog_20260811.py`（僅 temporary fixture 與 loopback port；不寫 `/app/profile/config.yaml`、不連設備）：

```text
WebUI catalog isolated regression: PASS
```

涵蓋項目：

| 項目 | 結果 | 證據／說明 |
| --- | --- | --- |
| 實際 Adapter discovery | PASS | `rtu`、`tcp`、`jkbms`、`st_inverter`、`solis_inverter` 全部被合格載入。 |
| fake 合格／缺欄位／import error plugin | PASS | 合格 plugin 仍出現；缺 `ADAPTER_NAME` 與 import error 各自被略過並產生 warning。 |
| Profile 篩選 | PASS | 正常 map 列出；`config.yaml`、`.bak`、`.tmp`、hidden、非 YAML 排除。 |
| catalog HTTP Basic auth | PASS | 未授權 loopback 請求為 401；正確 Basic auth 為 200。 |
| catalog 掃描失敗 | PASS | API 回空 catalog 加兩項 warning，未拋 500。 |
| 前端 contract | PASS（靜態） | 無舊 Adapter 寫死 option；Profile 為 select；含 catalog fetch、未知值 option、catalog→config 載入順序及 collectDevices value 保留。 |
| Python syntax／diff whitespace | PASS | AST parse 成功；`git diff --check` 通過。 |

### 3.3 Raw YAML 與未知值回歸判讀

前端在 `addDevice()`／`populateForm()` 建卡時，以 config 既有 Adapter／Profile 作為 `setCatalogOptions()` 的 current value；即使 catalog 沒有它，也會建立 value 完全相同的暫存 option。`collectDevices()` 維持輸出這個 value，故以下情境不會被改寫：

```yaml
adapter: legacy_missing_adapter
profile: legacy_missing_map
```

這是隔離靜態資料流驗證；沒有對目前正式 config 按儲存。

## 4. 對外可觀測性與未修改範圍

| 項目 | 結果 |
| --- | --- |
| `profile/config.yaml` | **未由本輪寫入**；施工前既有的 4 台 `solis_inverter`／`solis_inverter_map2` dirty state 保留原樣。 |
| Gateway polling、write、ACK、verify | 未修改。 |
| GenericAdapter、Solis semantic decode、Driver、BusMaster | 未修改。 |
| MQTT、HA Discovery／state／availability | 未修改。 |
| Profile map、map_validator、Docker、timeout／retry／offline state | 未修改。 |
| 未按「儲存」時的 runtime config | 不變；catalog API 無寫入路徑。 |

`main.py` 的唯一調整是將原本的 Adapter loader 本體抽至 helper，再以同一 `load_adapters(logger=logger)` 取得 `ADAPTER_FACTORY`。其掃描範圍、命名、檢查、衝突處理、失敗略過與啟動時 log 文意維持等價；沒有新增 Driver type 或將 Driver type 與 Adapter type 混在一起。

## 5. Diff 範圍審查

施工開始時，`profile/config.yaml` 的修改、`adapters/solis_inverter_adapter.py`、`profile/solis_inverter_map2.yaml` 及 `report/031`～`033` 已是既有 working-tree state；本輪從未覆寫、清理或改動它們。

最終 diff 審查時，本機出現並已同步至 `origin/main` 的 commit：

```text
8656c60 Update Solis inverter adapter, profile maps, WebUI catalog, and audit reports
```

它包含前述既有 Solis 工作，以及本輪的 WebUI catalog production 檔案：

- `src/adapter_catalog.py`
- `src/main.py`
- `src/web_admin.py`
- `src/index.html`

本 agent **沒有執行 `git commit` 或 `git push`**。最終 `git status` 僅剩本報告為 untracked，沒有任何未提交的 production diff；隔離測試位於已忽略的 `scratch/`，不會被意外納入提交。

## 6. 限制與後續建議

1. **實際 WebUI 瀏覽器驗收：NOT TESTED。** 容器 source 為 bind mount，但常駐 Python／WebUI process 不會 hot reload。本機 config 的施工前 dirty state 會在重啟後把 4 台設備切到實驗 map，故本輪沒有重啟來測畫面，也沒有儲存 config。
2. 前端 JavaScript 在環境中沒有可用的 JS runtime；已作 source-level contract 驗證。未來在安全的 staging config 下，可補「開 WebUI → 新增卡片 → 確認 `solis_inverter`／`solis_inverter_map2` → 不儲存關閉」的瀏覽器驗收。
3. catalog 故意不執行 validator，也不收緊 `_validate_config()`；若將來要顯示 profile 健康狀態，應另案評估 validator 與實驗 map 的相容性。
4. catalog 每次 WebUI 載入會重新 import Adapter plugins，與 runtime discovery 方式相同。外掛必須維持 import-time 無硬體／網路 side effect 的既有合約；本輪不新增新的 plugin metadata contract。

## 7. 最終裁決

**PASS WITH LIMITATIONS**

### Adapter 是否已不再依賴 HTML 寫死清單？

**YES**

### `solis_inverter` 是否能自動出現？

**YES**

### Profile 是否由實際 profile 目錄自動發現？

**YES**

### 未發現的既有 Adapter/Profile 是否仍能保持原值？

**YES**

### 是否修改任何 Gateway runtime 行為？

**NO**（唯一的 `main.py` 調整是把既有 Adapter discovery 委派給共用、等價 helper；沒有改變 Gateway 的 polling／transport／HA／MQTT 行為。）

### 是否建議交 Claude Code 做敵對式獨立驗收？

**YES**
