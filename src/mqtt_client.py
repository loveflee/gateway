# =============================================================================
#version mqtt_client.py - V1.12 工業封存版 (Paho V2 對齊與記憶體防護)
# 修復歷程：
# V1.8 : 自動重連、訂閱恢復、OOM 防護
# V1.9 : Paho V2 API reason_code 屬性對齊、鎖內拷貝優化、Queue 關機清空
# V1.10: [Bugfix] _DummyResult 類別屬性簡化，確保 rc 型別絕對安全且效能最佳
# V1.11: [Observability] subscribe() 與 _on_connect 補訂閱失敗由 DEBUG 提升為
#        ERROR。訂閱是北向指令的唯一入口，失敗後連線仍在、實體仍可用、狀態仍
#        更新，但 HA 按下去毫無反應且預設等級零線索。只改日誌等級與訊息，
#        控制流、回傳值與訂閱重試行為完全不變。
# V1.12: [Critical] 新增 on_subscribe，實際回收 broker 的 SUBACK（report/056 F1）。
#        subscribe() 的立即 rc 只代表「請求已送出」；broker 以 ACL 拒絕訂閱時 rc 仍
#        是 SUCCESS，失敗藏在後續 SUBACK 的 reason code 裡。沒有這條回路時，
#        連線正常、Discovery 照發、HA 上按鈕俱在，但指令永遠到不了網關，
#        且預設等級零線索。現以 mid → topic 對照，拒絕時印 ERROR 並累計
#        subscribe_failures／rejected_topics 供 health 廣播。
#        純觀測：不改變訂閱重試、連線或既有控制流；被拒絕的訂閱仍留在
#        _subscriptions，下次重連照樣再試。
# =============================================================================
import queue
import logging
import threading
import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

class _DummyResult:
    """✅ [Fix Bug V1.10] HA Manager 相容的空發布結果，確保 .rc 屬性存在且 != 0"""
    rc = mqtt.MQTT_ERR_CONN_LOST

