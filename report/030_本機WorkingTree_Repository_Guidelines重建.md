# 030 — 本機 Working Tree Repository Guidelines 重建報告

**日期**：2026-08-11

**任務性質**：唯讀架構盤點＋文件重寫

**唯一修改檔**：repository root `AGENTS.md`、本報告
**未進行**：production code、Adapter、profile、runtime config、Docker、Git commit／push／pull／remote 查詢

## 人話結論

1. **舊 Guidelines 已過期的部分**：它把專案說成「沒有自動化測試」，但本機已有多組 `scratch/validation_*`、`unittest` 型的 audit、replay 與 FC16 隔離驗收工具；正確說法應是「沒有 repository 根目錄的正式 test suite／CI」，而不是「完全沒有測試」。舊文件的 Docker bind mount、Web UI 設定保護與近期 GenericAdapter FC16 能力也不足。
2. **目前最主要的架構變化**：目前是 asyncio Gateway，具主動輪詢與監聽雙軌、BusMaster 寫入後 ACK／verify、MQTT／HA Discovery、已驗證／備份保護的 Web UI 設定，以及近期已落在本機 working tree 的 GenericAdapter 32-bit FC16 quantity=2 路徑。
3. **新版的重要規則**：以本機 working tree 為真相、先保存 dirty state、明確區分靜態／隔離／實機驗證、將既有 HA/MQTT 與寫入行為列為回歸邊界，並把 live OT 安全與最小修改列為硬原則。
4. **有沒有碰 production code？** 沒有。本輪未改 `src/`、`adapters/`、`profile/`、Docker 或 requirements。
5. **有沒有碰 GitHub remote？** 沒有。本輪沒有查詢、pull、merge、push 或以 remote 校正本機。
6. **有待另案處理但本輪未改的項目**：validator 與 GenericAdapter codec 能力的同步、`dcba` read/write 歷史不對稱、FC15／FC22／FC23／64-bit write 的需求評估，以及正式版本控制 test suite／CI 的建立。

## 1. 盤點範圍與 working-tree 保護

依規則先檢查本機 Git，不清理、不覆蓋：

```text
HEAD: fc45e9e  Refactor gateway configuration and device profile handling

已修改：
  adapters/generic_adapter.py
  profile/config.yaml
  report/021_南北向靜默失敗與跨模組記憶體洩漏審查.md

未追蹤：
  profile/6k2.yaml、profile/6k2p1.yaml、profile/6k2p2.yaml
  report/001_當前架構注意事項.md、report/022 至 report/029 等近期報告
```

完整 `git diff --no-ext-diff` 已在動工前讀取。其顯示 `generic_adapter.py` 的 FC16 施工 diff 及 `config.yaml` 的 poll interval 變更均為本輪既有 working-tree 狀態；本輪未改動它們。沒有使用 remote 指令。

## 2. 舊 Guidelines 審查結果

| 舊主題 | 判定 | 本次處理 |
| --- | --- | --- |
| Project structure | 部分正確 | 改以實際 `src/`、`adapters/`、`profile/`、`report/`、`scratch/` 與 Web UI／雙軌架構重寫。 |
| Build／restart commands | 部分正確 | 重新依六個現有 shell script 與 compose bind mounts 寫出適用時機。 |
| Adapter discovery | 部分正確 | 明確限定為 `/app/src/adapters` 的頂層 `*_adapter.py`，並保留 `ADAPTER_NAME`／`Adapter` contract。 |
| Profile validation | 部分正確 | 保留已存在的 container validator command，並說明它的覆蓋邊界及 Web UI 的 `.bak`／`.lock` 保護。 |
| Coding style／version header | 部分正確 | 改為「沿用既有檔局部風格」，不虛構 formatter 或強制 CI 規則。 |
| Configuration safety／`.env` | 仍正確但不足 | 補上 Web UI 原子寫入、備份還原、環境變數秘密界線與 live OT 禁忌。 |
| Docker bind mounts | 部分正確 | 更新為目前 compose 的三個 mount、host network、container name 與 restart/rebuild 判斷。 |
| Git／PR 規則 | 部分正確 | 以本機較新的 working tree 為主，禁止未授權同步、清理與提交。 |
| 「沒有 automated tests」 | 已過期且容易誤導 | 改為：無 root `tests/`／CI／通用 pytest，但有 static validator、scratch harness 和必要實機驗收。 |

