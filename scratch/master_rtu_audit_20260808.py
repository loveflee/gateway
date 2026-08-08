"""離線 Master Modbus RTU 解包審查。

此檔只匯入 production 模組，不連現場設備，也不修改 production 狀態。
執行：python3 scratch/master_rtu_audit_20260808.py
"""

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from adapters.generic_adapter import Adapter, DataDecodeError, calc_crc16
from driver import RobustAsyncTcpDriver


def rtu_frame(uid: int, fc: int, data: bytes) -> bytes:
    payload = bytes((uid, fc)) + data
    return payload + calc_crc16(payload)


def poll_context(fc: int, expected_len: int | None, command_id: str = "audit") -> dict:
    return {
        "type": "poll",
        "cmd": {"id": command_id, "fc": fc, "expected_len": expected_len},
    }


def adapter_for(uid: int, fc: int) -> Adapter:
    if fc in (1, 2):
        sensors = [{
            "key": "coil",
            "command_id": "audit",
            "offset": 3,
            "length": 1,
            "bits": [{"bit": 0, "id": "coil_0"}],
        }]
    elif fc in (3, 4):
        sensors = [{
            "key": "register",
            "command_id": "audit",
            "offset": 3,
            "length": 2,
            "datatype": "uint16",
        }]
    else:
        # 僅驗證 generic decoder 對固定 8-byte write echo 的 framing；
        # 真正的 production write ACK 並不走 Adapter.decode()。
        sensors = [{
            "key": "echo_address",
            "command_id": "audit",
            "offset": 2,
            "length": 2,
            "datatype": "uint16",
        }]
    return Adapter(uid, {"sensors": sensors})


class TestGenericAdapterRadar(unittest.TestCase):
    UID = 7

    def test_standard_response_fc_01_02_03_04_05_06_15_16(self):
        cases = {
            0x01: bytes((1, 0x01)),
            0x02: bytes((1, 0x01)),
            0x03: bytes((2, 0x12, 0x34)),
            0x04: bytes((2, 0x12, 0x34)),
            0x05: bytes.fromhex("0010 FF00"),
            0x06: bytes.fromhex("0010 1234"),
            0x0F: bytes.fromhex("0010 0008"),
            0x10: bytes.fromhex("0010 0001"),
        }
        for fc, data in cases.items():
            with self.subTest(fc=f"0x{fc:02X}"):
                frame = rtu_frame(self.UID, fc, data)
                result = adapter_for(self.UID, fc).decode(
                    frame, poll_context(fc, len(frame))
                )
                self.assertTrue(result)

    def test_prefix_and_suffix_garbage_are_ignored(self):
        frame = rtu_frame(self.UID, 0x03, bytes.fromhex("02 1234"))
        raw = b"\x99\x55\x00" + frame + b"\xDE\xAD"
        self.assertEqual(
            adapter_for(self.UID, 0x03).decode(raw, poll_context(0x03, len(frame))),
            {"register": 0x1234},
        )

    def test_bad_crc_candidate_then_good_candidate_resynchronizes(self):
        good = rtu_frame(self.UID, 0x03, bytes.fromhex("02 1234"))
        bad = bytearray(rtu_frame(self.UID, 0x03, bytes.fromhex("02 FFFF")))
        bad[-1] ^= 0xFF
        self.assertEqual(
            adapter_for(self.UID, 0x03).decode(bytes(bad) + good, poll_context(0x03, len(good))),
            {"register": 0x1234},
        )

    def test_wrong_uid_and_wrong_fc_are_ignored_before_good_frame(self):
        good = rtu_frame(self.UID, 0x03, bytes.fromhex("02 1234"))
        wrong_uid = rtu_frame(self.UID + 1, 0x03, bytes.fromhex("02 AAAA"))
        wrong_fc = rtu_frame(self.UID, 0x04, bytes.fromhex("02 BBBB"))
        self.assertEqual(
            adapter_for(self.UID, 0x03).decode(
                wrong_uid + wrong_fc + good, poll_context(0x03, len(good))
            ),
            {"register": 0x1234},
        )

    def test_partial_frame_and_orphaned_tail_fail(self):
        frame = rtu_frame(self.UID, 0x03, bytes.fromhex("02 1234"))
        adapter = adapter_for(self.UID, 0x03)
        for raw in (frame[:4], frame[3:]):
            with self.subTest(raw=raw.hex()):
                with self.assertRaises(DataDecodeError):
                    adapter.decode(raw, poll_context(0x03, len(frame)))

    def test_two_concatenated_frames_select_matching_uid_and_fc(self):
        other = rtu_frame(self.UID + 1, 0x03, bytes.fromhex("02 AAAA"))
        good = rtu_frame(self.UID, 0x03, bytes.fromhex("02 1234"))
        self.assertEqual(
            adapter_for(self.UID, 0x03).decode(other + good, poll_context(0x03, len(good))),
            {"register": 0x1234},
        )

    def test_byte_count_truncated_and_profile_length_mismatch_fail(self):
        # 宣告四個 data bytes，實際只給兩個：雷達依 ByteCount 等待 9 bytes，必須拒絕。
        truncated = bytes((self.UID, 0x03, 4, 0x12, 0x34))
        truncated += calc_crc16(truncated)
        with self.assertRaises(DataDecodeError):
            adapter_for(self.UID, 0x03).decode(truncated, poll_context(0x03, 7))

        # CRC 正確但回覆四個 data bytes；設定有 expected_len 時會拒絕。
        wrong_count = rtu_frame(self.UID, 0x03, bytes.fromhex("04 1234 5678"))
        with self.assertRaises(DataDecodeError):
            adapter_for(self.UID, 0x03).decode(wrong_count, poll_context(0x03, 7))

    def test_valid_wrong_byte_count_is_accepted_without_profile_response_len(self):
        """固定現況：沒有 response_len 時，未比對 request count。"""
        wrong_count = rtu_frame(self.UID, 0x03, bytes.fromhex("04 1234 5678"))
        self.assertEqual(
            adapter_for(self.UID, 0x03).decode(wrong_count, poll_context(0x03, None)),
            {"register": 0x1234},
        )

    def test_fc03_and_fc04_exceptions_are_not_currently_recognized(self):
        for fc in (0x03, 0x04):
            with self.subTest(fc=f"0x{fc:02X}"):
                exception = rtu_frame(self.UID, fc | 0x80, bytes((2,)))
                with self.assertRaisesRegex(DataDecodeError, "找不到合法 CRC"):
                    adapter_for(self.UID, fc).decode(
                        exception, poll_context(fc, None)
                    )


