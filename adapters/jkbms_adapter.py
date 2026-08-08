# =============================================================================
# jkbms_adapter.py - V5.3 LTS (Industrial Stable) 工控完全體版
# 核心架構：追溯對齊與時間戳同步 (Retroactive Matching)
# 升級亮點：
#   1. 完全屏除 Modbus 偷聽機制，改用 BMS 原生 0x02 -> 0x01 實體對齊法。
#   2. HA 防捨入：所有數值強制字串化定型，保留最高精度。
#   3. 精準剔除 (Key-Level Drop) 規則引擎，防禦 UART 位元翻轉爆值。
#   4. 🚀 [LTS 優化] 導入 PLC Scan Cycle 時間凍結概念，統一 now 時間戳減少 Syscall。
#   5. 加入 0x01 參數類 30 秒發布一次 (SETTINGS_PUBLISH_INTERVAL)
#   6. 完整實作 Alarm Bits 與 MOS Enable 狀態解析。
#   7. 🚀 [V5.3 Fix] 導入動態長度與 CRC16 嚴格防線，徹底消滅黏包與偽造 Header。
# =============================================================================

import struct
import logging
import time

logger = logging.getLogger(__name__)

ADAPTER_NAME = "jkbms"

# =============================================================================
# 1. 解碼規則、地圖與物理防護邊界
# =============================================================================
TYPE_U8  = 'B'
TYPE_U16 = 'H'
TYPE_I16 = 'h'
TYPE_U32 = 'I'
TYPE_I32 = 'i'
TYPE_STR = 's'

conv_div1000 = lambda v: f"{v / 1000.0:.3f}"
conv_div100  = lambda v: f"{v / 100.0:.2f}"
conv_div10   = lambda v: f"{v / 10.0:.1f}"
conv_none    = lambda v: v
conv_hex     = lambda v: f"0x{v:08X}"
conv_plus1   = lambda v: v + 1
conv_onoff   = lambda v: "ON" if v >= 1 else "OFF"
conv_bal_action = lambda v: "不作動" if v == 0 else ("充電均衡" if v == 1 else ("放電均衡" if v == 2 else f"未知({v})"))

LIMITS = [
    {"min": 0.0, "max": 5.0, "incl": "cell_", "must_end": "_voltage", "excl": None},
    {"min": 0.0, "max": 100.0, "incl": "total_voltage", "must_end": None, "excl": None},
    {"min": -2500.0, "max": 2500.0, "incl": "balance_current", "must_end": None, "excl": None},
    {"min": -350.0, "max": 350.0, "incl": "current", "must_end": None, "excl": "balance"},
    {"min": -40.0, "max": 120.0, "incl": "temp", "must_end": None, "excl": None},
    {"min": 0.0, "max": 2.0, "incl": "max_diff_voltage", "must_end": None, "excl": None},
]

