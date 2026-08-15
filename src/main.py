# =============================================================================
# version main.py v2.15
# 模組名稱：Edge Gateway V3 主控樞紐 (The Controller)
# 版本狀態：V2.15 (監聽熔斷復原 + 啟動不阻斷 + 指令丟棄可觀測 + 北向失敗可見)
# 核心職責：
#   1. 生命週期管理：解析 config.yaml，初始化底層通訊與 MQTT 代理。
#   2. 跨執行緒橋接：採用 Load Shedding (負載拋棄) 策略，防禦 MQTT 洪泛攻擊。
#   3. 雙軌工廠掛載：嚴格實體防撞 (Endpoint Collision Guard)，分發設備至 Bus/Listen。
#   4. 健康觀測站：獨立 Task 聚合底層錯誤計數，定期廣播 Heartbeat。
#   5. 優雅關機：原子化旗標，集中 Await 釋放 Adapter 資源，並確保 MQTT 遺言 Flush。
#   6. 網頁後台與探測：WebUI 旁路掛載，並支援無損切換至 Sniffer 獨占探測模式。
# 修復歷程：
#   - [V2.4] 限縮背景任務單次例外爆炸半徑，隔離重複 UID，並補齊雙軌與 MQTT 關機清理。
#   - [V2.5] 收斂 UID 型別與範圍、白名單驗證 mode，並依掛載結果移除未註冊 HA 管理器。
#   - [V2.6] [Critical] 消滅「單筆設定寫錯 → 整台網關無聲死亡」：
#            (1) 新增設定 preflight (_preflight_devices)：在 has_active/has_listen 之前
#                收斂 cfg 根結構、devices 容器與每一筆 device 元素的型別。原本
#                `any(d.get(...) for d in cfg["devices"])` 會在逐台隔離之前就對非 dict
#                元素呼叫 .get() 而拋 AttributeError。
#            (2) __main__ 的 `except Exception: sys.exit(1)` 原本不印任何東西，
#                traceback 與救援指示全部消失、WebUI daemon 也一併蒸發，
#                docker logs 一個字都沒有。改為 CRITICAL + traceback + 統一救援模板。
#            (3) 零設備時的模式訊息原本一律印「純旁路監聽模式」，即使一台 listen
#                都沒有；操作者清空 devices: 進 WebUI 搶修時看到的訊息是錯的。
#            全部沿用既有 _quarantine()／啟動橫幅／health 的 quarantined 欄位，未新增告警管道。
#   - [V2.7] health 發布結果變成可觀測：原本 `publish(); except: pass` 幾乎是死碼 ——
#            paho 的 publish() 失敗只回非零 rc、不拋例外，rc 被完全忽略，
#            「Health Monitor 還活著但 broker 收不到」在此之前沒有任何診斷。
#            現檢查 rc，累計 health_publish_failures_total 並放進 payload
#            （收到的那則必然成功，故此欄位的用途是「恢復後告訴你剛才漏了幾則」），
#            另加節流 warning（首次 + 每 10 次）與恢復 log。
#            未新增 mqtt_connected 欄位：health 為 QoS0，收得到就代表已連線，恆為 true。
#   - [V2.8] 設定值改在「消費點」收斂，補完 V2.5/V2.6 只做到 uid/mode/devices 的缺口。
#            WebUI 門口驗證只擋得住從 WebUI 寫進來的設定，擋不住 host 手改 ——
#            而 host 手改正是本專案文件化的救援路徑，門口在那條路上並不存在。
#            依欄位性質分兩類（與既有程式碼一致）：
#              識別／路由型（猜不出正確值）→ _quarantine 該台：
#                · adapter 非字串：原本 ADAPTER_FACTORY.get([]) 會拋
#                  unhashable type: 'list' 殺掉整台網關（與 V2.5 的 uid 同類）。
#              調校型（有合理預設）→ _coerce_number() 收斂＋告警：
#                · poll_interval：原本未驗證即交給 BusMaster，`poll_interval: []`
#                  會在 _reschedule() 的 base_time + interval 拋 TypeError；該例外被
#                  _arbitration_loop 接住後只 log 一次並續跑，但設備已被 pop 出
#                  slow_heap 且不會再 push 回去 —— 單台從此【無聲停止輪詢】。
#                · bus.offline_time：原本兩處 int(...) 會因 'bad' 拋 ValueError；
#                  bus: 非 dict 時連 .get() 都會 AttributeError。現一次收斂兩軌共用。
#            未處理 driver.* 的深層數值參數（timeout / inter_frame_delay 等）：
#            要驗完得複製每個 driver 的簽章，且其失效是排程迴圈每 5s 重印 traceback，
#            屬於大聲且持續記錄，不是無聲死亡。
#   - [V2.9] 修正 V2.8 _coerce_number() 的三個實錯（詳見該函式 docstring）：
#            (1) 用 float(value) 收斂，連 "20" 這類字串也接受 —— 數值欄位應只收
#                YAML 的數字型別（int/float，排除 bool），字串一律視為打錯。
#            (2) as_int=True 把 0.5 截成 0；offline_time 變 0 後，_reschedule() 對
#                離線設備算出 base+0 → 立刻重排 → 全速空轉打總線。已移除 as_int，
#                小數秒數（0.5）完全合法，BusMaster/ListenMaster 只做加減比較。
#            (3) 未擋 .inf：poll_interval=.inf 使 next_time 變無限大（該台永不再輪詢、
#                零日誌，正是 V2.8 聲稱要消滅的失效模式）；offline_time=.inf 則在
#                int() 拋 OverflowError，而原 try 只攔 TypeError/ValueError，
#                例外會穿出去殺掉整台網關。現以 math.isfinite() 一併擋下。
#            （.nan 在 V2.8 即已正確處理：nan > 0 為 False，直接落到預設值。）
#   - [V2.10] MQTT 帳密不再讀取 profile/config.yaml，統一由環境變數
#             MQTT_USERNAME／MQTT_PASSWORD 注入。兩者必須同時存在，避免部署遺漏 .env
#             後以匿名連線悄悄改變 MQTT／HA 可觀測行為。
#   - [V2.11] 依 report/012 修訂版 v2 實作監聽熔斷復原的 main 端配套：
#             (1) 監聽端首次 connect() 失敗不再 sys.exit(1)。「USB 此刻不在」是暫時性
#                 實體狀態（插拔、udev 重新列舉、開機競速），與「設定檔寫壞」性質不同；
#                 原本會連 WebUI 一起殺掉，反而斷了唯一的救援路徑。
#                 AsyncListenSerialDriver.read_stream() 已內建退避重連，此處只需不要
#                 提前退出即可由既有機制接手，未新增任何狀態機。
#                 僅放寬「實體 connect() 回傳 false」，缺欄位與不支援 type 仍為 fatal。
#             (2) health payload 新增 listen_decoding_locked，使熔斷鎖定狀態可從 MQTT
#                 側觀測；否則鎖定後設備只是 unavailable，與單純沒資料無法區分。
#   - [V2.12] [Observability] _mqtt_consumer_task 內三處裸 continue 補 WARNING：
#             topic 不符 set 契約、UID 非整數、payload 非 UTF-8。三者原本都讓指令
#             人間蒸發且零日誌，夾在一段刻意補齊 ERROR 分支的程式碼中間，明顯是漏網。
#             只新增日誌，判斷條件、continue 行為與其餘分支完全不變。
#   - [V2.13] [Observability] health payload 新增三個欄位（report/056 F1、F2）：
#             mqtt_subscribe_failures_total／mqtt_rejected_subscriptions —— broker 以
#             ACL 拒絕訂閱時，指令通道其實沒建立，但連線、Discovery、實體全部正常，
#             不從 MQTT 側曝光就只能翻 docker log。
#             devices[].state_publish_failures —— 「Modbus 讀得到但 MQTT 送不出去」
#             的次數；設備仍算 ONLINE（正確，兩者不該混為一談），但資料鏈斷在北向
#             這件事必須看得見。純新增欄位，既有欄位與語意不變。
#   - [V2.14] MQTT 重連的兩處補送路徑各加一行 ha_mgr.republish_state()
#             （report/058 F2）。原本只補 Discovery 與 availability，狀態本身
#             不補 —— 監聽軌因此可能長期「顯示在線但沒有數值」。
#   - [V2.15] [Observability] _on_mqtt_connected 的兩則網關發布補 rc 檢查
#             （report/060 F1）。原本丟棄回傳值，而 paho 失敗只回非零 rc 不拋例外，
#             故完全無痕跡。後果不對稱地嚴重：每個實體都是雙 topic +
#             availability_mode: all，網關那則沒送成功，broker 上就留著上一輪 LWT
#             的 offline → 全部設備的所有實體一起 unavailable，而日誌只說
#             「MQTT 上線」。任何一次 MQTT 重連都會重跑本函式而自癒，故不做重試，
#             只讓根因可見。未改用 ha_manager.publish_gateway_online()：那是
#             per-device 方法，零設備／全隔離時 ha_managers 為空會完全發不出去。
# =============================================================================
import asyncio
import signal
import yaml
import importlib
import logging
import json
import sys
import time
import os
import math
import threading

