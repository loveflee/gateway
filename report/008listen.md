好問題,兩個都值得仔細講。我先把監聽失效的完整鏈路追出來,再看 adapter 能不能自己解決。

兩個問題我都追到底了。

## 一、監聽失效的完整鏈路

**不是「不更新但資料還在」—— 是 HA 上的實體會整組消失。**

```
adapter.feed() 卡住
  ↓ wait_for 2 秒逾時 → streak=1，log ERROR，該 worker 永久佔用
  ↓ 下一個 chunk 進來 → 換一個 worker → 又卡 → streak=2
  ↓ streak=3 → 保險絲熔斷
      ├─ _force_all_offline()  → 全部 set_availability(False)
      ├─ 對每台 uid 呼叫 gateway.unregister_device(uid)
      │     └─ send_discovery(cleanup=True)
      │           → 對 Discovery config topic 發【空的 retained payload】
      │           → HA 收到空 payload = 刪除實體
      ├─ executor 重建、streak 歸零
      └─ self.adapters 清空
  ↓
監聽迴圈【繼續跑】，但 adapters 是空的
  → 每個 chunk 進來，_decode_all_adapters 拿到空清單、立刻回傳
  → 不解碼、不發布、不再有任何日誌
```

**最終狀態:**

| | 結果 |
|---|---|
| HA 上的實體 | **消失**（不是變灰,是被刪掉） |
| 歷史數據 | 保留在 HA 的資料庫裡,但實體沒了、不再更新 |
| 網關進程 | 活著,主動軌(如果有)照常運作 |
| 日誌 | 熔斷當下一則 CRITICAL,**之後永遠安靜** |
| 卡死的 thread | 永久佔用,無法回收 |

**恢復方式:`./restart.sh` 就夠,不需要 `down` + `up -d`。**

理由:洩漏的是**進程內**的執行緒,不是容器狀態。`docker restart` 會送 SIGTERM → `stop()` → 進程試圖退出 → 但直譯器會 join 卡死的 thread 而卡住 → Docker 等 10 秒 grace period → **SIGKILL** → 連卡死的 thread 一起清掉 → 容器重啟 → 全新 Python 進程 → 設備重新註冊、Discovery 重發、HA 實體回來。

`down` + `up -d` 效果一樣,只是多花時間重建容器。**唯一的差別是 restart 會慢個 10 秒**(就是被 SIGKILL 的那段等待) —— 而 V1.13 那則 WARNING 就是為了讓你在 log 裡看到「喔,原來是這個在拖」。

---

## 二、能不能改 adapter 解決?—— 我查完的結論是:**你的 adapter 本來就不會卡**

我逐行審了 `jkbms_adapter.feed()` 的 `while True` 迴圈,檢查每條路徑是否保證前進:

| 路徑 | 動作 | 是否前進 |
|---|---|---|
| 找不到任何表頭 | `>1024` 清空 → `break` | ✅ 終止 |
| JK 表頭但資料不足 | `break` | ✅ 終止 |
| JK CRC 通過 | `del buffer[:jk_idx + p_len]` | ✅ 縮短 |
| JK 假表頭 | `del buffer[:jk_idx + 4]` | ✅ 至少縮 4 bytes |
| Modbus 資料不足 | `break` | ✅ 終止 |
| Modbus 合法 | `del buffer[:mb_idx + cmd_len]` | ✅ 縮短 |
| Modbus 假表頭 | `del buffer[:mb_idx + 2]` | ✅ 至少縮 2 bytes |

**每一條 `continue` 之前都至少砍掉 2 bytes,每一條不砍的都 `break`。** 迴圈上界 = 緩衝區長度 ÷ 2,必然終止。而且全程是純位元組運算,**沒有任何 I/O、沒有 sleep、沒有鎖**。

所以:

> **子行程隔離、執行緒池、保險絲 —— 這整套是在防一個「未來某個 adapter 寫爛」的假想敵,不是在防你現在的 jkbms adapter。**

那個熔斷機制到目前為止**從來沒有、也不可能被你現在的程式碼觸發**。

## 那要不要改 adapter?

**不需要修,但可以加一道「未來防呆」—— 而且比子行程好一個數量級。**

在 `while True` 外面加一個迴圈次數上限:

```python
MAX_ITER = 512          # 單一 chunk 最多切 512 個封包，遠超實際需求
for _ in range(MAX_ITER):
    ...原本的迴圈體...
else:
    logger.error("[jkbms] 單次 feed 迭代超過上限，強制中止並清空緩衝區")
    self.buffer.clear()
```

三行,把「理論上可能無窮」變成「數學上不可能無窮」。**這才是正確的層級** —— 在會出事的地方畫界線,而不是在外面包一層跨行程隔離去對付它。

## 兩種方案的對照

| | 子行程隔離 | adapter 加迭代上限 |
|---|---|---|
| 程式碼量 | 大（序列化 chunk、跨行程傳 dict、行程生命週期管理、新的監控對象） | **3 行** |
| 效能 | 每個 chunk 跨行程序列化,JK BMS 平均 165ms 一幀 | 零成本 |
| 解決的是 | 卡住之後怎麼隔離 | **讓它不可能卡住** |
| 新增故障面 | 行程崩潰、pipe 斷、殭屍行程 | 無 |
| 現況需要嗎 | ❌ 你的 adapter 不會卡 | 🔸 純未來防呆 |

**我的建議:什麼都不用改。**

現有的三層防護（2 秒逾時 → 3 次熔斷 → 卸載設備 + V1.13 診斷）對一個**根本不會觸發**的情境已經過度保護了。要動的話,就是上面那三行迭代上限,而且是等你哪天改 adapter 邏輯時順手加,不是現在專程去改。

真的要花力氣,**B 選項比較實在:把 V1.13 同步到 py_jkbms**（那邊還停在 V1.10,是唯一真的跑 listen 的地方）。要我做嗎?