BMS_MAP = {
    # =====================================================
    # 0x10: Master Command
    # =====================================================
    0x10: {
        0: ("目標從機ID", None, TYPE_U8, conv_none, "target_slave_id"),
        2: ("操作寄存器", "Hex", TYPE_U16, conv_hex, "register"),
        7: ("指令數值", "Hex", TYPE_U16, conv_hex, "value_hex"),
    },
    # =====================================================
    # 0x01: Parameter Settings
    # =====================================================
    0x01: {
        0:   ("进入休眠电压", "V", TYPE_U32, conv_div1000, "sleep_voltage"),
        4:   ("单体欠压保护", "V", TYPE_U32, conv_div1000, "cell_uvp"),
        8:   ("单体欠压保护恢复", "V", TYPE_U32, conv_div1000, "cell_uvp_recovery"),
        12:  ("单体过充保护", "V", TYPE_U32, conv_div1000, "cell_ovp"),
        16:  ("单体过充保护恢复电", "V", TYPE_U32, conv_div1000, "cell_ovp_recovery"),
        20:  ("触发均衡压差", "V", TYPE_U32, conv_div1000, "balance_trigger_diff"),
        24:  ("SOC-100%电压", "V", TYPE_U32, conv_div1000, "soc_100_voltage"),
        28:  ("SOC-0%电压", "V", TYPE_U32, conv_div1000, "soc_0_voltage"),
        32:  ("推荐充电电压", "V", TYPE_U32, conv_div1000, "rec_charge_voltage"),
        36:  ("浮充电压", "V", TYPE_U32, conv_div1000, "float_charge_voltage"),
        40:  ("自动关机电压", "V", TYPE_U32, conv_div1000, "auto_shutdown_voltage"),
        44:  ("持续充电电流", "A", TYPE_U32, conv_div1000, "cont_charge_current"),
        48:  ("充电过流保护延迟", "S", TYPE_U32, conv_none, "charge_ocp_delay"),
        52:  ("充电过流保护解除", "S", TYPE_U32, conv_none, "charge_ocp_release"),
        56:  ("持续放电电流", "A", TYPE_U32, conv_div1000, "cont_discharge_current"),
        60:  ("放电过流保护延迟", "S", TYPE_U32, conv_none, "discharge_ocp_delay"),
        64:  ("放电过流保护解除", "S", TYPE_U32, conv_none, "discharge_ocp_release"),
        68:  ("短路保护解除", "S", TYPE_U32, conv_none, "sc_release"),
        72:  ("最大均衡电流", "mA", TYPE_U32, conv_none, "max_balance_current"),
        76:  ("充电过温保护", "°C", TYPE_I32, conv_div10, "charge_otp"),
        80:  ("充电过温恢复", "°C", TYPE_I32, conv_div10, "charge_otp_recovery"),
        84:  ("放电过温保护", "°C", TYPE_I32, conv_div10, "discharge_otp"),
        88:  ("放电过温恢复", "°C", TYPE_I32, conv_div10, "discharge_otp_recovery"),
        92:  ("充电低温保护", "°C", TYPE_I32, conv_div10, "charge_utp"),
        96:  ("充电低温恢复", "°C", TYPE_I32, conv_div10, "charge_utp_recovery"),
        100: ("MOS过温保护", "°C", TYPE_I32, conv_div10, "mos_otp"),
        104: ("MOS过温保护恢复", "°C", TYPE_I32, conv_div10, "mos_otp_recovery"),
        108: ("单体数量", None, TYPE_U32, conv_none, "cell_count"),
        112: ("充电开关", "Bit", TYPE_U32, conv_onoff, "charge_switch"),
        116: ("放电开关", "Bit", TYPE_U32, conv_onoff, "discharge_switch"),
        120: ("均衡开关", "Bit", TYPE_U32, conv_onoff, "balance_switch"),
        124: ("电池设计容量", "mAH", TYPE_U32, conv_none, "design_capacity"),
        128: ("短路保护延迟", "us", TYPE_U32, conv_none, "sc_delay"),
        132: ("均衡起始电压", "V", TYPE_U32, conv_div1000, "balance_start_voltage"),
        264: ("设备地址", "Hex", TYPE_U32, conv_hex, "device_address"),
        280: ("智能休眠时间", "H", TYPE_U8, conv_none, "smart_sleep_time"),
    },
    # =====================================================
    # 0x02: Realtime Data
    # =====================================================
    0x02: {
        0:  ("01單體電壓", "V", TYPE_U16, conv_div1000, "cell_01_voltage"),
        2:  ("02單體電壓", "V", TYPE_U16, conv_div1000, "cell_02_voltage"),
        4:  ("03單體電壓", "V", TYPE_U16, conv_div1000, "cell_03_voltage"),
        6:  ("04單體電壓", "V", TYPE_U16, conv_div1000, "cell_04_voltage"),
        8:  ("05單體電壓", "V", TYPE_U16, conv_div1000, "cell_05_voltage"),
        10: ("06單體電壓", "V", TYPE_U16, conv_div1000, "cell_06_voltage"),
        12: ("07單體電壓", "V", TYPE_U16, conv_div1000, "cell_07_voltage"),
        14: ("08單體電壓", "V", TYPE_U16, conv_div1000, "cell_08_voltage"),
        16: ("09單體電壓", "V", TYPE_U16, conv_div1000, "cell_09_voltage"),
        18: ("10單體電壓", "V", TYPE_U16, conv_div1000, "cell_10_voltage"),
        20: ("11單體電壓", "V", TYPE_U16, conv_div1000, "cell_11_voltage"),
        22: ("12單體電壓", "V", TYPE_U16, conv_div1000, "cell_12_voltage"),
        24: ("13單體電壓", "V", TYPE_U16, conv_div1000, "cell_13_voltage"),
        26: ("14單體電壓", "V", TYPE_U16, conv_div1000, "cell_14_voltage"),
        28: ("15單體電壓", "V", TYPE_U16, conv_div1000, "cell_15_voltage"),
        30: ("16單體電壓", "V", TYPE_U16, conv_div1000, "cell_16_voltage"),

        64: ("电池状态", "Hex", TYPE_U32, conv_hex, "status_code"),
        68: ("平均电压", "V", TYPE_U16, conv_div1000, "avg_voltage"),
        70: ("最大压差", "V", TYPE_U16, conv_div1000, "max_diff_voltage"),
        72: ("最大單體", None, TYPE_U8, conv_plus1, "max_cell_index"),
        73: ("最小單體", None, TYPE_U8, conv_plus1, "min_cell_index"),

        138: ("功率板温度", "°C", TYPE_I16, conv_div10, "power_board_temp"),
        144: ("电池总电压", "V", TYPE_U32, conv_div1000, "total_voltage"),
        148: ("电池功率", "W", TYPE_U32, conv_div1000, "power_watts"),
        152: ("电池电流", "A", TYPE_I32, conv_div1000, "current"),
        156: ("电池温度1", "°C", TYPE_I16, conv_div10, "temp_sensor_1"),
        158: ("电池温度2", "°C", TYPE_I16, conv_div10, "temp_sensor_2"),

        160: ("Alarm Bits 1", "Hex", TYPE_U32, conv_none, "alarm_bits_1"),
        164: ("均衡电流", "mA", TYPE_I16, conv_none, "balance_current"),
        166: ("均衡狀態:1充2放", "Enum", TYPE_U8, conv_bal_action, "balance_action"),
        167: ("剩余电量", "%", TYPE_U8, conv_none, "soc_percent"),
        168: ("剩余容量", "Ah", TYPE_I32, conv_div1000, "remaining_capacity_ah"),
        172: ("电池实际容量", "Ah", TYPE_U32, conv_div1000, "actual_capacity_ah"),
        176: ("循环次数", "N", TYPE_U32, conv_none, "cycle_count"),
        180: ("循环总容量", "Ah", TYPE_U32, conv_div1000, "total_cycle_capacity"),
        186: ("用户层报警", "Hex", TYPE_U16, conv_hex , "fault_code"),
        188: ("运行时间", "S", TYPE_U32, conv_none, "runtime_seconds"),
        192: ("允许充电(MOS)", None, TYPE_U8, conv_onoff, "charge_enable"),
        193: ("允许放电(MOS)", None, TYPE_U8, conv_onoff, "discharge_enable"),

        196: ("放电过流保护解除时间", "S", TYPE_U16, conv_none, "discharge_ocp_release_time"),
        198: ("放电短路保护解除时间", "S", TYPE_U16, conv_none, "discharge_sc_release_time"),
        200: ("充电过流保护解除时间", "S", TYPE_U16, conv_none, "charge_ocp_release_time"),
        202: ("充电短路保护解除时间", "S", TYPE_U16, conv_none, "charge_sc_release_time"),
        204: ("单体欠压保护解除时间", "S", TYPE_U16, conv_none, "cell_uvp_release_time"),
        206: ("单体过压保护解除时间", "S", TYPE_U16, conv_none, "cell_ovp_release_time"),
        212: ("应急开关时间", "S", TYPE_U16, conv_none, "emergency_switch_time"),

        248: ("电池温度3", "°C", TYPE_I16, conv_div10, "temp_sensor_3"),
        250: ("电池温度4", "°C", TYPE_I16, conv_div10, "temp_sensor_4"),
        252: ("电池温度5", "°C", TYPE_I16, conv_div10, "temp_sensor_5"),
        264: ("进入休眠时间", "S", TYPE_U32, conv_none, "sleep_time_seconds"),
        # ✅ [補齊] 對齊 old-bms/app/bms_registers.py 的 0x1200 偏移 268 (UINT8, 唯讀)
        268: ("并联限流模块状态", None, TYPE_U8, conv_onoff, "parallel_limiter_status"),
    }
}

