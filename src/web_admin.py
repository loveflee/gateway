# =============================================================================
# web_admin.py - V6.7 終極側錄版 (支援 Sniffer 獨占 + 總線即時唯讀側錄)
# 修復歷程：
#   - [V6.1] restore 改用與 save 相同的原子替換路徑。原本 /api/restore 直接
#            shutil.copy2(BACKUP_PATH, CONFIG_PATH) 覆寫目標，複製途中若斷電或
#            I/O 失敗會留下半份 config.yaml；而 save 走的是 temp+fsync+os.replace。
#            同一模組兩條寫入路徑保護等級不一致，現統一由 _atomic_write_config()
#            負責，讀者永遠只會看到完整的舊檔或完整的新檔。
#   - [V6.2] [Critical] 把「會讓網關開不起來的設定」擋在門口，而不是讓它寫進磁碟：
#            (1) WebUI 有 Raw 模式（index.html:611-615 逐字採用 textarea 內容），
#                而伺服端原本只檢查根結構是不是 dict。操作者在 Raw 模式打壞 mqtt:
#                縮排 → 儲存成功 → 重啟 → main.py 的 _require() 直接 sys.exit(1)
#                → WebUI 隨 daemon thread 消失 →「還原備份」按鈕再也按不到。
#                新增 _validate_config()，鏡射 main.py 全部會 sys.exit 的設定條件，
#                不合格就回錯誤訊息且完全不寫檔。設定檔因此永遠不會壞。
#            (2) 備份寫入也改走原子路徑（原本是 shutil.copy2）。一份被截斷的 .bak
#                會被 V6.1 的 restore 原子地寫成一份壞掉的 config.yaml —— 救援機制
#                反而變成破壞機制。
#            (3) restore 前先 yaml.safe_load 驗證 .bak。原子化只能防「這次寫壞」，
#                驗證能防任何原因造成的壞備份（磁碟壞軌、手動誤改、舊版殘檔）。
#   - [V6.3] 門口驗證補上「下游型別／數值」條件（V6.2 只擋結構錯誤）：
#            bus.offline_time、mqtt.port、每台的 adapter 與 poll_interval、
#            以及 driver／listen_driver 的端點欄位（TCP: host 非空字串 + port 1-65535；
#            USB: port 非空字串 + baudrate 正整數）。
#            注意：門口只保護「從 WebUI 寫進來」的設定，host 手改會完全繞過它 ——
#            真正的安全網是 main.py V2.8 在消費點的型別收斂，此處只負責即時回饋。
#   - [V6.4] _bad_positive() 改與 main.py V2.9 的 _coerce_number() 同一套規則：
#            只接受 int/float（排除 bool）＋ finite ＋ 大於 0。原本用 float(v)，
#            "20" 這種字串會被錯誤接受、.inf 也會放行。
#            as_int 改為只【判定】是否為整數值（供 port／baudrate），不再做 int() 截斷。
#            小數在非整數欄位（poll_interval、bus.offline_time）完全合法。
#   - [V6.5] 收緊 as_int 的語意：port／baudrate／mqtt.port 只接受 YAML 的 int，
#            502.0（float）、"502"（str）、502.5 一律拒絕 —— 這些欄位語意上就是整數，
#            收到 float 代表設定檔打錯。V6.4 用 float(v).is_integer() 判定，
#            會把 502.0 誤判為合法。poll_interval／bus.offline_time 不受影響，
#            仍接受 int 或 float（0.5 秒合法）。
#   - [V6.6] WebUI 與 MQTT 帳密改由容器環境變數提供：WEB_USER／WEB_PASS 與
#            MQTT_USERNAME／MQTT_PASSWORD。拒絕將 MQTT 帳密寫入 config.yaml；還原舊
#            備份時自動移除其舊欄位，避免秘密重新落盤。
#   - [V6.7] Adapter／Profile 下拉改讀唯讀 catalog：Adapter 與 Gateway 啟動共用
#            同一外掛 discovery helper；Profile 掃描現有 .yaml 地圖。catalog 不收緊
#            config 驗證，且前端保留既有但已無法發現的值，避免救援設定被下拉選單吃掉。
# =============================================================================
import yaml, os, signal, threading, time, secrets, fcntl, shutil, math
import asyncio, concurrent.futures
import app_state  # 🚨 必須確保同目錄下有 app_state.py
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from adapter_catalog import discover_adapter_catalog

