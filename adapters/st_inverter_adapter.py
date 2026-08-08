# =============================================================================
# File: generic_adapter.py 工業級通用 Modbus 設備解析器 V2.3 (架構重構版)
# 修復歷程：
#   V2.3 : 將 decode() 重構，拆分出純函數 _extract_data()。
#          解決子類 (如 TCP Adapter) 呼叫 super().decode() 時觸發 Python MRO
#          導致驗證邏輯錯亂的致命架構缺陷。
# =============================================================================
import struct
import logging

logger = logging.getLogger(__name__)

ADAPTER_NAME = "st_inverter"  # 宣告註冊名稱 generic > rtu

class DataDecodeError(Exception):
    pass

def calc_crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if crc & 1:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)

# 類別名稱改為 Adapter
class Adapter:
    def __init__(self, uid: int, profile: dict):
        self.uid = uid
        self.profile = profile
        self.definitions = profile.get("definitions", {})
        self.sensors = profile.get("sensors", [])
        self.settings = profile.get("settings", {})

        self._poll_cmds = self._prebuild_poll_cmds()
        self._poll_index = 0

    def _prebuild_poll_cmds(self) -> list:
        cmds = []
        for cmd in self.profile.get("read_commands", []):
            base = struct.pack('>BBHH', self.uid, cmd['fc'], cmd['start_addr'], cmd['count'])
            req = base + calc_crc16(base)
            interval = cmd.get('interval', 1)
            cmds.append({
                "id": cmd['id'],
                "fc": cmd['fc'],
                "req": req,
                "expected_len": cmd.get('response_len'),
                "interval": interval,        # 宣告讀取間隔 (預設 1)
                "skip_count": interval       # 初始設為 interval，確保開機第一輪「必定全讀」
            })
        return cmds

    def _verify_modbus_frame(self, raw_data: bytes, expected_fc: int = None, expected_len: int = None):
        if not raw_data or len(raw_data) < 5:
            raise DataDecodeError(f"封包過短 ({len(raw_data) if raw_data else 0} bytes)")

        if expected_len and len(raw_data) != expected_len:
            raise DataDecodeError(f"長度不符: 預期 {expected_len}, 實際 {len(raw_data)}")

        if raw_data[0] != self.uid:
            raise DataDecodeError(f"Slave ID 不符 (收到 {raw_data[0]}，預期 {self.uid})")

        if expected_fc is not None and raw_data[1] != expected_fc:
            raise DataDecodeError(f"FC 不符 (收到 0x{raw_data[1]:02X}，預期 0x{expected_fc:02X})")

        fc = raw_data[1]

        if fc in (1, 2, 3, 4):
            byte_count = raw_data[2]
            actual_data = len(raw_data) - 5
            if byte_count != actual_data:
                raise DataDecodeError(f"ByteCount 破裂: 標示 {byte_count} bytes, 實際載荷 {actual_data} bytes")

        elif fc in (5, 6, 15, 16):
            if len(raw_data) != 8:
                raise DataDecodeError(f"FC 0x{fc:02X} 回應長度不符: 預期 8 bytes, 實際 {len(raw_data)} bytes")

        payload, recv_crc = raw_data[:-2], raw_data[-2:]
        if calc_crc16(payload) != recv_crc:
            raise DataDecodeError("CRC16 校驗失敗（物理干擾或錯位）")

    def _unpack_value(self, chunk: bytes, datatype: str, word_order: str):
        if datatype == 'string':
            return chunk.decode('ascii', errors='ignore').strip('\x00 \t')

        if len(chunk) == 8:
            if word_order == 'little':
                chunk = chunk[::-1]
            elif word_order in ('swap', 'word_swap'):
                chunk = chunk[4:8] + chunk[0:4]
            elif word_order == 'dcba':
                chunk = chunk[6:8] + chunk[4:6] + chunk[2:4] + chunk[0:2]
            elif word_order == 'byte_swap':
                chunk = bytes([chunk[1], chunk[0], chunk[3], chunk[2], chunk[5], chunk[4], chunk[7], chunk[6]])

            if datatype == 'uint64':  return struct.unpack('>Q', chunk)[0]
            if datatype == 'int64':   return struct.unpack('>q', chunk)[0]
            if datatype == 'float64': return struct.unpack('>d', chunk)[0]

        elif len(chunk) == 4:
            if word_order in ('swap', 'word_swap'):
                chunk = chunk[2:4] + chunk[0:2]
            elif word_order == 'byte_swap':
                chunk = bytes([chunk[1], chunk[0], chunk[3], chunk[2]])
            elif word_order == 'little':
                chunk = chunk[::-1]

            if datatype == 'uint32':  return struct.unpack('>I', chunk)[0]
            if datatype == 'int32':   return struct.unpack('>i', chunk)[0]
            if datatype == 'float32': return struct.unpack('>f', chunk)[0]

        elif len(chunk) == 2:
            if word_order == 'little':
                chunk = chunk[::-1]
            if datatype == 'uint16': return struct.unpack('>H', chunk)[0]
            if datatype == 'int16':  return struct.unpack('>h', chunk)[0]

        elif len(chunk) == 1:
            if datatype == 'int8': return struct.unpack('>b', chunk)[0]
            return chunk[0]

        logger.warning(f"[Adapter] 未能解包: len={len(chunk)} datatype={datatype}")
        return None

    def build_poll_read(self) -> tuple[bytes, dict]:
        if not self._poll_cmds:
            raise RuntimeError("設備未定義 read_commands")

        start_index = self._poll_index
        while True:
            cmd = self._poll_cmds[self._poll_index]
            self._poll_index = (self._poll_index + 1) % len(self._poll_cmds)

            cmd['skip_count'] += 1
            # 檢查是否到達觸發週期
            if cmd['skip_count'] >= cmd['interval']:
                cmd['skip_count'] = 0
                return cmd['req'], {"type": "poll", "cmd": cmd}

            # 防呆機制：如果全部指令都在 Skip，強制拉出當前指令，避免死迴圈卡死 Bus
            if self._poll_index == start_index:
                cmd['skip_count'] = 0
                return cmd['req'], {"type": "poll", "cmd": cmd}

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

        base = struct.pack('>BBHH', self.uid, read_fc, target_addr, reg_count)
        req = base + calc_crc16(base)

        return req, {
            "type": "verify",
            "key": key,
            "read_fc": read_fc,
        }

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

        try:
            int_val = int(round(float(value) * scale))
        except (TypeError, ValueError):
            raise ValueError(f"寫入值無效: {value}")

        if fc == 6:
            base = struct.pack('>BBHH', self.uid, 0x06, target_addr, int_val & 0xFFFF)
            return base + calc_crc16(base)
        elif fc == 5:
            coil_val = 0xFF00 if int_val else 0x0000
            base = struct.pack('>BBHH', self.uid, 0x05, target_addr, coil_val)
            return base + calc_crc16(base)
        elif fc == 16:
            # 🚨 補齊 FC16 (0x10) 寫入單一暫存器 (Write Multiple Registers, Count=1)  封包格式: [UID] [10] [Addr_H] [Addr_L] [RegCount_H] [RegCount_L] [ByteCount] [Data_H] [Data_L]
            base = struct.pack('>BBHHB', self.uid, 0x10, target_addr, 1, 2) + struct.pack('>H', int_val & 0xFFFF)
            return base + calc_crc16(base)
        else:
            raise NotImplementedError(f"尚不支援功能碼 FC {fc} 組裝")

    # 💡 [修復核心] 拆分出純提取邏輯，供子類安全複用
    def _extract_data(self, raw_data: bytes, context: dict) -> dict:
        """純資料提取，假設 raw_data 已符合 RTU 格式 [UID][FC][Data...][CRC]"""
        result = {}
        ctx_type = context.get("type")

        if ctx_type == "verify":
            key = context["key"]
            read_fc = context.get("read_fc", 0x03) 

            if raw_data[1] & 0x80:
                raise DataDecodeError(f"設備回報 Modbus Exception Code: {raw_data[2]}")

            if read_fc in (0x03, 0x04):
                if len(raw_data) >= 7:
                    chunk = raw_data[3:5]
                    val = struct.unpack('>H', chunk)[0]
                    for cfg in self.settings.values():
                        if cfg['key'] == key:
                            scale = cfg.get("scale", 1.0)
                            link_sensor = cfg.get("link_sensor")
                            is_mapped = False
                            for s in self.sensors:
                                if s['key'] == link_sensor and "map_profile" in s:
                                    map_dict = self.definitions.get("value_maps", {}).get(s["map_profile"], {})
                                    if val in map_dict:
                                        result[key] = map_dict[val]
                                        is_mapped = True
                                    break
                            if not is_mapped:
                                result[key] = round(val / scale, 2) if scale != 1.0 else val
                            break

            elif read_fc in (0x01, 0x02):
                if len(raw_data) >= 6:
                    coil_byte = raw_data[3]
                    coil_val = bool(coil_byte & 0x01)
                    result[key] = "ON" if coil_val else "OFF"

        elif ctx_type == "poll":
            cmd = context["cmd"]
            for sensor in self.sensors:
                if sensor.get("command_id") != cmd['id']:
                    continue

                off = sensor.get("offset")
                length = sensor.get("length", 2)
                key = sensor['key']

                if off is None or off + length > len(raw_data) - 2:
                    logger.debug(f"[Adapter] sensor {key} offset 越界，跳過")
                    continue

                chunk = raw_data[off: off + length]

                try:
                    if "bits" in sensor:
                        val = chunk[0] if length == 1 else struct.unpack('>H', chunk)[0]
                        for bit_cfg in sensor["bits"]:
                            bit_idx = bit_cfg["bit"]
                            is_on = bool((val >> bit_idx) & 0x01)
                            p_on = bit_cfg.get("ha", {}).get("payload_on", "ON")
                            p_off = bit_cfg.get("ha", {}).get("payload_off", "OFF")
                            result[bit_cfg["id"]] = p_on if is_on else p_off
                        continue

                    datatype = sensor.get("datatype", "uint16" if length == 2 else "uint8")
                    if sensor.get("signed"):
                        datatype = datatype.replace("u", "", 1)
                    word_order = sensor.get("word_order", "big")
                    scale = sensor.get("scale", 1.0)

                    val = self._unpack_value(chunk, datatype, word_order)
                    if val is None:
                        continue

                    if "map_profile" in sensor:
                        map_dict = self.definitions.get("value_maps", {}).get(sensor["map_profile"], {})
                        result[key] = map_dict.get(val, str(val))
                    else:
                        result[key] = round(val / scale, 2) if scale != 1.0 else val

                except Exception as e:
                    logger.debug(f"[Adapter] 解析 sensor {key} 失敗: {e}")

        else:
            raise DataDecodeError(f"無效的 context type: {ctx_type}")

        return result

    def decode(self, raw_data: bytes, context: dict) -> dict:
        ctx_type = context.get("type")
        if ctx_type == "poll":
            cmd = context["cmd"]
            self._verify_modbus_frame(raw_data, expected_fc=cmd['fc'], expected_len=cmd.get('expected_len'))
        elif ctx_type == "verify":
            self._verify_modbus_frame(raw_data, expected_fc=context.get("read_fc", 0x03))

        result = self._extract_data(raw_data, context)

        if not result:
            raise DataDecodeError("解析成功但未提取出任何有效數據")
        return result