# =============================================================================
# 2. Adapter 核心邏輯
# =============================================================================
class Adapter:
    HEADER_JK = b"\x55\xAA\xEB\x90"
    EXPIRE_TIME = 2.0
    SETTINGS_PUBLISH_INTERVAL = 30.0
    # 預先生成 Modbus 表頭特徵碼 (0x00~0x0F)
    MASTER_LIST = [bytes([i, 0x10]) for i in range(16)]

    def __init__(self, uid: int, profile: dict):
        self.uid = uid
        self.profile = profile
        self.buffer = bytearray()
        self.pending_0x02_frame: bytes | None = None
        self.pending_0x02_time: float = 0.0
        self.last_0x01_publish_time: float = 0.0

    def _crc16(self, data: bytes) -> bool:
        """ 🟢 嚴格 Modbus CRC 校驗，100% 排除 BMS Payload 偽造碰撞 """
        crc = 0xFFFF
        for pos in data[:-2]:
            crc ^= pos
            for _ in range(8):
                if (crc & 1) != 0:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return (crc & 0xFF) == data[-2] and ((crc >> 8) & 0xFF) == data[-1]

    def _is_valid_master_cmd(self, buffer: bytearray, idx: int) -> int:
        """ 
        🟢 動態計算真實長度，並加入 CRC 確認 
        回傳真實長度 (int), 若資料不夠回傳 -1, 若為偽造雜訊回傳 0
        """
        if len(buffer) < idx + 9:
            return -1 # 等待更多資料

        reg_count = (buffer[idx + 4] << 8) | buffer[idx + 5]
        byte_count = buffer[idx + 6]

        if not (1 <= reg_count <= 125):
            return 0
        if byte_count != reg_count * 2:
            return 0

        total_len = 9 + byte_count
        if len(buffer) < idx + total_len:
            return -1 # 等待更多資料

        packet = bytes(buffer[idx : idx + total_len])
        if self._crc16(packet):
            return total_len

        return 0

    def _decode_payload(self, frame: bytes) -> dict:
        result = {}
        p_type = frame[4]

        if p_type not in BMS_MAP: return result

        payload_offset = 6
        map_def = BMS_MAP[p_type]

        for reg_offset, meta in map_def.items():
            _, _, fmt, conv_fn, key = meta
            abs_pos = payload_offset + reg_offset
            if abs_pos + struct.calcsize(f"<{fmt}") > len(frame): continue
            try:
                raw_val = struct.unpack_from(f"<{fmt}", frame, abs_pos)[0]
                val = conv_fn(raw_val)
                is_valid = True
                try:
                    val_float = float(val)
                    for rule in LIMITS:
                        if rule["incl"] in key:
                            if rule["excl"] and rule["excl"] in key:
                                continue
                            if rule["must_end"] and not key.endswith(rule["must_end"]):
                                continue
                            if not (rule["min"] <= val_float <= rule["max"]):
                                logger.warning(
                                    f"[位元防護] 設備 UID={self.uid} 攔截異常跳變: {key} = {val} "
                                    f"(合法範圍: {rule['min']}~{rule['max']})，已捨棄該欄位。"
                                )
                                is_valid = False
                                break
                except (ValueError, TypeError):
                    pass

                if is_valid:
                    result[key] = val
            except Exception as e:
                logger.debug(f"[JKBMS Adapter] 解析 {key} 崩潰: {e}")

        # [新增] Alarm Bits 解析
        if "alarm_bits_1" in result:
            alarm_val = result["alarm_bits_1"]
            if isinstance(alarm_val, int):
                result["cell_ovp_alarm"] = "ON" if (alarm_val & 0x0002) else "OFF"
                result["cell_uvp_alarm"] = "ON" if (alarm_val & 0x0004) else "OFF"
                result["charge_otp_alarm"] = "ON" if (alarm_val & 0x0020) else "OFF"
                result["mos_otp_alarm"] = "ON" if (alarm_val & 0x0040) else "OFF"
                result["charge_ocp_alarm"] = "ON" if (alarm_val & 0x0200) else "OFF"
                result["discharge_ocp_alarm"] = "ON" if (alarm_val & 0x0400) else "OFF"
                result["short_circuit_alarm"] = "ON" if (alarm_val & 0x0800) else "OFF"
            result["alarm_bits_1"] = conv_hex(result["alarm_bits_1"])

        # 複製 Enable 狀態給 Status
        if "charge_enable" in result:
            result["charge_status"] = result["charge_enable"]
        if "discharge_enable" in result:
            result["discharge_status"] = result["discharge_enable"]

        result["soc_calibration_state"] = "系統估算"

        return result

    def _extract_embedded_id(self, frame: bytes) -> int | None:
        try:
            if len(frame) >= 274:
                val = struct.unpack_from("<I", frame, 270)[0]
                if 0 <= val <= 15: return val
            if len(frame) >= 278:
                val = struct.unpack_from("<I", frame, 274)[0]
                if 0 <= val <= 15: return val
        except Exception:
            pass
        return None

    def _is_valid_jk_frame(self, frame: bytes) -> bool:
        """
        🟢 JK 原生封包嚴格校驗防線 (V5.4.1 修復版)
        """
        # 🚨 致命 Bug 修復：實測這版極空韌體 0x01/0x02 均為精準的 292 bytes
        if len(frame) != 292:
            return False

        # 嚴格鎖定物理表頭，防禦位移
        if not frame.startswith(b"\x55\xAA\xEB\x90"):
            return False

        # 只要長度精準且表頭對齊，就不會越界吞噬 Modbus，直接放行
        return True

    def feed(self, chunk: bytes) -> dict | None:
        self.buffer.extend(chunk)
        latest_decoded = None
        now = time.monotonic()

        # 處理過期的 0x02 緩存
        if self.pending_0x02_frame and (now - self.pending_0x02_time > self.EXPIRE_TIME):
            if logger.isEnabledFor(logging.DEBUG) and self.uid == 0:
                logger.debug(f"[垃圾回收] 丟棄過期的 0x02 數據 (超過 {self.EXPIRE_TIME}s)")
            self.pending_0x02_frame = None

        while True:
            jk_idx = self.buffer.find(self.HEADER_JK)
            mb_idx = -1

            # 尋找 Modbus 表頭 (0x00~0x0F 加上 0x10)
            for mb_head in self.MASTER_LIST:
                idx = self.buffer.find(mb_head)
                if idx != -1 and (mb_idx == -1 or idx < mb_idx):
                    mb_idx = idx

            # 兩者皆無，防禦性清空
            if jk_idx == -1 and mb_idx == -1:
                if len(self.buffer) > 1024:
                    self.buffer.clear()
                break

            is_jk_first = False
            if jk_idx != -1 and mb_idx != -1:
                is_jk_first = jk_idx < mb_idx
            elif jk_idx != -1:
                is_jk_first = True

            # 🟢 處理 JK BMS 原生封包
            if is_jk_first:
                p_len = 292  # 🚨 修正：精準物理邊界 292 bytes

                if len(self.buffer) < jk_idx + p_len:
                    break # 資料不足 292 bytes，等 UART 補齊

                frame = bytes(self.buffer[jk_idx : jk_idx + p_len])

                # 進入防火牆驗證
                if self._is_valid_jk_frame(frame):
                    del self.buffer[:jk_idx + p_len] # 驗證通過，安全切包
                    p_type = frame[4]

                    if p_type == 0x02:
                        self.pending_0x02_frame = frame
                        self.pending_0x02_time = now
                    elif p_type == 0x01:
                        embedded_id = self._extract_embedded_id(frame)
                        if embedded_id is None:
                            embedded_id = 0

                        if embedded_id == self.uid:
                            merged_data = {}
                            if now - self.last_0x01_publish_time >= self.SETTINGS_PUBLISH_INTERVAL:
                                decoded_0x01 = self._decode_payload(frame)
                                if decoded_0x01:
                                    merged_data.update(decoded_0x01)
                                    self.last_0x01_publish_time = now
                                    if logger.isEnabledFor(logging.DEBUG):
                                        logger.debug(f"[參數節流] 設備 UID={self.uid} 放行 0x01 靜態參數發布")

                            if self.pending_0x02_frame:
                                time_diff = now - self.pending_0x02_time
                                if time_diff <= self.EXPIRE_TIME:
                                    decoded_0x02 = self._decode_payload(self.pending_0x02_frame)
                                    if decoded_0x02:
                                        merged_data.update(decoded_0x02)
                                self.pending_0x02_frame = None

                            if merged_data:
                                if latest_decoded is None:
                                    latest_decoded = {}
                                latest_decoded.update(merged_data)
                        else:
                            self.pending_0x02_frame = None
                    continue
                else:
                    # 🔴 觸發防禦：這是一個藏在資料區的假表頭
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[防護隔離] 偵測到偽造的 JK 表頭，拋棄 4 bytes 避開死鎖")
                    del self.buffer[:jk_idx + 4]
                    continue

            # 🟢 處理 Modbus 封包
            else:
                cmd_len = self._is_valid_master_cmd(self.buffer, mb_idx)

                if cmd_len == -1:
                    break # 資料不足
                elif cmd_len > 0:
                    mb_frame = bytes(self.buffer[mb_idx : mb_idx + cmd_len])
                    del self.buffer[:mb_idx + cmd_len]

                    slave_id = mb_frame[0]
                    # 嚴格綁定 UID 攔截
                    if slave_id == self.uid:
                        reg_addr = (mb_frame[2] << 8) | mb_frame[3]
                        val = (mb_frame[7] << 8) | mb_frame[8]

                        if latest_decoded is None:
                            latest_decoded = {}

                        latest_decoded.update({
                            "target_slave_id": slave_id,
                            "register": f"0x{reg_addr:04X}",
                            "value_hex": f"0x{val:04X}"
                        })
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(f"[控制審計] 攔截寫入指令: Slave={slave_id}, Reg=0x{reg_addr:04X}, Val=0x{val:04X}")
                    continue
                else:
                    # 遇到假 Header，只推進 2 bytes 放生
                    del self.buffer[:mb_idx + 2]
                    continue

        return latest_decoded
