# =============================================================================
# adapter_helper.py - V1.2 工業封存版 verison
# 修復歷程 (V1.1 → V1.2)：
#   - [Critical] offset/length 加 int() 強制轉型 + TypeError 守衛，防 YAML 字串型別炸毀
#   - [Critical] 步驟 3 & 4 加 try-except 隔離罩，實現單點故障不擴散
#   - [Critical] scale float() 轉型移入 try-except 防護範圍
#   - [Bugfix] length/datatype 不一致加 warning，防靜默解錯數值
#   - [Bugfix] __init__ / parse() 入口加型別守衛
#   - [Bugfix] b_pos 強制 int() 轉型，防位元運算 TypeError
# =============================================================================
import struct
import logging

logger = logging.getLogger(__name__)

class StandardParser:

    def __init__(self, sensors: list, definitions: dict):
        # ✅ 入口守衛
        if not isinstance(sensors, list):
            logger.error(f"[StandardParser] sensors 型別錯誤：{type(sensors).__name__}，使用空列表")
            sensors = []
        if not isinstance(definitions, dict):
            logger.error(f"[StandardParser] definitions 型別錯誤：{type(definitions).__name__}，使用空字典")
            definitions = {}

        self.sensors = sensors
        self.value_maps = definitions.get("value_maps", {})

        self._cmd_groups = {}
        for s in sensors:
            if not isinstance(s, dict):
                continue
            cid = s.get("command_id")
            if not cid:
                continue
            if cid not in self._cmd_groups:
                self._cmd_groups[cid] = []
            self._cmd_groups[cid].append(s)

    def parse(self, resp: bytearray | bytes, cmd_id: str) -> dict:
        # ✅ resp 入口守衛
        if not isinstance(resp, (bytearray, bytes)) or not resp:
            logger.warning(f"[StandardParser] parse() 收到無效 resp: {type(resp).__name__}")
            return {}

        data = {}
        target_sensors = self._cmd_groups.get(cmd_id, [])

        for s in target_sensors:
            key = s.get("key")
            if not key:
                continue

            # ✅ [Fix #1] int() 強制轉型，攔截 YAML 字串型別
            try:
                off = int(s.get("offset"))
                ln  = int(s.get("length", 2))
            except (TypeError, ValueError) as e:
                logger.warning(f"[StandardParser] '{key}' offset/length 型別錯誤: {e}，跳過")
                continue

            dt    = s.get("datatype", "uint16")
            order = s.get("word_order", "big")

            if off is None or off + ln > len(resp):
                logger.debug(f"[StandardParser] '{key}' offset {off} 越界 (封包長度 {len(resp)})，跳過")
                continue

            chunk = bytes(resp[off: off + ln])

            # ── Bits ──
            if "bits" in s:
                try:
                    c = bytearray(chunk)
                    if ln == 2 and order == "little":
                        c = c[::-1]
                    elif ln == 4:
                        if order == "little":
                            c = c[::-1]
                        elif order in ("swap", "word_swap"):
                            c = c[2:4] + c[0:2]
                        elif order == "byte_swap":
                            c = bytearray([c[1], c[0], c[3], c[2]])

                    raw_int = int.from_bytes(bytes(c), byteorder="big")

                    for b in s["bits"]:
                        b_id  = b.get("id")
                        b_pos = b.get("bit")
                        if b_id and b_pos is not None:
                            try:
                                b_pos = int(b_pos)  # ✅ [Fix #5] 防 "3" 字串型別
                            except (TypeError, ValueError):
                                logger.warning(
                                    f"[StandardParser] '{key}' bit pos 型別錯誤: "
                                    f"{b_pos!r} ({type(b_pos).__name__})，跳過此 bit"
                                )
                                continue
                            data[b_id] = "ON" if (raw_int >> b_pos) & 1 else "OFF"
                except Exception as e:
                    logger.warning(f"[StandardParser] '{key}' Bits 解析異常: {e}")
                continue

            # ── ASCII ──
            if dt in ("ascii", "string"):
                try:
                    val = chunk.decode("ascii", errors="ignore").strip("\x00 \t\n\r")
                    data[key] = val
                except Exception as e:
                    logger.warning(f"[StandardParser] '{key}' ASCII 解碼異常: {e}")
                continue

            # ── 數值 ── ✅ [Fix #3] 步驟 3 & 4 全包進隔離罩
            try:
                val = self._unpack_value(chunk, dt, order, key)
                if val is None:
                    continue

                # ✅ [Fix #2] scale 轉型移入 try-except 內
                try:
                    scale = float(s.get("scale", 1.0))
                except (TypeError, ValueError) as e:
                    logger.warning(f"[StandardParser] '{key}' scale 型別錯誤: {e}，使用預設 1.0")
                    scale = 1.0

                if scale == 0:
                    logger.warning(f"[StandardParser] '{key}' scale=0 非法設定，跳過")
                    continue

                fv = round(val / scale, 2) if scale != 1.0 else val

                if "map_profile" in s:
                    mp = self.value_maps.get(s["map_profile"], {})
                    try:
                        lookup_key = int(fv) if isinstance(fv, float) and fv.is_integer() else fv
                    except (TypeError, OverflowError):
                        lookup_key = fv
                    result = mp.get(lookup_key)
                    data[key] = str(result) if result is not None else fv
                else:
                    data[key] = fv

            except Exception as e:
                logger.warning(f"[StandardParser] '{key}' 數值解析異常: {e}，跳過此點位")
                continue

        return data

    def _unpack_value(self, chunk: bytes, datatype: str, word_order: str, key_name: str):
        ln = len(chunk)
        try:
            if ln == 1:
                if datatype == "int8":
                    return struct.unpack(">b", chunk)[0]
                return chunk[0]

            elif ln == 2:
                # ✅ [Fix #4] datatype 與 length 不一致時發出警告
                if datatype in ("int32", "uint32", "float32", "int64", "uint64", "float64"):
                    logger.warning(
                        f"[StandardParser] '{key_name}' datatype={datatype} "
                        f"與 length=2 不符，強制以 uint16 解析"
                    )
                c = bytearray(chunk)
                if word_order == "little":
                    c = c[::-1]
                elif word_order == "byte_swap":
                    c = bytearray([c[1], c[0]])
                fmt = ">h" if datatype == "int16" else ">H"
                return struct.unpack(fmt, bytes(c))[0]

            elif ln == 4:
                c = bytearray(chunk)
                if word_order in ("swap", "word_swap"):
                    c = c[2:4] + c[0:2]
                elif word_order == "little":
                    c = c[::-1]
                elif word_order == "byte_swap":
                    c = bytearray([c[1], c[0], c[3], c[2]])
                if datatype == "float32":
                    return struct.unpack(">f", bytes(c))[0]
                elif datatype == "int32":
                    return struct.unpack(">i", bytes(c))[0]
                else:
                    return struct.unpack(">I", bytes(c))[0]

            elif ln == 8:
                c = bytearray(chunk)
                if word_order in ("swap", "word_swap"):
                    c = c[6:8] + c[4:6] + c[2:4] + c[0:2]
                elif word_order == "little":
                    c = c[::-1]
                elif word_order == "byte_swap":
                    c = bytearray([c[1], c[0], c[3], c[2], c[5], c[4], c[7], c[6]])
                if datatype == "float64":
                    return struct.unpack(">d", bytes(c))[0]
                elif datatype == "int64":
                    return struct.unpack(">q", bytes(c))[0]
                else:
                    return struct.unpack(">Q", bytes(c))[0]

            else:
                logger.warning(f"[StandardParser] '{key_name}' 不支援的 length={ln}，跳過")
                return None

        except Exception as e:
            logger.warning(
                f"[StandardParser] '{key_name}' 解包失敗 "
                f"chunk={chunk.hex()} datatype={datatype} order={word_order} err={e}"
            )
            return None

        return None
