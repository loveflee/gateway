# =============================================================================
#version local_serial_driver.py - V1.10 工業封存版 (抗 Kernel 假死 + 斷線自癒 + GC防護)
# V1.10: [Critical] write() 不再丟棄 response 一律回 True（report/058 F4）。設備以
#        Modbus Exception 明確拒絕時 driver 必須回 False，否則 BusMaster 的
#        「設備邏輯拒絕」分支永遠進不去；若 verify read 又剛好讀到目標值
#        （重複寫入現值最易命中），會被記成「寫入驗證成功」而設備其實沒改，
#        且無任何 ACK 拒絕日誌。判準與 driver.py V1.9／modbus_tcp_driver V1.4
#        同源：只對格式良好的 RTU 寫入請求套用 ACK 契約，其餘維持盲收。
# 模組名稱：USB RS485 本地實體串口驅動
# 修復歷程 (V1.8 → V1.9 完全南向閉環)：
#   - [Fix] 補回強引用集合 _bg_tasks，徹底根絕 asyncio GC 誤殺背景關閉任務的風險。
#   - [Fix] 補回斷線自癒機制，EMI 突波擊殺 FD 後，下一次輪詢可自動嘗試 connect() 站起。
#   - [Retain] 完全保留原有的 inter_frame_delay (RS485 幀間距保護) 與時間戳更新邏輯，
#              確保底層不會因為過快發送導致從站匯流排衝突。
# 修復歷程 (V1.9 → V2.0)：
#   - [Critical] 修復幀間距失效：_last_comm_time 原本僅在讀取成功後賦值，硬逾時與物理
#                錯誤路徑會跳過，使下一台設備的幀零間隔搶發，違反 RS485 turnaround 要求。
#                改以 try/finally 在鎖內確保所有離開路徑都計時。與 driver.py V1.7 同源修正。
# =============================================================================
import asyncio
import serial
import logging
import time

from driver import DriverTimeoutError, _has_valid_modbus_crc

logger = logging.getLogger(__name__)

