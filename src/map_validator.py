# =============================================================================
#version map_validator.py - V1.7 零侵入旁路地圖檢查器 (究極無死角版)
# 修復歷程 (V1.5 → V1.6)：
#   - [Critical] 封堵頂層結構靜默放行漏洞：sensors/settings/read_commands
#                若型別錯誤 (非 list / 非 dict)，不再靜默 return，改為強制報警。
#   - [Architecture] 達成 100% 結構與型別的 Fail-Fast，為 Adapter 提供絕對信任邊界。
# 修復歷程 (V1.6 → V1.7)：
#   - [Protocol] 追記：隨 report/037-047 的 FC15 施工新增 _check_coil_groups()，
#                當時未同步檔頭，此處補記。coil_groups 是唯一會讓多顆 relay
#                以單一幀原子切換的區塊，YAML 寫錯的代價是「一次動到不該動的
#                點位」，故採全量 Fail-Fast，掛載期即攔截：
#                  結構    groups 須為 dict；group key 須為非空字串；
#                          每組須為 dict。
#                  位址    start_addr／count 型別與範圍；start_addr + count
#                          不得超過 65536。
#                  成員    members 須為非空 list、不得重複、每個 member 都必須
#                          存在於 settings，且必須保留自己的 FC05 單路設定
#                          （群組寫入不得取消單點控制能力）；
#                          count 必須等於 len(members)。
#                  verify  verify_command_id 必須指向既有 read_commands、
#                          必須是 FC01，且其位址範圍須涵蓋整個群組 ——
#                          否則 verify 讀不到自己剛寫的 coil。
#                  states  須為非空 dict；每個 state 長度須等於 count；
#                          值只接受 bool／ON／OFF；同組內不得有重複的 coil
#                          vector（否則回讀時無法反查是哪個 state）。
#                  重疊    跨組共用同一顆 coil 位址一律拒絕。
#                另於 settings 檢查加入 key 重複偵測 —— settings 是「以位址為
#                鍵」的 dict，key 重複會讓 coil_groups 的 member → 位址對應
#                取到不確定的那一筆。
#   - [Critical] 封堵 report/052 的缺陷 F1（靜默資料消失）：sensors[].command_id
#                若打錯字或指向不存在的 read_command，本檢查器原本放行，而
#                generic_adapter._extract_data() 的輪詢分支以
#                `sensor.get("command_id") != cmd['id']: continue` 略過它 ——
#                該 sensor 對「每一個」command 都不相干，於是永遠不被解碼，
#                且因為不計入 declared，V2.5 的「丟棄 N/M 個 sensor」彙總
#                WARNING 也不會觸發。結果是：HA 實體照常建立卻永遠沒有數值，
#                全程零日誌。此版新增 _check_sensor_command_refs()，把 sensor
#                與 read_commands 的參照關係在掛載期就攔下來。
#                本次只新增檢查，未更動任何既有檢查的判準與訊息；Adapter 與
#                BusMaster 完全不動。
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
        setting_by_key = {}
        command_by_id = {}

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

                try:
                    addr = int(str(addr_str), 16) if isinstance(addr_str, str) else int(addr_str)
                except (TypeError, ValueError):
                    addr = None
                if key in setting_by_key:
                    errors.append(f"[{profile_name}] 💥 settings key '{key}' 重複，coil_groups 無法安全對應位址！")
                else:
                    setting_by_key[key] = (addr, cfg)

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
                command_by_id[cmd_id] = cmd
                
                for int_field in ["fc", "start_addr", "count", "command_code", "response_len"]:
                    if int_field in cmd:
                        try:
                            val = int(cmd[int_field])
                            if int_field == "count" and val <= 0:
                                errors.append(f"[{profile_name}] 💥 read_commands '{cmd_id}' 致命錯誤：count 必須大於 0！")
                        except (TypeError, ValueError):
                            errors.append(f"[{profile_name}] 💥 read_commands '{cmd_id}' 致命錯誤：{int_field} 必須是整數，收到 '{cmd[int_field]}'！")

        def _check_sensor_command_refs(items, commands):
            """封堵 F1：sensor 與 read_commands 的參照關係必須在掛載期成立。

            必須在 _check_read_commands() 之後呼叫（command_by_id 才已填好）。
            判準刻意保守，只攔「一定會造成靜默資料消失」的兩種情況：
              1. profile 有 read_commands，但 sensor 的 command_id 缺漏或指不到；
              2. profile 沒有 read_commands，sensor 卻宣告了 command_id。
            監聽軌（feed() 型 adapter）的 profile 兩者皆無，不受影響。
            """
            if not items or not isinstance(items, list):
                return

            has_commands = isinstance(commands, list) and bool(commands)
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                main_key = item.get("key", f"index_{idx}")
                cmd_id = item.get("command_id")

                if not has_commands:
                    if cmd_id:
                        errors.append(
                            f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤："
                            f"宣告了 command_id '{cmd_id}'，但本 profile 沒有任何 read_commands！"
                        )
                    continue

                if not cmd_id:
                    errors.append(
                        f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤："
                        f"缺少 command_id，輪詢解碼會永遠略過它（該實體將永遠沒有數值）！"
                    )
                elif cmd_id not in command_by_id:
                    errors.append(
                        f"[{profile_name}] 💥 sensors '{main_key}' 致命錯誤："
                        f"command_id '{cmd_id}' 不存在於 read_commands "
                        f"（可用的有：{sorted(command_by_id)}），"
                        f"輪詢解碼會永遠略過它（該實體將永遠沒有數值）！"
                    )

        def _check_coil_groups(groups):
            """Validate profile-defined FC15 groups without changing legacy maps."""
            if groups is None:
                return
            if not isinstance(groups, dict):
                errors.append(f"[{profile_name}] 💥 coil_groups 格式錯誤：預期 dict，收到 {type(groups).__name__}！")
                return

            used_addresses = {}
            for group_key, group in groups.items():
                label = f"coil_groups.{group_key}"
                if not isinstance(group_key, str) or not group_key:
                    errors.append(f"[{profile_name}] 💥 coil_groups group key 必須是非空字串！")
                if not isinstance(group, dict):
                    errors.append(f"[{profile_name}] 💥 {label} 必須是 dict！")
                    continue

                try:
                    if isinstance(group.get("start_addr"), bool):
                        raise ValueError("bool 不是合法 address")
                    start_addr = int(group["start_addr"])
                    if not 0 <= start_addr <= 0xFFFF:
                        raise ValueError("必須在 0..65535")
                except (KeyError, TypeError, ValueError) as e:
                    errors.append(f"[{profile_name}] 💥 {label}.start_addr 無效：{e}")
                    start_addr = None

                try:
                    if isinstance(group.get("count"), bool):
                        raise ValueError("bool 不是合法 count")
                    count = int(group["count"])
                    if not 1 <= count <= 2000:
                        raise ValueError("必須在 1..2000")
                except (KeyError, TypeError, ValueError) as e:
                    errors.append(f"[{profile_name}] 💥 {label}.count 無效：{e}")
                    count = None

                if start_addr is not None and count is not None and start_addr + count > 0x10000:
                    errors.append(f"[{profile_name}] 💥 {label} 位址超界：start_addr + count 超過 65536！")

                members = group.get("members")
                if not isinstance(members, list) or not members:
                    errors.append(f"[{profile_name}] 💥 {label}.members 必須是非空 list！")
                    members = []
                if count is not None and len(members) != count:
                    errors.append(f"[{profile_name}] 💥 {label}: count ({count}) 必須等於 members 長度 ({len(members)})！")
                if len(set(members)) != len(members):
                    errors.append(f"[{profile_name}] 💥 {label}.members 不得重複！")

                for index, member in enumerate(members):
                    if not isinstance(member, str) or not member:
                        errors.append(f"[{profile_name}] 💥 {label}.members[{index}] 必須是非空 setting key！")
                        continue
                    member_info = setting_by_key.get(member)
                    if member_info is None:
                        errors.append(f"[{profile_name}] 💥 {label} member '{member}' 不存在於 settings！")
                        continue
                    member_addr, member_cfg = member_info
                    if member_addr is None or start_addr is None or member_addr != start_addr + index:
                        errors.append(
                            f"[{profile_name}] 💥 {label}.members 必須按連續位址排列："
                            f"'{member}' 位址 {member_addr}，預期 {None if start_addr is None else start_addr + index}！"
                        )
                    try:
                        member_fc = int(member_cfg.get("write_fc", 6))
                    except (TypeError, ValueError):
                        member_fc = None
                    if member_fc != 5:
                        errors.append(f"[{profile_name}] 💥 {label} member '{member}' 必須保留 FC05 單路設定！")

                verify_command_id = group.get("verify_command_id")
                verify_command = command_by_id.get(verify_command_id)
                if not isinstance(verify_command_id, str) or not verify_command:
                    errors.append(f"[{profile_name}] 💥 {label}.verify_command_id 必須指向既有 read_commands！")
                else:
                    try:
                        verify_fc = int(verify_command.get("fc"))
                        verify_start = int(verify_command.get("start_addr"))
                        verify_count = int(verify_command.get("count"))
                        if verify_fc != 1:
                            errors.append(f"[{profile_name}] 💥 {label}.verify_command_id 必須使用 FC01，收到 FC{verify_fc}！")
                        if (start_addr is not None and count is not None
                                and not (verify_start <= start_addr
                                         and start_addr + count <= verify_start + verify_count)):
                            errors.append(f"[{profile_name}] 💥 {label} 不在 verify command 的 FC01 位址範圍內！")
                    except (TypeError, ValueError):
                        errors.append(f"[{profile_name}] 💥 {label}.verify_command_id 指向的 command 位址/數量無效！")

                states = group.get("states")
                if not isinstance(states, dict) or not states:
                    errors.append(f"[{profile_name}] 💥 {label}.states 必須是非空 dict！")
                    states = {}
                seen_vectors = set()
                for state_name, vector in states.items():
                    state_label = f"{label}.states.{state_name}"
                    if not isinstance(state_name, str) or not state_name:
                        errors.append(f"[{profile_name}] 💥 {label}.states key 必須是非空字串！")
                    if not isinstance(vector, list):
                        errors.append(f"[{profile_name}] 💥 {state_label} 必須是 list！")
                        continue
                    if count is not None and len(vector) != count:
                        errors.append(f"[{profile_name}] 💥 {state_label} 長度必須等於 count ({count})！")
                    normalized = []
                    valid_vector = True
                    for value in vector:
                        if isinstance(value, bool):
                            normalized.append(value)
                        elif isinstance(value, str) and value.upper() in {"ON", "OFF"}:
                            normalized.append(value.upper() == "ON")
                        else:
                            errors.append(f"[{profile_name}] 💥 {state_label} 只能使用 bool、ON 或 OFF，收到 {value!r}！")
                            valid_vector = False
                    if valid_vector:
                        marker = tuple(normalized)
                        if marker in seen_vectors:
                            errors.append(f"[{profile_name}] 💥 {label}.states 不得有重複的 coil vector！")
                        seen_vectors.add(marker)

                if start_addr is not None and count is not None:
                    for address in range(start_addr, start_addr + count):
                        prior = used_addresses.get(address)
                        if prior is not None:
                            errors.append(
                                f"[{profile_name}] 💥 coil_groups overlap：{label} 與 {prior} 共用 coil {address}！"
                            )
                        else:
                            used_addresses[address] = label

        # 依序執行所有安檢
        _check_frontend("B1_INFO", rmap.get("B1_INFO"))
        _check_frontend("B2_SETTING", rmap.get("B2_SETTING"))
        _check_frontend("B3_STATUS_BITS", rmap.get("B3_STATUS_BITS"))
        _check_backend(rmap.get("sensors"))
        _check_settings(rmap.get("settings"))
        _check_read_commands(rmap.get("read_commands"))
        # V1.7：必須排在 _check_read_commands 之後，command_by_id 才是完整的。
        _check_sensor_command_refs(rmap.get("sensors"), rmap.get("read_commands"))
        _check_coil_groups(rmap.get("coil_groups"))

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
