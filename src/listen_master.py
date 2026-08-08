# =============================================================================
#version listen_master.py - V1.13 真・工業旁路轉運工 (無頭殭屍防護版)
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
# =============================================================================

import asyncio
import time
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from listen_driver import DriverDisconnectError

logger = logging.getLogger(__name__)

def _values_equal(a, b, tolerance: float = 0.01) -> bool:
    if type(a) == type(b) and isinstance(a, (int, str)):
        return a == b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False

class ListenMasterDispatcher:
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
            state["online"] = False
            state["last_seen"] = now  
            ha_mgr = self.ha_managers.get(uid)
            if ha_mgr:
                ha_mgr.set_availability(False)
            logger.warning(f"[ListenMaster] 🚨 實體連線中斷或觸發熔斷，設備 #{uid} 強制判定 OFFLINE")

    async def _listen_loop(self):
        loop = asyncio.get_running_loop()
        while self.running:
            try:
                chunk = await self.driver.read_stream(chunk_size=1024, timeout=5.0)
                now = time.monotonic()

                if chunk:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[RAW RX] {chunk.hex().upper()}")
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

                        # 🚀 [V1.8.4 防護] 軟體保險絲熔斷：強制拔管所有 Listen 設備，守護 OS 核心資源
                        if self._decode_timeout_streak >= self._decode_timeout_threshold:
                            logger.critical(
                                f"[ListenMaster] 🚨🚨 連續 {self._decode_timeout_streak} 次解碼逾時！保險絲熔斷！\n"
                                f"為防止 OS Thread 記憶體耗盡，強制卸載所有監聽設備，請立即排查 Adapter 程式碼。"
                            )
                            self._force_all_offline()

                            # 🚀 修復 MEDIUM-B: 反向通知 Gateway 清理，避免 HA 殭屍與 Memory Leak
                            for uid in list(self.adapters.keys()):
                                try:
                                    import app_state
                                    if hasattr(app_state, 'gateway') and app_state.gateway:
                                        app_state.gateway.unregister_device(uid)
                                    else:
                                        self.unregister_device(uid)
                                except ImportError:
                                    self.unregister_device(uid)

                            self._executor.shutdown(wait=False, cancel_futures=True)
                            self._executor = ThreadPoolExecutor(max_workers=self._executor_workers, thread_name_prefix="ListenDecoder")
                            self._decode_timeout_streak = 0
                        continue

                    for uid, decoded_data in results:
                        ha_mgr = self.ha_managers.get(uid)
                        if not ha_mgr:
                            continue

                        if len(decoded_data) > self.MAX_KEYS_PER_FRAME:
                            logger.warning(f"[ListenMaster] 設備 #{uid} 單幀 Key 異常 (> {self.MAX_KEYS_PER_FRAME})，丟棄")
                            continue

                        clean_data = {}
                        for k, v in decoded_data.items():
                            if isinstance(v, str) and len(v) > 256:
                                continue
                            clean_data[k] = v

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
            state["online"] = True
            ha_mgr.set_availability(True)
            logger.info(f"[ListenMaster] 設備 #{uid} 嗅探到有效封包，恢復 ONLINE")

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
                state["online"] = False
                ha_mgr.set_availability(False)
                logger.warning(f"[ListenMaster] 設備 #{uid} 超過 {self.offline_time}s 無有效封包，判定 OFFLINE")

            # 🚀 修復 MEDIUM-C: 最終一致性對齊，解耦私有屬性 _availability_cache
            elif state["online"] and getattr(ha_mgr, '_availability_cache', True) is False:
                ha_mgr.set_availability(True)
                logger.info(f"[ListenMaster] 設備 #{uid} 補發延遲上線通知 (HA 去抖動補救)")
