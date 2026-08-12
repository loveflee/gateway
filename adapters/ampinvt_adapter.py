# =============================================================================
# ampinvt_adapter.py - V1.2 時控與群控擴展版
# 模組名稱：Ampinvt 逆變器私有協議轉譯器
# 核心職責：將 YAML 轉為私有位元組封包，並攔截所有異常回傳
# 修復歷程 (V1.1 → V1.2 功能與防禦升級)：
#   - [Feature] 補齊 0xD0 內 0x2D~0x30 的 HHMM 時間 BCD 拆碼邏輯。
#   - [Feature] 實作 0xDE 波特率群控廣播設定。
#   - [Feature] 實作 0xDF 即時時鐘自動對時注入。
#   - [Feature] decode 內直接消費 16 個 BCD RAW 點位，轉換為 HA number 可用的 Int。
#   - [Bugfix] 修正 decode 內 BCD 處理區塊的 Python 縮排異常。
#   - [Bugfix] 攔截 ACK 判斷加入 0xDE 與 0xDF。
# =============================================================================
import logging
from adapters.adapter_helper import StandardParser

logger = logging.getLogger(__name__)
ADAPTER_NAME = "ampinvt"

class DataDecodeError(Exception):
    pass

def calc_checksum(data: bytearray | bytes) -> int:
    return sum(data) & 0xFF

MAX_WRITE_CACHE = 50

