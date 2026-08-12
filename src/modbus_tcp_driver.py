# =============================================================================
#version modbus_tcp_driver.py - V1.3
# 繼承自 RobustAsyncTcpDriver，供 TCP 與 Gateway 傳輸路徑使用
#
# 改動點：
# 1. 原生 MBAP write ACK 由本類嚴格驗 transaction／protocol／length／UID／FC／echo。
# 2. RTU Driver 不動；其餘 Socket 管理、重連、OOM 防護、死鎖防禦仍完整繼承。
# 修復歷程：
# V1.1 : [Bugfix] 追加 MemoryError 攔截，防止致命異常被偽裝成 Timeout 靜默吞噬。
#        [架構堅持] 拒絕閹割 inter_frame_delay，以完美支援 TCP to RTU Gateway 物理特性。
# V1.2 : [Protocol] 移除盲目 write() 覆寫，統一使用父類的 Modbus RTU ACK 驗證。
# V1.3 : [Security] MBAP 無 CRC，不能再落入父類 RTU guard 的 return True；新增
#          FC05/06/15/16 normal ACK 與 exception 的完整 native TCP contract。
# =============================================================================

import logging
import struct

from driver import DriverTimeoutError, RobustAsyncTcpDriver

logger = logging.getLogger(__name__)

class AsyncModbusTcpDriver(RobustAsyncTcpDriver):
    """
    原生 Modbus TCP 驅動層
    相容純網路 PLC，以及底層為 RS485 的 Modbus TCP Gateway (串口伺服器)。
    """

    # __init__ 參數完全繼承父類，由 main.py 的 kwargs 透傳進來

    @staticmethod
    def _parse_mbap(frame: bytes, label: str) -> tuple[int, int, bytes]:
        """Return transaction id, UID and PDU after strict envelope validation."""
        if not isinstance(frame, (bytes, bytearray)) or len(frame) < 8:
            raise DriverTimeoutError(f"{label} MBAP frame 過短")
        tx_id, protocol_id, length = struct.unpack('>HHH', bytes(frame[:6]))
        if protocol_id != 0:
            raise DriverTimeoutError(f"{label} MBAP Protocol ID mismatch: {protocol_id}")
        if length != len(frame) - 6:
            raise DriverTimeoutError(
                f"{label} MBAP length mismatch: declared {length}, actual {len(frame) - 6}"
            )
        if length < 2:  # UID + at least FC
            raise DriverTimeoutError(f"{label} MBAP payload 過短")
        return tx_id, frame[6], bytes(frame[7:])

    @staticmethod
    def _validate_write_request(payload: bytes) -> tuple[int, int, int, bytes]:
        tx_id, uid, pdu = AsyncModbusTcpDriver._parse_mbap(payload, "Write request")
        fc = pdu[0]
        if fc not in (0x05, 0x06, 0x0F, 0x10):
            raise DriverTimeoutError(f"Write request 不支援 FC 0x{fc:02X}")

        if fc in (0x05, 0x06):
            if len(pdu) != 5:
                raise DriverTimeoutError(f"FC{fc:02X} request PDU length mismatch")
        else:
            if len(pdu) < 6:
                raise DriverTimeoutError(f"FC{fc:02X} request PDU 過短")
            _, quantity, byte_count = struct.unpack('>HHB', pdu[1:6])
            if quantity <= 0 or byte_count != len(pdu) - 6:
                raise DriverTimeoutError(f"FC{fc:02X} request quantity/byte-count mismatch")
            expected_bytes = (quantity + 7) // 8 if fc == 0x0F else quantity * 2
            if byte_count != expected_bytes:
                raise DriverTimeoutError(f"FC{fc:02X} request byte-count 不符合 quantity")

        # All standard write ACKs echo start address plus value/quantity.
        return tx_id, uid, fc, pdu[1:5]

    async def write(self, payload: bytes) -> bool:
        """Validate native Modbus TCP FC05/06/15/16 ACKs; exceptions return False."""
        sent_tx_id, sent_uid, sent_fc, sent_echo = self._validate_write_request(payload)
        response = await self._send_and_recv(payload)
        recv_tx_id, recv_uid, pdu = self._parse_mbap(response, "Write ACK")

        if recv_tx_id != sent_tx_id:
            raise DriverTimeoutError(
                f"Write ACK Transaction ID mismatch: expected {sent_tx_id}, received {recv_tx_id}"
            )
        if recv_uid != sent_uid:
            raise DriverTimeoutError(
                f"Write ACK UID mismatch: expected {sent_uid}, received {recv_uid}"
            )

        recv_fc = pdu[0]
        if recv_fc == (sent_fc | 0x80):
            if len(pdu) != 2:
                raise DriverTimeoutError("Write ACK exception length mismatch")
            logger.warning(
                f"[ModbusTcpDriver] Modbus Exception: Slave={sent_uid} "
                f"FC=0x{sent_fc:02X} Code={pdu[1]}"
            )
            return False

        if recv_fc != sent_fc:
            raise DriverTimeoutError(
                f"Write ACK FC mismatch: expected 0x{sent_fc:02X}, received 0x{recv_fc:02X}"
            )
        if len(pdu) != 5:
            raise DriverTimeoutError(f"Write ACK PDU length mismatch: expected 5, received {len(pdu)}")
        if pdu[1:5] != sent_echo:
            raise DriverTimeoutError("Write ACK echo mismatch")
        return True

    # read() 方法與實體層延遲防護直接繼承父類，保護 Gateway 下的 RS485 總線
