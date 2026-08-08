# =============================================================================
# modbus_tcp_adapter.py - V1.2
# 繼承自 GenericModbusAdapter，專為原生 Modbus TCP 設備設計
#
# 修復歷程：
# 1. 修正 expected_len 為 +4 (MBAP 6 - CRC 2 = 4)。
# 2. 補齊 Transaction ID 的 Context 傳遞與 _verify_modbus_frame 驗證。
# 3. decode() 改為靜態呼叫 GenericModbusAdapter._extract_data，避開 MRO 陷阱。
# =============================================================================
import struct
import logging
# [關鍵修正 1]：使用相對路徑 . 從同目錄的 generic_adapter 匯入，並將 Adapter 重新命名為 GenericModbusAdapter，讓底下代碼不用大改
from .generic_adapter import Adapter as GenericModbusAdapter, DataDecodeError, calc_crc16
logger = logging.getLogger(__name__)
ADAPTER_NAME = "tcp"  # 宣告註冊名稱 modbus_tcp > tcp
# [關鍵修正 2]：這個類別名稱必須叫做 Adapter，並繼承 GenericModbusAdapter
class Adapter(GenericModbusAdapter):
    def __init__(self, uid: int, profile: dict):
        super().__init__(uid, profile)
        self._tx_id = 0

    def _get_next_tx_id(self) -> int:
        self._tx_id = (self._tx_id + 1) & 0xFFFF
        return self._tx_id

    def _prebuild_poll_cmds(self) -> list:
        cmds = []
        for cmd in self.profile.get("read_commands", []):
            pdu = struct.pack('>BHH', cmd['fc'], cmd['start_addr'], cmd['count'])
            cmds.append({
                "id": cmd['id'],
                "fc": cmd['fc'],
                "pdu": pdu,
                "expected_len": cmd.get('response_len') + 4 if cmd.get('response_len') else None,
            })
        return cmds
######## 以下不動 
    def _add_mbap_header(self, pdu: bytes, tx_id: int) -> bytes:
        length = 1 + len(pdu)
        mbap = struct.pack('>HHH', tx_id, 0x0000, length)
        return mbap + bytes([self.uid]) + pdu

    def build_poll_read(self) -> tuple[bytes, dict]:
        if not self._poll_cmds:
            raise RuntimeError("設備未定義 read_commands")
        cmd = self._poll_cmds[self._poll_index]
        self._poll_index = (self._poll_index + 1) % len(self._poll_cmds)

        tx_id = self._get_next_tx_id()
        req_bytes = self._add_mbap_header(cmd['pdu'], tx_id)

        # [修復 2] 帶入 tx_id 供驗證
        return req_bytes, {"type": "poll", "cmd": cmd, "tx_id": tx_id}

    def build_verify_read(self, key: str) -> tuple[bytes, dict]:
        setting = None
        target_addr = None
        for addr_str, cfg in self.settings.items():
            if cfg['key'] == key:
                setting = cfg
                target_addr = int(addr_str, 16) if isinstance(addr_str, str) else addr_str
                break

        if not setting:
            raise ValueError(f"找不到寫入 Key: {key}")

        write_fc = setting.get("write_fc", 6)
        read_fc = 0x01 if write_fc in (5, 15) else 0x03
        reg_count = setting.get("verify_count", 1)

        pdu = struct.pack('>BHH', read_fc, target_addr, reg_count)
        tx_id = self._get_next_tx_id()
        req_bytes = self._add_mbap_header(pdu, tx_id)

        # 💡 [修復 2] 帶入 tx_id 供驗證
        return req_bytes, {
            "type": "verify",
            "key": key,
            "read_fc": read_fc,
            "tx_id": tx_id
        }
