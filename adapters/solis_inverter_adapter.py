# =============================================================================
# solis_inverter_adapter.py
# 目的：只在既有 GenericAdapter 成功讀取 43110 後，補上 Solis 儲能控制的唯讀中文語意。
# 邊界：不實作 Modbus、CRC、transport、ACK、write、reconnect 或 scheduling。
# =============================================================================

from .generic_adapter import Adapter as GenericAdapter


ADAPTER_NAME = "solis_inverter"


class Adapter(GenericAdapter):
    """Solis 43110 的 read-only semantic overlay，標準 Modbus 行為完全沿用 GenericAdapter。"""

    STORAGE_CONTROL_COMMAND = "read_storage_control"
    STORAGE_CONTROL_KEY = "set_storage_control"
    STORAGE_MODE_KEY = "storage_control_mode"
    STORAGE_MODIFIER_KEY = "storage_control_modifiers"

    # 名稱依 report/031 的上游 hybrid model；不是本機 firmware 寫入認證。
    MODE_BITS = (
        (0, "自用模式"),
        (1, "分時模式"),
        (2, "離網模式"),
        (4, "備援／保留模式"),
        (6, "饋網優先"),
        (11, "削峰模式"),
    )
    MODIFIER_BITS = (
        (3, "電池喚醒功能（上游定義）"),
        (5, "市電充電功能（上游定義，極性未在本機確認）"),
        (7, "電池 OVC（上游定義）"),
        (8, "電池強制充電／削峰功能（上游定義）"),
        (9, "電池電流校正（上游定義）"),
        (10, "電池修復模式（上游定義）"),
    )
    KNOWN_BITS = frozenset(bit for bit, _ in MODE_BITS + MODIFIER_BITS)

    @classmethod
    def _decode_storage_control(cls, raw_value):
        """Return ordered mode/modifier text for a valid raw uint16, otherwise None."""
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            return None
        if not 0 <= raw_value <= 0xFFFF:
            return None

        modes = [name for bit, name in cls.MODE_BITS if raw_value & (1 << bit)]
        modifiers = [name for bit, name in cls.MODIFIER_BITS if raw_value & (1 << bit)]
        modifiers.extend(
            f"未知 Bit {bit}"
            for bit in range(16)
            if bit not in cls.KNOWN_BITS and raw_value & (1 << bit)
        )
        return "｜".join(modes) if modes else "無", "｜".join(modifiers) if modifiers else "無"

    def decode(self, raw_data: bytes, context: dict) -> dict:
        """Preserve GenericAdapter decode, then add text diagnostics for 43110 poll replies only."""
        result = super().decode(raw_data, context)
        if context.get("type") != "poll":
            return result

        command = context.get("cmd") or {}
        if command.get("id") != self.STORAGE_CONTROL_COMMAND:
            return result

        semantic = self._decode_storage_control(result.get(self.STORAGE_CONTROL_KEY))
        if semantic is not None:
            result[self.STORAGE_MODE_KEY], result[self.STORAGE_MODIFIER_KEY] = semantic
        return result
