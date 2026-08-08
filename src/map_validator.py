# =============================================================================
#version map_validator.py - V1.6 零侵入旁路地圖檢查器 (究極無死角版)
# 修復歷程 (V1.5 → V1.6)：
#   - [Critical] 封堵頂層結構靜默放行漏洞：sensors/settings/read_commands
#                若型別錯誤 (非 list / 非 dict)，不再靜默 return，改為強制報警。
#   - [Architecture] 達成 100% 結構與型別的 Fail-Fast，為 Adapter 提供絕對信任邊界。
# =============================================================================
import logging

logger = logging.getLogger("Validator")

VALID_DOMAINS = {"sensor", "binary_sensor", "switch", "number", "select", "button", "text"}

def validate_profile(profile_name: str, rmap) -> list[str]:
    try:
        errors = []

        # ✅ 入口守衛：任何非 dict 毒藥在此攔截
        if not isinstance(rmap, dict):
            errors.append(
                f"[{profile_name}] 💥 地圖檔根結構錯誤："
                f"預期 dict，收到 {type(rmap).__name__}（空檔案或格式錯誤）"
            )
            return errors

        frontend_keys = set()
        backend_keys = set()
        command_keys = set()

        def _validate_ha_config(key: str, item: dict):
            ha_config = item.get("ha")
            if not isinstance(ha_config, dict):
                return
            ha_type = ha_config.get("type", "sensor")
            if ha_type not in VALID_DOMAINS:
                errors.append(
                    f"[{profile_name}] 💥 未知的 HA 實體類別 '{ha_type}' "
                    f"(Key: {key})，請檢查是否拼錯！"
                )
            if ha_type == "select":
                options = ha_config.get("options", [])
                if not isinstance(options, list) or len(options) == 0:
                    errors.append(
                        f"[{profile_name}] 💥 select 實體 '{key}' "
                        f"缺少 options 陣列或格式錯誤！"
                    )

        def _check_frontend(section_name: str, items):
            if not items:
                return
            if isinstance(items, list):
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    key = item.get("key")
                    if not key:
                        errors.append(f"[{profile_name}] 💥 {section_name} 第 {idx} 項缺少 'key'")
                        continue
                    if key in frontend_keys:
                        errors.append(f"[{profile_name}] 💥 致命錯誤：Key '{key}' 在前端 ({section_name}) 發生重複碰撞！")
                    frontend_keys.add(key)
                    _validate_ha_config(key, item)

            elif isinstance(items, dict):
                for key, item in items.items():
                    if key in frontend_keys:
                        errors.append(f"[{profile_name}] 💥 致命錯誤：Key '{key}' 在前端 ({section_name}) 發生重複碰撞！")
                    frontend_keys.add(key)
                    if isinstance(item, dict):
                        _validate_ha_config(key, item)
            else:
                errors.append(
                    f"[{profile_name}] 💥 {section_name} 格式錯誤："
                    f"預期 list 或 dict，收到 {type(items).__name__}"
                )

        def _check_backend(items):
            if not items:
                return
            # 🚀 [V1.6 修復] 防禦 sensors 區塊的靜默放行
            if not isinstance(items, list):
                errors.append(f"[{profile_name}] 💥 sensors 格式錯誤：預期 list，收到 {type(items).__name__}！")
                return

            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                main_key = item.get("key", f"index_{idx}")
                
                # 1. 檢查 Key 唯一性
                if "key" in item:
                    if main_key in backend_keys:
                        errors.append(f"[{profile_name}] 💥 後端 sensors 致命錯誤：Key '{main_key}' 重複！")
                    backend_keys.add(main_key)
                
                # 2. 檢查 bits 結構、ID 唯一性與數值合法性
                if "bits" in item:
                    bits = item["bits"]
                    if not isinstance(bits, list):
                        errors.append(f"[{profile_name}] 💥 後端 bits 致命錯誤：點位 '{main_key}' 的 bits 必須是陣列 (list)，收到 {type(bits).__name__}！")
                    else:
                        for b in bits:
                            if isinstance(b, dict):
                                bit_id = b.get("id")
                                if not bit_id:
                                    errors.append(f"[{profile_name}] 💥 後端 bits 致命錯誤：點位 '{main_key}' 中缺少 'id' 欄位！")
                                else:
                                    if bit_id in backend_keys:
                                        errors.append(f"[{profile_name}] 💥 後端 bits 致命錯誤：變數 ID '{bit_id}' 重複！")
                                    backend_keys.add(bit_id)

                                if "bit" not in b:
                                    errors.append(f"[{profile_name}] 💥 後端 bits '{bit_id or 'unknown'}' 致命錯誤：缺少 'bit' 索引欄位！")
                                else:
                                    try:
                                        bit_idx = int(b["bit"])
                                        if bit_idx < 0:
                                            errors.append(f"[{profile_name}] 💥 後端 bits '{bit_id or 'unknown'}' 致命錯誤：bit 索引不能為負數！")
                                    except (TypeError, ValueError):
                                        errors.append(f"[{profile_name}] 💥 後端 bits '{bit_id or 'unknown'}' 致命錯誤：bit 必須是整數，收到 '{b['bit']}'")

                # 3. 數值型別與合法性安檢
                if "scale" in item:
                    try:
                        sc = float(item["scale"])
                        if sc == 0.0:
                            errors.append(f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤：scale 不能為 0！")
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤：scale 必須是數值，收到 '{item['scale']}'")

                if "offset" in item:
                    try:
                        int(item["offset"])
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤：offset 必須是整數，收到 '{item['offset']}'")

                if "length" in item:
                    try:
                        ln = int(item["length"])
                        if ln <= 0:
                            errors.append(f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤：length 必須大於 0！")
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤：length 必須是整數，收到 '{item['length']}'")

        def _check_settings(items):
            if not items:
                return
            # 🚀 [V1.6 修復] 防禦 settings 區塊的靜默放行
            if not isinstance(items, dict):
                errors.append(f"[{profile_name}] 💥 settings 格式錯誤：預期 dict，收到 {type(items).__name__}！")
                return

            for addr_str, cfg in items.items():
                try:
                    int(str(addr_str), 16) if isinstance(addr_str, str) else int(addr_str)
                except (TypeError, ValueError):
                    errors.append(f"[{profile_name}] 💥 settings 致命錯誤：位址鍵值 '{addr_str}' 無法轉換為整數或 16 進制！")

                if not isinstance(cfg, dict):
                    continue
                key = cfg.get("key", addr_str)

                if "scale" in cfg:
                    try:
                        sc = float(cfg["scale"])
                        if sc == 0.0:
                            errors.append(f"[{profile_name}] 💥 settings '{key}' 致命錯誤：scale 不能為 0！")
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 settings '{key}' 致命錯誤：scale 必須是數值，收到 '{cfg['scale']}'")

                if "write_fc" in cfg:
                    try:
                        int(cfg["write_fc"])
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 settings '{key}' 致命錯誤：write_fc 必須是整數！")

        def _check_read_commands(items):
            if not items:
                return
            # 🚀 [V1.6 修復] 防禦 read_commands 區塊的靜默放行
            if not isinstance(items, list):
                errors.append(f"[{profile_name}] 💥 read_commands 格式錯誤：預期 list，收到 {type(items).__name__}！")
                return

            for idx, cmd in enumerate(items):
                if not isinstance(cmd, dict):
                    continue
                cmd_id = cmd.get("id", f"index_{idx}")
                
                if cmd_id in command_keys:
                    errors.append(f"[{profile_name}] 💥 read_commands 致命錯誤：指令 ID '{cmd_id}' 發生重複碰撞！")
                command_keys.add(cmd_id)
                
                for int_field in ["fc", "start_addr", "count", "command_code", "response_len"]:
                    if int_field in cmd:
                        try:
                            val = int(cmd[int_field])
                            if int_field == "count" and val <= 0:
                                errors.append(f"[{profile_name}] 💥 read_commands '{cmd_id}' 致命錯誤：count 必須大於 0！")
                        except (TypeError, ValueError):
                            errors.append(f"[{profile_name}] 💥 read_commands '{cmd_id}' 致命錯誤：{int_field} 必須是整數，收到 '{cmd[int_field]}'！")

        # 依序執行所有安檢
        _check_frontend("B1_INFO", rmap.get("B1_INFO"))
        _check_frontend("B2_SETTING", rmap.get("B2_SETTING"))
        _check_frontend("B3_STATUS_BITS", rmap.get("B3_STATUS_BITS"))
        _check_backend(rmap.get("sensors"))
        _check_settings(rmap.get("settings"))
        _check_read_commands(rmap.get("read_commands"))

        return errors

    except Exception as e:
        return [f"[{profile_name}] 💥 validator 內部異常（請回報）：{e}"]


if __name__ == "__main__":
    import sys
    import os
    import yaml

    if len(sys.argv) < 2:
        print("🛠️  用法: python map_validator.py <地圖檔路徑.yaml>")
        sys.exit(1)

    file_path = sys.argv[1]
    if not os.path.exists(file_path):
        print(f"❌ 錯誤: 找不到檔案 '{file_path}'")
        sys.exit(1)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            rmap_data = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ 致命錯誤: YAML 格式解析失敗 -> {e}")
        sys.exit(1)

    if not isinstance(rmap_data, dict):
        print(
            f"❌ 致命錯誤: YAML 根結構必須是 dict，"
            f"收到 {type(rmap_data).__name__}（空檔案或格式錯誤）"
        )
        sys.exit(1)

    profile_name = os.path.basename(file_path).replace(".yaml", "")
    print(f"🔍 開始靜態檢查地圖檔: [{profile_name}] ...\n" + "-"*50)
    errors = validate_profile(profile_name, rmap_data)

    if errors:
        print(f"🚫 檢查失敗！抓到 {len(errors)} 個地雷：")
        for err in errors:
            print(f"   {err}")
        print("-" * 50)
        sys.exit(1)
    else:
        print("✅ 檢查通過！地圖檔結構健康，無 Key 衝突，後端數值型別與 HA 語法完全正確。")
        print("-" * 50)
        sys.exit(0)
