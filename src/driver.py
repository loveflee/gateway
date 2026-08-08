# =============================================================================
#version driver.py - V1.9 工業封存版 (資源釋放與任務生命週期升級) verison
# 相容：BusMaster V3.8+、ModbusTcpDriver V1.0+
# 修復歷程：
# V1.1 : TCP 假死重連、flush 上限、connect timeout、FC 誤殺防護
# V1.3 : 短連線串口伺服器抗性、time.monotonic() NTP 免疫、_io_lock 死鎖修復
# V1.4 : 新增 TX/RX 底層 Hex Dump 觀測點 (需將 config.yaml 設為 DEBUG)
# V1.5 : 修復 Socket 斷線重連時的 Linux FD 洩漏，增加重連持鎖阻塞觀測。
# V1.6 : [Critical] 導入官方推薦的 Task 強引用集合與自清理機制 (add_done_callback)，
#        徹底修復背景回收小精靈因無主 Task 而被 GC 誤殺的深層 Bug。
# V1.7 : [Critical] 修復幀間距失效：_last_comm_time 原本僅在成功路徑更新且位於 _io_lock
#        之外，快速失敗路徑（TCP FIN／連線重置）會跳過賦值，使下一台設備的幀零間隔
#        搶發（實測 0.05s，遠低於 inter_frame_delay 0.35s），違反逆變器 >300ms 的
#        turnaround 要求並引發連鎖逾時。改以 try/finally 在鎖內確保所有離開路徑都計時。
#        慢速失敗（讀取逾時）因逾時本身已耗時，原本即無違規，行為不變。
# V1.8 : [Protocol] 寫入 ACK 必須驗 UID、FC、固定長度、CRC 與 request echo；合法
#        Exception 回傳 False，壞 ACK 不再被誤判為寫入成功。
# V1.9 : [Fix] ACK 驗證守衛加上 request 端 CRC 檢查，只對格式良好的 RTU 寫入請求
#        套用 RTU ACK 契約。V1.8 對原生 Modbus TCP（MBAP，無 CRC）封包會在
#        transaction id 低位元組恰為 5/6/15/16 時（約 1.56%）誤入驗證並必然失敗；
#        修正後 MBAP 自動落回盲收，隧道 RTU（adapter: rtu）保護完全不變。
# =============================================================================
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

class DriverTimeoutError(Exception):
    """硬體層無回應或物理干擾，交由 Bus Master 處置"""
    pass


def _has_valid_modbus_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    crc = 0xFFFF
    for byte in frame[:-2]:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return (crc & 0xFF) == frame[-2] and ((crc >> 8) & 0xFF) == frame[-1]


