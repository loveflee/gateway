# =============================================================================
# generic_adapter.py - V2.9 fc 15 實作補足版
# 核心職責：標準 Modbus RTU 解析器 (TCP/Serial 共用卡車頭)
# 修復歷程：
#   V2.3 : 將 decode() 重構，拆分出純函數 _extract_data()。
#          解決子類 (如 TCP Adapter) 呼叫時觸發 Python MRO 導致驗證邏輯錯亂的致命架構缺陷。
#   V2.4R: [Critical] 全面導入 int() 強制轉型，防禦 YAML 字串型別引發的 struct.pack 崩潰。
#          [Critical] encode_write 補 fc/scale float()/int() 型別守衛。
#          [Bugfix]   _extract_data bit_idx 強制 int() 轉型，防禦 TypeError。
#          [Arch]     撤回 Gemini 未授權架構變更：移除 StandardParser 依賴，
#                     恢復 _unpack_value 自解碼。adapter_helper 歸位為非標設備輔助工具，
#                     兩條線各自獨立，故障不互染  補齊 FC16 (0x10) 寫入單一暫存器
#                     (scale=0 防護由 map_validator 在掛載期攔截，adapter 層不重複)
#   V2.5 : [Observability] 修復輪詢解析的靜默丟棄：offset 越界 (DEBUG)、_unpack_value
#          回傳 None (完全無日誌)、解析例外 (DEBUG) 三條路徑，只要還有一個 sensor 解得
#          出來 decode() 就不拋錯，設備維持 ONLINE 但 HA 上大片實體無數值，預設 INFO
#          等級查不到任何線索。改為累計三種丟棄數並於命令解析結束後輸出彙總 WARNING。
#          僅新增日誌，不改變任何解析結果與例外行為。
#   V2.6 : [Protocol] Deep CRC Radar 增加合法 Exception 候選；由 request count 自動
#          推導 FC01/02/03/04 回覆資料長度，拒絕同 UID/FC 的錯 quantity 舊幀。
#   V2.7 : [Protocol] 新增 FC15 Write Multiple Coils 與 profile coil_groups。
#   V2.8 : [Safety] 正常 FC01 poll 亦重算 coil_groups，避免 FC05／外部狀態改變
#          後把舊 group state 快取重播給 HA；單路 verify 則先標示未知；未命名
#          vector 固定為 __UNMATCHED__。
#   V2.9 : [Safety] FC15 group write 的 pending target 改為 verify 建立時一次性消費；
#          成功、timeout、ACK 拒絕、decode 失敗與 retry 耗盡後均不可被後續
#          build_verify_read() 重用舊 target。
#          群組寫入只建立一筆 FC15，verify 共用完整 FC01 decoder；任何未命名
#          狀態固定回 __UNMATCHED__，不得落入 BusMaster 的 None 成功相容分支。
# =============================================================================

import struct
import logging
import math

logger = logging.getLogger(__name__)

ADAPTER_NAME = "rtu"
COIL_GROUP_UNMATCHED = "__UNMATCHED__"

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

