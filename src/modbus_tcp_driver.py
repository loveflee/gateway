# =============================================================================
#version modbus_tcp_driver.py - V1.1
# 繼承自 RobustAsyncTcpDriver，專為原生 Modbus TCP 與 Gateway 設備設計
#
# 改動點：
# 1. 拿掉 RTU 專屬的 Modbus Exception 偵測，交由 Adapter 處理 MBAP。
# 2. 其餘所有 Socket 管理、重連、OOM 防護、死鎖防禦，100% 繼承。
# 修復歷程：
# V1.1 : [Bugfix] 追加 MemoryError 攔截，防止致命異常被偽裝成 Timeout 靜默吞噬。
#        [架構堅持] 拒絕閹割 inter_frame_delay，以完美支援 TCP to RTU Gateway 物理特性。
# =============================================================================

import logging
from driver import RobustAsyncTcpDriver, DriverTimeoutError

logger = logging.getLogger(__name__)

class AsyncModbusTcpDriver(RobustAsyncTcpDriver):
    """
    原生 Modbus TCP 驅動層
    相容純網路 PLC，以及底層為 RS485 的 Modbus TCP Gateway (串口伺服器)。
    """

    # __init__ 參數完全繼承父類，由 main.py 的 kwargs 透傳進來

    async def write(self, payload: bytes) -> bool:
        """
        Modbus TCP 寫入
        原生 TCP 的 Exception 判斷會看 MBAP Header，長度不是 5，
        所以直接盲發盲收，把解碼與 Exception 判定權力還給 Adapter。
        """
        try:
            # 盲發盲收
            await self._send_and_recv(payload)
            return True
        except DriverTimeoutError:
            # 斷線或 Timeout 往上拋，交給 BusMaster
            raise
        except MemoryError:
            # ✅ [Fix V1.1] 致命系統異常絕不偽裝，直接拋出讓程式崩潰重啟
            raise
        except Exception as e:
            logger.warning(f"[TCP Driver] 寫入未預期異常: {e}")
            raise DriverTimeoutError("寫入失敗，視同斷線")

    # read() 方法與實體層延遲防護直接繼承父類，保護 Gateway 下的 RS485 總線