#########
    def encode_write(self, key: str, value) -> bytes:
        setting = None
        target_addr = None
        for addr_str, cfg in self.settings.items():
            if cfg['key'] == key:
                setting = cfg
                target_addr = int(addr_str, 16) if isinstance(addr_str, str) else addr_str
                break

        if not setting:
            raise ValueError(f"未知的寫入 Key '{key}'")

        fc = setting.get("write_fc", 6)
        scale = setting.get("scale", 1.0)

        link_sensor = setting.get("link_sensor")
        for s in self.sensors:
            if s['key'] == link_sensor and "map_profile" in s:
                map_dict = self.definitions.get("value_maps", {}).get(s["map_profile"], {})
                for k, v in map_dict.items():
                    if str(v) == str(value):
                        value = k
                        break

        # 💡 [新增防呆機制] 攔截 HA 傳來的字串，強制轉為數值 1/0
        if isinstance(value, str):
            v_upper = value.upper()
            if v_upper in ("ON", "TRUE"):
                value = 1
            elif v_upper in ("OFF", "FALSE"):
                value = 0

        # 這裡的 float() 現在遇到 1 或 0 就能安全過關了
        try:
            int_val = int(round(float(value) * scale))
        except (TypeError, ValueError):
            raise ValueError(f"寫入值無效: {value}")

        tx_id = self._get_next_tx_id()

        if fc == 6:
            pdu = struct.pack('>BHH', 0x06, target_addr, int_val & 0xFFFF)
            return self._add_mbap_header(pdu, tx_id)
        elif fc == 5:
            # FC05 專屬邏輯：只要數值大於 0 就是 0xFF00 (開啟)，否則是 0x0000 (關閉)
            coil_val = 0xFF00 if int_val else 0x0000
            pdu = struct.pack('>BHH', 0x05, target_addr, coil_val)
            return self._add_mbap_header(pdu, tx_id)
        else:
            raise NotImplementedError(f"TCP 尚不支援 FC {fc} 組裝")
#########
    def _verify_modbus_frame(self, raw_data: bytes, expected_fc: int = None, expected_len: int = None, expected_tx_id: int = None):
        if not raw_data or len(raw_data) < 9:
            raise DataDecodeError(f"TCP 封包過短 ({len(raw_data) if raw_data else 0} bytes)")

        tx_id, proto_id, length = struct.unpack('>HHH', raw_data[0:6])
        uid = raw_data[6]

        if proto_id != 0x0000:
            raise DataDecodeError(f"非 Modbus TCP 協議 (Protocol ID: 0x{proto_id:04X})")

        # [修復 2] 驗證 Transaction ID 防錯位
        if expected_tx_id is not None and tx_id != expected_tx_id:
            raise DataDecodeError(f"Transaction ID 不符（收到 {tx_id}，預期 {expected_tx_id}），封包錯位")

        actual_remaining = len(raw_data) - 6
        if length != actual_remaining:
            raise DataDecodeError(f"MBAP 長度不符: 標示 {length}, 實際後續載荷 {actual_remaining}")

        if uid != self.uid:
            raise DataDecodeError(f"Slave ID 不符 (收到 {uid}，預期 {self.uid})")

        fc = raw_data[7]
        if fc & 0x80:
            exc_code = raw_data[8]
            raise DataDecodeError(f"設備回報 Modbus Exception Code: {exc_code}")

        if expected_fc is not None and fc != expected_fc:
            raise DataDecodeError(f"FC 不符 (收到 0x{fc:02X}，預期 0x{expected_fc:02X})")

        if expected_len and len(raw_data) != expected_len:
            raise DataDecodeError(f"長度不符: 預期 {expected_len}, 實際 {len(raw_data)}")

    def decode(self, raw_data: bytes, context: dict) -> dict:
        ctx_type = context.get("type")
        expected_tx_id = context.get("tx_id")

        if ctx_type == "poll":
            expected_fc = context["cmd"]["fc"]
            expected_len = context["cmd"].get("expected_len")
        elif ctx_type == "verify":
            expected_fc = context.get("read_fc")
            expected_len = None
        else:
            raise DataDecodeError(f"未知的 context type: {ctx_type}")

        self._verify_modbus_frame(raw_data, expected_fc, expected_len, expected_tx_id)

        rtu_like = raw_data[6:] + calc_crc16(raw_data[6:])

        # 💡 [修復 3] 直接調用父類的 extraction，避開 MRO 陷阱
        result = GenericModbusAdapter._extract_data(self, rtu_like, context)

        if not result:
            raise DataDecodeError("TCP 解析成功但未提取出任何有效數據")
        return result
