# =============================================================================
#version bus_master.py - V4.11 真・工業封存版 (離線寫入防禦 + 總線側錄版)
# 相容：HAManager V2.9+、RobustAsyncTcpDriver V1.3+、GenericAdapter V2.2+
# 修復歷程 (V4.4 -> V4.5)：
#   - [Feature] 導入「無損旁路側錄 (Read-Only Monitor)」機制。
#   - [Feature] 使用惰性載入 (Lazy Import) app_state，確保模組低耦合，
#               即使脫離當前網關框架單獨使用，亦不會引發 ImportError。
# 修復歷程 (V4.5 -> V4.6)：
#   - [Observability] 補上兩處靜默丟棄寫入的日誌：submit_write 遇未註冊 UID、
#     以及 _get_next_write_task 遇排隊期間被註銷的設備。原本指令有去無回且
#     日誌毫無痕跡（在 HA 按了沒反應卻查無線索）。純新增日誌，不改變控制流程。
# 修復歷程 (V4.6 -> V4.7)：
#   - [Contract] register_device 明確回傳掛載成功與否，供上游阻斷 HA 殭屍實體。
# 修復歷程 (V4.7 -> V4.8)：
#   - [Observability] submit_write 的同 key 覆寫補 DEBUG 記錄。合併語意（最後值
#     才算數）刻意不變，但原本被吞掉的中間指令完全無痕跡 —— HA 拖動 number
#     滑桿必然觸發。只新增日誌，pending_writes 的行為與上限判斷完全不動。
# 修復歷程 (V4.8 -> V4.9)：
#   - [State] _process_write 回讀值不符的分支補 publish_state(decoded)。原本手上
#     握著設備真實現值卻只寫 log 就丟棄，成功路徑發布、不符路徑不發布，導致 HA
#     停在使用者剛設定的值，最長要等一整輪輪詢（≈90s）才被更正。
#     不動 _record_success／_record_failure 的計數語意（通訊層記成功是正確的），
#     不動重試次數與控制流，只補一次狀態發布。
#     註：重試耗盡分支不另外補發布 —— 迴圈內每次不符都已發布，耗盡時 HA 早已是
#     最新值，再發一次是重複。ack is False（設備邏輯拒絕）分支在 verify read
#     送出前就 return，該處沒有 decoded，補發布需額外一次總線交易，不在本次範圍。
# 修復歷程 (V4.9 -> V4.10)：
#   - [Critical] _record_success／_record_failure 改為「availability 發布成功才提交
#     state["online"]」（report/056 F3）。原本先提交再通知，通知失敗後轉換守衛
#     永不重入，HA 與 gateway 兩邊狀態長期分叉且無任何補送。
#   - [Critical] _record_failure 的 `timeout_count == 5` 改為 `>= 5`。原本只在第 5 次
#     嘗試一次；配合上一條後，若那一次發布失敗，第 6 次起再也不會進來，OFFLINE
#     將永遠傳不到 HA。改為 >= 後每次失敗都重試，成功即提交並關閉守衛。
#     副作用：發布持續失敗期間，該設備維持一般輪詢間隔而非退避到 offline_time
#     （因為尚未真的判定 OFFLINE）。這是刻意的 —— 連「它離線了」都還沒送出去，
#     不應該先把探測放慢。
# 修復歷程 (V4.10 -> V4.11)：
#   - [Observability] _process_write 的寫入驗證成功分支改為檢查 publish_state 回傳值
#     （report/056 F5）。原本無論狀態有沒有真的送出去，都印「寫入驗證成功」——
#     「設備已照做」與「HA 已知道」被混為一談。被 200ms 節流吞掉時改印 WARNING
#     並說明值仍在快取、下一輪輪詢會補。不改變節流、重試或寫入的任何行為。
#   - [Observability] _process_poll 累計每台的 state_publish_failures，首次與每 10 次
#     告警（report/056 F2）。刻意**不**改變 _record_success：MQTT 發不出去不代表
#     Modbus 讀不到，把兩者混為一談會讓 broker 斷線時四台設備一起假離線。
#     此欄位一併進 health payload，讓「gateway 讀得到、HA 收不到」的分叉可見。
# =============================================================================

import asyncio
import heapq
import time
import logging

from driver import DriverTimeoutError

logger = logging.getLogger(__name__)

class DataDecodeError(Exception):
    pass

def _values_equal(a, b, tolerance: float = 0.01) -> bool:
    if type(a) == type(b) and isinstance(a, (int, str)):
        return a == b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return False