## 3. 新版 Guidelines 的內容與證據

| 新章節 | 實際依據 |
| --- | --- |
| Working-tree 真相與 Git 保護 | 本輪 `git status --short`、`git diff --stat`、完整 `git diff`、本機 HEAD。 |
| Runtime／雙軌／寫入鏈 | `src/main.py`、`src/bus_master.py`、`src/listen_master.py`、`src/driver.py`。 |
| MQTT、HA、Web UI | `src/mqtt_client.py`、`src/ha_manager.py`、`src/web_admin.py`、`src/app_state.py`。 |
| Adapter discovery 與 contract | `src/main.py::load_adapters()`、`BusMasterScheduler` 的 required methods、實際 `adapters/` 檔案。 |
| GenericAdapter FC16 現況 | 現行 `adapters/generic_adapter.py`；近期 `report/026`、`027`、`028`、`029` 僅作為已落地能力與驗收背景的交叉證據。 |
| Profile 與 validator | `profile/config.yaml`、各 `profile/*.yaml`、`src/map_validator.py`、`src/web_admin.py`。 |
| Docker／指令 | `docker-compose.yaml`、`Dockerfile`、`build.sh`、`restart.sh`、`up.sh`、`down.sh`、`log.sh`、`it.sh`。 |
| 測試能力與限制 | repository 檔案盤點、`scratch/master_rtu_audit_20260808.py`、`scratch/validation_012` 至 `validation_028`；`scratch/solis_modbus-master/tests` 明確視為第三方參考專案測試，非本 gateway CI。 |

## 4. 實際架構摘要

- Container command 為 `python /app/src/main.py`，working directory 是 `/app/profile`。`main.py` 以 `EdgeGateway` 組裝 config、Driver、Adapter、HA/MQTT 與 Web UI。
- 主動設備經 `BusMasterScheduler` 排程輪詢與寫入；寫入路徑具有 ACK 後 verify read 的 contract。監聽設備經 `ListenMasterDispatcher`／Adapter `feed()` 處理。
- `docker-compose.yaml` 將 host 的 `src/`、`adapters/`、`profile/` 掛載進容器；故變更檔案後仍需 restart 才會由 Python process 重新載入。
- `web_admin.py` 對 config 實作 Basic Auth、驗證、`.bak`、`.lock` 與原子寫入／還原；秘密由 `.env` 的環境變數提供。
- Adapter loader 只載入頂層 `*_adapter.py`，需要 `ADAPTER_NAME` 與 `Adapter`。GenericAdapter 實際程式已包含 FC01–04 read、FC05／06／16 write；FC16 在可明確推導為 4-byte `uint32`／`int32`／`float32` metadata 時使用 quantity=2，否則保留 legacy count=1。

## 5. 本輪修改與自我驗證

### 實際修改

1. 以繁體中文完全重寫 root `AGENTS.md`，未建立另一份 Guidelines 檔。
2. 新增本報告，保存本次本機架構盤點、舊文件判定與證據來源。

### 沒有修改

`src/`、`adapters/`、`profile/`、`Dockerfile`、`docker-compose.yaml`、`requirements.txt`、shell script 與既有 report 均未由本輪修改。

### 文件逐項回查

- 所有命令均來自目前存在的 script 或 `src/map_validator.py` 的 CLI；未加入 pytest、CI 或不存在的部署指令。
- GenericAdapter 只描述目前程式可驗證的 FC16 quantity=2／32-bit metadata 路徑，未把 deferred FC15／FC22／FC23／64-bit write 寫成現有能力。
- 測試章節明確區分 root 正式 test suite 缺席、scratch harness 的侷限與實機必要性；沒有將第三方 `solis_modbus` 的 tests 當作本專案 CI。
- 本輪最後會再次檢查 diff 範圍，確認只出現 `AGENTS.md` 與本報告的新文件變更，並保留原有 dirty state。

## 6. 停止點

本任務完成後不 commit、不 push、不 pull、不 restart Docker。等待人工先審閱目前本機 production 變更、reports 與新版 `AGENTS.md`；GitHub 同步前審查必須另開任務。