class Adapter:
    def __init__(self, uid: int, profile: dict):
        self.uid = uid
        self.profile = profile
        self.definitions = profile.get("definitions", {})
        self.sensors = profile.get("sensors", [])
        self.settings = profile.get("settings", {})
        self.coil_groups = profile.get("coil_groups", {})
        # BusMaster 每個 attempt 都同步呼叫 encode_write() 後立刻呼叫
        # build_verify_read()。pending target 必須由後者一次性消費，絕不從
        # 裝置狀態推測或做 read-modify-write。
        self._pending_group_states = {}

        self._poll_cmds = self._prebuild_poll_cmds()
        self._poll_index = 0

    def _prebuild_poll_cmds(self) -> list:
        cmds = []
        for cmd in self.profile.get("read_commands", []):
            # ✅ [Fix V2.4R] 強制 int 轉型，防止 struct.pack 被 YAML 字串炸毀
            try:
                fc         = int(cmd['fc'])
                start_addr = int(cmd['start_addr'])
                count      = int(cmd['count'])
            except (TypeError, ValueError, KeyError) as e:
                logger.error(f"[Adapter] UID:{self.uid} read_commands 參數無效，跳過: {e}")
                continue

            base = struct.pack('>BBHH', self.uid, fc, start_addr, count)
            req  = base + calc_crc16(base)

            # 讀取回覆不含起始位址，必須由本次 request 的 quantity 鎖定資料長度，
            # 否則同 UID／同 FC 的舊幀可能在 CRC 正確時被誤認為本次回覆。
            expected_data_bytes = None
            protocol_expected_len = None
            if fc in (1, 2):
                expected_data_bytes = (count + 7) // 8
                protocol_expected_len = 5 + expected_data_bytes
            elif fc in (3, 4):
                expected_data_bytes = count * 2
                protocol_expected_len = 5 + expected_data_bytes

            profile_expected_len = cmd.get('response_len')
            cmds.append({
                "id":           cmd['id'],
                "fc":           fc,
                "req":          req,
                # 保留既有 map 的 response_len 語意；未設定時自動套用協定推導值。
                "expected_len": profile_expected_len if profile_expected_len is not None else protocol_expected_len,
                "expected_data_bytes": expected_data_bytes,
                "count":        count,
            })
        return cmds

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
                chunk = bytes([chunk[1], chunk[0], chunk[3], chunk[2],
                               chunk[5], chunk[4], chunk[7], chunk[6]])

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

    def _find_setting(self, key: str) -> tuple[dict, int]:
        """Return a setting and its numeric Modbus address without changing profile syntax."""
        for addr_str, cfg in self.settings.items():
            if cfg['key'] == key:
                target_addr = int(addr_str, 16) if isinstance(addr_str, str) else addr_str
                return cfg, target_addr
        raise ValueError(f"未知的寫入 Key '{key}'")

    def _resolve_write_sensor(self, setting: dict, key: str):
        """Resolve write metadata: explicit link_sensor first, then existing same-key fallback."""
        sensor_key = setting.get("link_sensor") or key
        for sensor in self.sensors:
            if sensor.get("key") == sensor_key:
                return sensor
        return None

    def _resolve_fc16_codec(self, setting: dict, key: str):
        """Return the explicitly inferable P1 two-register codec, or None for legacy FC16."""
        sensor = self._resolve_write_sensor(setting, key)
        if sensor is None:
            return None

        try:
            length = int(sensor.get("length", 2))
        except (TypeError, ValueError) as e:
            raise ValueError(f"FC16 寫入 metadata length 無效: {e}")

        # A normal 16-bit sensor remains on the legacy count=1 path exactly.
        if length == 2:
            return None
        if length != 4:
            raise ValueError(f"FC16 本輪只支援 2 或 4 bytes metadata，收到 {length}")

        datatype = sensor.get("datatype", "uint8")
        if sensor.get("signed"):
            datatype = datatype.replace("u", "", 1)
        if datatype not in ("uint32", "int32", "float32"):
            raise ValueError(f"FC16 32-bit 寫入不支援 datatype '{datatype}'")

        word_order = sensor.get("word_order", "big")
        if word_order not in ("big", "little", "swap", "word_swap", "byte_swap"):
            raise ValueError(f"FC16 32-bit 寫入不支援 word_order '{word_order}'")

        return {
            "datatype": datatype,
            "word_order": word_order,
            "register_count": 2,
            "data_bytes": 4,
        }

    @staticmethod
    def _coerce_legacy_16bit(scaled_value: float) -> int:
        """Preserve signed/unsigned 16-bit legacy bytes while rejecting wraparound."""
        if not math.isfinite(scaled_value):
            raise ValueError("寫入值必須是有限數值")
        int_val = int(round(scaled_value))
        # Existing maps can represent both uint16 and int16.  Preserve both legal
        # forms, but never silently turn an invalid value into another register.
        if not -0x8000 <= int_val <= 0xFFFF:
            raise ValueError(f"16-bit 寫入值超出範圍: {int_val}")
        return int_val

    @staticmethod
    def _apply_write_word_order(chunk: bytes, word_order: str) -> bytes:
        """Inverse of the existing 32-bit read transforms (all are self-inverse)."""
        if word_order in ("swap", "word_swap"):
            return chunk[2:4] + chunk[0:2]
        if word_order == "byte_swap":
            return bytes([chunk[1], chunk[0], chunk[3], chunk[2]])
        if word_order == "little":
            return chunk[::-1]
        return chunk

    def _encode_fc16_32bit_value(self, scaled_value: float, codec: dict) -> bytes:
        """Encode an approved P1 32-bit value into device-order register bytes."""
        datatype = codec["datatype"]
        try:
            if datatype == "uint32":
                int_val = int(round(scaled_value))
                if not 0 <= int_val <= 0xFFFFFFFF:
                    raise ValueError(f"uint32 寫入值超出範圍: {int_val}")
                chunk = struct.pack('>I', int_val)
            elif datatype == "int32":
                int_val = int(round(scaled_value))
                if not -0x80000000 <= int_val <= 0x7FFFFFFF:
                    raise ValueError(f"int32 寫入值超出範圍: {int_val}")
                chunk = struct.pack('>i', int_val)
            else:  # float32 is the remaining codec accepted by _resolve_fc16_codec.
                chunk = struct.pack('>f', scaled_value)
        except (OverflowError, struct.error) as e:
            raise ValueError(f"{datatype} 寫入值無法編碼: {e}")
        return self._apply_write_word_order(chunk, codec["word_order"])

    def _build_fc16_request(self, target_addr: int, register_bytes: bytes) -> bytes:
        if not register_bytes or len(register_bytes) % 2:
            raise ValueError("FC16 register payload 長度必須是非零偶數")
        register_count = len(register_bytes) // 2
        base = struct.pack('>BBHHB', self.uid, 0x10, target_addr, register_count, len(register_bytes))
        return base + register_bytes + calc_crc16(base + register_bytes)

    # =========================================================================
    # FC15 / Profile coil_groups
    # =========================================================================
    @staticmethod
    def _coerce_coil_bool(value) -> bool:
        """Accept only the explicit coil tokens used by profile/HA."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            token = value.strip().upper()
            if token == "ON":
                return True
            if token == "OFF":
                return False
        raise ValueError(f"coil value 必須是 bool、ON 或 OFF，收到 {value!r}")

    @classmethod
    def pack_coils_lsb_first(cls, values) -> bytes:
        """Pack consecutive coils in Modbus FC15's LSB-first wire order."""
        coils = [cls._coerce_coil_bool(value) for value in values]
        if not coils:
            raise ValueError("FC15 至少需要一個 coil")
        if len(coils) > 2000:
            raise ValueError(f"FC15 coil 數量超出 2000 上限: {len(coils)}")

        packed = bytearray((len(coils) + 7) // 8)
        for index, enabled in enumerate(coils):
            if enabled:
                packed[index // 8] |= 1 << (index % 8)
        return bytes(packed)

    @classmethod
    def build_fc15_pdu(cls, start_addr: int, values) -> bytes:
        """Build a transport-neutral FC15 PDU for RTU and MBAP adapters."""
        try:
            start_addr = int(start_addr)
        except (TypeError, ValueError) as e:
            raise ValueError(f"FC15 start_addr 無效: {e}")
        coils = list(values)
        packed = cls.pack_coils_lsb_first(coils)
        quantity = len(coils)
        if not 0 <= start_addr <= 0xFFFF or start_addr + quantity > 0x10000:
            raise ValueError(f"FC15 位址範圍無效: start={start_addr}, quantity={quantity}")
        return struct.pack('>BHHB', 0x0F, start_addr, quantity, len(packed)) + packed

    def _find_coil_group(self, key: str):
        if not isinstance(self.coil_groups, dict):
            raise ValueError("coil_groups 必須是 dict")
        group = self.coil_groups.get(key)
        if group is None:
            return None
        if not isinstance(group, dict):
            raise ValueError(f"coil_groups.{key} 必須是 dict")
        return group

    def _group_state_vector(self, key: str, group: dict, state_name: str) -> list[bool]:
        states = group.get("states")
        if not isinstance(states, dict) or state_name not in states:
            raise ValueError(f"coil group '{key}' 不支援 state '{state_name}'")
        try:
            count = int(group["count"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"coil group '{key}' count 無效: {e}")
        vector = states[state_name]
        if not isinstance(vector, list) or len(vector) != count:
            raise ValueError(f"coil group '{key}' state '{state_name}' vector 長度不符")
        return [self._coerce_coil_bool(item) for item in vector]

    def _prepare_coil_group_write(self, key: str, value) -> tuple[int, list[bool]]:
        group = self._find_coil_group(key)
        if group is None:
            raise ValueError(f"未知的 coil group '{key}'")
        if not isinstance(value, str) or not value:
            raise ValueError(f"coil group '{key}' 必須使用 profile 定義的命名 state")
        try:
            start_addr = int(group["start_addr"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"coil group '{key}' start_addr 無效: {e}")
        vector = self._group_state_vector(key, group, value)
        # Only commit the pending state after all validation succeeds.
        self._pending_group_states[key] = value
        return start_addr, vector

    def _build_coil_group_verify_spec(self, key: str) -> tuple[dict, dict, str]:
        group = self._find_coil_group(key)
        if group is None:
            raise ValueError(f"未知的 coil group '{key}'")
        # A group target belongs to exactly one encode_write() -> verify attempt.
        # BusMaster retries by calling encode_write() again before this method,
        # so consuming it here cannot lose a retry target.  Conversely, pop()
        # closes every terminal path outside the Adapter (ACK reject, timeout,
        # decode error, mismatch exhaustion, or success): a later direct verify
        # cannot resurrect a previous command's target.
        state_name = self._pending_group_states.pop(key, None)
        if state_name is None:
            raise ValueError(f"coil group '{key}' 尚未建立 FC15 write，拒絕 verify")
        # Re-validate the state here so a stale/bad pending value cannot turn into
        # a permissive verify path.
        self._group_state_vector(key, group, state_name)
        verify_command_id = group.get("verify_command_id")
        for command in self.profile.get("read_commands", []):
            if isinstance(command, dict) and command.get("id") == verify_command_id:
                return group, command, state_name
        raise ValueError(f"coil group '{key}' verify_command_id 不存在: {verify_command_id!r}")

    def _match_coil_group_state(self, group: dict, decoded: dict) -> str:
        """Return only a named exact vector; every other outcome is fail-closed."""
        try:
            members = group.get("members")
            states = group.get("states")
            if not isinstance(members, list) or not isinstance(states, dict):
                return COIL_GROUP_UNMATCHED
            observed = tuple(self._coerce_coil_bool(decoded.get(member)) for member in members)
            for state_name, vector in states.items():
                normalized = tuple(self._coerce_coil_bool(item) for item in vector)
                if normalized == observed:
                    return state_name
        except (TypeError, ValueError):
            pass
        return COIL_GROUP_UNMATCHED

    def _append_polled_coil_group_states(self, command: dict, decoded: dict) -> None:
        """Derive every group tied to this FC01 command from the fresh coil decode.

        HA state is a merged cache.  A group value must therefore be included in
        normal polls as well as write verify responses; otherwise a prior named
        state can be republished after an FC05 or external coil change.  The
        existing exact matcher deliberately returns ``__UNMATCHED__`` for every
        non-profile vector, rather than retaining or guessing a previous state.
        """
        command_id = command.get("id") if isinstance(command, dict) else None
        if not command_id or not isinstance(self.coil_groups, dict):
            return

        for group_key, group in self.coil_groups.items():
            if not isinstance(group, dict):
                continue
            if group.get("verify_command_id") != command_id:
                continue
            decoded[group_key] = self._match_coil_group_state(group, decoded)

    def _mark_partial_verify_groups_unmatched(self, key: str, decoded: dict) -> None:
        """Fail closed when a legacy single-coil verify cannot prove a group.

        FC05 verify intentionally reads only the written coil to preserve its
        long-standing request/response contract.  It cannot establish the other
        members' physical states, so retaining a previously named group state
        would be false.  Publish the explicit unknown sentinel until the next
        full FC01 poll computes the exact vector.
        """
        if not isinstance(self.coil_groups, dict):
            return
        for group_key, group in self.coil_groups.items():
            if isinstance(group, dict) and key in group.get("members", []):
                decoded[group_key] = COIL_GROUP_UNMATCHED

    def build_poll_read(self) -> tuple[bytes, dict]:
        if not self._poll_cmds:
            raise RuntimeError("設備未定義 read_commands")
        cmd = self._poll_cmds[self._poll_index]
        logger.info(f"[Adapter] UID:{self.uid} Preparing to poll command: {cmd['id']}")
        self._poll_index = (self._poll_index + 1) % len(self._poll_cmds)
        return cmd['req'], {"type": "poll", "cmd": cmd}

    def build_verify_read(self, key: str) -> tuple[bytes, dict]:
        group = self._find_coil_group(key)
        if group is not None:
            group, command, state_name = self._build_coil_group_verify_spec(key)
            try:
                read_fc = int(command["fc"])
                start_addr = int(command["start_addr"])
                count = int(command["count"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"coil group '{key}' verify command 無效: {e}")
            if read_fc != 0x01:
                raise ValueError(f"coil group '{key}' verify 必須使用 FC01，收到 FC{read_fc}")
            base = struct.pack('>BBHH', self.uid, read_fc, start_addr, count)
            return base + calc_crc16(base), {
                "type": "verify",
                "key": key,
                "read_fc": read_fc,
                "strict_verify": True,
                "expected_data_bytes": (count + 7) // 8,
                "expected_count": count,
                "coil_group": group,
                "group_state": state_name,
                "verify_command": command,
            }

        try:
            setting, target_addr = self._find_setting(key)
        except ValueError:
            raise ValueError(f"找不到寫入 Key: {key}")

        # ✅ [Fix V2.4R] 強制 int 轉型
        try:
            write_fc  = int(setting.get("write_fc", 6))
            reg_count = int(setting.get("verify_count", 1))
        except (TypeError, ValueError) as e:
            raise ValueError(f"build_verify_read 參數無效: {e}")

        codec = self._resolve_fc16_codec(setting, key) if write_fc == 16 else None
        if codec:
            reg_count = codec["register_count"]

        read_fc = 0x01 if write_fc in (5, 15) else 0x03
        base = struct.pack('>BBHH', self.uid, read_fc, target_addr, reg_count)
        req  = base + calc_crc16(base)

        # Legacy contexts intentionally remain byte-for-byte and behaviourally
        # unchanged.  Strict quantity validation is only for the new 32-bit path.
        context = {"type": "verify", "key": key, "read_fc": read_fc}
        if codec:
            context.update({
                "strict_verify": True,
                "expected_data_bytes": codec["data_bytes"],
                "expected_count": codec["register_count"],
                "codec": codec,
            })
        return req, context

    def encode_write(self, key: str, value) -> bytes:
        group = self._find_coil_group(key)
        if group is not None:
            target_addr, vector = self._prepare_coil_group_write(key, value)
            pdu = self.build_fc15_pdu(target_addr, vector)
            base = bytes([self.uid]) + pdu
            return base + calc_crc16(base)

        setting, target_addr = self._find_setting(key)

        # ✅ [Fix V2.4R] 強制數值轉型
        try:
            fc    = int(setting.get("write_fc", 6))
            scale = float(setting.get("scale", 1.0))
        except (TypeError, ValueError) as e:
            raise ValueError(f"encode_write 參數型別無效: {e}")

        link_sensor = setting.get("link_sensor")
        for s in self.sensors:
            if s['key'] == link_sensor and "map_profile" in s:
                map_dict = self.definitions.get("value_maps", {}).get(s["map_profile"], {})
                for k, v in map_dict.items():
                    if str(v) == str(value):
                        value = k
                        break

        if isinstance(value, str):
            v_upper = value.upper()
            if v_upper in ("ON", "TRUE"):
                value = 1
            elif v_upper in ("OFF", "FALSE"):
                value = 0

        try:
            scaled_value = float(value) * scale
        except (TypeError, ValueError):
            raise ValueError(f"寫入值無效: {value!r} (型別: {type(value).__name__})")
        if not math.isfinite(scaled_value):
            raise ValueError(f"寫入值必須是有限數值: {value!r}")

        if fc == 6:
            int_val = self._coerce_legacy_16bit(scaled_value)
            base = struct.pack('>BBHH', self.uid, 0x06, target_addr, int_val & 0xFFFF)
            return base + calc_crc16(base)
        elif fc == 5:
            int_val = self._coerce_legacy_16bit(scaled_value)
            coil_val = 0xFF00 if int_val else 0x0000
            base = struct.pack('>BBHH', self.uid, 0x05, target_addr, coil_val)
            return base + calc_crc16(base)
        elif fc == 16:
            codec = self._resolve_fc16_codec(setting, key)
            if codec:
                return self._build_fc16_request(
                    target_addr,
                    self._encode_fc16_32bit_value(scaled_value, codec),
                )
            # Legacy FC16 count=1 packet layout remains exactly unchanged.
            int_val = self._coerce_legacy_16bit(scaled_value)
            return self._build_fc16_request(target_addr, struct.pack('>H', int_val & 0xFFFF))
        else:
            raise NotImplementedError(f"尚不支援功能碼 FC {fc} 組裝")

    def _extract_data(self, raw_data: bytes, context: dict) -> dict:
        """純資料提取，假設 raw_data 已符合 RTU 格式 [UID][FC][Data...][CRC]"""
        result = {}
        ctx_type = context.get("type")

        if ctx_type == "verify":
            key     = context["key"]
            read_fc = context.get("read_fc", 0x03)

            if raw_data[1] & 0x80:
                raise DataDecodeError(f"設備回報 Modbus Exception Code: {raw_data[2]}")

            if read_fc in (0x03, 0x04):
                if len(raw_data) >= 7:
                    codec = context.get("codec") if context.get("strict_verify") else None
                    if codec:
                        chunk = raw_data[3:3 + codec["data_bytes"]]
                        val = self._unpack_value(chunk, codec["datatype"], codec["word_order"])
                        if val is None:
                            raise DataDecodeError("FC16 multi-register verify 無法解碼")
                    else:
                        # Do not tighten this legacy path: valid longer frames
                        # from the intentional dual-master test must retain the
                        # historical first-register comparison behaviour.
                        chunk = raw_data[3:5]
                        val   = struct.unpack('>H', chunk)[0]
                    for cfg in self.settings.values():
                        if cfg['key'] == key:
                            scale       = cfg.get("scale", 1.0)
                            link_sensor = cfg.get("link_sensor")
                            is_mapped   = False
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
                coil_group = context.get("coil_group")
                if coil_group is not None:
                    verify_command = context.get("verify_command")
                    if not isinstance(verify_command, dict):
                        raise DataDecodeError("coil group verify 缺少 read command context")
                    # Reuse the existing poll decoder.  This updates every normal
                    # switch key from the one FC01 response; no second bit decoder.
                    decoded_switches = self._extract_data(
                        raw_data, {"type": "poll", "cmd": verify_command}
                    )
                    result.update(decoded_switches)
                    result[key] = self._match_coil_group_state(coil_group, decoded_switches)
                elif len(raw_data) >= 6:
                    coil_byte = raw_data[3]
                    coil_val  = bool(coil_byte & 0x01)
                    result[key] = "ON" if coil_val else "OFF"
                    self._mark_partial_verify_groups_unmatched(key, result)

        elif ctx_type == "poll":
            cmd = context["cmd"]
            # ✅ [Fix V2.5] 靜默丟棄計數器：原本三種丟棄路徑全是 DEBUG 或完全無日誌，
            #    只要還有一個 sensor 解得出來 decode() 就不拋錯，設備顯示健康，
            #    但 HA 上大片實體沒有數值，預設 INFO 等級完全看不見。
            declared = skipped_oob = skipped_null = skipped_err = 0

            for sensor in self.sensors:
                if sensor.get("command_id") != cmd['id']:
                    continue

                declared += 1
                off    = sensor.get("offset")
                length = sensor.get("length", 2)
                key    = sensor['key']

                if off is None or off + length > len(raw_data) - 2:
                    skipped_oob += 1
                    logger.debug(f"[Adapter] sensor {key} offset 越界，跳過")
                    continue

                chunk = raw_data[off: off + length]

                try:
                    if "bits" in sensor:
                        val = chunk[0] if length == 1 else struct.unpack('>H', chunk)[0]
                        for bit_cfg in sensor["bits"]:
                            # ✅ [Fix V2.4R] bit_idx 強制 int() 防字串型別 TypeError
                            try:
                                bit_idx = int(bit_cfg["bit"])
                            except (TypeError, ValueError) as e:
                                logger.warning(f"[Adapter] '{key}' bit 索引型別錯誤: {e}，跳過")
                                continue
                            is_on = bool((val >> bit_idx) & 0x01)
                            p_on  = bit_cfg.get("ha", {}).get("payload_on",  "ON")
                            p_off = bit_cfg.get("ha", {}).get("payload_off", "OFF")
                            result[bit_cfg["id"]] = p_on if is_on else p_off
                        continue

                    datatype   = sensor.get("datatype", "uint16" if length == 2 else "uint8")
                    if sensor.get("signed"):
                        datatype = datatype.replace("u", "", 1)
                    word_order = sensor.get("word_order", "big")
                    scale      = sensor.get("scale", 1.0)

                    val = self._unpack_value(chunk, datatype, word_order)
                    if val is None:
                        skipped_null += 1
                        continue

                    if "map_profile" in sensor:
                        map_dict    = self.definitions.get("value_maps", {}).get(sensor["map_profile"], {})
                        result[key] = map_dict.get(val, str(val))
                    else:
                        result[key] = round(val / scale, 2) if scale != 1.0 else val

                except Exception as e:
                    skipped_err += 1
                    logger.debug(f"[Adapter] 解析 sensor {key} 失敗: {e}")

            # ✅ [Fix V2.5] 彙總告警：一次輪詢丟掉多少 sensor 必須在預設等級可見。
            #    雙主站匯流排（原廠 WiFi 模組共線）下，收到他人回應會使長度對不上，
            #    整批 sensor 因越界被丟棄卻不留痕跡 —— 這行就是唯一的線索。
            dropped = skipped_oob + skipped_null + skipped_err
            if dropped:
                logger.warning(
                    f"[Adapter] UID:{self.uid} cmd={cmd['id']} 丟棄 {dropped}/{declared} 個 sensor "
                    f"(offset越界 {skipped_oob} / 未能解包 {skipped_null} / 解析失敗 {skipped_err})"
                    f"，實際封包 {len(raw_data)}B"
                )

            # FC01 poll is the authoritative physical state.  Recompute groups
            # only after the existing bit decoder has populated every member.
            # Do not reuse a pending write value or HA cache value here.
            self._append_polled_coil_group_states(cmd, result)

        else:
            raise DataDecodeError(f"無效的 context type: {ctx_type}")

        return result

    def decode(self, raw_data: bytes, context: dict) -> dict:
        if not raw_data or len(raw_data) < 5:
            raise DataDecodeError(f"封包過短 ({len(raw_data) if raw_data else 0} bytes)")

        ctx_type = context.get("type")
        expected_fc = context["cmd"]['fc'] if ctx_type == "poll" else context.get("read_fc", 0x03)

        # --- 🚀 防禦 RS485 前置與後置浮接雜訊 (Deep CRC Radar) ---
        normal_header = bytes([self.uid, expected_fc])
        exception_header = bytes([self.uid, expected_fc | 0x80])
        valid_frame = None
        search_idx = 0
        
        while True:
            normal_idx = raw_data.find(normal_header, search_idx)
            exception_idx = raw_data.find(exception_header, search_idx)
            candidates = [idx for idx in (normal_idx, exception_idx) if idx != -1]
            if not candidates:
                break
            start_idx = min(candidates)
                
            candidate = raw_data[start_idx:]
            fc = candidate[1]
            actual_len = len(candidate)
            
            if fc & 0x80:  # Exception frame
                actual_len = 5
            elif fc in (1, 2, 3, 4):
                if len(candidate) >= 3:
                    actual_len = 3 + candidate[2] + 2
            elif fc in (5, 6, 15, 16):
                actual_len = 8
                
            if len(candidate) >= actual_len:
                candidate_frame = candidate[:actual_len]
                payload, recv_crc = candidate_frame[:-2], candidate_frame[-2:]
                if calc_crc16(payload) == recv_crc:
                    valid_frame = candidate_frame
                    break  # 找到完美的有效封包
                    
            search_idx = start_idx + 1
            
        if valid_frame:
            raw_data = valid_frame
        else:
            raise DataDecodeError(f"在 {len(raw_data)} bytes 的雜訊中找不到合法 CRC 的封包")

        if raw_data[1] == (expected_fc | 0x80):
            # Radar 已驗 UID、FC、固定 5 bytes 與 CRC；此處只轉成可觀測的協定語意。
            raise DataDecodeError(
                f"Modbus Exception: UID={self.uid} FC={expected_fc:02X} Code={raw_data[2]:02X}"
            )

        ctx_type = context.get("type")
        if ctx_type == "poll":
            cmd = context["cmd"]
            self._verify_modbus_frame(raw_data,
                                      expected_fc=cmd['fc'],
                                      expected_len=cmd.get('expected_len'),
                                      expected_data_bytes=cmd.get('expected_data_bytes'),
                                      expected_count=cmd.get('count'))
        elif ctx_type == "verify":
            if context.get("strict_verify"):
                self._verify_modbus_frame(
                    raw_data,
                    expected_fc=context.get("read_fc", 0x03),
                    expected_data_bytes=context.get("expected_data_bytes"),
                    expected_count=context.get("expected_count"),
                )
            else:
                # 027 hard regression boundary: retain historical legacy verify
                # behaviour for verify_count omitted or equal to one.
                self._verify_modbus_frame(raw_data,
                                          expected_fc=context.get("read_fc", 0x03))

        result = self._extract_data(raw_data, context)

        if not result:
            raise DataDecodeError("解析成功但未提取出任何有效數據")
        return result

    def _verify_modbus_frame(self, raw_data: bytes,
                             expected_fc: int = None,
                             expected_len: int = None,
                             expected_data_bytes: int = None,
                             expected_count: int = None):
        if not raw_data or len(raw_data) < 5:
            raise DataDecodeError(f"封包過短 ({len(raw_data) if raw_data else 0} bytes)")

        if raw_data[0] != self.uid:
            raise DataDecodeError(f"Slave ID 不符 (收到 {raw_data[0]}，預期 {self.uid})")

        if expected_fc is not None and raw_data[1] != expected_fc:
            raise DataDecodeError(f"FC 不符 (收到 0x{raw_data[1]:02X}，預期 0x{expected_fc:02X})")

        fc = raw_data[1]

        if fc in (1, 2, 3, 4):
            byte_count  = raw_data[2]
            actual_data = len(raw_data) - 5
            if byte_count != actual_data:
                raise DataDecodeError(
                    f"ByteCount 破裂: 標示 {byte_count} bytes, 實際載荷 {actual_data} bytes"
                )
            if expected_data_bytes is not None and byte_count != expected_data_bytes:
                raise DataDecodeError(
                    f"Response quantity mismatch: expected {expected_count} count "
                    f"({expected_data_bytes} bytes), received {byte_count} bytes"
                )

        elif fc in (5, 6, 15, 16):
            if len(raw_data) != 8:
                raise DataDecodeError(
                    f"FC 0x{fc:02X} 回應長度不符: 預期 8 bytes, 實際 {len(raw_data)} bytes"
                )

        payload, recv_crc = raw_data[:-2], raw_data[-2:]
        if calc_crc16(payload) != recv_crc:
            raise DataDecodeError("CRC16 校驗失敗（物理干擾或錯位）")

        if expected_len and len(raw_data) != expected_len:
            raise DataDecodeError(f"長度不符: 預期 {expected_len}, 實際 {len(raw_data)}")
