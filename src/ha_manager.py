# =============================================================================
# ha_manager.py - V3.0.14 真・工業防護版 (UI 隔離閉環版)
# 模組名稱：Home Assistant MQTT Discovery 管理模組
# 升級亮點：無損升級，相容舊有地圖檔，全面導入防禦性編程 (Defensive Programming)
# 修復歷程：
#   - [V3.0.0] 新增獨立 Connectivity 實體與 Text (字串輸入) 實體支援
#   - [V3.0.1] 修復 Switch 狀態綁定：state_on/off 預設動態跟隨 payload_on/off
#   - [V3.0.2] 終極型別防禦：強制字串化 payload_on/off，阻斷 HA 因 YAML 型態導致的 Schema 拒絕
#   - [V3.0.3] 修復 Button 實體缺少 entity_category 與 device_class 的漏判問題，確保危險按鈕精準分流至設定區。
#   - [V3.0.6] [Critical] 修復 set_availability 的 online 方向節流會「丟棄」狀態轉換：
#              被吞後 _availability_cache 不更新，而上游 (bus_master/listen_master) 已將
#              內部狀態改為 online 且轉換分支永不重入，導致設備實際在線但 HA 永久顯示
#              不可用。實測：離線後 0.8s 內恢復即穩定重現；listen_master 的補救呼叫亦被
#              同一節流二次吞沒 (需 >1.1s 才生效)。改由頂部去重守衛單獨負責防洪。
#              已驗證兩軌 state 資料流逐字不變，offline 方向行為不變。
#   - [V3.0.7] [Critical] 新增 reset_discovery() 與 republish_availability()：
#              _discovery_sent 閂鎖使 send_discovery() 第二次起永遠空轉，
#              _availability_cache 去重守衛使設備可用性也不重發。Broker 重啟且未開
#              持久化時 retained 全失，實體再也不會回到 HA，只能重啟整個網關進程。
#              兩者皆在 MQTT 重連時由 main.py 呼叫；首次連線因閂鎖為 False、快取為
#              None 而完全無作用，保證首次連線訊息序列與修正前逐字相同。
#   - [V3.0.8] publish_state 改為回傳 bool（True=真的送出／False=被節流或拒絕），
#              供 listen_master 判斷是否撤回 diff 記帳，根治監聽軌數值永久遺失。
#              bus_master 忽略回傳值，行為完全不變。
#   - [V3.0.9] 修復 MQTT publish rc 失敗仍回報成功，並以更新後實際 key 聯集校驗快取上限。
#   - [V3.0.10] 簡化成功發布回傳語意：非零 rc 已提前拒絕，其餘成功路徑直接回傳 True。
#   - [V3.0.11] [Critical] 修補 availability 的兩個孔，兩者共用 _availability_cache
#               的生命週期，刻意一次改對而非分兩次：
#               (1) set_availability 先提交快取、後發布、丟棄回傳值 —— 與 V3.0.6
#                   修掉的節流吞沒同形態。發布失敗後去重守衛使該轉換永不重入，
#                   設備在線但 HA 停在舊值。改為發布失敗即撤回記帳
#                   （同 listen_master V3.0.8 的作法）。
#               (2) republish_availability 對 cache=None 直接 return，於是上一個
#                   進程殘留的 retained online 在本進程確認任何狀態之前都不會被
#                   更正 —— HA 顯示可用且帶陳舊數值。改為主動發 offline。
#                   這推翻 V3.0.7「首次連線訊息序列逐字不變」的取捨：那個取捨
#                   保的是相容性，代價是把一句未經確認的 online 留在 broker 上。
#               行為變更（預期且必要）：每次網關啟動，各設備會先顯示 unavailable，
#               直到累積 2 次成功輪詢才轉 online（poll_interval 6s 時約 6～12 秒）。
#   - [V3.0.12] [Critical] set_availability 改為回傳 bool，修補 V3.0.11 引入的回歸
#               （report/056 F3）。V3.0.11 只做到「發布失敗就撤回自己的記帳」，
#               但呼叫端 bus_master/_record_success 是**先**提交 state["online"]=True
#               **再**通知，於是兩本帳提交時機不一致：對方已 online、本端撤回成
#               None，`not state["online"]` 從此為假 → 該轉換永不重入 → 沒有任何
#               東西補送。更糟的是 V3.0.11 的 republish 對 cache=None 會主動發
#               offline，MQTT 重連時反而斬釘截鐵宣告一台正常運作的設備離線 ——
#               比 V3.0.10「僥倖靠重連自癒」更差。
#               本版把成敗回報給呼叫端，由呼叫端在發布成功後才提交自身狀態；
#               去重早退視為「broker 端已是此值」回傳 True。
#   - [V3.0.13] [Critical] send_discovery 的閂鎖改為「全部發布成功才鎖」
#               （report/056 F4）。原本進函式就先設 _discovery_sent=True，之後每個
#               _safe_publish 的回傳值都被丟棄，最後照樣印「Discovery 完成」；
#               任一則 retained config 失敗，該實體就不會出現在 HA，而閂鎖已鎖，
#               下次一般呼叫直接 early return，永遠不補送。
#               現累計失敗數，0 失敗才上鎖，有失敗則保持解鎖並印 ERROR 彙總。
#               _process_item 改為回傳 bool；地圖本身的問題（ha 非 dict、未知
#               domain、builder 回 None）一律視為成功 —— 那些重送幾次都一樣，
#               計入失敗會讓閂鎖永遠鎖不上、每次重連整批重送。
#               ⚠️ 這只讓「可重送」與「看得見」，本身不是重試觸發器：實際重送
#               仍僅發生在下一次 MQTT 重連（main.py 的 _staggered_discovery）。
#   - [V3.0.14] [Critical] 新增 republish_state()，MQTT 重連時補送整包狀態快取
#               （report/058 F2）。state topic 是 QoS0 且非 retained，斷線期間的
#               狀態一律消失。主動軌每輪重送整包快取，最多一個 poll_interval
#               自癒；監聽軌只在值變動時才發布，設備持續送相同數值時 HA 會停在
#               空白或舊值直到某個值再次改變 —— 期間實體顯示 online 且零日誌。
#               本方法刻意繞過 200ms 節流與變動判斷，呼叫點僅 MQTT 重連
#               （main.py 已分批間隔 2s）。
# =============================================================================
import logging
import json
import time