from web_admin import start_webui
from driver import RobustAsyncTcpDriver
from bus_master import BusMasterScheduler
from mqtt_client import RobustMQTTClient
from ha_manager import HAManager
from modbus_tcp_driver import AsyncModbusTcpDriver
from local_serial_driver import LocalSerialDriver
from map_validator import validate_profile
from adapter_catalog import load_adapters

# [Listen Mode] 匯入監聽專用模組
from listen_driver import AsyncListenTcpDriver
from listen_master import ListenMasterDispatcher

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("Main")


class ProfileLoadError(Exception):
    """地圖檔載入失敗 —— 隔離該台設備，不影響其他設備與 WebUI"""
    pass


DRIVER_FACTORY = {
    "rtu": RobustAsyncTcpDriver,
    "tcp": AsyncModbusTcpDriver,
    "usb": LocalSerialDriver,
}

# =============================================================================
# Plugin Loader
# =============================================================================
# 與 WebUI catalog 共用同一套 discovery 規則；helper 不建立 Gateway 或 transport。
ADAPTER_FACTORY = load_adapters(logger=logger)

# =============================================================================
# EdgeGateway 核心邏輯
# =============================================================================
class EdgeGateway:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.running = False
        self.start_time = time.monotonic()

        self.driver = None
        self.bus_master: BusMasterScheduler | None = None

        self.listen_driver = None
        self.listen_master: ListenMasterDispatcher | None = None

        self.mqtt_client: RobustMQTTClient | None = None
        self.ha_managers: dict[int, HAManager] = {}
        self.node_id = "edge_gw"

        # ✅ [Fix V2.7] health 發布結果可觀測。paho 的 publish() 失敗只回非零 rc、
        #    不拋例外，原本的 `except: pass` 幾乎是死碼，rc 被完全忽略 ——
        #    「health monitor 還活著但 broker 收不到」在此之前零診斷。
        self._health_publish_failures = 0   # 累計失敗次數（會放進 health payload）
        self._health_failed_streak = 0      # 連續失敗次數（供節流與恢復判定）

        self._cmd_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bg_tasks = []
        self._shutdown_task = None

        self._stopping = False
        self._shutdown_event = asyncio.Event()
        self.discovery_prefix = "homeassistant"

        # 🚨 被隔離的設備清單（地圖／Adapter 問題），供啟動告警、WebUI 橫幅、MQTT health
        self.quarantined: list[dict] = []

        # Discovery 分批派送：每台之間的間隔秒數，與其背景任務控制代碼
        self.discovery_interval: float = 2.0
        self._discovery_task: asyncio.Task | None = None

        # 🚨 補丁 2：防禦性初始化，避免其他 Task 提早讀取造成 AttributeError
        self.sniffer_mode = False
        self._sniffer_driver = None

    def _load_config(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:
            logger.critical(f"無法讀取設定檔 {self.config_path}", exc_info=True)
            sys.exit(1)

    def _load_profile(self, profile_name: str) -> dict:
        """
        載入地圖檔。失敗一律拋 ProfileLoadError，由呼叫端逐台隔離。

        ✅ [Fix] 原本四處 sys.exit(1) 會讓「一台設備的地圖問題」殺掉整個網關：
           config.yaml 裡 profile 打錯一個字，四台設備全停，而且 WebUI 是 daemon
           thread 會跟著死、docker restart:unless-stopped 造成崩潰迴圈，改壞設定的
           人反而無法從 WebUI 改回來，只能 SSH 救援。改為隔離單台 + 啟動彙總告警。
        """
        try:
            with open(f"{profile_name}.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            data = None
        except OSError as e:
            raise ProfileLoadError(f"地圖檔 {profile_name}.yaml 讀取失敗: {e}")
        except yaml.YAMLError as e:
            raise ProfileLoadError(f"地圖檔 {profile_name}.yaml YAML 語法錯誤: {e}")
        else:
            if not isinstance(data, dict):
                raise ProfileLoadError(
                    f"地圖檔 {profile_name}.yaml 根結構必須是 dict，"
                    f"收到 {type(data).__name__}（空檔案或格式錯誤）"
                )
            return data

        # 找不到 .yaml，退回嘗試同名 Python 模組
        try:
            mod = importlib.import_module(profile_name)
        except Exception as e:
            raise ProfileLoadError(
                f"找不到地圖檔 {profile_name}.yaml，且無法載入同名模組: {e}"
                f"（請檢查 config.yaml 的 profile 欄位是否拼錯）"
            )

        result = {k: getattr(mod, k) for k in dir(mod) if not k.startswith('_')}
        if not result:
            raise ProfileLoadError(f"地圖模組 {profile_name} 未提供任何定義")
        return result

    def _quarantine(self, uid, profile_name: str, reason: str):
        """記錄被隔離的設備，供啟動彙總告警／WebUI／MQTT health 呈現"""
        self.quarantined.append({
            "uid": uid,
            "profile": profile_name,
            "reason": reason,
        })
        logger.error(f"🚨 UID={uid} [{profile_name}] 已隔離：{reason}")

    @staticmethod
    def _coerce_number(value, default, label: str):
        """
        「調校型」設定值的型別收斂：只接受有限的正數，否則退回預設值並告警。

        規則（int 保持 int、float 保持 float，小數完全合法）：
            15   → 15      0.5 → 0.5      7.5 → 7.5
            "20" → 預設    True → 預設    [] → 預設
            0    → 預設    -5   → 預設    .nan / .inf → 預設

        ✅ [Fix V2.8] 與「識別／路由型」欄位（uid、mode、adapter → 直接隔離）刻意採取
           不同策略：調校型欄位有合理預設，沒必要為了一個打錯的數字讓整台設備下線。
           既有的 mqtt.discovery_interval 就是這個做法，此處只是把它抽成共用函式。

           收斂在「消費點」而不是只靠 WebUI 門口驗證，是因為 config.yaml 也可能由
           host 手改（那正是本專案文件化的救援路徑），門口在那條路上並不存在。

        ✅ [Fix V2.9] 修正 V2.8 的三個實錯：
           (1) 原本用 float(value) 收斂，連 "20" 這類字串也接受 —— 設定檔的數值欄位
               就該是 YAML 數字型別，字串一律視為打錯。
           (2) 原本的 as_int=True 會把 0.5 直接 int() 成 0。offline_time 被截成 0 後，
               _reschedule() 對離線設備算出 base+0 → 立刻重排 → 全速空轉打總線。
               現已移除 as_int：BusMaster/ListenMaster 對這些秒數只做加減比較，
               0.5 秒完全合法。
           (3) 未擋 .inf：poll_interval=.inf 會讓 next_time 變無限大（該台永不再輪詢，
               且無任何日誌，正是本函式要消滅的失效模式）；offline_time=.inf 則會在
               int() 拋 OverflowError —— 而原本的 try 只攔 TypeError/ValueError，
               例外會直接穿出去殺掉整台網關。現以 math.isfinite() 一併擋下。
        """
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and value > 0:
                return value
        logger.warning(
            f"{label} 設定值無效（收到 {value!r}），必須是大於 0 的有限數字，改用預設 {default}"
        )
        return default

    def _preflight_devices(self, cfg) -> list:
        """
        設定 preflight：在任何 d.get() 之前把 devices 收斂成「純 dict 元素的 list」。

        ✅ [Fix V2.6] 原本 has_active/has_listen 直接對 cfg["devices"] 的每個元素呼叫
           .get()，`devices: [42]` 或 `devices: [null]` 會拋 AttributeError，穿到
           __main__ 後被無聲 sys.exit(1) 吃掉 —— 單筆設定錯誤升級成全機消失，
           且 docker logs 完全空白、WebUI daemon 一併蒸發。

        壞元素一律走既有的 _quarantine()（自動出現在啟動橫幅與 health 的 quarantined
        欄位），不新增任何告警管道。全部壞掉時回傳空 list，等同 `devices: []`，
        自然落入既有的純 WebUI 救援模式 —— 不需要新增模式旗標。
        """
        if not isinstance(cfg, dict):
            self._quarantine("(全部)", "(config.yaml)",
                             f"YAML 根結構必須是字典，收到 {type(cfg).__name__}；"
                             f"全部設備已停用，請至 WebUI 修正")
            return []

        raw = cfg.get("devices")
        if raw is None:
            return []

        if not isinstance(raw, list):
            self._quarantine("(全部)", "(config.yaml)",
                             f"devices: 必須是清單，收到 {type(raw).__name__}；"
                             f"全部設備已停用，請至 WebUI 修正")
            return []

        clean = []
        for idx, dev in enumerate(raw, 1):
            if not isinstance(dev, dict):
                self._quarantine(f"(devices 第 {idx} 筆)", "(未指定)",
                                 f"devices 條目必須是字典，收到 {type(dev).__name__}: {dev!r}")
                continue
            clean.append(dev)
        return clean

    @staticmethod
    def _require(cfg: dict, *keys: str):
        node = cfg
        path = []
        for k in keys:
            path.append(k)
            if not isinstance(node, dict) or k not in node:
                logger.critical(f"config.yaml 缺少必填欄位: {' → '.join(path)}")
                sys.exit(1)
            node = node[k]
        return node

    def unregister_device(self, uid: int):
        """安全卸載設備，僅清除 HA 狀態與管理器快取 (Adapter 清理交由 stop 處理)"""
        if uid in self.ha_managers:
            try:
                # 1. 先設為離線
                self.ha_managers[uid].set_availability(False)

                # 2. 🚀 [Fix] 消除 Discovery 殭屍：發送 Cleanup 抹除 Broker 上的 retain 訊息
                self.ha_managers[uid].send_discovery(cleanup=True)

                # 3. 清理本機快取
                self.ha_managers[uid].cleanup()
            except Exception as e:
                logger.warning(f"清理設備 {uid} 狀態時發生異常: {e}")
            del self.ha_managers[uid]

        if self.bus_master and uid in self.bus_master.adapters:
            self.bus_master.unregister_device(uid)
        if self.listen_master and uid in self.listen_master.adapters:
            self.listen_master.unregister_device(uid)

        logger.info(f"✅ 設備已卸載: UID={uid}")

    def _on_mqtt_connected(self):
        logger.info("MQTT 上線，執行 Discovery 與訂閱...")
        cmd_topic = f"{self.node_id}/+/+/set/+"
        self.mqtt_client.subscribe(cmd_topic, qos=1)

        restart_topic_sub = f"{self.node_id}/system/set/restart"
        self.mqtt_client.subscribe(restart_topic_sub, qos=1)

        if self.mqtt_client:
            discovery_prefix = self.discovery_prefix
            gw_device_info = {
                "identifiers": [self.node_id],
                "name": self.node_id,
                "manufacturer": "Python Gateway",
                "model": "Edge Gateway V3.9"
            }
            gw_discovery_topic = f"{discovery_prefix}/binary_sensor/{self.node_id}_status/config"
            gw_payload = {
                "name": "連線狀態",
                "object_id": f"{self.node_id}_status",
                "unique_id": f"{self.node_id}_status",
                "state_topic": f"{self.node_id}/status",
                "device_class": "connectivity",
                "payload_on": "online",
                "payload_off": "offline",
                "device": gw_device_info
            }
            # ✅ [V2.15] 檢查 rc（report/060 F1）。這兩則原本丟棄回傳值，而 paho 的
            #    publish() 失敗只回非零 rc、不拋例外，故失敗時完全無痕跡。
            #    後果不對稱地嚴重：每個 HA 實體都是雙 topic + availability_mode: all，
            #    網關這則沒送成功，broker 上就留著上一輪 LWT 的 offline ——
            #    **四台設備的每一顆實體全部 unavailable**，即使資料照樣在 state
            #    topic 上流動。而日誌只會說「MQTT 上線」，查不到根因。
            #    任何一次 MQTT 重連都會重跑本函式而自動修復，故不另做重試，
            #    只要讓根因在日誌裡看得見即可。
            r = self.mqtt_client.publish(gw_discovery_topic, json.dumps(gw_payload), qos=1, retain=True)
            if getattr(r, "rc", 0) != 0:
                logger.error(
                    f"[MQTT] 🚨 網關 Discovery 發布失敗 rc={r.rc} —— "
                    f"HA 上的網關連線狀態實體可能不會出現"
                )

            r = self.mqtt_client.publish(f"{self.node_id}/status", "online", qos=1, retain=True)
            if getattr(r, "rc", 0) != 0:
                logger.error(
                    f"[MQTT] 🚨 網關 online 狀態發布失敗 rc={r.rc} —— "
                    f"broker 上仍是舊值（很可能是上一輪 LWT 的 offline），"
                    f"HA 上【全部設備的所有實體】將顯示不可用，直到下一次 MQTT 重連"
                )

        # ✅ [Fix] 設備層 Discovery 改為分批派送。
        #    本函式跑在 paho 網路執行緒上，絕不可在此 sleep —— 會癱瘓收訊與 keepalive。
        #    因此丟回 asyncio loop，以 await asyncio.sleep 拉開每台的間隔。
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule_staggered_discovery)
        else:
            # Loop 尚未就緒的極早期路徑：退回同步一次發完，確保不漏發
            for ha_mgr in self.ha_managers.values():
                ha_mgr.reset_discovery()
                ha_mgr.send_discovery(cleanup=False)
                ha_mgr.republish_availability()
                ha_mgr.republish_state()

    def _schedule_staggered_discovery(self):
        """在 event loop 執行緒上建立分批任務；重連時取代前一個未完成的任務"""
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
        self._discovery_task = self._loop.create_task(self._staggered_discovery())

    async def _staggered_discovery(self):
        """
        逐台發送 Discovery 與可用性，每台之間間隔 discovery_interval 秒。

        每台約 51 則 retained 訊息，四台合計 204 則。一次全部灌出雖然仍在 paho
        佇列上限內，但分批可避免 Broker／HA 端瞬時尖峰，也讓重連時的洪流風險歸零。
        """
        mgrs = list(self.ha_managers.items())
        total = len(mgrs)
        if not total:
            return
        try:
            for i, (uid, ha_mgr) in enumerate(mgrs, 1):
                ha_mgr.reset_discovery()
                ha_mgr.send_discovery(cleanup=False)
                ha_mgr.republish_availability()
                ha_mgr.republish_state()
                logger.info(f"[Discovery] ({i}/{total}) UID={uid} 已送出")
                if i < total:
                    await asyncio.sleep(self.discovery_interval)
            logger.info(f"[Discovery] 全部 {total} 台送出完畢"
                        f"（間隔 {self.discovery_interval}s）")
        except asyncio.CancelledError:
            logger.info("[Discovery] 分批派送已中止（MQTT 重連或關機）")
            raise

    def _on_mqtt_message(self, msg):
        if self._loop is None or self._loop.is_closed():
            return

        def _put():
            try:
                self._cmd_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.error(f"[MQTT Bridge] 🚨 佇列滿載(>500)，執行負載拋棄，丟棄指令 topic={msg.topic}")

        self._loop.call_soon_threadsafe(_put)

    async def _mqtt_consumer_task(self):
        logger.info("[MQTT Consumer] 已啟動")
        while self.running:
            try:
                try:
                    msg = await asyncio.wait_for(self._cmd_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if msg.topic == f"{self.node_id}/system/set/restart":
                    if self._loop and not self._loop.is_closed():
                        if not self._shutdown_task:
                            self._shutdown_task = self._loop.create_task(self.stop())
                    continue

                topic_parts = msg.topic.split('/')
                if len(topic_parts) < 5 or topic_parts[3] != "set":
                    # ✅ [v2.12] 原為裸 continue：指令消失且零日誌。
                    logger.warning(
                        f"[Command] 丟棄：topic 格式不符 set 契約 topic={msg.topic}"
                    )
                    continue

                uid_str = topic_parts[2]
                key = topic_parts[4]
                try:
                    uid = int(uid_str)
                except ValueError:
                    # ✅ [v2.12] 原為裸 continue。
                    logger.warning(
                        f"[Command] 丟棄：UID 非整數 uid='{uid_str}' topic={msg.topic}"
                    )
                    continue

                try:
                    payload_str = msg.payload.decode('utf-8').strip()
                except Exception:
                    # ✅ [v2.12] 原為裸 continue：在 HA 按了沒反應且查無痕跡。
                    logger.warning(
                        f"[Command] 丟棄：payload 非 UTF-8 uid={uid} key={key} "
                        f"({len(msg.payload)} bytes)"
                    )
                    continue

                if self.listen_master and uid in self.listen_master.adapters:
                    logger.warning(f"[Command] 拒絕寫入：UID={uid} 為唯讀監聽模式")
                    continue

                logger.info(f"[Command] UID={uid} key={key} value={payload_str}")

                if self.bus_master and uid in self.bus_master.adapters:
                    await self.bus_master.submit_write(uid, key, payload_str)
                else:
                    # ✅ [Fix] 原本此處靜默 fall-through：上面已印「收到指令」，
                    #    之後指令人間蒸發，在 HA 按了沒反應卻查無任何線索。
                    #    最常見成因是該設備地圖／Adapter 有問題被隔離（見啟動告警）。
                    q = next((x for x in self.quarantined if x.get("uid") == uid), None)
                    if q:
                        logger.error(
                            f"[Command] 🚨 丟棄寫入 UID={uid} key={key}："
                            f"該設備啟動時已被隔離（profile={q['profile']}：{q['reason']}），"
                            f"請至 WebUI 修正設定後重啟"
                        )
                    elif not self.bus_master:
                        logger.error(
                            f"[Command] 🚨 丟棄寫入 UID={uid} key={key}："
                            f"目前為純旁路監聽模式，未啟用主動總線，無法寫入"
                        )
                    else:
                        logger.error(
                            f"[Command] 🚨 丟棄寫入 UID={uid} key={key}："
                            f"UID 未掛載於主動總線（已掛載：{sorted(self.bus_master.adapters)}），"
                            f"請確認 config.yaml 的 uid 與 mode 設定"
                        )

            except asyncio.CancelledError:
                logger.info("[MQTT Consumer] 任務已安全中止")
                break
            except Exception:
                logger.exception("[MQTT Consumer] 單次迭代發生未預期例外，0.5s 後繼續")
                await asyncio.sleep(0.5)

    async def _health_monitor_task(self):
        logger.info("[Health Monitor] 已啟動")
        health_topic = f"{self.node_id}/health"

        while self.running:
            try:
                await asyncio.sleep(60)
                if not self.mqtt_client:
                    continue

                devices_health = {}
                if self.bus_master:
                    for uid, state in self.bus_master.device_states.items():
                        devices_health[str(uid)] = {
                            "online":        state.get("online", False),
                            "timeout_count": state.get("timeout_count", 0),
                            "success_count": state.get("success_count", 0),
                            # ✅ [V2.13] 「Modbus 讀得到但 MQTT 送不出去」的次數。
                            #    設備仍算 ONLINE（正確），但此欄位讓資料鏈斷在
                            #    北向這件事在 health 上看得見（report/056 F2）。
                            "state_publish_failures": state.get("state_publish_failures", 0),
                            "mode":          "active"
                        }

                if self.listen_master:
                    for uid, state in self.listen_master.device_states.items():
                        devices_health[str(uid)] = {
                            "online":        state.get("online", False),
                            "timeout_count": 0,
                            "success_count": 1 if state.get("online") else 0,
                            "mode":          "listen"
                        }

                payload: dict = {
                    "uptime_s":       int(time.monotonic() - self.start_time),
                    "cmd_queue_size": self._cmd_queue.qsize(),
                    "devices":        devices_health,
                    # 🚨 隔離清單一併廣播，讓 HA 端也能對「設定改錯」告警
                    "quarantined":    self.quarantined,
                    # ✅ [Fix V2.7] 累計 health 發布失敗次數。收到的這則必然是成功的，
                    #    所以此欄位的用途是「恢復後告訴你剛才漏掉幾則」。
                    "health_publish_failures_total": self._health_publish_failures,
                    # ✅ [V2.13] 訂閱是北向指令的唯一入口。broker 以 ACL 拒絕訂閱時
                    #    連線正常、Discovery 照發、HA 上按鈕俱在，但指令永遠到不了
                    #    網關（report/056 F1）。這兩個欄位讓該狀態在 MQTT 側可見，
                    #    不必翻 docker log。正常時為 0 與空陣列。
                    "mqtt_subscribe_failures_total": getattr(self.mqtt_client, "subscribe_failures", 0),
                    "mqtt_rejected_subscriptions": sorted(
                        getattr(self.mqtt_client, "rejected_topics", set())
                    ),
                }

                # ✅ [Fix V2.11] 監聽軌解碼鎖定狀態必須可觀測。熔斷鎖定後設備只是
                #    unavailable，與「單純沒收到資料」在 HA 上外觀完全相同；沒有這個
                #    欄位，操作者只能翻 docker log 才知道是熔斷還是斷線。
                if self.listen_master is not None:
                    payload["listen_decoding_locked"] = bool(
                        getattr(self.listen_master, "_decoding_disabled", False)
                    )

                # ✅ [Fix V2.7] 檢查 publish 的 rc。paho 失敗只回非零 rc、不拋例外，
                #    原本的 `except: pass` 幾乎是死碼。節流規則：第一次失敗印一次，
                #    之後每 10 次印一次（health 本身 60s 一輪，不會洗版）。
                try:
                    result = self.mqtt_client.publish(
                        health_topic, json.dumps(payload), qos=0, retain=False
                    )
                    rc = getattr(result, "rc", 0)
                except Exception:
                    logger.exception("[Health Monitor] health payload 組裝或發布時發生例外")
                    rc = -1

                if rc != 0:
                    self._health_publish_failures += 1
                    self._health_failed_streak += 1
                    if self._health_failed_streak == 1 or self._health_failed_streak % 10 == 0:
                        logger.warning(
                            f"[Health Monitor] health 發布失敗 rc={rc}"
                            f"（連續 {self._health_failed_streak} 次，累計 {self._health_publish_failures} 次）"
                            f"—— HA 端此刻收不到健康度，請檢查 MQTT broker"
                        )
                elif self._health_failed_streak:
                    logger.info(
                        f"[Health Monitor] health 發布已恢復"
                        f"（本次中斷連續 {self._health_failed_streak} 次，累計 {self._health_publish_failures} 次）"
                    )
                    self._health_failed_streak = 0

            except asyncio.CancelledError:
                logger.info("[Health Monitor] 任務已安全中止")
                break
            except Exception:
                logger.exception("[Health Monitor] 單次迭代發生未預期例外，0.5s 後繼續")
                await asyncio.sleep(0.5)

    async def start(self):
        cfg = self._load_config()

        # ✅ [Fix V2.6] 設定 preflight 必須跑在任何 cfg.get()／d.get() 之前
        devices = self._preflight_devices(cfg)
        devices_declared = len(devices) + len(self.quarantined)
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["devices"] = devices

        sys_cfg = cfg.get("system", {})
        if not isinstance(sys_cfg, dict):
            logger.error(f"config.yaml 的 system: 必須是字典，收到 {type(sys_cfg).__name__}，改用預設值")
            sys_cfg = {}
        self.node_id = sys_cfg.get("node_id", "edge_gw_01")
        log_level = str(sys_cfg.get("log_level", "INFO")).upper()
        logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
        self._loop = asyncio.get_running_loop()

        self._shutdown_event.clear()
        self._stopping = False

        # ✅ [Fix V2.8] bus.offline_time 一次收斂、兩軌共用。
        #    原本兩處都是 int(cfg.get("bus",{}).get("offline_time",60))，
        #    `offline_time: bad` 會拋 ValueError 殺掉整台網關；而 `bus:` 若不是 dict
        #    連 .get() 都會 AttributeError。
        bus_cfg = cfg.get("bus")
        if bus_cfg is not None and not isinstance(bus_cfg, dict):
            logger.warning(f"config.yaml 的 bus: 必須是字典，收到 {type(bus_cfg).__name__}，改用預設值")
            bus_cfg = {}
        elif bus_cfg is None:
            bus_cfg = {}
        offline_time = self._coerce_number(
            bus_cfg.get("offline_time", 60), 60, "bus.offline_time"
        )

        has_active = any(d.get("mode", "active") == "active" for d in devices)
        has_listen = any(d.get("mode") == "listen" for d in devices)

        if has_active and has_listen:
            act_port = cfg.get("driver", {}).get("port")
            lst_port = cfg.get("listen_driver", {}).get("port")
            if act_port and lst_port and str(act_port) == str(lst_port):
                logger.critical(f"🚨 致命錯誤：禁止 Active 與 Listen 模式共用同一通訊埠 (Port: {act_port})，會引發實體碰撞！")
                sys.exit(1)

        # ==================== 軌道 1：初始化主動總線 ====================
        if has_active:
            drv_cfg = cfg.get("driver", {})
            d_type = drv_cfg.get("type", "rtu")
            DriverClass = DRIVER_FACTORY.get(d_type)
            if not DriverClass:
                logger.critical(f"啟動失敗：不支援的主動 Driver 類型 '{d_type}'")
                sys.exit(1)

            if d_type in ("rtu", "tcp"):
                self._require(drv_cfg, "host")
                self._require(drv_cfg, "port")
            elif d_type == "usb":
                self._require(drv_cfg, "port")
                self._require(drv_cfg, "baudrate")

            drv_params = {k: v for k, v in drv_cfg.items() if k != "type"}
            try:
                self.driver = DriverClass(**drv_params)
            except TypeError as e:
                logger.critical(f"啟動失敗：主動 Driver 初始化參數錯誤: {e}")
                sys.exit(1)

            if not await self.driver.connect():
                target = (f"{drv_cfg.get('host')}:{drv_cfg.get('port')}" if d_type in ("rtu", "tcp")
                          else f"{drv_cfg.get('port')} @ {drv_cfg.get('baudrate')}bps")
                logger.critical(
                    f"啟動失敗：主動總線 driver type={d_type} 無法連線至 {target}。"
                    f"WebUI 為 daemon thread，會隨本次退出一併終止；"
                    f"若需進入 WebUI 修改設定，請在 host 上編輯 bind-mount 的 profile/config.yaml，"
                    f"將 devices: 清空後重啟，即可進入純 WebUI 模式開機。"
                )
                sys.exit(1)

            self.bus_master = BusMasterScheduler(self.driver, offline_time=offline_time)
        else:
            # ✅ [Fix V2.6] 區分「有 listen 設備」與「一台都沒有」。
            #    原本兩種情況都印「純旁路監聽模式」，操作者清空 devices: 進 WebUI
            #    搶修時，看到的模式名稱是錯的。
            if has_listen:
                logger.info("🔌 未偵測到主動設備，略過主動總線初始化，進入 [純旁路監聽模式]")
            else:
                logger.warning(
                    "🔌 設定中沒有任何可掛載的設備，不建立通訊 driver，"
                    "進入 [純 WebUI 救援模式]：MQTT、health 與 WebUI 仍運作，"
                    "請至 WebUI 修正 config.yaml 後重啟。"
                )

        # ==================== 軌道 2：初始化監聽總線 ====================
        if has_listen:
            ld_cfg = cfg.get("listen_driver")
            if not ld_cfg:
                logger.critical("啟動失敗：發現 mode=listen 設備，但 config.yaml 缺少 listen_driver 設定塊")
                if self.driver: await self.driver.disconnect()
                sys.exit(1)

            ld_type = ld_cfg.get("type", "tcp")

            if ld_type == "tcp":
                self._require(ld_cfg, "host")
                self._require(ld_cfg, "port")
                self.listen_driver = AsyncListenTcpDriver(host=ld_cfg["host"], port=ld_cfg["port"])
            elif ld_type == "usb":
                self._require(ld_cfg, "port")
                self._require(ld_cfg, "baudrate")
                from listen_driver import AsyncListenSerialDriver
                self.listen_driver = AsyncListenSerialDriver(port=ld_cfg["port"], baudrate=ld_cfg["baudrate"])
            else:
                logger.critical(f"啟動失敗：監聽驅動暫不支援類型 '{ld_type}'")
                if self.driver: await self.driver.disconnect()
                sys.exit(1)

            # ✅ [Fix V2.11] 監聽端首次連線失敗不再 sys.exit(1)。
            #    原本會殺掉整個網關，WebUI（daemon thread）一併消失 —— 但「USB 裝置
            #    此刻不在」與「設定檔寫壞」是完全不同性質的問題：前者是暫時性的實體
            #    狀態（插拔、udev 重新列舉、開機競速），本來就該靠重連解決。
            #    AsyncListenSerialDriver.read_stream() 已內建重連：偵測到未連線即呼叫
            #    _reconnect()，1s 起指數退避、USB 上限 10s、含 jitter，連上後歸零。
            #    因此這裡只需「不要提前退出」，既有機制就會接手，無須新增狀態機。
            #    ⚠️ 僅放寬「實體 connect() 回傳 false」這一種情況；缺必填欄位與
            #       不支援的 driver type 仍維持 fatal（見上方 _require 與 else 分支）。
            if not await self.listen_driver.connect():
                target = (f"{ld_cfg.get('host')}:{ld_cfg.get('port')}" if ld_type == "tcp"
                          else f"{ld_cfg.get('port')} @ {ld_cfg.get('baudrate')}bps")
                logger.error(
                    f"⚠️ 監聽總線 driver type={ld_type} 首次連線失敗（目標：{target}）。\n"
                    f"  · 網關【不會】因此退出：WebUI、MQTT、health 全部照常運作。\n"
                    f"  · 監聽設備先標記為 unavailable，由 driver 既有的退避重連持續嘗試\n"
                    f"    （1s 起指數退避，USB 上限 10s，含 jitter），連上後第一個有效幀即恢復 ONLINE。\n"
                    f"  · 若長時間未恢復，請確認裝置是否存在：ls -l {ld_cfg.get('port')}"
                )

            self.listen_master = ListenMasterDispatcher(self.listen_driver, offline_time=offline_time)

        # ==================== MQTT 與設備掛載 ====================
        mqtt_cfg = cfg.get("mqtt", {})
        self.discovery_prefix = mqtt_cfg.get("discovery_prefix", "homeassistant")
        try:
            self.discovery_interval = max(0.0, float(mqtt_cfg.get("discovery_interval", 2.0)))
        except (TypeError, ValueError):
            logger.warning("mqtt.discovery_interval 型別錯誤，改用預設 2.0s")
            self.discovery_interval = 2.0

        mqtt_username = os.getenv("MQTT_USERNAME")
        mqtt_password = os.getenv("MQTT_PASSWORD")
        if not mqtt_username or not mqtt_password:
            logger.critical(
                "啟動失敗：缺少 MQTT_USERNAME 或 MQTT_PASSWORD；"
                "請在專案根目錄 .env 設定後再啟動。"
            )
            if self.listen_driver:
                await self.listen_driver.disconnect()
            if self.driver:
                await self.driver.disconnect()
            sys.exit(1)

        self.mqtt_client = RobustMQTTClient(
            broker=self._require(mqtt_cfg, "broker"),
            port=mqtt_cfg.get("port", 1883),
            client_id=self.node_id,
            username=mqtt_username,
            password=mqtt_password,
        )
        self.mqtt_client.set_lwt(f"{self.node_id}/status", payload="offline", retain=True)
        self.mqtt_client.on_connected_callback = self._on_mqtt_connected

        def _bridged_on_message(client, userdata, msg):
            try:
                self._on_mqtt_message(msg)
            except Exception:
                logger.exception("[MQTT Bridge] Callback 執行異常")

        self.mqtt_client.client.on_message = _bridged_on_message

        seen_uids = set()
        for dev in cfg.get("devices", []):
            uid = dev.get("uid")
            adapter_name = dev.get("adapter", "generic")
            profile_name = dev.get("profile")
            device_type = dev.get("device_type", "device")
            mode = dev.get("mode", "active")
            poll_interval = dev.get("poll_interval", 10)

            if uid is None or not profile_name:
                self._quarantine(uid, profile_name or "(未指定)",
                                 "config.yaml 條目缺少 uid 或 profile 欄位")
                continue

            original_uid = uid
            if isinstance(uid, bool):
                uid = None
            elif isinstance(uid, int):
                pass
            elif isinstance(uid, str):
                try:
                    uid = int(uid.strip())
                except (TypeError, ValueError):
                    uid = None
            elif isinstance(uid, float) and uid.is_integer():
                uid = int(uid)
            else:
                uid = None

            if uid is None:
                self._quarantine(
                    repr(original_uid), profile_name,
                    f"UID 型別或數值無法無損轉為整數：{original_uid!r}"
                )
                continue

            if mode == "listen" and uid < 0:
                self._quarantine(
                    str(uid), profile_name,
                    f"監聽軌 UID 必須大於或等於 0，收到 {uid}"
                )
                continue
            if mode != "listen" and uid <= 0:
                self._quarantine(
                    str(uid), profile_name,
                    f"主動軌 UID 必須為正整數，收到 {uid}"
                )
                continue

            if not isinstance(mode, str) or mode not in {"active", "listen"}:
                self._quarantine(
                    str(uid), profile_name,
                    f"mode 設定值 {mode!r} 無效；合法值為 'active' 或 'listen'"
                )
                continue

            if uid in seen_uids:
                self._quarantine(
                    uid, profile_name,
                    f"重複 UID={uid}：UID 必須在 active 與 listen 兩軌間全域唯一"
                )
                continue
            seen_uids.add(uid)

            # ✅ [Fix V2.8] adapter 是「識別／路由」欄位，猜不出正確值 → 隔離該台。
            #    原本直接 ADAPTER_FACTORY.get(adapter_name)，`adapter: []` 會拋
            #    TypeError: unhashable type: 'list' 並殺掉整台網關
            #    —— 與 V2.5 修掉的 uid unhashable 是同一類問題。
            if not isinstance(adapter_name, str) or not adapter_name.strip():
                self._quarantine(
                    uid, profile_name,
                    f"adapter 必須是非空字串，收到 {type(adapter_name).__name__}: {adapter_name!r}"
                )
                continue
            adapter_name = adapter_name.strip()

            # ✅ [Fix V2.8] poll_interval 是「調校」欄位，有合理預設 → 收斂＋告警，
            #    不必為了一個打錯的數字讓整台設備下線。
            #    原本未驗證就交給 BusMaster，`poll_interval: []` 會在 _reschedule()
            #    的 `base_time + interval` 拋 TypeError；該例外被 _arbitration_loop
            #    的 except Exception 接住後只 log 一次並續跑，但該設備已被 pop 出
            #    slow_heap 且永遠不會再被 push 回去 —— 單台從此無聲停止輪詢。
            poll_interval = self._coerce_number(
                dev.get("poll_interval", 10), 10, f"UID={uid} 的 poll_interval"
            )

            AdapterClass = ADAPTER_FACTORY.get(adapter_name)
            if not AdapterClass:
                self._quarantine(uid, profile_name,
                                 f"找不到 Adapter '{adapter_name}'（可用：{sorted(ADAPTER_FACTORY)}）")
                continue

            try:
                profile_data = self._load_profile(profile_name)
            except ProfileLoadError as e:
                self._quarantine(uid, profile_name, str(e))
                continue
            except Exception as e:
                self._quarantine(uid, profile_name, f"地圖檔載入時發生未預期錯誤: {e}")
                continue

            validation_errors = validate_profile(profile_name, profile_data)
            if validation_errors:
                for err in validation_errors:
                    logger.error(err)
                self._quarantine(uid, profile_name,
                                 f"地圖檔校驗失敗（{len(validation_errors)} 項錯誤，詳見上方日誌）")
                continue

            try:
                adapter = AdapterClass(uid, profile_data)
            except Exception as e:
                logger.exception(f"UID={uid} Adapter 實例化失敗")
                self._quarantine(uid, profile_name, f"Adapter '{adapter_name}' 實例化失敗: {e}")
                continue

            if mode == "listen":
                if not hasattr(adapter, "feed"):
                    self._quarantine(uid, profile_name,
                                     f"Adapter '{adapter_name}' 缺少監聽必備方法 feed()")
                    continue
            else:
                missing = [m for m in ["build_poll_read", "build_verify_read", "encode_write", "decode"] if not hasattr(adapter, m)]
                if missing:
                    self._quarantine(uid, profile_name,
                                     f"Adapter '{adapter_name}' 缺少主動必備方法 {missing}")
                    continue

            ha_mgr = HAManager(
                mqtt_client=self.mqtt_client,
                node_id=self.node_id,
                device_type=device_type,
                uid=uid,
                rmap=profile_data,
                discovery_prefix=self.discovery_prefix
            )
            self.ha_managers[uid] = ha_mgr

            if mode == "listen":
                if not self.listen_master:
                    del self.ha_managers[uid]
                    self._quarantine(
                        uid, profile_name,
                        "監聽軌未啟用；請檢查 mode 拼字與 listen_driver: 設定塊"
                    )
                    continue
                if self.listen_master.register_device(uid, adapter, ha_mgr):
                    logger.info(f"✅ 掛載 [監聽] 設備: UID={uid} adapter={adapter_name}")
                else:
                    del self.ha_managers[uid]
                    self._quarantine(uid, profile_name, "監聽軌拒絕掛載設備")
                    continue
            else:
                if not self.bus_master:
                    del self.ha_managers[uid]
                    self._quarantine(
                        uid, profile_name,
                        "主動軌未啟用；請檢查 mode 拼字與 driver: 設定塊"
                    )
                    continue
                if self.bus_master.register_device(uid, adapter, ha_mgr, poll_interval):
                    logger.info(f"✅ 掛載 [主動] 設備: UID={uid} adapter={adapter_name}")
                else:
                    del self.ha_managers[uid]
                    self._quarantine(uid, profile_name, "主動軌拒絕掛載設備")
                    continue

        self.running = True
        self.mqtt_client.connect()

        # ====== 🚨 攔截邏輯：Sniffer 模式 ======
        self.sniffer_mode = sys_cfg.get("enable_sniffer", False)

        if self.sniffer_mode:
            logger.warning("⚠️ 系統進入 [Sniffer 偵錯模式]！主動輪詢與旁路監聽已全面暫停，底層 I/O 交由 WebUI 獨占！")

            # 關鍵防護 1：釋放純旁路模式佔用的實體串口，否則 OS 會報錯 Device busy
            if self.listen_driver:
                await self.listen_driver.disconnect()

            # 關鍵防護 2：針對純監聽現場，建立專屬的「主動探測 Driver」
            ld_cfg = cfg.get("listen_driver")
            if not self.driver and ld_cfg and ld_cfg.get("type") == "usb":
                try:
                    from local_serial_driver import LocalSerialDriver
                    # 🚨 完整傳遞所有進階參數，與設定檔連動
                    self._sniffer_driver = LocalSerialDriver(
                        port=ld_cfg["port"],
                        baudrate=ld_cfg.get("baudrate", 9600),
                        timeout=ld_cfg.get("timeout", 5.0),
                        inter_frame_delay=ld_cfg.get("inter_frame_delay", 0.18)
                    )
                    await self._sniffer_driver.connect()

                    # 關鍵防護 3：強制清空 OS 殘留的 UART 垃圾資料
                    if hasattr(self._sniffer_driver, 'serial_conn'):
                        try:
                            self._sniffer_driver.serial_conn.reset_input_buffer()
                            self._sniffer_driver.serial_conn.reset_output_buffer()
                        except Exception:
                            pass
                    else:
                        logger.warning("⚠️ [Sniffer] 驅動未暴露 serial_conn，無法執行 FIFO Flush，首批讀取可能含殘包！")
                    logger.info(f"🛠️ [Sniffer] 探測專屬驅動已掛載至 {ld_cfg['port']}")
                except Exception as e:
                    logger.error(f"🛠️ [Sniffer] 探測驅動掛載失敗: {e}")

            # 故意不呼叫 self.bus_master.start() 與 self.listen_master.start()
        else:
            if self.bus_master:
                self.bus_master.start()
            if self.listen_master:
                self.listen_master.start()

        self._bg_tasks = [
            asyncio.create_task(self._mqtt_consumer_task()),
            asyncio.create_task(self._health_monitor_task())
        ]

        # 🚨 隔離彙總告警：放在啟動完成訊息之後，確保是操作者最後看到的內容
        logger.info("🚀 Edge Gateway V3.9 啟動完成")

        if self.quarantined:
            # ✅ [Fix V2.6] 用 preflight 前的宣告總數，否則被 preflight 剔除的壞條目
            #    不會計入分母，橫幅會顯示「1/1 未能掛載」這種看不出漏了幾台的數字。
            total = devices_declared
            bar = "═" * 66
            lines = [
                "",
                f"╔{bar}╗",
                f"║ 🚨 設定有誤：{len(self.quarantined)}/{total} 台設備未能掛載，已隔離",
                f"║ 網關與 WebUI 仍在運行，請至 WebUI 修正 config.yaml 或地圖檔後重啟",
                f"╠{bar}╣",
            ]
            for q in self.quarantined:
                lines.append(f"║ UID={q['uid']}  profile={q['profile']}")
                lines.append(f"║    └─ {q['reason']}")
            lines.append(f"╚{bar}╝")
            logger.critical("\n".join(lines))
        else:
            logger.info(f"✅ 全部 {len(self.ha_managers)} 台設備掛載正常，無隔離")

        await self._shutdown_event.wait()

        if self._shutdown_task:
            try:
                await self._shutdown_task
            except Exception:
                logger.exception("Shutdown task 發生異常")

    async def stop(self):
        if self._stopping or not self.running:
            return
        self._stopping = True

        logger.info("執行優雅關機...")
        self.running = False
        self._shutdown_event.set()

        if self.bus_master:
            try: await self.bus_master.stop()
            except Exception: pass

        if self.listen_master:
            try: await self.listen_master.stop()
            except Exception: pass

        # ✅ 取消 Discovery 分批任務，避免關機期間仍對已斷線的 Broker 發送
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except (asyncio.CancelledError, Exception):
                pass
        self._discovery_task = None

        for t in self._bg_tasks:
            t.cancel()

        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        close_tasks = []
        for uid in list(self.ha_managers.keys()):
            adapter = None
            if self.bus_master and uid in self.bus_master.adapters:
                adapter = self.bus_master.adapters[uid]
            elif self.listen_master and uid in self.listen_master.adapters:
                adapter = self.listen_master.adapters[uid]

            if adapter and hasattr(adapter, "close"):
                if asyncio.iscoroutinefunction(adapter.close):
                    close_tasks.append(adapter.close())
                else:
                    try: adapter.close()
                    except Exception: pass

            self.unregister_device(uid)

        self.ha_managers.clear()

        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

        if self.mqtt_client:
            if self.mqtt_client.client.is_connected():
                logger.info("發送最終 Gateway Offline 遺言...")
                msg_info = self.mqtt_client.client.publish(f"{self.node_id}/status", "offline", qos=1, retain=True)
                try:
                    await asyncio.wait_for(asyncio.to_thread(msg_info.wait_for_publish), timeout=2.0)
                    logger.info("Gateway 遺言發送成功")
                except asyncio.TimeoutError:
                    logger.warning("遺言發送逾時 (Broker 無回應)，強制斷線")
                except Exception as e:
                    logger.error(f"遺言發送異常: {e}")

            self.mqtt_client.disconnect()

        if self.driver:
            await self.driver.disconnect()

        if self.listen_driver:
            await self.listen_driver.disconnect()

        # 🚨 補丁 1：釋放 Sniffer 專屬驅動的串口 FD，防禦 Docker 重啟時遇到 Device busy
        if getattr(self, '_sniffer_driver', None):
            try:
                await self._sniffer_driver.disconnect()
            except Exception:
                pass
            self._sniffer_driver = None

        logger.info("💤 系統已安全停止，記憶體/Task 已徹底清理")

if __name__ == "__main__":
    gateway = EdgeGateway("config.yaml")

    # 🚨 將 Gateway 實體存入跨模組變數，讓 web_admin.py 可以取得
    import app_state
    app_state.gateway = gateway

    # WebUI 旁路掛載 (daemon=True 保證主程式結束時跟著結束)
    web_port = int(os.getenv("WEB_PORT", 8000))
    web_thread = threading.Thread(target=start_webui, args=(web_port,), daemon=True)
    web_thread.start()
    logger.info(f"🌐 WebUI 已啟動於 Port {web_port}")

    # 保留 V2.1 的防重入 Signal Handler
    def handle_signal(*args):
        logger.warning("收到終止訊號，開始優雅關機...")
        if gateway._shutdown_task:
            return
        if gateway._loop and not gateway._loop.is_closed():
            def _do_shutdown():
                if not gateway._shutdown_task:
                    gateway._shutdown_task = gateway._loop.create_task(gateway.stop())
            gateway._loop.call_soon_threadsafe(_do_shutdown)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(gateway.start())
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass
    except Exception:
        # ✅ [Fix V2.6] 原本這裡是裸的 sys.exit(1)：traceback 被吞、docker logs 一片空白，
        #    WebUI 因為是 daemon thread 也一併蒸發 —— 操作者完全沒有線索可查。
        #    改為印出完整 traceback 並附上與 driver 連線失敗同一份救援模板。
        logger.critical(
            "🚨 網關啟動時發生未預期例外，程序即將退出。"
            "WebUI 為 daemon thread，會隨本次退出一併終止；"
            "若需進入 WebUI 修改設定，請在 host 上編輯 bind-mount 的 profile/config.yaml，"
            "將 devices: 清空後重啟，即可進入純 WebUI 模式開機。",
            exc_info=True
        )
        sys.exit(1)