class RobustAsyncTcpDriver:
    """
    協議盲目 TCP→RS485 驅動層

    短連線抗性設計：
      廉價串口伺服器會主動踢閒置 TCP，timeout 視同死連線
      每次 timeout/斷線都強制重建 Socket，不依賴長連線假設

    死鎖防護：
      _disconnect_locked()：內部版，在已持有 _io_lock 時呼叫
      disconnect()：外部版，自己拿鎖再呼叫內部版
      _reconnect_locked()：在鎖內執行，sleep 在鎖外（由呼叫方負責）
    """

    def __init__(self, host: str, port: int,
                 timeout: float = 1.0,
                 inter_frame_delay: float = 0.18,
                 connect_timeout: float = 5.0,
                 idle_timeout: float = 0.1,
                 max_response_bytes: int = 2048,
                 max_frame_time: float = 1.0,
                 flush_max_time: float = 0.05):
        """
        :param timeout:           等待設備第一筆回應（秒）
        :param inter_frame_delay: RS485 收發切換硬體極限（秒）
        :param connect_timeout:   TCP connect 上限，防 SYN 卡 2 分鐘（秒）
        :param idle_timeout:      碎包 Idle 判定（秒），30ms 夠用
        :param max_response_bytes:單次最大接收量，防 OOM 與雜訊攻擊
        :param max_frame_time:    整個 Frame 最大接收時間（秒），防滴漏攻擊
        :param flush_max_time:    flush 緩衝區最大時間（秒），防高頻雜訊卡死
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.inter_frame_delay = inter_frame_delay
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.max_response_bytes = max_response_bytes
        self.max_frame_time = max_frame_time
        self.flush_max_time = flush_max_time

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_comm_time: float = 0.0
        self._io_lock = asyncio.Lock()
        
        # ✅ [Fix Bug V1.6] 背景任務的強引用集合，防止 GC 誤殺 (Python 官方標準寫法)
        self._bg_tasks = set()

    # =========================================================================
    # 連線管理與資源回收
    # =========================================================================

    async def connect(self) -> bool:
        """建立 TCP 連線，加 connect_timeout 防 SYN 無回應凍結"""
        try:
            logger.info(f"[Driver] 連線 {self.host}:{self.port}")
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout
            )
            logger.info("[Driver] 連線成功")
            return True
        except asyncio.TimeoutError:
            logger.error(f"[Driver] 連線 Timeout ({self.connect_timeout}s)")
            return False
        except asyncio.CancelledError:
            # 關機打斷時，確保半初始化的 writer 被清理，防 FD 洩漏
            self._reader = None
            if self._writer:
                try:
                    self._writer.close()
                except Exception:
                    pass
                self._writer = None
            raise  # 重新拋出讓 asyncio 正常取消
        except Exception:
            logger.exception("[Driver] 連線失敗")
            return False

    async def _safe_wait_closed(self, writer: asyncio.StreamWriter):
        """背景小精靈：非同步等待 Socket 徹底關閉，安全歸還 FD 給 OS"""
        try:
            writer.close()
            # 加 timeout 防止對端死機導致 wait_closed 永久卡住
            await asyncio.wait_for(writer.wait_closed(), timeout=3.0)
        except Exception:
            pass

    def _disconnect_locked(self) -> asyncio.StreamWriter | None:
        """內部斷線（在已持有 _io_lock 時呼叫），剝離並回傳舊的 writer"""
        writer = self._writer
        self._reader = None
        self._writer = None
        return writer

    async def disconnect(self):
        """外部安全斷線（關機時由主程式呼叫）"""
        async with self._io_lock:
            old_writer = self._disconnect_locked()
            
        if old_writer:
            # 關機時允許在鎖外直接等待，徹底乾淨
            await self._safe_wait_closed(old_writer)
            
        # ✅ 關機時順手清理所有殘留的背景清理小精靈
        for t in self._bg_tasks:
            t.cancel()
        self._bg_tasks.clear()
            
        logger.info("[Driver] 已斷線")

    async def _reconnect_locked(self):
        """內部重連（在已持有 _io_lock 時呼叫）"""
        old_writer = self._disconnect_locked()
        
        if old_writer:
            try:
                loop = asyncio.get_running_loop()
                # ✅ [Fix Bug V1.6] 建立 Task 並放入 _bg_tasks 產生強引用，
                # 執行完畢後透過 add_done_callback 自動將自己移除。
                task = loop.create_task(self._safe_wait_closed(old_writer))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            except RuntimeError:
                old_writer.close() # 若 event loop 已關閉的備用方案

        logger.warning(f"[Driver] 重連中，_io_lock 持鎖最長 {self.connect_timeout}s，其他設備暫停")
        success = await self.connect()
        
        if not success:
            raise DriverTimeoutError("TCP 重連失敗，下次排程繼續重試")
        logger.info("[Driver] 重連成功")

    # =========================================================================
    # 底層 I/O
    # =========================================================================

    async def _flush_buffer(self):
        """清除殘留雜訊"""
        if not self._reader:
            return
        flushed = 0
        deadline = time.monotonic() + self.flush_max_time
        try:
            while flushed < self.max_response_bytes:
                if time.monotonic() > deadline:
                    logger.warning(f"[Driver] Flush 超時 ({self.flush_max_time}s)，強制中斷")
                    break
                chunk = await asyncio.wait_for(self._reader.read(1024), timeout=0.05)
                if not chunk:
                    break
                flushed += len(chunk)
                logger.debug(f"[Driver] 丟棄殘留 {len(chunk)}B")
        except asyncio.TimeoutError:
            pass  # 預期行為，buffer 已空
        except Exception as e:
            logger.debug(f"[Driver] Flush 異常（可忽略）: {e}")

    async def _enforce_inter_frame_delay(self):
        """確保 RS485 收發切換有足夠硬體時間"""
        elapsed = time.monotonic() - self._last_comm_time
        if elapsed < self.inter_frame_delay:
            await asyncio.sleep(self.inter_frame_delay - elapsed)

    async def _send_and_recv(self, payload: bytes) -> bytes:
        """底層原子化盲發盲收"""
        async with self._io_lock:
            reader = self._reader
            writer = self._writer

            if not reader or not writer:
                await self._reconnect_locked()
                reader = self._reader
                writer = self._writer

            await self._enforce_inter_frame_delay()
            await self._flush_buffer()

            # ✅ [Fix V1.7] 自此刻起本次交易已佔用 RS485 匯流排，
            #    無論成功、逾時、斷線或被取消，離開時都必須更新時間戳。
            try:
                # 發送
                try:
                    logger.debug(f"[Driver] TX: {payload.hex(' ').upper()}")
                    writer.write(payload)
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    logger.warning(f"[Driver] 發送失敗（TCP 斷線）: {e}")
                    await self._reconnect_locked()
                    raise DriverTimeoutError("發送失敗，已重連，下次重試")

                # 接收
                raw_response = bytearray()
                frame_deadline = time.monotonic() + self.max_frame_time

                try:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=self.timeout)
                    if not chunk:
                        logger.warning("[Driver] 對端關閉連線（TCP FIN）")
                        await self._reconnect_locked()
                        raise DriverTimeoutError("對端關閉連線，已重連")
                    raw_response.extend(chunk)

                    while True:
                        if len(raw_response) >= self.max_response_bytes:
                            logger.warning("[Driver] 接收溢出，遭受雜訊攻擊，強制截斷回傳")
                            break
                        if time.monotonic() > frame_deadline:
                            logger.warning("[Driver] Frame 超時（滴漏/浮接雜訊），強制截斷回傳")
                            break
                        try:
                            chunk = await asyncio.wait_for(
                                reader.read(1024), timeout=self.idle_timeout
                            )
                            if not chunk:
                                break
                            raw_response.extend(chunk)
                        except asyncio.TimeoutError:
                            break  # Idle 到期，Frame 收完

                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    logger.warning(f"[Driver] 接收失敗（TCP 斷線）: {e}")
                    await self._reconnect_locked()
                    raise DriverTimeoutError("接收失敗，已重連，下次重試")

                except asyncio.TimeoutError:
                    logger.warning("[Driver] 設備無回應（Timeout），強制重置 Socket")
                    await self._reconnect_locked()
                    raise DriverTimeoutError("無回應，已清洗 Socket，下次重試")

            finally:
                # ✅ [Fix V1.7] 快速失敗路徑（TCP FIN／連線重置）原本會跳過此賦值，
                #    導致下一台設備的幀零間隔搶發，違反 RS485 turnaround 硬體要求。
                self._last_comm_time = time.monotonic()

        logger.debug(f"[Driver] RX: {bytes(raw_response).hex(' ').upper()}")

        return bytes(raw_response)

    # =========================================================================
    # Bus Master 介面合約
    # =========================================================================

    async def write(self, payload: bytes) -> bool:
        """驗證 Modbus RTU 寫入 ACK；合法 Exception 回傳 False。"""
        resp = await self._send_and_recv(payload)

        # ✅ [Fix V1.9] 僅對「格式良好的 Modbus RTU 寫入請求」套用 RTU ACK 契約。
        #
        #    driver.type: tcp 同時服務兩種語意不同的傳輸，而 driver 無從分辨，
        #    只有 adapter 知道自己送的是哪一種：
        #      · adapter: rtu —— 串口伺服器隧道，完整 RTU 幀（含尾端 CRC16）原樣穿過 TCP。
        #        payload[1] 就是 FC，尾端兩 bytes 是合法 CRC → 通過本守衛 → 照常驗證 ACK。
        #      · adapter: tcp —— 原生 Modbus TCP，MBAP 框架（7 bytes 標頭 + PDU，無 CRC）。
        #        payload[1] 是 transaction id 低位元組而非 FC，且尾端無 CRC。
        #
        #    V1.8 只檢查 len 與 payload[1]，MBAP 封包在 transaction id 低位元組恰為
        #    5/6/15/16 時（_get_next_tx_id 逐次遞增，約 4/256 ≈ 1.56%）會誤入 RTU 驗證，
        #    而 MBAP 回應為 12 bytes 且無 CRC，必然拋 DriverTimeoutError —— 呈間歇性、
        #    難以重現。加上 CRC 檢查後，MBAP 封包（誤判率 1/65536）自動落回盲收語意，
        #    等同 modbus_tcp_driver V1.1 的既有行為，而隧道 RTU 路徑的保護完全不變。
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

    async def read(self, payload: bytes) -> bytes:
        """協議盲目：回傳 Raw Bytes，解碼由 Adapter 負責"""
        return await self._send_and_recv(payload)
