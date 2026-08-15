# =============================================================================
#version listen_master.py - V1.17 真・工業旁路轉運工 (無頭殭屍防護版)
# 相容：HAManager V2.9.5+、ListenDriver V1.9.2+、(自定義) ListenAdapter
# 核心職責：
#   1. 流式派發：將底層 ListenDriver 吐出的位元組流，派發給註冊的切包機。
#   2. 狀態防洪：內建 Diff 比對，只有數值發生實質改變時才觸發 MQTT 推播。
#   3. 被動看門狗：若超時未收到有效幀，自動觸發 HA 離線狀態。
#   4. 即時斷線：精準捕捉 DriverDisconnectError，網路異常時秒切 OFFLINE。
# 修復歷程 (V1.8.4 -> V1.8.5 最終南向閉環)：
#   - [Fix] 屬性解耦：_check_offline_status 改用 getattr 讀取 _availability_cache，防禦 HA Manager 改版崩潰。
#   - [Fix] 殭屍防護：_force_all_offline 拔除條件判斷，確保保險絲熔斷強制註銷前，必定對 HA 觸發 set_availability(False)，杜絕孤兒殭屍。
# 修復歷程 (V1.8.5 -> V1.9)：
#   - [Critical] 修復 diff 快取「先記帳、後發布」導致的數值永久遺失：
#     publish_state 若被 HAManager 的 200ms 節流吞掉，快取已記為已送出，
#     同值再來時 has_changed=False，該筆變更永遠不再補送。實測 JK BMS 的
#     chunk 平均 165ms 到達（低於節流窗），告警位元 OFF→ON 落在節流窗且其後
#     資料流靜止時，HA 將永遠收不到告警。改為依 publish_state 回傳值決定
#     是否撤回記帳，未送出即還原，下一幀重試至成功為止。
# 修復歷程 (V1.9 -> V1.10)：
#   - [Fix] Adapter 解碼逾時僅於達到熔斷門檻時重建 executor，避免每次逾時疊加新 thread pool。
# 修復歷程 (V1.10 -> V1.11)：
#   - [Contract] register_device 明確回傳掛載成功與否，供上游阻斷 HA 殭屍實體。
# 修復歷程 (V1.11 -> V1.12)：
#   - [Observability] 新增在途解碼工作計數與關機診斷。shutdown(wait=False,
#     cancel_futures=True) 只能取消「尚未開始」的工作；已在執行且永久卡死的
#     adapter.feed() 無法中止，而 Python 直譯器退出前會 join 所有 non-daemon
#     worker（concurrent.futures 的 _python_exit），因此關機會一路卡到 Docker
#     grace period 用完被 SIGKILL —— 在此之前操作者完全沒有線索。
#     現於 stop() 印 CRITICAL 指出卡住幾個、為何無法取消、根因該查哪裡。
#     純觀測，不改變任何解碼、熔斷或關機的既有行為。
# 修復歷程 (V1.12 -> V1.13)：
#   - [Fix] 計數點與措辭修正。V1.12 在「提交端」增量，等於把仍排在佇列中、
#     會被 shutdown(cancel_futures=True) 直接取消的工作也算進去；那些不會擋住關機。
#     改為在 _decode_all_adapters() 的第一行增量，只計「已開始執行」的工作。
#   - [Fix] 關機 log 由 CRITICAL 降為 WARNING，措辭不再宣稱「卡死」：正常解碼只需
#     毫秒級，關機當下剛好有一筆在跑完全可能且會自行結束。誤報一則 CRITICAL 的
#     代價比漏報高。現改為陳述現況並附上「若關機真的卡住」的排查方向。
# 修復歷程 (V1.13 -> V1.14) —— 依 report/012 修訂版 v2 實作熔斷復原：
#   - [Critical] 熔斷不再刪除 HA 實體。原本呼叫 gateway.unregister_device() →
#     send_discovery(cleanup=True)，對 Discovery topic 發空 retained，等同把實體
#     從 HA 直接刪掉（歷史數據雖在，實體已不存在且不再更新）。現僅 set_availability
#     (False)，實體完整保留、只顯示 unavailable。
#   - [Critical] 熔斷改依 process uptime 分流：
#       > UPTIME_RESTART_THRESHOLD → 判為暫時性卡頓，os._exit(75) 交由 Docker 重啟，
#         由 OS 回收卡死的 non-daemon thread（Python 無法中止執行中的 thread）。
#       ≤ UPTIME_RESTART_THRESHOLD → 代表剛重啟又熔斷，判為持續性故障，
#         改為鎖定解碼並續活，使自動重啟次數上界為 1，不會形成迴圈。
#   - [Critical] 退出前補足 Docker restart policy 的 10 秒 arming 門檻。官方文件明載
#     「restart policy 僅於容器成功存活至少 10 秒後才生效」；早於此退出容器會停死、
#     WebUI 一併消失。自然週期（啟動約 5s + 3×2s 熔斷）約 11s 屬臨界值，必須補足。
#   - [Critical] register_device() 註冊即發布 offline，關閉「陳舊 retained online」
#     競態：否則上一進程未送達的 offline 會讓 broker 停在 online，與新進程的
#     gateway online 同時成立（availability_mode: all），HA 把陳舊值顯示為可用。
#   - [Fix] 新增 _decoding_disabled guard，且必須置於 run_in_executor() 之前 ——
#     置於取回結果之後無效，工作早已提交，worker 仍會被逐一燒光。
#   - [Fix] 移除熔斷時的 executor 重建：卡死 thread 屬舊 pool 且無法回收，
#     重建只是在卡死 thread 仍在的情況下疊加新 pool。
# 修復歷程 (V1.14 -> V1.15)：
#   - [Critical] 與 bus_master V4.10 同步（report/056 F3）：_process_valid_frame 的
#     ONLINE 轉換與 _check_offline_status 的 OFFLINE 轉換，皆改為 availability
#     發布成功才提交 state["online"]。原本先提交再通知，通知失敗後兩處守衛都
#     永不重入，沒有補送機制。失敗時維持原狀態：ONLINE 方向等下一個有效封包
#     重試，OFFLINE 方向由下一輪 _check_offline_status（條件仍成立）重試，
#     未新增計時器。register_device 的初始 offline 發布（V1.14）不需門檻，
#     該處本來就沒有要提交的狀態轉換。
# 修復歷程 (V1.15 -> V1.16)：
#   - [Critical] _force_all_offline 補上 V1.15 漏掉的門檻（report/058 F3）。該處先把
#     本地狀態改 offline 再通知，通知失敗時 availability cache 被撤回成 online，
#     而本地已 offline → _check_offline_status 兩個分支都進不去 → 永不補送；
#     MQTT 一恢復，republish 反而依舊 cache 重送 online，把已斷線設備宣告在線。
#     改為發布成功才提交，失敗時維持 online 且不更新 last_seen，使離線檢查
#     下一輪立刻重入（更新 last_seen 會把重試推遲整整一個 offline_time）。
# 修復歷程 (V1.16 -> V1.17)：
#   - [Observability] 超長字串（>256）欄位的丟棄補 WARNING（report/060 F4）。守衛
#     本身正確（防垃圾幀撐爆 state cache），錯的只有不出聲：同幀其他欄位照常發布、
#     設備仍 online，HA 上就那一個欄位停在舊值。現行 12 份地圖最長的 string 欄位
#     為 32 bytes 且只存在於主動軌的 relay_8ch_full_map，此路徑目前不可觸發。
# =============================================================================

