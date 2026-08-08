# =============================================================================
#version modbus_tcp_driver.py - V1.2
# 繼承自 RobustAsyncTcpDriver，供 TCP 與 Gateway 傳輸路徑使用
#
# 改動點：
# 1. 寫入 ACK 直接委派父類驗證，確保與 RTU 路徑的 UID／FC／echo／CRC 契約一致。
# 2. 其餘所有 Socket 管理、重連、OOM 防護、死鎖防禦，100% 繼承。
# 修復歷程：
# V1.1 : [Bugfix] 追加 MemoryError 攔截，防止致命異常被偽裝成 Timeout 靜默吞噬。
#        [架構堅持] 拒絕閹割 inter_frame_delay，以完美支援 TCP to RTU Gateway 物理特性。
# V1.2 : [Protocol] 移除盲目 write() 覆寫，統一使用父類的 Modbus RTU ACK 驗證。
# =============================================================================

from driver import RobustAsyncTcpDriver

class AsyncModbusTcpDriver(RobustAsyncTcpDriver):
    """
    原生 Modbus TCP 驅動層
    相容純網路 PLC，以及底層為 RS485 的 Modbus TCP Gateway (串口伺服器)。
    """

    # __init__ 參數完全繼承父類，由 main.py 的 kwargs 透傳進來

    async def write(self, payload: bytes) -> bool:
        """與 RTU Driver 使用相同的 Modbus RTU 寫入 ACK 驗證。"""
        return await super().write(payload)

    # read() 方法與實體層延遲防護直接繼承父類，保護 Gateway 下的 RS485 總線