# 🚨 新增：惰性載入的側錄輔助函數 (不影響主流程，安全靜默)
_traffic_log_ref = None
_traffic_log_resolved = False

def _log_traffic(msg: str):
    global _traffic_log_ref, _traffic_log_resolved
    if not _traffic_log_resolved:
        try:
            import app_state
            if hasattr(app_state, 'traffic_log'):
                _traffic_log_ref = app_state.traffic_log
        except ImportError:
            pass
        _traffic_log_resolved = True

    if _traffic_log_ref is not None:
        _traffic_log_ref.append(msg)

_ADAPTER_REQUIRED = ("encode_write", "build_verify_read", "build_poll_read", "decode")

class BusMasterScheduler:
    MAX_PENDING_WRITES = 200

    def __init__(self, driver, offline_time: int = 60):
        self.driver = driver
        self.offline_time = offline_time

        self.adapters = {}
        self.ha_managers = {}
        self.device_states = {}

        self.pending_writes = {}
        self.write_event = asyncio.Event()
        self.write_lock = asyncio.Lock()

        self.slow_heap = []
        self.bus_lock = asyncio.Lock()

        self.running = False
        self.consecutive_fast = 0
        self._task = None

    # =========================================================================
    # 設備管理
    # =========================================================================

    def register_device(self, uid: int, adapter, ha_manager, poll_interval: int = 10) -> bool:
        if not isinstance(uid, int) or uid <= 0:
            logger.error(f"[BusMaster] 無效 UID: {uid}，必須為正整數")
            return False
        for method in _ADAPTER_REQUIRED:
            if not hasattr(adapter, method):
                logger.error(f"[BusMaster] 設備 #{uid} adapter 缺少 {method}()，註冊失敗")
                return False

        self.adapters[uid] = adapter
        self.ha_managers[uid] = ha_manager
        self.device_states[uid] = {
            "timeout_count": 0,
            "success_count": 0,
            "online": False,
            "interval": poll_interval,
            # ✅ [V4.11] 累計「解碼成功但 MQTT 發布失敗」次數，供 health 觀測。
            "state_publish_failures": 0,
        }
        heapq.heappush(self.slow_heap, (time.monotonic(), uid))
        logger.info(f"[BusMaster] 設備 #{uid} 已註冊，輪詢間隔 {poll_interval}s")
        return True

    def unregister_device(self, uid: int):
        if uid not in self.adapters:
            return

        del self.adapters[uid]
        del self.ha_managers[uid]
        del self.device_states[uid]

        self.pending_writes = {
            k: v for k, v in self.pending_writes.items()
            if k[0] != uid
        }

        logger.info(f"[BusMaster] 設備 #{uid} 已安全註銷")

    # =========================================================================
    # 啟動/停止
    # =========================================================================

    def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._arbitration_loop())
        logger.info("[BusMaster] 調度器已啟動")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[BusMaster] 調度器已安全停止")

    async def submit_write(self, uid: int, key: str, value):
        if uid not in self.adapters:
            # ✅ [Fix] 原本靜默 return，指令有去無回且日誌毫無痕跡。
            #    main.py 已在上游擋一層並說明原因，此處為防禦性補網。
            logger.error(
                f"[BusMaster] 🚨 丟棄寫入：UID={uid} key={key} 未註冊於總線 "
                f"(已註冊：{sorted(self.adapters)})"
            )
            return
        async with self.write_lock:
            if (len(self.pending_writes) >= self.MAX_PENDING_WRITES
                    and (uid, key) not in self.pending_writes):
                logger.error(
                    f"[BusMaster] pending_writes 達上限 {self.MAX_PENDING_WRITES}，"
                    f"丟棄新 key uid={uid} key={key}"
                )
                return
            # ✅ [V4.8] 同 key 覆寫是刻意的合併語意（最後值才算數），但原本完全無
            #    痕跡。HA 拖動 number 滑桿必然送出中間值並在此被吞掉，事後無從得知
            #    「送了幾筆、實際只寫了哪一筆」。僅記錄，不改變合併行為。
            if (uid, key) in self.pending_writes:
                logger.debug(
                    f"[BusMaster] 待處理寫入被覆蓋 uid={uid} key={key} "
                    f"舊值={self.pending_writes[(uid, key)]} 新值={value}"
                )
            self.pending_writes[(uid, key)] = value
        self.write_event.set()

    # =========================================================================
    # 調度核心
    # =========================================================================

    async def _get_next_write_task(self):
        async with self.write_lock:
            while self.pending_writes:
                k = next(iter(self.pending_writes))
                v = self.pending_writes.pop(k)
                if k[0] in self.adapters:
                    if not self.pending_writes:
                        self.write_event.clear()
                    return (k[0], k[1], v)
                # ✅ [Fix] 設備在入列後、派發前被註銷（熔斷／卸載）時，
                #    原本靜默丟棄；補上日誌以免指令無聲消失。
                logger.warning(
                    f"[BusMaster] 丟棄待派發寫入：UID={k[0]} key={k[1]}"
                    f"（設備已於排隊期間被註銷）"
                )
            self.write_event.clear()
            return None

    async def _arbitration_loop(self):
        while self.running:
            try:
                while self.slow_heap and self.slow_heap[0][1] not in self.adapters:
                    heapq.heappop(self.slow_heap)

                now = time.monotonic()
                next_poll_time = self.slow_heap[0][0] if self.slow_heap else now + 60
                sleep_time = max(0.0, next_poll_time - now)

                # 🚀 完美的 Write Budget 節流防護
                if self.consecutive_fast >= 5 and sleep_time <= 0:
                    await self._process_poll()
                    self.consecutive_fast = 0
                    continue

                write_task = await self._get_next_write_task()
                if write_task:
                    await self._process_write(write_task)
                    self.consecutive_fast += 1
                    continue

                try:
                    await asyncio.wait_for(self.write_event.wait(), timeout=sleep_time)
                except asyncio.TimeoutError:
                    await self._process_poll()
                    self.consecutive_fast = 0

            except asyncio.CancelledError:
                logger.info("[BusMaster] 調度迴圈已被安全取消")
                break
            except Exception:
                logger.exception("[BusMaster] 核心迴圈未預期例外，5s 後重試")
                await asyncio.sleep(5)

    # =========================================================================
    # 快車道：原子化寫入
    # =========================================================================

    async def _process_write(self, task):
        uid, key, value = task
        adapter = self.adapters.get(uid)
        ha_mgr = self.ha_managers.get(uid)
        if not adapter or not ha_mgr:
            return

        # 🚀 離線設備快速失敗機制
        state = self.device_states.get(uid)
        is_offline = state and not state.get("online", False)
        max_attempts = 2 if is_offline else 4

        physical_fault_count = 0

        for attempt in range(1, max_attempts):
            raw_data = None
            read_context = None

            try:
                write_payload = adapter.encode_write(key, value)
                read_cmd_bytes, read_context = adapter.build_verify_read(key)
            except Exception:
                logger.exception(f"[{uid}] adapter 編碼失敗，跳過此次寫入")
                return

            try:
                async with self.bus_lock:
                    # 🚨 寫入 TX 側錄
                    _log_traffic(f"[Write-TX] {write_payload.hex(' ').upper()}")

                    ack = await asyncio.wait_for(self.driver.write(write_payload), timeout=5.0)

                    if ack is False:
                        logger.warning(f"[{uid}] 設備邏輯拒絕 key={key}，不重試")
                        self._record_success(uid)
                        return

                    # 🚨 驗證 TX 側錄
                    _log_traffic(f"[Verify-TX] {read_cmd_bytes.hex(' ').upper()}")

                    raw_data = await asyncio.wait_for(self.driver.read(read_cmd_bytes), timeout=5.0)

                    # 🚨 驗證 RX 側錄
                    if raw_data:
                        _log_traffic(f"[Verify-RX] {raw_data.hex(' ').upper()}")

            except (DriverTimeoutError, asyncio.TimeoutError):
                logger.warning(f"[{uid}] 物理 Timeout (嘗試 {attempt}/{max_attempts-1})")
                physical_fault_count += 1
                await asyncio.sleep(0.5)
                continue
            except Exception:
                logger.exception(f"[{uid}] 臨界區未預期例外 (嘗試 {attempt}/{max_attempts-1})")
                physical_fault_count += 1
                await asyncio.sleep(0.5)
                continue

            if raw_data is not None:
                try:
                    decoded = adapter.decode(raw_data, read_context)

                    if not isinstance(decoded, dict) or not decoded:
                        raise DataDecodeError(f"Adapter 解碼無效: 預期非空 dict，收到 {type(decoded)}")
                    # 🚀 [Fix] 若 Adapter 因 datatype 不支援而解出 None，無法進行數值比對，視為寫入成功放行
                    if decoded.get(key) is None:
                        logger.warning(f"[{uid}] 寫入驗證回讀 key={key} 值為 None (可能不支援該資料型別)，無法比對，視為成功")
                        ha_mgr.publish_state(decoded)
                        self._record_success(uid)
                        return

                    if _values_equal(decoded.get(key), value):
                        # ✅ [V4.11] 寫入確認若被 HAManager 的 200ms 節流吞掉，原本
                        #    無聲回 False，而這裡照樣印「寫入驗證成功」——「設備已
                        #    照做」與「HA 已知道」被混為一談（report/056 F5）。
                        #    使用者剛好在輪詢發布後 200ms 內動滑桿即命中：設備確實
                        #    改了，畫面卻要等下一輪輪詢才跟上。
                        #    值仍在 _state_cache，不會遺失；此處只讓延遲可見。
                        published = ha_mgr.publish_state(decoded)
                        self._record_success(uid)
                        if published:
                            logger.info(f"[{uid}] 寫入驗證成功 {key}={value}")
                        else:
                            logger.warning(
                                f"[{uid}] 寫入驗證成功 {key}={value}，但狀態發布被節流"
                                f"／拒絕，HA 需等下一輪輪詢才會更新（值已在快取，不會遺失）"
                            )
                        return
                    else:
                        logger.warning(
                            f"[{uid}] 回讀值不符 key={key} "
                            f"寫入={value} 回讀={decoded.get(key)} (嘗試 {attempt}/{max_attempts-1})"
                        )
                        # ✅ [V4.9] 回讀不符時，decoded 裡就是設備的真實現值，原本
                        #    只寫 log 就丟掉。成功路徑會發布、失敗路徑不發布，於是
                        #    HA 停在使用者剛設定的值，要等輪詢輪到涵蓋該暫存器的
                        #    command 才會被更正（15 個 command × poll_interval 6s
                        #    ≈ 最長 90s）。使用者看到的是「設定完好像成功，一分多鐘
                        #    後無預警跳回去」。此處發布的是回讀到的事實，不改變
                        #    重試次數、_record_success/_record_failure 的計數語意，
                        #    也不改變本迴圈的控制流。
                        ha_mgr.publish_state(decoded)

                except DataDecodeError as e:
                    logger.warning(f"[{uid}] 解析失敗: {e} (嘗試 {attempt}/{max_attempts-1})")
                    physical_fault_count += 1
                except Exception:
                    logger.exception(f"[{uid}] 解碼未預期例外 (嘗試 {attempt}/{max_attempts-1})")
                    physical_fault_count += 1

            await asyncio.sleep(0.5)

        if physical_fault_count > 0:
            logger.error(f"[{uid}] 寫入耗盡，主因物理異常 ({physical_fault_count}/{max_attempts-1})")
            self._record_failure(uid)
        else:
            logger.error(f"[{uid}] 寫入耗盡，回讀值不符（硬體限制），設備維持 ONLINE")
            self._record_success(uid)

    # =========================================================================
    # 慢車道：常規輪詢
    # =========================================================================

    async def _process_poll(self):
        if not self.slow_heap:
            return

        scheduled_time, uid = heapq.heappop(self.slow_heap)

        adapter = self.adapters.get(uid)
        ha_mgr = self.ha_managers.get(uid)
        if not adapter or not ha_mgr:
            return

        raw_data = None
        poll_context = None
        rescheduled = False

        try:
            read_cmd_bytes, poll_context = adapter.build_poll_read()
        except Exception:
            logger.exception(f"[{uid}] adapter build_poll_read 失敗")
            self._reschedule(uid, scheduled_time)
            return

        try:
            async with self.bus_lock:
                # 🚨 輪詢 TX 側錄
                _log_traffic(f"[Poll-TX] {read_cmd_bytes.hex(' ').upper()}")

                raw_data = await asyncio.wait_for(self.driver.read(read_cmd_bytes), timeout=5.0)

                # 🚨 輪詢 RX 側錄
                if raw_data:
                    _log_traffic(f"[Poll-RX] {raw_data.hex(' ').upper()}")

        except (DriverTimeoutError, asyncio.TimeoutError):
            rescheduled = self._record_failure(uid)
        except Exception:
            logger.exception(f"[{uid}] 輪詢臨界區未預期例外")
            rescheduled = self._record_failure(uid)

        if raw_data is not None:
            try:
                decoded = adapter.decode(raw_data, poll_context)

                if not isinstance(decoded, dict) or not decoded:
                    raise DataDecodeError(f"Adapter 解碼無效: 預期非空 dict，收到 {type(decoded)}")

                # ✅ [V4.11] 輪詢資料若沒送到 HA，設備仍是「通訊成功」——這一點
                #    不改：MQTT 發不出去不代表 Modbus 讀不到，把兩者混為一談會讓
                #    broker 斷線時四台設備一起假離線（report/056 F2）。
                #    但原本連「這一輪的資料沒出去」都無從得知：_safe_publish 的
                #    WARNING 不帶 uid，health 也只看得到 success_count 一直加。
                #    此處累計每台的發布失敗數並於首次與每 10 次告警，讓
                #    「gateway 讀得到、HA 收不到」這個分叉在 MQTT 側就看得見。
                if not ha_mgr.publish_state(decoded):
                    state = self.device_states.get(uid)
                    if state is not None:
                        state["state_publish_failures"] = state.get("state_publish_failures", 0) + 1
                        n = state["state_publish_failures"]
                        if n == 1 or n % 10 == 0:
                            logger.warning(
                                f"[{uid}] 輪詢資料未送達 HA（累計 {n} 次）：Modbus 讀取正常，"
                                f"MQTT 發布被節流或拒絕。設備維持 ONLINE，值留在快取待下次補送"
                            )
                self._record_success(uid)

            except DataDecodeError as e:
                logger.warning(f"[{uid}] 輪詢解析失敗: {e}")
                rescheduled = self._record_failure(uid)
            except Exception:
                logger.exception(f"[{uid}] 輪詢解碼未預期例外")
                rescheduled = self._record_failure(uid)

        if not rescheduled:
            self._reschedule(uid, scheduled_time)

    def _reschedule(self, uid: int, base_time: float | None = None):
        state = self.device_states.get(uid)
        if state:
            is_dead = (not state["online"] and state["timeout_count"] >= 5)
            interval = self.offline_time if is_dead else state["interval"]

            next_time = (base_time or time.monotonic()) + interval

            now = time.monotonic()
            if next_time < now:
                next_time = now

            heapq.heappush(self.slow_heap, (next_time, uid))

    # =========================================================================
    # 狀態管理
    # =========================================================================

    def _record_failure(self, uid: int) -> bool:
        state = self.device_states.get(uid)
        ha_mgr = self.ha_managers.get(uid)
        if not state or not ha_mgr:
            return False

        state["timeout_count"] += 1
        state["success_count"] = 0
        logger.error(f"[{uid}] 通訊失敗，累計 {state['timeout_count']} 次")

        # ✅ [V4.10] 與 ONLINE 方向同一原則：發布成功才提交，否則不改變本端狀態。
        #    `== 5` 一併改為 `>= 5`：原本只在第 5 次嘗試一次，若那一次發布失敗，
        #    第 6 次起就再也不會進來，OFFLINE 永遠傳不到 HA。改為 >= 之後，
        #    後續每次失敗都會重試，直到成功；成功後 online 轉 False，守衛自然關閉。
        if state["timeout_count"] >= 5 and state["online"]:
            if not ha_mgr.set_availability(False):
                logger.warning(
                    f"[{uid}] 已達 OFFLINE 條件但 availability 發布失敗，"
                    f"維持內部 ONLINE，下次失敗重試（累計 {state['timeout_count']} 次）"
                )
                return False

            state["online"] = False
            logger.critical(f"[{uid}] 判定 OFFLINE，進入 {self.offline_time}s 慢速探測")

            self.slow_heap = [(t, u) for t, u in self.slow_heap if u != uid]
            heapq.heapify(self.slow_heap)

            heapq.heappush(
                self.slow_heap,
                (time.monotonic() + self.offline_time, uid)
            )
            return True

        return False

    def _record_success(self, uid: int):
        state = self.device_states.get(uid)
        ha_mgr = self.ha_managers.get(uid)
        if not state or not ha_mgr:
            return

        state["timeout_count"] = 0
        state["success_count"] += 1

        if not state["online"] and state["success_count"] >= 2:
            # ✅ [V4.10] 先確認 HA 真的收到，才提交本端狀態。原本先提交 online=True
            #    再通知，一旦通知失敗，本守衛的 `not state["online"]` 從此為假，
            #    這個轉換永不重入 —— 沒有任何東西會補送（report/056 F3）。
            #    改為發布成功才提交；失敗則維持 offline，下次成功輪詢自然重試。
            if ha_mgr.set_availability(True):
                state["online"] = True
                logger.info(f"[{uid}] 連續通訊成功，恢復 ONLINE")
            else:
                logger.warning(
                    f"[{uid}] 已達 ONLINE 條件但 availability 發布失敗，"
                    f"維持內部 OFFLINE，下次成功輪詢重試"
                )