class LocalSerialDriver:
    """USB RS485 本地實體串口驅動 V1.9"""

    def __init__(self, port: str, baudrate: int,
                 timeout: float = 1.0,
                 inter_frame_delay: float = 0.18,
                 inter_byte_timeout: float = 0.05,
                 **kwargs):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.inter_frame_delay = inter_frame_delay
        self.inter_byte_timeout = inter_byte_timeout
        self.serial_conn = None
        self._last_comm_time = 0.0
        self._io_lock = asyncio.Lock()

        # 🚨 GC 防護：背景擊殺任務的強引用集合
        self._bg_tasks: set[asyncio.Task] = set()

    async def connect(self) -> bool:
        try:
            self.serial_conn = await asyncio.to_thread(
                serial.Serial,
                port=self.port,
                baudrate=self.baudrate,
                bytesize=8, parity='N', stopbits=1,
                timeout=self.timeout,
                inter_byte_timeout=self.inter_byte_timeout
            )
            logger.info(f"[Driver] 本地實體串口已連線: {self.port} @ {self.baudrate}bps")
            return True
        except MemoryError:
            raise
        except Exception as e:
            logger.error(f"[Driver] 串口連線失敗 {self.port}: {e}")
            return False

    async def disconnect(self):
        """持鎖關閉，防關機競態與 OSError"""
        async with self._io_lock:
            if self.serial_conn and self.serial_conn.is_open:
                try:
                    await asyncio.to_thread(self.serial_conn.close)
                    logger.info(f"[Driver] 串口已安全關閉: {self.port}")
                except Exception as e:
                    logger.error(f"[Driver] 關閉串口異常: {e}")
                finally:
                    self.serial_conn = None

        # 🚀 鎖外清理所有殘留的背景擊殺任務
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()

    async def _send_and_recv(self, payload: bytes) -> bytes:
        """底層原子化盲發盲收"""
        async with self._io_lock:
            # 🚀 EMI 擊殺後自癒重連
            if not self.is_connected:
                logger.warning(f"[Driver] 串口 {self.port} 已斷線，嘗試重連...")
                if not await self.connect():
                    raise DriverTimeoutError(f"串口 {self.port} 重連失敗")

            elapsed = time.monotonic() - self._last_comm_time
            if elapsed < self.inter_frame_delay:
                await asyncio.sleep(self.inter_frame_delay - elapsed)

            await self.flush()

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[TX] {len(payload):02d}B: {payload.hex(' ').upper()}")

            # ✅ [Fix V2.0] 自此刻起本次交易已佔用 RS485 匯流排，
            #    無論成功、硬逾時或物理錯誤，離開時都必須更新時間戳。
            try:
                await asyncio.to_thread(self.serial_conn.write, payload)
                await asyncio.to_thread(self.serial_conn.flush)

                try:
                    _hard_timeout = self.timeout + 0.5
                    data = await asyncio.wait_for(
                        asyncio.to_thread(self.serial_conn.read, 256),
                        timeout=_hard_timeout
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[Driver] 🚨 串口讀取硬逾時 ({_hard_timeout}s)，Kernel 假死，拋棄 FD！")
                    if self.serial_conn:
                        bad_conn = self.serial_conn
                        self.serial_conn = None  # 立即斬斷參考
                        try:
                            # 🚀 背景非阻塞擊殺 + 強引用防禦
                            loop = asyncio.get_running_loop()
                            task = loop.create_task(asyncio.to_thread(bad_conn.close))
                            self._bg_tasks.add(task)
                            task.add_done_callback(self._bg_tasks.discard)
                        except Exception as e:
                            logger.debug(f"[Driver] 背景擊殺指派異常: {e}")
                    raise DriverTimeoutError(f"串口讀取硬逾時 (>{_hard_timeout}s)，強制重置實體連線")
                except MemoryError:
                    raise
                except Exception as e:
                    raise DriverTimeoutError("串口物理讀取失敗") from e

            finally:
                # ✅ [Fix V2.0] 快速失敗路徑原本會跳過此賦值，
                #    導致下一台設備的幀零間隔搶發，違反 RS485 turnaround 硬體要求。
                self._last_comm_time = time.monotonic()

            if not data:
                raise DriverTimeoutError(f"設備無回應 (收到 0 bytes, timeout={self.timeout}s)")

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[RX] {len(data):02d}B: {data.hex(' ').upper()}")

            return data

    # =========================================================================
    # 鴨子型別對齊：標準介面合約
    # =========================================================================

    async def read(self, payload: bytes) -> bytes:
        """協議盲目：回傳 Raw Bytes，解碼由 Adapter 負責"""
        if not payload:
            raise ValueError("read() 必須傳入 payload bytes")
        return await self._send_and_recv(payload)

    async def write(self, payload: bytes) -> bool:
        """
        回傳 bool：True 代表設備正常 ACK，False 代表設備以 Modbus Exception 明確拒絕。

        ✅ [Fix V1.10] 原本收到 response 就直接丟棄並一律回 True（report/058 F4）。
           設備回 Exception（例如寫入超出範圍、寫唯讀暫存器）時，driver 不回 False，
           BusMaster 的「設備邏輯拒絕」分支就永遠進不去；若 verify read 又剛好讀到
           目標值（重複寫入目前值最容易命中），會被記成「寫入驗證成功」，
           而設備其實一個 bit 都沒改，全程沒有 ACK 拒絕日誌。
           判準與 driver.py V1.9／modbus_tcp_driver V1.4 同源：只對「格式良好的
           RTU 寫入請求」套用 RTU ACK 契約，其餘一律維持既有盲收語意，
           不影響任何非標準協定的 USB 設備。
        """
        try:
            resp = await self._send_and_recv(payload)

            if (len(payload) < 8
                    or payload[1] not in (5, 6, 15, 16)
                    or not _has_valid_modbus_crc(payload)):
                return True

            sent_uid, sent_fc = payload[0], payload[1]
            if len(resp) == 5 and resp[0] == sent_uid and resp[1] == (sent_fc | 0x80):
                if not _has_valid_modbus_crc(resp):
                    raise DriverTimeoutError("Write ACK Exception CRC mismatch")
                logger.warning(
                    f"[Driver] Modbus Exception: Slave={sent_uid} "
                    f"FC=0x{sent_fc:02X} Code={resp[2]}"
                )
                return False

            if len(resp) != 8:
                raise DriverTimeoutError(f"Write ACK length mismatch: expected 8, received {len(resp)}")
            if resp[0] != sent_uid:
                raise DriverTimeoutError(f"Write ACK UID mismatch: expected {sent_uid}, received {resp[0]}")
            if resp[1] != sent_fc:
                raise DriverTimeoutError(f"Write ACK FC mismatch: expected 0x{sent_fc:02X}, received 0x{resp[1]:02X}")
            if not _has_valid_modbus_crc(resp):
                raise DriverTimeoutError("Write ACK CRC mismatch")
            if resp[2:6] != payload[2:6]:
                raise DriverTimeoutError("Write ACK echo mismatch")
            return True
        except DriverTimeoutError:
            raise
        except MemoryError:
            raise
        except Exception as e:
            logger.error(f"[Driver] 寫入時發生物理錯誤: {e}")
            raise DriverTimeoutError("寫入失敗，視同斷線") from e

    async def flush(self):
        if self.is_connected:
            await asyncio.to_thread(self.serial_conn.reset_input_buffer)
            await asyncio.to_thread(self.serial_conn.reset_output_buffer)

    @property
    def is_connected(self) -> bool:
        return self.serial_conn is not None and self.serial_conn.is_open