import asyncio
import os
import time
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from listen_driver import DriverDisconnectError

logger = logging.getLogger(__name__)

# ✅ [Fix V1.14] 進程起點。本模組於 main.py 頂部被匯入，且容器 command 直接執行
#    main.py（PID 1），故此值即等同「容器啟動時刻」，可用於 Docker restart policy
#    的 10 秒 arming 判斷。匯入耗時使此值略晚於容器真正啟動，計算出的 uptime 會
#    略微低估 —— 方向安全（寧可多等，不可少等）。
_PROCESS_START = time.monotonic()

def _values_equal(a, b, tolerance: float = 0.01) -> bool:
    if type(a) == type(b) and isinstance(a, (int, str)):
        return a == b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False

class ListenMasterDispatcher:
    # ✅ [Fix V1.14] 熔斷分流門檻。
    #   · UPTIME_RESTART_THRESHOLD：進程存活超過此秒數才判定為「暫時性卡頓」，
    #     值得交由 Docker 重啟試一次；否則視為「重啟後仍持續故障」直接鎖定，
    #     使自動重啟次數上界為 1，不會形成迴圈。須遠大於「啟動+熔斷」單次週期。
    #   · DOCKER_ARM_SECONDS：Docker 官方文件明載 restart policy 僅於容器成功存活
    #     至少 10 秒後才生效；早於此退出，容器會直接停死、WebUI 一併消失。
    UPTIME_RESTART_THRESHOLD = 300.0
    DOCKER_ARM_SECONDS = 10.0

    def __init__(self, driver, offline_time: int = 60):
        self.driver = driver
        self.offline_time = offline_time

        self.adapters = {}
        self.ha_managers = {}
        self.device_states = {}
        self._last_state_cache = {}

        self.running = False
        self._task = None
        self.MAX_KEYS_PER_FRAME = 256

        # 專屬執行緒池隔離解碼器
        self._executor_workers = 4
        self._executor = ThreadPoolExecutor(max_workers=self._executor_workers, thread_name_prefix="ListenDecoder")

        # 🚀 [V1.8.4 防護] OS Thread 洩漏保險絲計數器
        self._decode_timeout_streak = 0
        self._decode_timeout_threshold = 3

        # ✅ [Fix V1.14] 熔斷後的解碼鎖定旗標。為真時不再向 executor 提交工作，
        #    避免持續性故障把 4 個 worker 全部燒光。driver 仍照常讀取，
        #    app_state.traffic_log 的原始側錄不受影響。
        self._decoding_disabled = False

        # ✅ [Fix V1.12/V1.13] 「已開始執行但尚未跑完」的解碼工作計數，供關機診斷。
        #    增減量都發生在 worker 執行緒（多個 worker 併發），故需鎖保護
        #    —— int 的 += 在多執行緒下不是原子操作。
        self._inflight_decodes = 0
        self._inflight_lock = threading.Lock()

    def register_device(self, uid: int, adapter, ha_manager) -> bool:
        if not isinstance(uid, int) or uid < 0:
            logger.error(f"[ListenMaster] 無效 UID: {uid}，必須為正整數")
            return False

        if not hasattr(adapter, "feed"):
            logger.error(f"[ListenMaster] 設備 #{uid} adapter 缺少 feed()，註冊失敗")
            return False

        self.adapters[uid] = adapter
        self.ha_managers[uid] = ha_manager
        self.device_states[uid] = {
            "last_seen": time.monotonic(),
            "online": False
        }
        self._last_state_cache[uid] = OrderedDict()

        # ✅ [Fix V1.14] 註冊即發布 offline，關閉「陳舊 retained online」競態。
        #    HAManager 的 _availability_cache 初值為 None，republish_availability()
        #    會因此直接 return；而未上線的設備不會走到 _check_offline_status 的任一
        #    分支（兩者皆要求 state["online"] is True）。若上一進程的 offline 未送達
        #    （_force_all_offline 沒有 wait_for_publish），broker 上的 online 就會永久
        #    存活，與新進程的 gateway online 同時成立 → HA 把陳舊數值顯示為「可用」。
        #    此處補一次發布同時覆蓋 broker retained 並讓 cache 變為 False，
        #    使日後 MQTT 重連的 republish_availability() 生效。
        try:
            ha_manager.set_availability(False)
        except Exception:
            logger.exception(f"[ListenMaster] 設備 #{uid} 初始 availability 發布失敗（不影響掛載）")

        logger.info(f"[ListenMaster] 🎧 監聽設備 #{uid} 已註冊 (離線判定: {self.offline_time}s)")
        return True

    def unregister_device(self, uid: int):
        if uid not in self.adapters:
            return

        # 🚀 [Fix] 消除 Discovery 殭屍：拔管前先發送 Cleanup
        ha_mgr = self.ha_managers.get(uid)
        if ha_mgr:
            try:
                ha_mgr.send_discovery(cleanup=True)
            except Exception as e:
                logger.debug(f"[ListenMaster] 設備 #{uid} 清理 Discovery 異常: {e}")

        del self.adapters[uid]
        del self.ha_managers[uid]
        del self.device_states[uid]
        del self._last_state_cache[uid]
        logger.info(f"[ListenMaster] 設備 #{uid} 已安全註銷並清理 MQTT 殘留")

    def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._listen_loop())
        logger.info("[ListenMaster] 旁路轉運工已啟動")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # ✅ [Fix V1.13] 關機診斷。措辭刻意不說「卡死」——
        #    這個計數是「已開始但尚未跑完」，正常解碼只需毫秒級，關機當下剛好有一筆
        #    在跑也完全可能，那種情況會自行結束、不影響關機。
        #    它的用途是：當 docker stop 真的卡住時，這一行就是唯一的線索。
        with self._inflight_lock:
            running = self._inflight_decodes
        if running > 0:
            logger.warning(
                f"[ListenMaster] ⚠️ 關機時仍有 {running} 個 Adapter 解碼工作執行中"
                f"（共 {self._executor_workers} 個 worker）。若它們能正常結束則本次關機不受影響；"
                f"若關機在此卡住，成因與排查方向如下：\n"
                f"  · shutdown(wait=False, cancel_futures=True) 只能取消【尚未開始】的工作，"
                f"已在執行的 thread 無法中止。\n"
                f"  · Python 直譯器退出前會 join 這些 non-daemon thread"
                f"（concurrent.futures 的 _python_exit），因此關機可能等到 Docker 的"
                f" grace period 用完後被 SIGKILL 強制結束。\n"
                f"  · 根因請排查 adapter.feed() 是否有無窮迴圈或永久阻塞的外部 I/O。"
            )

        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info(f"[ListenMaster] 旁路轉運工已安全停止（關機當下執行中的解碼工作: {running}）")

    def _decode_all_adapters(self, adapters_snapshot: list, chunk: bytes) -> list:
        # ✅ [Fix V1.13] 增量移到 worker 內第一行（原本在提交端）。
        #    只有「已經開始執行」的工作才會擋住關機：尚在佇列中的會被
        #    shutdown(cancel_futures=True) 直接取消，不該計入。
        with self._inflight_lock:
            self._inflight_decodes += 1
        results = []
        try:
            for uid, adapter in adapters_snapshot:
                try:
                    decoded = adapter.feed(chunk)
                    if isinstance(decoded, dict) and decoded:
                        results.append((uid, decoded))
                except Exception:
                    logger.exception(f"[ListenMaster] 設備 #{uid} Adapter 解析崩潰")
            return results
        finally:
            # ✅ [Fix V1.12] 只有真的跑完才減量。feed() 若永久阻塞就永遠走不到這裡，
            #    計數會一直掛著 —— 那正是關機診斷要抓的訊號。
            with self._inflight_lock:
                self._inflight_decodes -= 1

    def _force_all_offline(self):
        """實體斷線或保險絲熔斷時強制刷寫狀態"""
        now = time.monotonic()
        for uid, state in self.device_states.items():
            # 🚨 [V1.8.5 修復] 拔除 if state["online"] 判斷。
            # 無論當前狀態為何，既然要強制拔管，就必須對 HA 補最後一槍，杜絕孤兒殭屍。
            ha_mgr = self.ha_managers.get(uid)
            # ✅ [V1.16] 與 V1.15 的另外兩處同一原則（report/058 F3）。V1.15 漏了
            #    這一處：本函式先把本地狀態改成 offline 再通知，通知失敗時
            #    set_availability 會把 availability cache 撤回成前值（online），
            #    而本地已是 offline → _check_offline_status 的兩個分支都進不去
            #    → 永不補送；MQTT 一恢復，republish_availability 反而依那個舊
            #    cache 重送 online，把一台實際已斷線的設備宣告為在線。
            #    改為發布成功才提交；失敗則維持 online 且**不更新 last_seen**，
            #    讓 _check_offline_status 下一輪立刻重入補送（若更新了
            #    last_seen，重試會被推遲整整一個 offline_time）。
            if ha_mgr is None or ha_mgr.set_availability(False):
                state["online"] = False
                state["last_seen"] = now
                logger.warning(f"[ListenMaster] 🚨 實體連線中斷或觸發熔斷，設備 #{uid} 強制判定 OFFLINE")
            else:
                logger.warning(
                    f"[ListenMaster] 🚨 設備 #{uid} 需強制 OFFLINE，但 availability 發布失敗，"
                    f"維持內部 ONLINE 且不更新 last_seen，由離線檢查下一輪重試"
                )

    async def _listen_loop(self):
        loop = asyncio.get_running_loop()
        while self.running:
            try:
                chunk = await self.driver.read_stream(chunk_size=1024, timeout=5.0)
                now = time.monotonic()

                if chunk:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[RAW RX] {chunk.hex().upper()}")

                    # ✅ [Fix V1.14] guard 必須置於 run_in_executor() 之前。
                    #    若放在取回結果之後，工作早已提交，worker 仍會被逐一燒光
                    #    （實測：提交 6 件卡死工作即佔滿全部 4 個 worker）。
                    #    此處仍呼叫 _check_offline_status 以保留離線判定。
                    if self._decoding_disabled:
                        self._check_offline_status(now)
                        continue

                    adapters_snapshot = list(self.adapters.items())

                    try:
                        results = await asyncio.wait_for(
                            loop.run_in_executor(self._executor, self._decode_all_adapters, adapters_snapshot, chunk),
                            timeout=2.0
                        )
                        self._decode_timeout_streak = 0  # 🚀 成功解碼，保險絲降溫歸零

                    except asyncio.TimeoutError:
                        self._decode_timeout_streak += 1
                        logger.error(
                            f"[ListenMaster] 🚨 Adapter 解碼超時 ({self._decode_timeout_streak}/{self._decode_timeout_threshold})，"
                            f"等待保險絲門檻..."
                        )

                        # 🚀 [V1.8.4 防護] 軟體保險絲熔斷
                        # ✅ [Fix V1.14] 熔斷處置全面改寫，詳見檔頭修復歷程。
                        #    移除 unregister_device()／Discovery cleanup／executor 重建，
                        #    改為「保留實體 + 依 uptime 分流」。
                        if self._decode_timeout_streak >= self._decode_timeout_threshold:
                            self._force_all_offline()
                            uptime = time.monotonic() - _PROCESS_START

                            if uptime > self.UPTIME_RESTART_THRESHOLD:
                                # 已穩定運行一段時間才熔斷 → 研判為暫時性卡頓，
                                # 重啟可由 OS 回收卡死的 non-daemon thread。
                                logger.critical(
                                    f"[ListenMaster] 🚨🚨 連續 {self._decode_timeout_streak} 次解碼逾時，保險絲熔斷！\n"
                                    f"  · 進程已運行 {uptime:.0f}s（> {self.UPTIME_RESTART_THRESHOLD:.0f}s），"
                                    f"研判為暫時性卡頓 → 主動退出，交由 Docker restart policy 重啟以回收卡死的 thread。\n"
                                    f"  · HA Discovery 完整保留，設備僅轉為 unavailable，不會被刪除。"
                                )
                                # Docker 官方文件：restart policy 僅於容器成功存活至少 10 秒後才生效。
                                # 早於此退出，容器會直接停死，WebUI 也一併消失 —— 那比重啟迴圈更糟。
                                if uptime < self.DOCKER_ARM_SECONDS:
                                    wait = self.DOCKER_ARM_SECONDS + 0.1 - uptime
                                    logger.critical(
                                        f"[ListenMaster] 等待 {wait:.1f}s 以跨過 Docker restart policy 的 "
                                        f"{self.DOCKER_ARM_SECONDS:.0f} 秒 arming 門檻，確保容器會被重啟…"
                                    )
                                    await asyncio.sleep(wait)
                                # 給 _force_all_offline() 的 QoS1 publish 一個送出窗口。
                                # 即使未送達也不致命：下一進程註冊時會再發一次 offline（Fix V1.14 B1）。
                                await asyncio.sleep(0.3)
                                os._exit(75)
                            else:
                                # 進程剛起就熔斷 → 代表重啟並未解決問題（或本來就是持續性故障）。
                                # 再退出只會形成重啟迴圈，且 WebUI 會反覆消失。改為鎖定解碼。
                                self._decoding_disabled = True
                                logger.critical(
                                    f"[ListenMaster] 🚨🚨 連續 {self._decode_timeout_streak} 次解碼逾時，保險絲熔斷！\n"
                                    f"  · 進程僅運行 {uptime:.0f}s（≤ {self.UPTIME_RESTART_THRESHOLD:.0f}s），"
                                    f"研判為持續性故障 → 已鎖定解碼，不再自動重啟以免形成迴圈。\n"
                                    f"  · HA 實體、WebUI、health 全部保留供診斷；設備將顯示為 unavailable。\n"
                                    f"  · 根因請排查 adapter.feed() 是否有無窮迴圈或永久阻塞的外部 I/O；\n"
                                    f"    修復 adapter 後重啟容器即可恢復（Python 無法中止已卡死的 thread）。"
                                )
                        continue

                    for uid, decoded_data in results:
                        ha_mgr = self.ha_managers.get(uid)
                        if not ha_mgr:
                            continue

                        if len(decoded_data) > self.MAX_KEYS_PER_FRAME:
                            logger.warning(f"[ListenMaster] 設備 #{uid} 單幀 Key 異常 (> {self.MAX_KEYS_PER_FRAME})，丟棄")
                            continue

                        clean_data = {}
                        dropped_long = []
                        for k, v in decoded_data.items():
                            if isinstance(v, str) and len(v) > 256:
                                # ✅ [V1.17] 原為裸 continue（report/060 F4）。這道守衛
                                #    本身是對的（防解析器對著垃圾幀吐出超長字串撐爆
                                #    state cache），錯的只有它不出聲：同一幀其他欄位
                                #    照常發布、設備仍標 online，HA 上就那一個欄位停在
                                #    舊值，使用者幾乎不可能察覺是被網關丟掉的。
                                dropped_long.append((k, len(v)))
                                continue
                            clean_data[k] = v

                        if dropped_long:
                            logger.warning(
                                f"[ListenMaster] 設備 #{uid} 丟棄超長字串欄位 "
                                f"{[(k, f'{n}字元') for k, n in dropped_long]}（上限 256）—— "
                                f"該欄位在 HA 上會停在舊值，其餘欄位照常更新"
                            )

                        if clean_data:
                            self._process_valid_frame(uid, ha_mgr, clean_data, now)
                else:
                    await asyncio.sleep(0.5)

                self._check_offline_status(now)

            except asyncio.CancelledError:
                logger.info("[ListenMaster] 收到系統取消訊號")
                break
            except DriverDisconnectError:
                logger.error("[ListenMaster] 偵測到底層實體斷線，強制切換設備為 OFFLINE，並等待重連...")
                self._force_all_offline()
                await asyncio.sleep(1.0)
            except Exception:
                logger.exception("[ListenMaster] 核心迴圈發生未預期例外，1s 後重試")
                await asyncio.sleep(1)

    def _process_valid_frame(self, uid: int, ha_mgr, decoded_data: dict, now: float):
        state = self.device_states[uid]
        state["last_seen"] = now

        if not state["online"]:
            # ✅ [V1.15] 與 bus_master V4.10 同一原則（report/056 F3）：發布成功才
            #    提交本端狀態。原本先提交再通知，通知失敗後本守衛永不重入，
            #    沒有任何東西會補送 ONLINE。失敗則維持 offline，下一個有效封包重試。
            if ha_mgr.set_availability(True):
                state["online"] = True
                logger.info(f"[ListenMaster] 設備 #{uid} 嗅探到有效封包，恢復 ONLINE")
            else:
                logger.warning(
                    f"[ListenMaster] 設備 #{uid} 已收到有效封包但 availability 發布失敗，"
                    f"維持內部 OFFLINE，下一個有效封包重試"
                )

        cache = self._last_state_cache[uid]

        # ✅ [Fix V1.9] 記錄本次變更的原始狀態，供發布失敗時撤回。
        #    原本是「先寫入快取、再發布」，發布若被 HAManager 的 200ms 節流吞掉，
        #    快取已記帳為「已送出」，同值再來時 has_changed=False，該筆變更
        #    永遠不會補送。實測 JK BMS chunk 平均 165ms 到達（< 200ms 節流窗），
        #    告警位元 OFF→ON 若落在節流窗且其後資料流靜止，HA 將永遠收不到。
        changed = []            # [(key, 原本是否存在, 原值)]
        for key, new_val in decoded_data.items():
            had = key in cache
            old_val = cache.get(key)
            if not had or not _values_equal(old_val, new_val):
                changed.append((key, had, old_val))
                cache[key] = new_val
                cache.move_to_end(key)
            else:
                cache.move_to_end(key)

        while len(cache) > 500:
            popped_key, _ = cache.popitem(last=False)
            logger.debug(f"[ListenMaster] 設備 #{uid} 快取超載，已剔除最舊 Key: {popped_key}")

        if changed:
            if not ha_mgr.publish_state(decoded_data):
                # 未真正送出 → 撤回記帳，下一幀會重新判定為變更並再次嘗試，
                # 直到成功送出為止（成功後同值即回到 has_changed=False，自動停止）
                for key, had, old_val in changed:
                    if had:
                        cache[key] = old_val
                    else:
                        cache.pop(key, None)

    def _check_offline_status(self, now: float):
        for uid, state in list(self.device_states.items()):
            ha_mgr = self.ha_managers.get(uid)
            if not ha_mgr:
                continue

            if state["online"] and (now - state["last_seen"] > self.offline_time):
                # ✅ [V1.15] 同上：發布成功才提交。失敗時維持 online，下一輪
                #    _check_offline_status（條件仍成立）會再試，不需新增計時器。
                if ha_mgr.set_availability(False):
                    state["online"] = False
                    logger.warning(f"[ListenMaster] 設備 #{uid} 超過 {self.offline_time}s 無有效封包，判定 OFFLINE")
                else:
                    logger.warning(
                        f"[ListenMaster] 設備 #{uid} 已達 OFFLINE 條件但 availability "
                        f"發布失敗，維持內部 ONLINE，下一輪重試"
                    )

            # 🚀 修復 MEDIUM-C: 最終一致性對齊，解耦私有屬性 _availability_cache
            elif state["online"] and getattr(ha_mgr, '_availability_cache', True) is False:
                ha_mgr.set_availability(True)
                logger.info(f"[ListenMaster] 設備 #{uid} 補發延遲上線通知 (HA 去抖動補救)")
