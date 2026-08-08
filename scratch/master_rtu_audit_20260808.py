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
from bus_master import BusMasterScheduler
from driver import DriverTimeoutError, RobustAsyncTcpDriver
from modbus_tcp_driver import AsyncModbusTcpDriver


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


def adapter_with_read_command(uid: int, fc: int, count: int) -> Adapter:
    """以真實 build_poll_read() context 驗證 request quantity 約束。"""
    if fc in (1, 2):
        sensors = [{
            "key": "coil",
            "command_id": "read",
            "offset": 3,
            "length": 1,
            "bits": [{"bit": 0, "id": "coil_0"}],
        }]
    else:
        sensors = [{
            "key": "register",
            "command_id": "read",
            "offset": 3,
            "length": 2,
            "datatype": "uint16",
        }]
    return Adapter(uid, {
        "read_commands": [{"id": "read", "fc": fc, "start_addr": 0, "count": count}],
        "sensors": sensors,
    })


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

    def test_request_count_rejects_valid_wrong_byte_count_without_profile_response_len(self):
        """驗收：不必手填 response_len，仍須綁定本次 TX count。"""
        adapter = adapter_with_read_command(self.UID, 0x03, 1)
        _, context = adapter.build_poll_read()
        wrong_count = rtu_frame(self.UID, 0x03, bytes.fromhex("04 1234 5678"))
        with self.assertRaisesRegex(DataDecodeError, "Response quantity mismatch"):
            adapter.decode(wrong_count, context)

    def test_fc03_and_fc04_exceptions_are_precisely_recognized(self):
        for fc in (0x03, 0x04):
            with self.subTest(fc=f"0x{fc:02X}"):
                exception = rtu_frame(self.UID, fc | 0x80, bytes((2,)))
                with self.assertRaisesRegex(
                    DataDecodeError,
                    rf"Modbus Exception: UID={self.UID} FC={fc:02X} Code=02",
                ):
                    adapter_for(self.UID, fc).decode(
                        exception, poll_context(fc, None)
                    )

    def test_crc_bad_exception_is_not_accepted(self):
        exception = bytearray(rtu_frame(self.UID, 0x83, bytes((2,))))
        exception[-1] ^= 0xFF
        with self.assertRaises(DataDecodeError):
            adapter_for(self.UID, 0x03).decode(
                bytes(exception), poll_context(0x03, None)
            )

    def test_fc03_count_12_accepts_exact_24_data_bytes(self):
        adapter = adapter_with_read_command(self.UID, 0x03, 12)
        _, context = adapter.build_poll_read()
        data = bytes((24,)) + bytes(range(24))
        result = adapter.decode(rtu_frame(self.UID, 0x03, data), context)
        self.assertEqual(result, {"register": 1})

    def test_fc01_and_fc02_count_9_require_two_data_bytes(self):
        for fc in (0x01, 0x02):
            with self.subTest(fc=f"0x{fc:02X}"):
                adapter = adapter_with_read_command(self.UID, fc, 9)
                _, context = adapter.build_poll_read()
                result = adapter.decode(
                    rtu_frame(self.UID, fc, bytes((2, 0x01, 0x00))), context
                )
                self.assertEqual(result, {"coil_0": "ON"})


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
    async def _request(self, response: bytes, gap: float, is_write: bool = False,
                       payload: bytes | None = None):
        server, completed = await _run_single_response_server(response, gap)
        port = server.sockets[0].getsockname()[1]
        driver = RobustAsyncTcpDriver(
            "127.0.0.1", port, timeout=0.5, inter_frame_delay=0,
            idle_timeout=0.1, flush_max_time=0.01,
        )
        try:
            self.assertTrue(await driver.connect())
            payload = payload or rtu_frame(7, 0x03, bytes.fromhex("0000 0001"))
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

    async def test_normal_write_ack_fc_05_06_15_16_passes(self):
        cases = {
            0x05: (bytes.fromhex("0010 FF00"), bytes.fromhex("0010 FF00")),
            0x06: (bytes.fromhex("0010 1234"), bytes.fromhex("0010 1234")),
            # FC15/16 request 有 byte count 與 data；ACK 僅 echo address + quantity。
            0x0F: (bytes.fromhex("0010 0008 01 FF"), bytes.fromhex("0010 0008")),
            0x10: (bytes.fromhex("0010 0001 02 1234"), bytes.fromhex("0010 0001")),
        }
        for fc, (request_body, ack_body) in cases.items():
            with self.subTest(fc=f"0x{fc:02X}"):
                request = rtu_frame(7, fc, request_body)
                ack = rtu_frame(7, fc, ack_body)
                self.assertTrue(await self._request(ack, 0.0, True, request))

    async def test_fc06_and_fc16_write_exceptions_return_false(self):
        for fc in (0x06, 0x10):
            with self.subTest(fc=f"0x{fc:02X}"):
                request_body = (bytes.fromhex("0010 0001") if fc == 0x06
                                else bytes.fromhex("0010 0001 02 1234"))
                request = rtu_frame(7, fc, request_body)
                exception = rtu_frame(7, fc | 0x80, bytes((2,)))
                self.assertFalse(await self._request(exception, 0.0, True, request))

    async def test_write_ack_uid_fc_crc_and_echo_mismatch_fail(self):
        request = rtu_frame(7, 0x06, bytes.fromhex("0010 1234"))
        invalid_acks = {
            "uid": rtu_frame(8, 0x06, bytes.fromhex("0010 1234")),
            "fc": rtu_frame(7, 0x05, bytes.fromhex("0010 1234")),
            "crc": bytes.fromhex("07 06 0010 1234 0000"),
            "echo_address": rtu_frame(7, 0x06, bytes.fromhex("0011 1234")),
            "echo_value": rtu_frame(7, 0x06, bytes.fromhex("0010 1235")),
        }
        for name, ack in invalid_acks.items():
            with self.subTest(kind=name):
                with self.assertRaises(DriverTimeoutError):
                    await self._request(ack, 0.0, True, request)

    async def test_fc16_write_ack_quantity_mismatch_fails(self):
        request = rtu_frame(7, 0x10, bytes.fromhex("0010 0001 02 1234"))
        wrong_quantity_ack = rtu_frame(7, 0x10, bytes.fromhex("0010 0002"))
        with self.assertRaises(DriverTimeoutError):
            await self._request(wrong_quantity_ack, 0.0, True, request)