logger = logging.getLogger(__name__)

class HAManager:
    """HA MQTT Discovery 管理器 V3.0.3 - 支援動態前綴、雙態地圖與防護節流"""

    def __init__(self, mqtt_client, node_id: str, device_type: str, uid: int, rmap, discovery_prefix: str = "homeassistant"):
        self.mqtt = mqtt_client
        self.node_id = node_id
        self.device_type = device_type
        self.uid = uid
        self.rmap = rmap

        self.entity_base          = f"{node_id}_{device_type}_{uid}"
        self.base_topic           = discovery_prefix
        self.state_topic          = f"{node_id}/{device_type}/{uid}/state"
        self.device_status_topic  = f"{node_id}/{device_type}/{uid}/status"
        self.gateway_status_topic = f"{node_id}/status"
        self.device_identifiers   = [f"{node_id}_{device_type}_addr{uid}"]

        self.CONNECTIVITY_KEY     = "connectivity"

        self._state_cache = {}
        self._availability_cache: bool | None = None

        self._last_state_publish = 0.0
        self._state_min_interval = 0.2
        self._last_availability_publish = 0.0
        self._availability_min_interval = 1.0
        self._discovery_sent = False

    def cleanup(self):
        self._state_cache.clear()
        self._availability_cache = None
        self._discovery_sent = False

    def reset_discovery(self):
        """
        ✅ [Fix V3.0.7] MQTT 重連後呼叫：解除 _discovery_sent 閂鎖讓 Discovery 重發。

        原本閂鎖使 send_discovery() 第二次起永遠空轉。平時無妨（Discovery 是 retained），
        但 Broker 重啟且未開持久化時 retained 全失，實體再也不會回到 HA，
        只能重啟整個網關進程才救得回來。
        """
        self._discovery_sent = False

    def republish_availability(self):
        """
        ✅ [Fix V3.0.7] MQTT 重連後補回設備可用性的 retained 訊息。

        set_availability() 的去重守衛會擋掉「值沒變」的重發，但 Broker 端的 retained
        可能已經不見了，因此這裡直接發布快取值，不走去重。

        ⚠️ [V3.0.11] 快取為 None 的處置已變更。原本直接 return（保證首次連線訊息
        序列與修正前逐字相同）；現在改為主動發 offline，理由見檔頭 V3.0.11 (2)。
        因此首次連線的訊息序列**不再**與 V3.0.10 以前相同 —— 每台設備會多一則
        retained offline，並在累積 2 次成功輪詢後才轉 online。
        """
        if self._availability_cache is None:
            # ✅ [Fix V3.0.11] 原本直接 return（V3.0.7 刻意保留「首次連線訊息序列
            #    與修正前逐字相同」）。但 cache 為 None 代表「本進程從未成功告訴
            #    broker 這台設備的任何狀態」，而 broker 上很可能還留著上一個進程的
            #    retained online —— 那是一句我們尚未更正的謊。此時 HA 顯示設備可用
            #    且帶著陳舊數值，直到本進程累積 2 次成功輪詢才會被覆蓋。
            #    改為主動發 offline：這是該時刻唯一誠實的敘述（我們還沒確認過）。
            #    走 set_availability() 而非直接發布，以共用其撤回記帳與去重守衛。
            self.set_availability(False)
            return
        self._safe_publish(self.device_status_topic,
                           "online" if self._availability_cache else "offline",
                           qos=1, retain=True, is_json=False)

    def republish_state(self) -> bool:
        """
        ✅ [Fix V3.0.14] MQTT 重連後補送整包狀態快取（report/058 F2）。

        state topic 是 QoS0 且非 retained，broker 重啟或斷線期間發出的狀態一律
        消失。主動軌因為每輪都重送整包快取，最多一個 poll_interval 就自癒；
        但監聽軌只在「值有變動」時才發布，設備若持續送相同數值，HA 會停在
        空白或舊值，直到某個數值再次改變為止 —— 期間實體顯示 online，
        沒有任何錯誤日誌。

        本方法直接發布，刻意繞過 200ms 節流與變動判斷：呼叫點只有 MQTT 重連
        （每台之間已由 main.py 分批間隔 2s），不會造成洪泛。
        快取為空代表本進程還沒解出任何資料，無事可補。
        """
        if not self._state_cache:
            return True
        self._last_state_publish = time.monotonic()
        return self._safe_publish(self.state_topic, self._state_cache,
                                  qos=0, retain=False, is_json=True)

    def _get_rmap_field(self, field_name: str):
        if isinstance(self.rmap, dict):
            return self.rmap.get(field_name)
        return getattr(self.rmap, field_name, None)

    def _safe_publish(self, topic: str, payload, qos: int = 1,
                      retain: bool = False, is_json: bool = True) -> bool:
        try:
            if payload is None:
                data = None
            else:
                data = json.dumps(payload, allow_nan=False) if is_json else payload

            result = self.mqtt.publish(topic, data, qos=qos, retain=retain)

            if hasattr(result, "rc") and result.rc != 0:
                logger.warning(f"[{self.entity_base}] publish rc={result.rc} topic={topic}")
                return False
            return True

        except (TypeError, ValueError):
            logger.exception(f"[{self.entity_base}] JSON 序列化失敗 topic={topic}")
            return False
        except Exception:
            logger.exception(f"[{self.entity_base}] MQTT 發布失敗 topic={topic}")
            return False

    def send_discovery(self, cleanup: bool = False):
        if not cleanup and self._discovery_sent:
            return

        # ✅ [Fix V3.0.13] 閂鎖改為「全部發布成功才鎖」（report/056 F4）。
        #    原本進函式就先設 _discovery_sent=True，之後每一個 _safe_publish 的
        #    回傳值都被丟棄，最後照樣印「Discovery 完成」。任一則 retained config
        #    發布失敗，該實體就不會出現在 HA，而閂鎖已鎖 —— 下次一般呼叫直接
        #    early return，永遠不補送。
        #    現改為累計失敗數，只有 0 失敗才上鎖；有失敗則保持解鎖並印出彙總。
        #    ⚠️ 這只讓「可重送」與「看得見」，本身不是重試觸發器：實際重送仍
        #    僅發生在下一次 MQTT 重連（main.py 的 _staggered_discovery）。
        self._discovery_sent = False
        failed = 0
        total = 0

        op = "清除" if cleanup else "發送"
        logger.info(f"[{self.entity_base}] {op} HA Discovery V3.0.3...")

        connectivity_topic = f"{self.base_topic}/binary_sensor/{self.entity_base}/{self.CONNECTIVITY_KEY}/config"
        total += 1
        if cleanup:
            ok = self._safe_publish(connectivity_topic, None, qos=1, retain=True, is_json=False)
        else:
            conn_payload = self._build_connectivity_payload()
            ok = self._safe_publish(connectivity_topic, conn_payload, qos=1, retain=True, is_json=True)
        if not ok:
            failed += 1

        b1_info = self._get_rmap_field("B1_INFO")
        if isinstance(b1_info, list):
            for item in b1_info:
                if not isinstance(item, dict) or "ha" not in item or "key" not in item:
                    continue
                total += 1
                if not self._process_item(item, item["key"], cleanup):
                    failed += 1

        b2_setting = self._get_rmap_field("B2_SETTING")
        if isinstance(b2_setting, list):
            for item in b2_setting:
                if not isinstance(item, dict) or "ha" not in item or "key" not in item:
                    continue
                total += 1
                if not self._process_item(item, item["key"], cleanup):
                    failed += 1

        b3_bits = self._get_rmap_field("B3_STATUS_BITS")
        if isinstance(b3_bits, dict):
            for key, item in b3_bits.items():
                if not isinstance(item, dict) or "ha" not in item:
                    continue
                total += 1
                if not self._process_item({**item, "key": key}, key, cleanup):
                    failed += 1
        # 🚀 [Fix V3.0.4] 補齊 List 格式支援，對齊 map_validator 合約，消滅靜默跳過
        elif isinstance(b3_bits, list):
            for item in b3_bits:
                if not isinstance(item, dict) or "ha" not in item or "key" not in item:
                    continue
                total += 1
                if not self._process_item(item, item["key"], cleanup):
                    failed += 1

        if failed:
            logger.error(
                f"[{self.entity_base}] 🚨 Discovery {op}未完成：{failed}/{total} 則失敗。"
                f"這些實體不會出現在 HA（或不會被清除）。閂鎖維持解鎖，"
                f"下次 MQTT 重連時會整批重送。"
            )
        else:
            self._discovery_sent = not cleanup
            logger.info(f"[{self.entity_base}] Discovery {op}完成（{total} 則全部成功）")

    def _process_item(self, item: dict, key: str, cleanup: bool) -> bool:
        """
        回傳 True 代表「這一則沒有可重試的失敗」，False 代表 MQTT 發布失敗。

        ✅ [V3.0.13] 只有真正的發布失敗才回 False。地圖本身的問題（ha 非 dict、
           未知 domain、builder 回 None）一律回 True —— 那些重送幾次都一樣，
           若計入失敗會讓閂鎖永遠鎖不上，每次重連都整批重送。它們原本就各自
           有 ERROR／WARNING 日誌。
        """
        ha_conf = item.get("ha", {})
        if not isinstance(ha_conf, dict):
            return True

        domain = ha_conf.get("type", "sensor")
        config_topic = f"{self.base_topic}/{domain}/{self.entity_base}/{key}/config"

        if cleanup:
            return self._safe_publish(config_topic, None, qos=1, retain=True, is_json=False)

        builder_map = {
            "sensor":        self._build_sensor_payload,
            "binary_sensor": self._build_binary_sensor_payload,
            "switch":        self._build_switch_payload,
            "number":        self._build_number_payload,
            "select":        self._build_select_payload,
            "button":        self._build_button_payload,
            "text":          self._build_text_payload,
        }

        builder = builder_map.get(domain)
        if not builder:
            logger.error(f"[{self.entity_base}] 錯誤：未知的 HA 實體類別 '{domain}' (Key: {key})")
            return True

        payload = builder(item, key)

        if payload is None:
            return True
        return self._safe_publish(config_topic, payload, qos=1, retain=True, is_json=True)

    def publish_state(self, data_dict: dict) -> bool:
        """
        發布狀態。回傳 True 代表本次真的送出，False 代表被節流或拒絕。

        ✅ [Fix V3.0.8] 新增回傳值。呼叫端若採「先記帳、後發布」（listen_master 的
           diff 快取），必須依此判斷是否撤回記帳，否則被節流的變更會因為
           has_changed=False 而永遠不再補送。bus_master 忽略回傳值，行為不變。
        """
        if not isinstance(data_dict, dict) or not data_dict:
            return False

        if len(self._state_cache.keys() | data_dict.keys()) > 500:
            logger.error(f"[{self.entity_base}] _state_cache 異常膨脹 (>500 keys)，拒絕更新！")
            return False

        self._state_cache.update(data_dict)

        now = time.monotonic()
        if now - self._last_state_publish < self._state_min_interval:
            return False

        self._last_state_publish = now
        return self._safe_publish(self.state_topic, self._state_cache, qos=0, retain=False, is_json=True)

    def set_availability(self, online: bool) -> bool:
        """
        回傳 True 代表「broker 端已是此狀態」（本次成功送出，或先前已成功送出而去重）。
        回傳 False 代表本次發布失敗、記帳已撤回。

        ✅ [Fix V3.0.12] 呼叫端（bus_master／listen_master）必須依此決定要不要提交
           自己的 state["online"]。否則兩本帳的提交時機不一致：對方先記成 online，
           本端撤回成 None，之後「狀態轉換」的守衛永不重入 —— 沒有任何東西會補送。
           見 report/056 F3。
        """
        if self._availability_cache == online:
            return True

        # ✅ [Fix V3.0.6] 移除 online 方向的節流早退。
        #    原邏輯 `if online and (now - _last_availability_publish < 1.0): return`
        #    是「丟棄」而非「延後」：被吞掉後 _availability_cache 維持舊值，
        #    而 bus_master._record_success 已先將 state["online"]=True，該轉換分支
        #    永不重入 → 設備實際在線但 HA 永久顯示不可用。
        #    listen_master 的補救呼叫也會被同一節流再吞一次（實測需 >1s 才生效）。
        #    頂部去重守衛已足以防洪：availability 僅在 5 次失敗／2 次成功的狀態
        #    轉換時才呼叫，本質低頻（實測最壞 0.74 則/秒，遠低於 publish_state
        #    既有容許的 5 則/秒）。offline 方向原本就不受節流，行為不變。
        # ✅ [Fix V3.0.11] 撤回記帳。原本「先提交快取、後發布、丟棄回傳值」與
        #    V3.0.6 修掉的節流吞沒是同一形態：發布失敗後 _availability_cache 已被
        #    改成新值，頂部去重守衛便讓之後每一次同值呼叫都提早 return，該狀態
        #    轉換永不重入 —— 設備實際在線，HA 卻停在上一個成功發布的值。
        #    與 listen_master V3.0.8 的「先記帳後發布」同源，處理方式一致：
        #    發布失敗就把記帳還原，讓下一次狀態轉換能重新嘗試。
        prev = self._availability_cache
        self._availability_cache = online
        self._last_availability_publish = time.monotonic()

        if not self._safe_publish(self.device_status_topic,
                                  "online" if online else "offline",
                                  qos=1, retain=True, is_json=False):
            self._availability_cache = prev
            return False
        return True

    def publish_gateway_online(self):
        self._safe_publish(self.gateway_status_topic, "online", qos=1, retain=True, is_json=False)
        logger.info(f"📡 [{self.node_id}] 網關：online")

    def publish_gateway_offline(self):
        self._safe_publish(self.gateway_status_topic, "offline", qos=1, retain=True, is_json=False)
        logger.info(f"📡 [{self.node_id}] 網關：offline")

    def _get_base_payload(self, item: dict, key: str) -> dict:
        unique_id = f"{self.entity_base}_{key}"
        return {
            "name":           item.get("name", key),
            "unique_id":      unique_id,
            "object_id":      unique_id,
            "state_topic":    self.state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": {
                "identifiers": self.device_identifiers,
                "name":        f"{self.node_id} {self.device_type.upper()} [ID:{self.uid}]",
                "model":       self.device_type.upper(),
                "manufacturer": "Edge-BusMaster",
                "via_device": self.node_id
            },
            "availability": [
                {"topic": self.gateway_status_topic, "payload_available": "online", "payload_not_available": "offline"},
                {"topic": self.device_status_topic, "payload_available": "online", "payload_not_available": "offline"},
            ],
            "availability_mode": "all",
        }

    def _apply_common(self, payload: dict, item: dict) -> dict:
        ha = item.get("ha", {})
        unit = item.get("unit")
        if unit and unit not in ("Hex", "Bit", "Enum"):
            payload["unit_of_measurement"] = unit

        for field in ("device_class", "state_class", "icon", "value_template", "suggested_display_precision", "entity_category"):
            if field in ha:
                payload[field] = ha[field]
        return payload

    def _build_connectivity_payload(self) -> dict:
        unique_id = f"{self.entity_base}_{self.CONNECTIVITY_KEY}"
        return {
            "name": "連線狀態",
            "unique_id": unique_id,
            "object_id": unique_id,
            "device_class": "connectivity",
            "entity_category": "diagnostic",
            "state_topic": self.device_status_topic,
            "payload_on": "online",
            "payload_off": "offline",
            "device": {
                "identifiers": self.device_identifiers,
                "name": f"{self.node_id} {self.device_type.upper()} [ID:{self.uid}]",
                "model": self.device_type.upper(),
                "manufacturer": "Edge-BusMaster",
                "via_device": self.node_id
            },
            "availability": [
                {"topic": self.gateway_status_topic, "payload_available": "online", "payload_not_available": "offline"},
                {"topic": self.device_status_topic,  "payload_available": "online", "payload_not_available": "offline"},
            ],
            "availability_mode": "all",
        }

    def _build_sensor_payload(self, item: dict, key: str) -> dict:
        return self._apply_common(self._get_base_payload(item, key), item)

    def _build_binary_sensor_payload(self, item: dict, key: str) -> dict:
        payload = self._apply_common(self._get_base_payload(item, key), item)
        ha = item.get("ha", {})
        payload["payload_on"]  = ha.get("payload_on", "ON")
        payload["payload_off"] = ha.get("payload_off", "OFF")
        return payload

    def _build_switch_payload(self, item: dict, key: str) -> dict:
        payload = self._apply_common(self._get_base_payload(item, key), item)
        ha = item.get("ha", {})
        payload["command_topic"] = f"{self.node_id}/{self.device_type}/{self.uid}/set/{key}"
        state_key = ha.get("state_key")
        if state_key:
            payload["value_template"] = f"{{{{ value_json.{state_key} }}}}"

        p_on = str(ha.get("payload_on", "ON"))
        p_off = str(ha.get("payload_off", "OFF"))
        payload["payload_on"]  = p_on
        payload["payload_off"] = p_off
        payload["state_on"]    = str(ha.get("state_on", p_on))
        payload["state_off"]   = str(ha.get("state_off", p_off))

        payload.pop("optimistic", None)
        if ha.get("optimistic") is True:
            payload["optimistic"] = True
            payload.pop("state_topic", None)
            payload.pop("value_template", None)
        if "entity_category" in ha:
            payload["entity_category"] = ha["entity_category"]
        else:
            payload.pop("entity_category", None)
        return payload

    def _build_number_payload(self, item: dict, key: str) -> dict:
        payload = self._apply_common(self._get_base_payload(item, key), item)
        ha = item.get("ha", {})
        payload["command_topic"] = f"{self.node_id}/{self.device_type}/{self.uid}/set/{key}"
        # 🚀 [Fix] 補救：若 ha 字典內帶有 unit_of_measurement，強制覆寫進 payload
        if "unit_of_measurement" in ha:
            payload["unit_of_measurement"] = ha["unit_of_measurement"]
        try:
            payload["min"]  = float(ha.get("min", 0))
            payload["max"]  = float(ha.get("max", 100))
            payload["step"] = float(ha.get("step", 1))
        except (TypeError, ValueError):
            logger.error(f"[{self.entity_base}] number '{key}' min/max/step 型別錯誤，跳過該實體")
            return None
        payload["mode"] = ha.get("mode", "box")
        state_key = ha.get("state_key")
        if state_key:
            payload["value_template"] = f"{{{{ value_json.{state_key} }}}}"
        payload.pop("optimistic", None)
        if ha.get("optimistic") is True:
            payload["optimistic"] = True
            payload.pop("state_topic", None)
            payload.pop("value_template", None)
        if "entity_category" in ha:
            payload["entity_category"] = ha["entity_category"]
        else:
            payload.pop("entity_category", None)
        return payload

    def _build_select_payload(self, item: dict, key: str):
        ha = item.get("ha", {})
        options = ha.get("options", [])
        if not isinstance(options, list) or not options:
            logger.warning(f"[{self.entity_base}] select '{key}' options 缺失，跳過")
            return None
        if not all(isinstance(o, str) for o in options):
            logger.warning(f"[{self.entity_base}] select '{key}' options 含非字串元素，跳過")
            return None
        payload = self._apply_common(self._get_base_payload(item, key), item)
        payload["command_topic"] = f"{self.node_id}/{self.device_type}/{self.uid}/set/{key}"
        payload["options"] = options
        state_key = ha.get("state_key", ha.get("link_b1"))
        if state_key:
            payload["value_template"] = f"{{{{ value_json.{state_key} }}}}"
        payload.pop("optimistic", None)
        if ha.get("optimistic") is True:
            payload["optimistic"] = True
            payload.pop("state_topic", None)
            payload.pop("value_template", None)
        if "entity_category" in ha:
            payload["entity_category"] = ha["entity_category"]
        else:
            payload.pop("entity_category", None)
        return payload

    def _build_button_payload(self, item: dict, key: str) -> dict:
        payload = self._get_base_payload(item, key)
        payload.pop("state_topic", None)
        payload.pop("value_template", None)
        ha = item.get("ha", {})
        payload["command_topic"] = f"{self.node_id}/{self.device_type}/{self.uid}/set/{key}"
        # 🚀 [Fix V3.0.5] 解除字串硬碼限制，支援讀取 yaml 的 payload_press 自訂魔術值
        payload["payload_press"] = str(ha.get("payload_press", "PRESS"))
        if ha.get("icon"):
            payload["icon"] = ha["icon"]
        # 🚀 [V3.0.3 修復] 補齊 entity_category 與 device_class 支援，完善四象限隔離
        if ha.get("device_class"):
            payload["device_class"] = ha["device_class"]
        if "entity_category" in ha:
            payload["entity_category"] = ha["entity_category"]
        else:
            payload.pop("entity_category", None)
        return payload

    def _build_text_payload(self, item: dict, key: str) -> dict:
        ha = item.get("ha", {})
        payload = self._apply_common(self._get_base_payload(item, key), item)
        payload["command_topic"] = f"{self.node_id}/{self.device_type}/{self.uid}/set/{key}"

        state_key = ha.get("state_key") or ha.get("link_b1") or key
        payload["value_template"] = f"{{{{ value_json.{state_key} }}}}"

        if ha.get("pattern"):
            payload["pattern"] = ha["pattern"]
        if ha.get("min"):
            try:
                payload["min"] = int(ha["min"])
            except (TypeError, ValueError):
                pass
        if ha.get("max"):
            try:
                payload["max"] = int(ha["max"])
            except (TypeError, ValueError):
                pass

        if "entity_category" in ha:
            payload["entity_category"] = ha["entity_category"]
        else:
            payload.pop("entity_category", None)

        return payload