async def _run_single_response_server(response: bytes, gap: float = 0.0):
    received = asyncio.get_running_loop().create_future()

    async def handler(reader, writer):
        try:
            await reader.read(1024)
            split = min(3, len(response))
            writer.write(response[:split])
            await writer.drain()
            if gap:
                await asyncio.sleep(gap)
            writer.write(response[split:])
            await writer.drain()
            await asyncio.sleep(0.2)
        finally:
            writer.close()
            await writer.wait_closed()
            if not received.done():
                received.set_result(None)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, received


class TestDriverIdleBoundary(unittest.IsolatedAsyncioTestCase):
    async def _request(self, response: bytes, gap: float, is_write: bool = False):
        server, completed = await _run_single_response_server(response, gap)
        port = server.sockets[0].getsockname()[1]
        driver = RobustAsyncTcpDriver(
            "127.0.0.1", port, timeout=0.5, inter_frame_delay=0,
            idle_timeout=0.1, flush_max_time=0.01,
        )
        try:
            self.assertTrue(await driver.connect())
            payload = rtu_frame(7, 0x03, bytes.fromhex("0000 0001"))
            result = await (driver.write(payload) if is_write else driver.read(payload))
            return result
        finally:
            await driver.disconnect()
            server.close()
            await server.wait_closed()
            await completed

    async def test_tcp_fragment_under_idle_timeout_is_preserved(self):
        frame = rtu_frame(7, 0x03, bytes.fromhex("02 1234"))
        self.assertEqual(await self._request(frame, 0.03), frame)

    async def test_tcp_fragment_over_idle_timeout_is_truncated(self):
        frame = rtu_frame(7, 0x03, bytes.fromhex("02 1234"))
        self.assertEqual(await self._request(frame, 0.13), frame[:3])

    async def test_driver_write_accepts_invalid_non_exception_ack(self):
        # 固定現況：write() 僅辨識 exception，沒有驗 UID/FC/echo/CRC。
        invalid_ack = bytes.fromhex("07 06 0010 1234 0000")
        self.assertTrue(await self._request(invalid_ack, 0.0, is_write=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