class TestModbusTcpDriverWriteParity(unittest.IsolatedAsyncioTestCase):
    async def _write(self, response: bytes, request: bytes):
        driver = AsyncModbusTcpDriver("127.0.0.1", 1)

        async def fake_send_and_recv(payload):
            self.assertEqual(payload, request)
            return response

        driver._send_and_recv = fake_send_and_recv
        return await driver.write(request)

    async def test_tcp_driver_uses_same_rtu_ack_contract(self):
        request = rtu_frame(7, 0x10, bytes.fromhex("0010 0001 02 1234"))
        valid_ack = rtu_frame(7, 0x10, bytes.fromhex("0010 0001"))
        self.assertTrue(await self._write(valid_ack, request))

        exception = rtu_frame(7, 0x90, bytes((2,)))
        self.assertFalse(await self._write(exception, request))

        for invalid_ack in (
            rtu_frame(8, 0x10, bytes.fromhex("0010 0001")),
            rtu_frame(7, 0x10, bytes.fromhex("0011 0001")),
            bytes.fromhex("07 10 0010 0001 0000"),
        ):
            with self.subTest(ack=invalid_ack.hex()):
                with self.assertRaises(DriverTimeoutError):
                    await self._write(invalid_ack, request)


class _FakeHA:
    def __init__(self):
        self.states = []
        self.availability = []

    def publish_state(self, payload):
        self.states.append(payload)
        return True

    def set_availability(self, value):
        self.availability.append(value)


class _FakeDriver:
    def __init__(self, verify_response: bytes):
        self.verify_response = verify_response
        self.write_payloads = []
        self.read_payloads = []

    async def write(self, payload):
        self.write_payloads.append(payload)
        return True

    async def read(self, payload):
        self.read_payloads.append(payload)
        return self.verify_response


class TestBusMasterNormalWriteRegression(unittest.IsolatedAsyncioTestCase):
    async def test_normal_write_then_verify_read_is_unchanged(self):
        uid = 7
        adapter = Adapter(uid, {
            "settings": {0x0010: {"key": "setpoint", "write_fc": 6}},
            "sensors": [],
        })
        driver = _FakeDriver(rtu_frame(uid, 0x03, bytes.fromhex("02 0005")))
        ha = _FakeHA()
        master = BusMasterScheduler(driver)
        self.assertTrue(master.register_device(uid, adapter, ha))
        master.device_states[uid]["online"] = True

        await master._process_write((uid, "setpoint", 5))

        self.assertEqual(len(driver.write_payloads), 1)
        self.assertEqual(len(driver.read_payloads), 1)
        self.assertEqual(ha.states, [{"setpoint": 5}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
