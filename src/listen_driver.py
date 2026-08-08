# =============================================================================
#version listen_driver.py - V1.9.3 監聽專屬工業驅動 (無損側錄版)
# 核心職責：專注於 Byte Stream 讀取與物理層斷線重連，絕不干涉總線寫入。
# 架構特點：
#   - 雙重介面：同時支援 TCP 與 Serial 物理層。
#   - 終極拔管：導入 transport.abort()，強制回收 OS 級別的 Half-open FD。
#   - 語意精準：修復 read_stream 吞沒實體錯誤的問題，明確拋出 DriverDisconnectError。
#   - 無損側錄：導入惰性載入 _log_traffic，安全銜接前端 UI 監控黑窗。
# =============================================================================
import asyncio
import logging
import random
try:
    import serial_asyncio
except ImportError:
    serial_asyncio = None

logger = logging.getLogger(__name__)

class DriverDisconnectError(Exception):
    pass

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

# =============================================================================
# TCP 實體層監聽驅動
# =============================================================================
class AsyncListenTcpDriver:
    def __init__(self, host: str, port: int, connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._is_connected = False
        self._reconnect_delay = 1.0
        self._reconnect_lock = asyncio.Lock()

    def _task_done(self, task: asyncio.Task):
        self._bg_tasks.discard(task)

    def _track_task(self, task: asyncio.Task):
        self._bg_tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _safe_wait_closed(self, writer: asyncio.StreamWriter):
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=3.0)
        except Exception:
            transport = getattr(writer, "transport", None)
            if transport:
                transport.abort()

    async def connect(self) -> bool:
        reader = None
        writer = None
        try:
            logger.info(f"[ListenDriver] 監聽端連線 {self.host}:{self.port} ...")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout
            )
            self._reader, self._writer = reader, writer
            self._is_connected = True
            self._reconnect_delay = 1.0
            logger.info("[ListenDriver] TCP 監聽端連線成功")
            return True
        except asyncio.TimeoutError:
            logger.error(f"[ListenDriver] 連線 Timeout ({self.connect_timeout}s)")
            if writer:
                await self._safe_wait_closed(writer)
            return False
        except asyncio.CancelledError:
            if writer:
                try: writer.close()
                except Exception: pass
            raise
        except Exception as e:
            logger.error(f"[ListenDriver] TCP 監聽端連線失敗: {e}")
            if writer:
                await self._safe_wait_closed(writer)
            return False

    async def disconnect(self):
        self._is_connected = False
        writer = self._writer
        self._reader = None
        self._writer = None

        tasks = list(self._bg_tasks)
        for t in tasks: t.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()

        if writer:
            await self._safe_wait_closed(writer)
        logger.info("[ListenDriver] TCP 監聽端已安全斷線")

    async def _reconnect(self):
        async with self._reconnect_lock:
            if self._is_connected: return
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer:
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(self._safe_wait_closed(writer))
                    self._track_task(task)
                except RuntimeError:
                    writer.close()

            jitter = random.uniform(0, 0.3)
            logger.warning(f"[ListenDriver] TCP 嗅探中斷，{self._reconnect_delay:.1f}s 後重連...")
            await asyncio.sleep(self._reconnect_delay + jitter)

            success = await self.connect()
            if not success:
                self._reconnect_delay = min(5.0, self._reconnect_delay * 2)
                raise DriverDisconnectError("TCP 重連失敗")

    async def read_stream(self, chunk_size: int = 1024, timeout: float = 5.0) -> bytes:
        if not self._is_connected or not self._reader:
            await self._reconnect()
            return b""
        try:
            chunk = await asyncio.wait_for(self._reader.read(chunk_size), timeout=timeout)

            if not chunk:
                self._is_connected = False
                logger.warning("[ListenDriver] 收到 EOF，TCP 對端關閉連線")
                raise DriverDisconnectError("EOF Received")
            
            # 🚨 無損旁路側錄：安全推播至 UI
            _log_traffic(f"[Listen-RX] {chunk.hex(' ').upper()}")
            
            return chunk
        except asyncio.TimeoutError:
            return b""
        except (ConnectionResetError, BrokenPipeError, OSError, RuntimeError) as e:
            self._is_connected = False
            logger.warning(f"[ListenDriver] 網路實體異常: {e}")
            raise DriverDisconnectError(f"Physical Error: {e}")

# =============================================================================
# USB/Serial 實體層監聽驅動
# =============================================================================
class AsyncListenSerialDriver:
    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._is_connected = False
        self._reconnect_delay = 1.0
        self._reconnect_lock = asyncio.Lock()

        if serial_asyncio is None:
            raise RuntimeError("未安裝 pyserial-asyncio，無法啟用 USB 監聽模式")

    def _task_done(self, task: asyncio.Task):
        self._bg_tasks.discard(task)

    def _track_task(self, task: asyncio.Task):
        self._bg_tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _safe_wait_closed(self, writer: asyncio.StreamWriter):
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=3.0)
        except Exception:
            transport = getattr(writer, "transport", None)
            if transport:
                transport.abort()

    async def connect(self) -> bool:
        if serial_asyncio is None: return False
        try:
            logger.info(f"[ListenDriver] 嘗試開啟本機串口 {self.port} (Baud: {self.baudrate})...")
            reader, writer = await serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baudrate)
            self._reader, self._writer = reader, writer
            self._is_connected = True
            self._reconnect_delay = 1.0
            logger.info(f"[ListenDriver] 串口 {self.port} 開啟成功，開始物理層嗅探")
            return True
        except Exception as e:
            logger.error(f"[ListenDriver] 串口 {self.port} 開啟失敗: {e}")
            return False

    async def disconnect(self):
        self._is_connected = False
        writer = self._writer
        self._reader = None
        self._writer = None

        tasks = list(self._bg_tasks)
        for t in tasks: t.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()

        if writer:
            await self._safe_wait_closed(writer)
        logger.info(f"[ListenDriver] 串口 {self.port} 已安全關閉")

    async def _reconnect(self):
        async with self._reconnect_lock:
            if self._is_connected: return
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer:
                try:
                    loop = asyncio.get_running_loop()
                    # 🚨 修復原本斷行的語法錯誤
                    task = loop.create_task(self._safe_wait_closed(writer))
                    self._track_task(task)
                except RuntimeError:
                    writer.close()

            jitter = random.uniform(0, 0.3)
            logger.warning(f"[ListenDriver] 串口異常，{self._reconnect_delay:.1f}s 後嘗試重連...")
            await asyncio.sleep(self._reconnect_delay + jitter)

            success = await self.connect()
            if not success:
                self._reconnect_delay = min(10.0, self._reconnect_delay * 2)
                raise DriverDisconnectError("USB 重連失敗")

    async def read_stream(self, chunk_size: int = 1024, timeout: float = 5.0) -> bytes:
        if not self._is_connected or not self._reader:
            await self._reconnect()
            return b""
        try:
            chunk = await asyncio.wait_for(self._reader.read(chunk_size), timeout=timeout)

            if not chunk:
                self._is_connected = False
                logger.warning("[ListenDriver] 收到 EOF，USB 可能被拔除")
                raise DriverDisconnectError("USB EOF Received")
            
            # 🚨 無損旁路側錄：安全推播至 UI
            _log_traffic(f"[Listen-RX] {chunk.hex(' ').upper()}")
            
            return chunk
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            self._is_connected = False
            logger.warning(f"[ListenDriver] 串口實體異常: {e}")
            raise DriverDisconnectError(f"Physical Error: {e}")
