# 015 Host 計數看門狗方案裁決

**裁決：REJECT（原樣不可採用）；在下列修正後可條件式 ADOPT。** 核心的「跨 Docker 重啟計數」能取代 `uptime > 300s` 判斷，但 `/tmp` 單檔 bind mount、第三次後的重啟語意、Docker 10 秒啟動門檻及 HA 初始 offline 均未處理。原樣實作不符合「最小且外部一致」。

## 現況證據

- 現有熔斷為連續 **3** 次，每次 `run_in_executor()` 等 **2 秒**；達門檻後設 offline，接著呼叫 `gateway.unregister_device()`、清 Discovery、重建 executor（[listen_master.py:207](../src/listen_master.py#L207)–[245](../src/listen_master.py#L245)）。
- `unregister_device()` 會送 retained 空 Discovery 並刪除 HA manager（[main.py:351](../src/main.py#L351)–[370](../src/main.py#L370)），所以現況不是只顯示 unavailable。
- Compose 已有 `restart: unless-stopped`，但沒有狀態掛載（[docker-compose.yaml:6](../docker-compose.yaml#L6)、[14](../docker-compose.yaml#L14)–[20](../docker-compose.yaml#L20)）。目前 `config.yaml` 全為 `active`，沒有 listen 實機可驗證（[profile/config.yaml:21](../profile/config.yaml#L21)–[45](../profile/config.yaml#L45)）。

## 十項回答與決策

| # | 問題 | 判定與最小要求 |
|---|---|---|
| 1 | 可否取代 300s heuristic | **可以，且較正確。** 狀態檔跨進程，首次啟動不加計；僅在第三個真實 2s timeout fuse 寫入。不過退出前要等容器已存活 10.1 秒，這是 Docker restart policy 的啟動條件，不是舊的 300s 故障判斷。`start_time` 現在每個進程重設（[main.py:166](../src/main.py#L166)–[170](../src/main.py#L170)），不能承擔跨重啟計數。 |
| 2 | 最小安全 Compose 掛載 | **不可 bind 單一 `/tmp/gateway.status` 檔。** `os.replace()` 的目標若是檔案掛載點，不能保證 rename 成功/原子。若必須新增掛載，預先建立 host 目錄 `/var/lib/py_ginlong/listen-watchdog`，掛成 `/run/listen-watchdog`，再寫其內的 `status.json`。 |
| 3 | 最小 `listen_master` 改動 | 新增持久 store、`_decoding_disabled`、熔斷分支及穩定期欄位；刪除熔斷分支的 unregister／Discovery cleanup／executor 重建。`_force_all_offline()` 可保留（[listen_master.py:184](../src/listen_master.py#L184)–[195](../src/listen_master.py#L195)）。 |
| 4 | 併發/鎖 | 用同目錄 sidecar `.lock` 的 `fcntl.flock(LOCK_EX)` 包住「讀取→加一→寫入」。單一 container 正常只有 event loop 一個 writer，但手動雙啟動或管理工具會造成 lost update，不能假設沒有併發。 |
| 5 | 原子 JSON | temp 必須與 status 在**同一目錄**：`mkstemp` → JSON → `flush/fsync` → `os.replace` → `fsync(directory)`；寫入或 JSON 讀取失敗時採安全鎖定，**不可**退出後讓計數遺失。 |
| 6 | executor 前 guard | 必須在目前 snapshot/submit 前（[listen_master.py:204](../src/listen_master.py#L204)–[212](../src/listen_master.py#L212)）判斷 `_decoding_disabled`。第三次熔斷後不再 submit；driver 仍可讀取以保留 raw traffic，但不得再解碼。 |
| 7 | HA availability/Discovery | 刪除 unregister 路徑，僅 offline，Discovery retained 才會留存。另必須在 listen 註冊時 `ha_manager.set_availability(False)`；現有註冊只設內部 `online=False`（[listen_master.py:95](../src/listen_master.py#L95)–[103](../src/listen_master.py#L103)），而 HA cache 初值 `None` 會使重連補發直接 return（[ha_manager.py:53](../src/ha_manager.py#L53)–[89](../src/ha_manager.py#L89)）。否則重啟後可遺留 stale `online`。 |
| 8 | 有效 JK 幀 + 15 分鐘 reset | 可行，但只在 `trip_count < 3` 的健康重啟窗口做。任何 timeout 清空 `valid_jkbms_since`；第一個有效 JK decoded dict 設 monotonic 起點，持續 900 秒無 timeout 才寫 0。**第三次已 guard 的鎖定狀態無法再解碼，故不可能自動看到有效幀；它必須跨手動重啟仍鎖定，待修復後由明確管理動作清除。** 目前 dispatcher 未保存 adapter name（[listen_master.py:63](../src/listen_master.py#L63)–[101](../src/listen_master.py#L101)），若要求「JK」而非任一有效幀，`main.py` 掛載處（[main.py:892](../src/main.py#L892)–[926](../src/main.py#L926)）須傳入 `adapter_name`。 |
| 9 | Debian 13 的 `/tmp` | **不能當跨 VM reboot 計數。** systemd-tmpfiles 會在開機及定期管理/清理 temporary path；`/tmp` 的壽命與 mount 設定均不保證。用 `/var/lib` 或既有持久 bind mount；FHS 明確把 reboot 保留需求指定給 `/var/tmp`，但長期狀態仍以 `/var/lib` 較合適。[​systemd-tmpfiles](https://www.freedesktop.org/software/systemd/man/systemd-tmpfiles.html) [FHS `/var/tmp`](https://specifications.freedesktop.org/fhs/latest/varTmp.html) |
| 10 | 更小的跨重啟替代 | **建議。** 既有 `./profile:/app/profile:rw`（[docker-compose.yaml:17](../docker-compose.yaml#L17)–[20](../docker-compose.yaml#L20)）已是 host 持久 bind mount；用 `/app/profile/.listen-watchdog.json` 及 `.lock`，並加入 `.gitignore`，可省掉 Compose 改動且避開 `/tmp`。Docker 本身沒有可讓程式安全讀取的重啟次數；`on-failure:2` 會讓第三次容器停死、WebUI 也不在，不符合目標。 |

## Docker 與外部可觀測行為

`os._exit(75)` 會跳過 [main.py:1026](../src/main.py#L1026)–[1109](../src/main.py#L1109) 的優雅 stop（正常 stop 會 unregister，反而清 Discovery）。這正好保留 Discovery，但 `_force_all_offline()` 的 QoS 1 publish 未等待送達；下一進程的「註冊即 availability=false」是必要補償。Gateway LWT 已設定為 retained offline（[main.py:765](../src/main.py#L765)）。

`unless-stopped` 會在非零退出後重啟，但 Docker 只在 container 已成功存活至少 10 秒後才啟用 restart monitoring；因此「三次 timeout 約 6 秒就 exit」不能保證會重啟。[​Docker restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/) 最小做法是在第一、二次 fuse：先寫成功、立刻設 guard、必要時 sleep 至 process age 10.1s，再 `os._exit(75)`。不要清 adapter buffer，也不要假裝能殺掉卡住的 Python thread。

另有獨立邊界：listen driver 首次 `connect()` 失敗就直接 `sys.exit(1)`（[main.py:698](../src/main.py#L698)–[734](../src/main.py#L734)），發生在 dispatcher/計數器建立之前。此 Host counter 不會改善該啟動失敗，且它也可能落在 Docker 的 10 秒門檻前；不得把兩種故障混為同一個「自動復原」承諾。

## 建議的最小補丁計畫

採 **既有 profile bind mount** 版本（較新增 Compose mount 更小）：

1. 在 `listen_master.py` 加約 50–70 行狀態 helper；狀態含 `trip_count`、wall-clock `last_trip`、可選 `locked`。啟動讀到 `locked/trip_count>=3` 即先鎖定；狀態毀損或無法持久寫入也鎖定並記 CRITICAL。
2. `register_device()` 設 HA initial offline；若必須限定 JK，讓 `main.py` 傳入已知 `adapter_name`。維持 Discovery 發送流程（[main.py:374](../src/main.py#L374)–[438](../src/main.py#L438)）。
3. 三次 timeout 時：offline → 在鎖內加計 → count 1/2 等 Docker 10.1s arming 後 exit 75；count 3 只鎖定。移除 [listen_master.py:231](../src/listen_master.py#L231)–[244](../src/listen_master.py#L244) 的 unregister/recreate。
4. 將 guard 放在 executor 前；有效 JK 幀啟動 900s 穩定時計時，任何 timeout 取消它。count 3 鎖定後只允許經文件化管理動作清除，不能宣稱會自復。
5. `.gitignore` 排除兩個 runtime status 檔；不改 `config.yaml`、不清 buffer、不新增 thread-kill。

## 隔離驗證

執行：

```bash
timeout 60 python3 -u scratch/validation_015/harness.py \
  > scratch/validation_015/out/run.log 2> scratch/validation_015/out/run.err
```

結果：`exit=0`、`validation_015: PASS`。harness 僅使用標準庫與 fake driver、bounded slow/valid JK adapter、mock retained broker：

- 靜態確認目前門檻/cleanup/restart policy/無 status mount/無 listen 實機設定。
- 6 個 process 各加 20 次，sidecar lock + atomic replace 得到精確 `trip_count=120`，無殘留 temp。
- 模擬三次 fuse：trip 1/2 請求 exit 75；trip 3 offline 並鎖定，後續 8 chunk 的 executor submit 為 0，Discovery 保留。
- 模擬有效 JK 穩定期 reset 及 timeout 使穩定期失效。

產物僅在 `scratch/validation_015/`；未修改 production source/config、Docker/Compose、container、hardware 或 MQTT。此為模型與靜態驗證，因現行設定沒有 listen 設備，尚非實機驗收。