_sniffer_lock = threading.Lock()
app = FastAPI()
security = HTTPBasic()
CONFIG_PATH   = "/app/profile/config.yaml"
BACKUP_PATH   = "/app/profile/config.yaml.bak"
LOCAL_JS_PATH = "/app/profile/js-yaml.min.js"
LOCK_PATH     = CONFIG_PATH + ".lock"
PROFILE_DIR   = os.path.dirname(CONFIG_PATH)

# 🚨 指定分拆出去的 HTML 檔案路徑 (與 web_admin.py 在同一目錄)
INDEX_HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")

WEB_USER = os.getenv("WEB_USER")
WEB_PASS = os.getenv("WEB_PASS")
if not WEB_USER or not WEB_PASS:
    raise RuntimeError("缺少 WEB_USER 或 WEB_PASS；請在專案根目錄 .env 設定。")

def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok = (secrets.compare_digest(credentials.username.encode(), WEB_USER.encode()) and
          secrets.compare_digest(credentials.password.encode(), WEB_PASS.encode()))
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def get_index():
    # 實時讀取獨立的 index.html 檔案並回傳給瀏覽器
    if os.path.exists(INDEX_HTML_PATH):
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    else:
        raise HTTPException(status_code=404, detail="找不到 index.html 檔案，請確認它是否與 web_admin.py 放在同一目錄。")

@app.get("/api/js-yaml.min.js")
def get_js_yaml():
    if os.path.exists(LOCAL_JS_PATH):
        return FileResponse(LOCAL_JS_PATH, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="Local JS not found")