class RobustMQTTClient:
    def __init__(self, broker: str, port: int,
                 client_id: str = "",
                 username: str = None,
                 password: str = None):

        self.broker = broker
        self.port = port
        self.msg_queue = queue.Queue(maxsize=2000)
        self.on_connected_callback = None
        self._subscriptions = set()
        self._sub_lock = threading.Lock()

        # ✅ [V1.12] SUBACK 對照表與失敗統計（report/056 F1）。
        #    mid → topic：subscribe() 的立即 rc 只代表「請求已送出」，broker 是否
        #    真的接受（ACL 可能拒絕）要等 SUBACK。沒有這條回路時，被拒絕的訂閱
        #    在網關端毫無跡象：連線正常、Discovery 照發、HA 上按鈕俱在，
        #    但指令永遠到不了網關。
        self._pending_subs: dict[int, str] = {}
        self.subscribe_failures = 0          # 累計 SUBACK 失敗次數（供 health）
        self.rejected_topics: set[str] = set()   # 被 broker 拒絕的 topic

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id
        )

        if username:
            self.client.username_pw_set(username, password)

        self.client.max_queued_messages_set(1000)
        self.client.max_inflight_messages_set(50)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe

    def set_lwt(self, topic: str, payload: str = "offline", retain: bool = True):
        self.client.will_set(topic, payload, qos=1, retain=retain)

    def connect(self):
        try:
            logger.info(f"[MQTT] connect {self.broker}:{self.port}")
            self.client.connect_async(self.broker, self.port, keepalive=60)
            self.client.loop_start()
        except Exception:
            logger.exception("[MQTT] start connect failed")

    def disconnect(self):
        self.client.disconnect()
        self.client.loop_stop()
        
        # ✅ [Fix V1.9] 關機時清空 Queue，防止熱重載時的記憶體汙染
        while not self.msg_queue.empty():
            try:
                self.msg_queue.get_nowait()
            except queue.Empty:
                break

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        try:
            return self.client.publish(topic, payload, qos=qos, retain=retain)
        except Exception:
            logger.exception(f"[MQTT] publish failed topic={topic}")
            # ✅ [Fix V1.9] 回傳 DummyResult 確保 ha_manager 取 .rc 時不崩潰
            return _DummyResult()

    def subscribe(self, topic: str, qos: int = 0):
        with self._sub_lock:
            self._subscriptions.add((topic, qos))
        try:
            rc, mid = self.client.subscribe(topic, qos=qos)
            if rc == mqtt.MQTT_ERR_SUCCESS and mid is not None:
                # ✅ [V1.12] 立即 rc 只代表「請求送出去了」，broker 是否接受要看
                #    後續 SUBACK。記下 mid → topic 供 _on_subscribe 對照。
                with self._sub_lock:
                    self._pending_subs[mid] = topic
            if rc != mqtt.MQTT_ERR_SUCCESS:
                # ✅ [V1.11] 原為 DEBUG，預設等級完全看不見。訂閱是北向指令的唯一
                #    入口：失敗後連線仍在、實體仍可用、狀態仍更新，但 HA 按了不會有
                #    任何反應，且無任何線索。失效後果最嚴重的路徑不得用最低等級。
                logger.error(
                    f"[MQTT] 🚨 訂閱失敗 rc={rc} topic={topic} —— "
                    f"網關將收不到 HA 下發的指令（寫入、重啟）"
                )
        except Exception:
            logger.exception(f"[MQTT] subscribe error topic={topic}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # ✅ [Fix V1.9] Paho V2 標準寫法：使用 reason_code.is_failure 判斷
        if not reason_code.is_failure:
            logger.info(f"[MQTT] connected {self.broker}")

            # ✅ [Fix V1.9] 鎖內拷貝，鎖外 I/O，奈秒級持鎖
            with self._sub_lock:
                subs_copy = list(self._subscriptions)

            for topic, qos in subs_copy:
                try:
                    rc, mid = client.subscribe(topic, qos)
                    if rc == mqtt.MQTT_ERR_SUCCESS and mid is not None:
                        with self._sub_lock:
                            self._pending_subs[mid] = topic
                    if rc != mqtt.MQTT_ERR_SUCCESS:
                        # ✅ [V1.11] 同上：重連後補訂閱失敗代表指令通道沒回來。
                        logger.error(
                            f"[MQTT] 🚨 重連後補訂閱失敗 rc={rc} topic={topic} —— "
                            f"網關將收不到 HA 下發的指令（寫入、重啟）"
                        )
                except Exception as e:
                    logger.error(f"[MQTT] resub error topic={topic}: {e}")

            if self.on_connected_callback:
                self.on_connected_callback()
        else:
            logger.error(f"[MQTT] connect refused rc={reason_code}")

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties=None):
        """
        ✅ [V1.12] 收 broker 的 SUBACK，判斷訂閱是否真的被接受（report/056 F1）。

        Paho v2 每個訂閱 topic 回一個 ReasonCode。granted QoS 為 0/1/2 代表接受；
        0x80（Unspecified error）或任何 is_failure 為真者代表 broker 拒絕 ——
        最常見的原因是 ACL 沒給該 topic 的訂閱權限。

        這條回路只做觀測與計數，不改變任何重試或連線行為：被拒絕的訂閱仍留在
        _subscriptions 裡，下次重連照樣會再試一次。
        """
        with self._sub_lock:
            topic = self._pending_subs.pop(mid, f"<mid={mid}>")

        try:
            codes = list(reason_code_list) if reason_code_list is not None else []
        except TypeError:
            codes = [reason_code_list]

        for rc in codes:
            failed = getattr(rc, "is_failure", None)
            if failed is None:                      # 舊式：直接是 granted qos 整數
                failed = isinstance(rc, int) and rc >= 0x80
            if failed:
                self.subscribe_failures += 1
                self.rejected_topics.add(topic)
                logger.error(
                    f"[MQTT] 🚨 broker 拒絕訂閱 topic={topic} reason={rc} —— "
                    f"連線正常但指令通道未建立，HA 上的開關／數值／按鈕按下去"
                    f"不會有任何反應。最常見原因是 broker ACL 未授權此 topic。"
                )
            else:
                self.rejected_topics.discard(topic)
                logger.info(f"[MQTT] 訂閱已被 broker 接受 topic={topic} qos={rc}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        # ✅ [Fix V1.9] Paho V2 標準寫法：使用 reason_code.is_failure 判斷
        if reason_code.is_failure:
            logger.warning(f"[MQTT] unexpected disconnect rc={reason_code}")
        else:
            logger.info("[MQTT] disconnected")

    def _on_message(self, client, userdata, msg):
        try:
            self.msg_queue.put_nowait(msg)
        except queue.Full:
            logger.error(f"[MQTT] queue full drop topic={msg.topic}")
