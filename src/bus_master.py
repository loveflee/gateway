# =============================================================================
#version bus_master.py - V4.7 真・工業封存版 (離線寫入防禦 + 總線側錄版)
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
                        ha_mgr.publish_state(decoded)
                        self._record_success(uid)
                        logger.info(f"[{uid}] 寫入驗證成功 {key}={value}")
                        return
                    else:
                        logger.warning(
                            f"[{uid}] 回讀值不符 key={key} "
                            f"寫入={value} 回讀={decoded.get(key)} (嘗試 {attempt}/{max_attempts-1})"
                        )

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

                ha_mgr.publish_state(decoded)
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

        if state["timeout_count"] == 5 and state["online"]:
            state["online"] = False
            ha_mgr.set_availability(False)
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
            state["online"] = True
            ha_mgr.set_availability(True)
            logger.info(f"[{uid}] 連續通訊成功，恢復 ONLINE")