def _atomic_write(path: str, text: str):
    """
    以原子方式覆寫任一檔案：temp 檔 → fsync 檔案 → os.replace → fsync 目錄。

    ✅ [Fix V6.1/V6.2] save、backup、restore 三條路徑共用。讀者永遠只會看到完整的
       舊檔或完整的新檔，不會讀到寫到一半的殘檔。呼叫端必須自行持有 config.yaml.lock。
    """
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(text)
            tmp_f.flush()
            os.fsync(tmp_f.fileno())

        os.replace(tmp_path, path)
    except Exception:
        # 失敗時清掉半成品，避免 profile/ 累積殘留的 .tmp
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    dir_path = os.path.dirname(path) or "."
    dir_fd = os.open(dir_path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

# main.py 支援的 driver 型別；與 DRIVER_FACTORY 保持一致
_ACTIVE_DRIVER_TYPES = ("rtu", "tcp", "usb")
_LISTEN_DRIVER_TYPES = ("tcp", "usb")

def _validate_config(cfg) -> list:
    """
    鏡射 main.py 中「會導致 sys.exit(1)」的全部設定條件，回傳錯誤訊息清單。

    ✅ [Fix V6.2] 目的是讓壞設定根本寫不進磁碟 —— 因為一旦寫進去並重啟，網關會
       fatal 退出，而 WebUI 是 daemon thread 會跟著消失，操作者連「還原備份」都按不到。

    ⚠️ 只檢查 main.py 真的會 exit 的條件，不做額外的「不建議但能跑」判斷，
       以免擋掉本來可以正常啟動的設定。唯一的例外是 mqtt.broker 為空字串：
       main.py 不會因此 exit，但 MQTT 將永遠連不上、網關等同廢掉，而 WebUI 表單
       在欄位留白時就會產生它，是最容易踩到的一個，故一併擋下。
    """
    errs = []

    def _bad_positive(v, as_int=False):
        """
        回傳 True 代表「不是合法的正數」。兩種嚴格度：

        as_int=False —— poll_interval、bus.offline_time
            接受 YAML 的 int 或 float，須 finite 且 > 0。小數完全合法（0.5 秒可用）。

        as_int=True  —— port、baudrate、mqtt.port
            ✅ [Fix V6.5] 只接受 YAML 的 int。這些欄位語意上就是整數，
               收到 502.0（float）、"502"（str）、502.5 都代表設定檔打錯了，一律拒絕。
               V6.4 用 float(v).is_integer() 判定，會把 502.0 當成合法。

        bool 一律無效：YAML 的 yes/no 會被解析成 bool，而 isinstance(True, int) 為 True，
        不特別排除的話 `port: yes` 會變成 port=1。
        """
        if isinstance(v, bool):
            return True
        if as_int:
            return not isinstance(v, int) or v <= 0
        if not isinstance(v, (int, float)):
            return True
        return not math.isfinite(v) or v <= 0

    def _check_endpoint(blk_name: str, blk: dict, dtype: str):
        """依 driver type 檢查連線端點欄位；鏡射 main.py 的 _require 與各 driver 的期待"""
        if dtype == "usb":
            port = blk.get("port")
            if port is None:
                errs.append(f"{blk_name}.port 為必填（type=usb，應為裝置路徑）")
            elif not isinstance(port, str) or not port.strip():
                errs.append(f"{blk_name}.port 必須是非空字串（type=usb），收到 {port!r}")
            baud = blk.get("baudrate")
            if baud is None:
                errs.append(f"{blk_name}.baudrate 為必填（type=usb）")
            elif _bad_positive(baud, as_int=True):
                errs.append(f"{blk_name}.baudrate 必須是正整數（不接受小數或字串），收到 {baud!r}")
        else:  # rtu / tcp：TCP 端點
            host = blk.get("host")
            if host is None:
                errs.append(f"{blk_name}.host 為必填（type={dtype}）")
            elif not isinstance(host, str) or not host.strip():
                errs.append(f"{blk_name}.host 必須是非空字串，收到 {host!r}")
            port = blk.get("port")
            if port is None:
                errs.append(f"{blk_name}.port 為必填（type={dtype}）")
            elif _bad_positive(port, as_int=True) or port > 65535:
                errs.append(f"{blk_name}.port 必須是 1–65535 的整數（不接受 502.0 這類小數寫法或字串），收到 {port!r}")

    if not isinstance(cfg, dict):
        return [f"YAML 根結構必須是字典，收到 {type(cfg).__name__}"]

    # ---- 頂層區塊型別 ----
    for blk in ("system", "mqtt", "driver", "listen_driver", "bus"):
        if blk in cfg and cfg[blk] is not None and not isinstance(cfg[blk], dict):
            errs.append(f"{blk}: 必須是字典，收到 {type(cfg[blk]).__name__}")

    # ---- mqtt.broker 為必填（main.py:625 的 _require）----
    mqtt_cfg = cfg.get("mqtt")
    if not isinstance(mqtt_cfg, dict):
        errs.append("缺少 mqtt: 區塊（或型別錯誤）；main.py 會因缺少 mqtt.broker 直接退出")
    else:
        broker = mqtt_cfg.get("broker")
        if broker is None:
            errs.append("mqtt.broker 為必填欄位")
        elif isinstance(broker, str) and not broker.strip():
            errs.append("mqtt.broker 不可留空，否則 MQTT 將永遠無法連線")
        m_port = mqtt_cfg.get("port")
        if m_port is not None and (_bad_positive(m_port, as_int=True) or m_port > 65535):
            errs.append(f"mqtt.port 必須是 1–65535 的整數（不接受小數或字串），收到 {m_port!r}")
        for credential_key, env_name in (("username", "MQTT_USERNAME"), ("password", "MQTT_PASSWORD")):
            if credential_key in mqtt_cfg:
                errs.append(f"mqtt.{credential_key} 不可寫入 config.yaml；請改在 .env 設定 {env_name}")

    # ---- bus.offline_time（main.py 會 int(...) 它）----
    bus_cfg = cfg.get("bus")
    if isinstance(bus_cfg, dict):
        ot = bus_cfg.get("offline_time")
        if ot is not None and _bad_positive(ot):
            errs.append(f"bus.offline_time 必須是正數，收到 {ot!r}")

    # ---- devices 容器與元素 ----
    devices = cfg.get("devices")
    if devices is None:
        devices = []
    elif not isinstance(devices, list):
        errs.append(f"devices: 必須是清單，收到 {type(devices).__name__}")
        devices = []

    clean_devs = []
    for idx, dev in enumerate(devices, 1):
        if not isinstance(dev, dict):
            errs.append(f"devices 第 {idx} 筆必須是字典，收到 {type(dev).__name__}")
            continue
        clean_devs.append(dev)

        tag = f"devices 第 {idx} 筆（uid={dev.get('uid')!r}）"
        # adapter 會被拿去查 ADAPTER_FACTORY，非 hashable 會拋 unhashable type
        adp = dev.get("adapter")
        if adp is not None and (not isinstance(adp, str) or not adp.strip()):
            errs.append(f"{tag} 的 adapter 必須是非空字串，收到 {adp!r}")
        # poll_interval 會參與排程的時間加法，非數值會讓該台永久掉出輪詢佇列
        pi = dev.get("poll_interval")
        if pi is not None and _bad_positive(pi):
            errs.append(f"{tag} 的 poll_interval 必須是正數，收到 {pi!r}")

    has_active = any(d.get("mode", "active") == "active" for d in clean_devs)
    has_listen = any(d.get("mode") == "listen" for d in clean_devs)

    # ---- 主動軌必填（main.py:530-542）----
    if has_active:
        drv = cfg.get("driver")
        if not isinstance(drv, dict):
            errs.append("有 mode: active 設備，但缺少 driver: 區塊（或型別錯誤）")
        else:
            d_type = drv.get("type", "rtu")
            if d_type not in _ACTIVE_DRIVER_TYPES:
                errs.append(f"driver.type '{d_type}' 不支援；可用：{list(_ACTIVE_DRIVER_TYPES)}")
            else:
                _check_endpoint("driver", drv, d_type)

    # ---- 監聽軌必填（main.py:580-593）----
    if has_listen:
        ld = cfg.get("listen_driver")
        if not isinstance(ld, dict):
            errs.append("有 mode: listen 設備，但缺少 listen_driver: 區塊（或型別錯誤）")
        else:
            ld_type = ld.get("type", "tcp")
            if ld_type not in _LISTEN_DRIVER_TYPES:
                errs.append(f"listen_driver.type '{ld_type}' 不支援；可用：{list(_LISTEN_DRIVER_TYPES)}")
            else:
                _check_endpoint("listen_driver", ld, ld_type)

    # ---- 雙軌實體防撞（main.py:512-517）----
    if has_active and has_listen:
        act_port = (cfg.get("driver") or {}).get("port") if isinstance(cfg.get("driver"), dict) else None
        lst_port = (cfg.get("listen_driver") or {}).get("port") if isinstance(cfg.get("listen_driver"), dict) else None
        if act_port and lst_port and str(act_port) == str(lst_port):
            errs.append(f"禁止 active 與 listen 共用同一通訊埠（Port: {act_port}），會造成實體碰撞")

    return errs


def _remove_legacy_mqtt_credentials(cfg) -> bool:
    """移除舊備份中的 MQTT 帳密；秘密只應由 .env 注入。"""
    if not isinstance(cfg, dict):
        return False
    mqtt_cfg = cfg.get("mqtt")
    if not isinstance(mqtt_cfg, dict):
        return False

    removed = False
    for credential_key in ("username", "password"):
        if credential_key in mqtt_cfg:
            mqtt_cfg.pop(credential_key)
            removed = True
    return removed

@app.get("/api/config", dependencies=[Depends(require_auth)])
def get_config():
    if not os.path.exists(CONFIG_PATH):
        return {"yaml_content": ""}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return {"yaml_content": f.read()}


def _discover_profiles() -> tuple[list[str], list[str]]:
    """列出可選地圖名稱，不驗證也不改變 runtime 對 profile 的彈性。"""
    profiles = []
    warnings = []
    config_name = os.path.basename(CONFIG_PATH)
    try:
        with os.scandir(PROFILE_DIR) as entries:
            for entry in entries:
                name = entry.name
                if (not entry.is_file() or name == config_name or not name.endswith(".yaml")
                        or name.startswith(".") or ".bak." in name or ".tmp." in name):
                    continue
                profiles.append(name[:-5])
    except OSError as exc:
        warnings.append(f"無法讀取 Profile 目錄：{exc}")
    return sorted(profiles), warnings


@app.get("/api/catalog", dependencies=[Depends(require_auth)])
def get_catalog():
    """WebUI 唯讀選項 catalog；故障退化為 warning，絕不修改設定或重啟。"""
    result = {"adapters": [], "profiles": [], "warnings": []}
    try:
        adapter_catalog = discover_adapter_catalog()
        result["adapters"] = adapter_catalog.get("adapters", [])
        result["warnings"].extend(adapter_catalog.get("warnings", []))
    except Exception as exc:
        result["warnings"].append(f"Adapter catalog 掃描失敗：{exc}")

    profiles, profile_warnings = _discover_profiles()
    result["profiles"] = profiles
    result["warnings"].extend(profile_warnings)
    return result

@app.post("/api/config", dependencies=[Depends(require_auth)])
def save_config(payload: dict):
    yaml_str = payload.get("yaml_content", "").strip()
    if not yaml_str:
        return {"status": "error", "msg": "設定內容不能為空白"}

    if len(yaml_str) > 1024 * 1024:
        return {"status": "error", "msg": "設定檔過大 (超過 1MB)，拒絕寫入"}

    try:
        parsed = yaml.safe_load(yaml_str)
        if not isinstance(parsed, dict):
            return {"status": "error", "msg": "YAML 根結構必須是 dict"}

        # ✅ [Fix V6.2] 門口驗證：擋下所有會讓網關 fatal 退出的設定。
        #    一旦寫進磁碟並重啟，WebUI 會隨程序消失，連還原按鈕都按不到。
        errs = _validate_config(parsed)
        if errs:
            return {
                "status": "error",
                "msg": "設定未通過驗證，已拒絕寫入（config.yaml 未變動）：\n"
                       + "\n".join(f"• {e}" for e in errs)
            }

        lock_fd = open(LOCK_PATH, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            # ✅ [Fix V6.2] 備份也走原子路徑。半份 .bak 會讓 restore 原子地寫出
            #    一份壞掉的 config.yaml —— 救援機制反而變成破壞機制。
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as cur_f:
                    _atomic_write(BACKUP_PATH, cur_f.read())

            _atomic_write(CONFIG_PATH, yaml_str)

        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/api/restore", dependencies=[Depends(require_auth)])
def restore_config():
    if not os.path.exists(BACKUP_PATH):
        return {"status": "error", "msg": "找不到備份檔案"}
    try:
        lock_fd = open(LOCK_PATH, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            # ✅ [Fix V6.1] 原本是 shutil.copy2(BACKUP_PATH, CONFIG_PATH) 直接覆寫，
            #    複製途中中斷會留下半份 config.yaml。改為先讀進記憶體再走原子替換。
            with open(BACKUP_PATH, "r", encoding="utf-8") as bak_f:
                backup_text = bak_f.read()

            # ✅ [Fix V6.2] 還原前先驗證備份本身。原子化只能防「這次寫壞」，
            #    驗證能防任何原因造成的壞備份（磁碟壞軌、手動誤改、舊版殘檔）。
            #    這是救援路徑，寧可拒絕還原也不能寫出一份壞掉的 config.yaml。
            try:
                bak_parsed = yaml.safe_load(backup_text)
            except Exception as e:
                return {"status": "error", "msg": f"備份檔 YAML 解析失敗，拒絕還原：{e}"}

            # 舊版備份可能仍含 MQTT 帳密；不改動備份本體，僅在還原時移除。
            credentials_removed = _remove_legacy_mqtt_credentials(bak_parsed)
            bak_errs = _validate_config(bak_parsed)
            if bak_errs:
                return {
                    "status": "error",
                    "msg": "備份檔未通過驗證，拒絕還原（config.yaml 未變動）：\n"
                           + "\n".join(f"• {e}" for e in bak_errs)
                }

            restored_text = yaml.safe_dump(
                bak_parsed, allow_unicode=True, default_flow_style=False, sort_keys=False
            ) if credentials_removed else backup_text
            _atomic_write(CONFIG_PATH, restored_text)

        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

        return {
            "status": "ok",
            "msg": "已還原設定；已移除舊版 MQTT 帳密，請確認 .env 已設定。"
                   if credentials_removed else "已還原設定"
        }
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/api/restart", dependencies=[Depends(require_auth)])
def restart_system():
    def graceful_kill():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=graceful_kill, daemon=True).start()
    return {"status": "restarting"}

@app.get("/api/quarantine", dependencies=[Depends(require_auth)])
def get_quarantine():
    """回報啟動時被隔離的設備（地圖檔／Adapter 有問題），供 WebUI 顯示告警橫幅。

    設計取捨：地圖載入失敗改為隔離單台而非 sys.exit，網關與本 WebUI 才能存活，
    讓改壞設定的人可以直接從瀏覽器修回來，不必 SSH。代價是失敗變得「安靜」，
    因此必須由這支 API + 前端橫幅把告警重新變大聲。
    """
    gw = getattr(app_state, 'gateway', None)
    items = list(getattr(gw, 'quarantined', []) or []) if gw else []
    return {"status": "ok", "count": len(items), "items": items}

@app.get("/api/sniffer/status", dependencies=[Depends(require_auth)])
def get_sniffer_status():
    gw = getattr(app_state, 'gateway', None)
    is_active = getattr(gw, 'sniffer_mode', False) if gw else False
    return {"active": is_active}

@app.post("/api/sniffer/send", dependencies=[Depends(require_auth)])
def sniffer_send(payload: dict):
    acquired = _sniffer_lock.acquire(blocking=False)
    if not acquired:
        return {"status": "error", "msg": "上一筆指令仍在等待設備回應，請勿連續點擊"}
    try:
        gw = getattr(app_state, 'gateway', None)
        if not gw or not getattr(gw, 'sniffer_mode', False):
            return {"status": "error", "msg": "系統不在 Sniffer 模式，請先於設定開啟並重啟"}

        active_driver = getattr(gw, '_sniffer_driver', None) or gw.driver
        if not active_driver:
            return {"status": "error", "msg": "底層無可用的通訊 Driver"}

        hex_str = payload.get("hex", "").replace(" ", "")
        try:
            raw_bytes = bytes.fromhex(hex_str)
        except ValueError:
            return {"status": "error", "msg": "Hex 格式錯誤"}

        if payload.get("auto_crc", False):
            try:
                from adapters.generic_adapter import calc_crc16
                crc_bytes = calc_crc16(raw_bytes)
                raw_bytes += crc_bytes
            except Exception as e:
                return {"status": "error", "msg": f"CRC 計算失敗 (確認 generic_adapter.py 存在): {str(e)}"}

        if not gw._loop or gw._loop.is_closed():
            return {"status": "error", "msg": "主程式 Event Loop 未就緒"}

        try:
            future = asyncio.run_coroutine_threadsafe(
                active_driver.read(raw_bytes),
                gw._loop
            )

            driver_timeout = getattr(active_driver, "timeout", 5.0)
            wait_time = float(driver_timeout) + 2.0

            response_bytes = future.result(timeout=wait_time)

            if response_bytes:
                return {"status": "ok", "response": response_bytes.hex(" ").upper(), "tx_hex": raw_bytes.hex(" ").upper()}
            else:
                return {"status": "error", "msg": "設備有收到指令，但回傳空白資料"}

        except concurrent.futures.TimeoutError:
            future.cancel()
            # 讓出 Thread 時間，確保 Event Loop 將底層 read() 的 CancelledError 徹底傳播完畢
            time.sleep(0.1)
            return {"status": "error", "msg": f"設備無回應 (WebUI 等待 {wait_time} 秒逾時)"}
        except Exception as e:
            return {"status": "error", "msg": f"底層發生錯誤: {str(e)}"}

    finally:
        if acquired:
            _sniffer_lock.release()

# 🚨 新增：唯讀側錄用 API
@app.get("/api/traffic", dependencies=[Depends(require_auth)])
def get_traffic_log():
    log_list = list(getattr(app_state, 'traffic_log', []))
    log_list.reverse() # 讓最新的封包顯示在最上面
    return {"status": "ok", "log": "\n".join(log_list)}

def start_webui(port: int = 8000):
    import logging
    try:
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server.run()
    except Exception as e:
        logging.getLogger("Main").critical(f"🚨 WebUI 啟動遭遇致命錯誤: {e}", exc_info=True)