class Adapter:
    def __init__(self, uid: int, profile: dict):
        self.uid = uid
        self.profile = profile
        self.sensors = profile.get("sensors", [])
        self.settings = profile.get("settings", {})
        self.definitions = profile.get("definitions", {})

        cmds = profile.get("read_commands", [{}])
        self._has_checksum = cmds[0].get("has_checksum", True)
        self._response_len = cmds[0].get("response_len", 93)

        self._poll_cmds = self._prebuild_poll_cmds()
        self._poll_index = 0
        self._write_verify_cache = {}
        self.parser = StandardParser(self.sensors, self.definitions)

    def _prebuild_poll_cmds(self) -> list[dict]:
        cmds = []
        for cmd in self.profile.get("read_commands", []):
            try:
                command_code = int(cmd["command_code"])
            except (KeyError, TypeError, ValueError) as e:
                logger.error(f"[{ADAPTER_NAME}] UID:{self.uid} command_code 無效，跳過此命令: {e}")
                continue

            try:
                ctrl_code = int(cmd.get("ctrl_code", 0x00))
            except (TypeError, ValueError):
                ctrl_code = 0x00

            req = bytearray([
                self.uid, command_code, ctrl_code,
                0x00, 0x00, 0x00, 0x00
            ])
            req.append(calc_checksum(req))
            cmds.append({
                "id":           cmd["id"],
                "command_code": command_code,
                "req":          bytes(req),
                "response_len": cmd.get("response_len", 93),
                "has_checksum": cmd.get("has_checksum", True)
            })
        return cmds

    def build_poll_read(self) -> tuple[bytes, dict]:
        if not self._poll_cmds:
            raise RuntimeError(f"[{ADAPTER_NAME}] 地圖未定義 read_commands")
        cmd = self._poll_cmds[self._poll_index]
        self._poll_index = (self._poll_index + 1) % len(self._poll_cmds)
        return cmd["req"], {"type": "poll", "cmd": cmd}

    def build_verify_read(self, key: str) -> tuple[bytes, dict]:
        if not self._poll_cmds:
            raise RuntimeError(f"[{ADAPTER_NAME}] 地圖未定義 read_commands，無法執行驗證回讀")
        cmd = self._poll_cmds[0]
        return cmd["req"], {"type": "poll", "cmd": cmd}

    def encode_write(self, key: str, value) -> bytes:
        target_info = None
        target_code = None
        for addr_str, info in self.settings.items():
            if info.get("key") == key:
                target_info = info
                target_code = int(addr_str, 16) if isinstance(addr_str, str) else int(addr_str)
                break

        if not target_info:
            raise ValueError(f"[{ADAPTER_NAME}] 地圖未定義寫入 key: {key}")

        fc = target_info.get("write_fc", 0xD0)

        if len(self._write_verify_cache) >= MAX_WRITE_CACHE:
            logger.warning(f"[{ADAPTER_NAME}] _write_verify_cache 異常膨脹 (>50)，強制清空")
            self._write_verify_cache.clear()
        self._write_verify_cache[key] = value

        if fc == 0xC0:
            req = bytearray([self.uid, 0xC0, target_code & 0xFF, 0x00, 0x00, 0x00, 0x00])
            req.append(calc_checksum(req))
            return bytes(req)

        if fc == 0xD0:
            try:
                scale = float(target_info.get("scale", 1))
            except (TypeError, ValueError):
                raise ValueError(f"[{ADAPTER_NAME}] key={key} scale 型別錯誤")

            # 🚀 處理 0x2D~0x30 (時控時間 HHMM 拆碼成 4 Bytes BCD)
            if target_code in (0x2D, 0x2E, 0x2F, 0x30):
                try:
                    hhmm = int(round(float(value)))
                    hour   = hhmm // 100
                    minute = hhmm % 100
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError(f"時間超出範圍: {hour:02d}:{minute:02d}")
                    d1 = hour // 10
                    d2 = hour % 10
                    d3 = minute // 10
                    d4 = minute % 10
                except (TypeError, ValueError) as e:
                    raise ValueError(f"[{ADAPTER_NAME}] 時控格式錯誤 (應為 HHMM, 例如 1430): {e}")

                req = bytearray([self.uid, 0xD0, target_code & 0xFF, d1, d2, d3, d4])
                req.append(calc_checksum(req))
                return bytes(req)

            # 常規數值寫入處理
            if isinstance(value, str):
                mapped_int = None
                for map_name, mapping in self.definitions.get("value_maps", {}).items():
                    for k, v in mapping.items():
                        if str(v) == str(value):
                            try:
                                mapped_int = int(k)
                            except ValueError:
                                mapped_int = k
                            break
                    if mapped_int is not None:
                        break

                if mapped_int is not None:
                    value = mapped_int
                else:
                    try:
                        value = float(value)
                    except ValueError:
                        raise ValueError(f"[{ADAPTER_NAME}] 無法將 '{value}' 轉換為數值")

            try:
                final_val = int(round(float(value) * scale))
            except (TypeError, ValueError) as e:
                raise ValueError(f"[{ADAPTER_NAME}] 寫入值無效: {value!r} ({e})")

            if not (-(2**31) <= final_val <= 2**32 - 1):
                raise ValueError(f"[{ADAPTER_NAME}] key={key} 計算值 {final_val} 超出 32-bit 範圍")

            d1 = d2 = d3 = d4 = 0x00
            if target_code <= 0x12:
                d4 = final_val & 0xFF
            elif target_code <= 0x2C:
                d3 = (final_val >> 8) & 0xFF
                d4 = final_val & 0xFF
            else:
                d1 = (final_val >> 24) & 0xFF
                d2 = (final_val >> 16) & 0xFF
                d3 = (final_val >> 8) & 0xFF
                d4 = final_val & 0xFF

            req = bytearray([self.uid, 0xD0, target_code & 0xFF, d1, d2, d3, d4])
            req.append(calc_checksum(req))
            return bytes(req)

        # 🚀 處理 0xDE 群控波特率設定 (廣播地址 0x00)
        if fc == 0xDE:
            try:
                baud_code = int(round(float(value)))
                if baud_code not in (1, 2, 3, 4):
                    raise ValueError("波特率碼必須為 1~4")
            except Exception as e:
                raise ValueError(f"[{ADAPTER_NAME}] 波特率碼無效: {e}")
            req = bytearray([0x00, 0xDE, 0x42, baud_code, 0x00, 0x00, 0x00])
            req.append(calc_checksum(req))
            return bytes(req)

        # 🚀 處理 0xDF 即時時鐘自動對時注入
        if fc == 0xDF:
            import datetime
            now = datetime.datetime.now()
            y, m, d, h, mn = now.year % 100, now.month, now.day, now.hour, now.minute
            req = bytearray([self.uid, 0xDF, y & 0xFF, m & 0xFF, d & 0xFF, h & 0xFF, mn & 0xFF])
            req.append(calc_checksum(req))
            return bytes(req)

        raise ValueError(f"[{ADAPTER_NAME}] 未支援的寫入命令碼: 0x{fc:02X}")

    def decode(self, raw_data: bytes, context: dict) -> dict:
        resp = bytearray(raw_data)
        cmd = context.get("cmd", {})
        has_chk = cmd.get("has_checksum", self._has_checksum)
        exp_len = cmd.get("response_len", self._response_len)
        cmd_id  = cmd.get("id", "")

        if len(resp) < 4:
            raise DataDecodeError(f"封包過短，無法解析或驗證校驗碼 ({len(resp)} bytes)")

        if resp[0] == self.uid and resp[1] == 0xEE:
            err_map = {
                1: "當前狀態不能完成操作",
                2: "不能識別的參數代碼",
                3: "參數數據溢出"
            }
            err_code = resp[2]
            raise DataDecodeError(f"設備拒絕: {err_map.get(err_code, f'未知代碼 {err_code}')}")

        if has_chk:
            if calc_checksum(resp[:-1]) != resp[-1]:
                raise DataDecodeError("Checksum 驗證失敗")

        if resp[0] != self.uid:
            raise DataDecodeError(f"UID 不符 (收到 {resp[0]}, 期望 {self.uid})")

        if exp_len > 0 and len(resp) < exp_len:
            raise DataDecodeError(f"封包不完整 ({len(resp)} < {exp_len} bytes)")

        # 🚀 ACK 攔截加入 0xDE, 0xDF
        if len(resp) >= 2 and resp[1] in (0xD0, 0xC0, 0xDE, 0xDF):
            return {"_write_ack": True}

        # 資料解析
        result = self.parser.parse(resp, cmd_id)
        # 🚨 [修復空白 Bug & 增強容錯] 強制從封包提取 RAW 數字，分開長度判斷確保絕對安全
        if cmd_id == "poll_b1":
            if len(resp) >= 14:
                result["baud_rate_raw"] = int(resp[13])
            if len(resp) >= 54:
                result["time_ctrl_flag_raw"] = int(resp[53])
        # 🚀 BCD 時控時間合拼：取出 16 個獨立 Byte，轉為 4 組 HHMM 整數 (供 HA number 實體使用)
        _time_fields = [
            ("time1_on",  "time1_on_hour_ten",  "time1_on_hour_one", "time1_on_min_ten",   "time1_on_min_one"),
            ("time1_off", "time1_off_hour_ten", "time1_off_hour_one", "time1_off_min_ten",  "time1_off_min_one"),
            ("time2_on",  "time2_on_hour_ten",  "time2_on_hour_one", "time2_on_min_ten",   "time2_on_min_one"),
            ("time2_off", "time2_off_hour_ten", "time2_off_hour_one", "time2_off_min_ten",  "time2_off_min_one"),
        ]

        for out_key, h10, h1, m10, m1 in _time_fields:
            if all(k in result for k in (h10, h1, m10, m1)):
                try:
                    hour   = int(result.pop(h10)) * 10 + int(result.pop(h1))
                    minute = int(result.pop(m10)) * 10 + int(result.pop(m1))
                    # 輸出整數，例：8 點 30 分 -> 830
                    result[out_key] = hour * 100 + minute
                except (TypeError, ValueError):
                    # 若發生異常則清理現場，不污染結果陣列
                    for k in (h10, h1, m10, m1):
                        result.pop(k, None)

        if self._write_verify_cache:
            result.update(self._write_verify_cache)
            self._write_verify_cache.clear()

        return result
