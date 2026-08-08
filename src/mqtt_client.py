# =============================================================================
#version mqtt_client.py - V1.10 工業封存版 (Paho V2 對齊與記憶體防護)
# 修復歷程：
# V1.8 : 自動重連、訂閱恢復、OOM 防護
# V1.9 : Paho V2 API reason_code 屬性對齊、鎖內拷貝優化、Queue 關機清空
# V1.10: [Bugfix] _DummyResult 類別屬性簡化，確保 rc 型別絕對安全且效能最佳
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
            rc, _ = self.client.subscribe(topic, qos=qos)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"[MQTT] subscribe rc={rc} topic={topic}")
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
                    rc, _ = client.subscribe(topic, qos)
                    if rc != mqtt.MQTT_ERR_SUCCESS:
                        logger.debug(f"[MQTT] resub rc={rc} topic={topic}")
                except Exception as e:
                    logger.error(f"[MQTT] resub error topic={topic}: {e}")

            if self.on_connected_callback:
                self.on_connected_callback()
        else:
            logger.error(f"[MQTT] connect refused rc={reason_code}")

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
