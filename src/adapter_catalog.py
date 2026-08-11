"""無 Gateway 啟動副作用的 Adapter catalog 與 runtime loader 共用邏輯。"""

import importlib
import os
import pkgutil
import sys


def _discover_adapters(adapters_dir=None, package_name="adapters", logger=None, announce=False):
    """依 runtime 契約掃描 Adapter，回傳 ``(factory, warnings)``。

    此函式只做外掛目錄掃描與 module import；不建立 Gateway、Driver、MQTT 或 WebUI。
    ``announce=True`` 保留 main.py 啟動時既有的日誌語意；WebUI catalog 使用預設值，
    將單一外掛問題回傳為 warning，避免一次掃描失敗造成 HTTP 500。
    """
    factory = {}
    warnings = []
    current_dir = os.path.dirname(os.path.abspath(__file__))
    adapters_dir = adapters_dir or os.path.join(current_dir, "adapters")
    adapters_dir = os.path.abspath(adapters_dir)
    package_parent = os.path.dirname(adapters_dir)
    if package_parent not in sys.path:
        sys.path.insert(0, package_parent)

    if not os.path.exists(adapters_dir):
        message = f"[AdapterLoader] 外掛目錄不存在: {adapters_dir}"
        warnings.append(message)
        if logger:
            logger.warning(message)
        return factory, warnings

    importlib.invalidate_caches()
    required_base = ["ADAPTER_NAME", "Adapter"]

    for _, module_name, ispkg in pkgutil.iter_modules([adapters_dir]):
        if ispkg or not module_name.endswith("_adapter"):
            continue

        try:
            module = importlib.import_module(f"{package_name}.{module_name}")

            missing = [name for name in required_base if not hasattr(module, name)]
            if missing:
                message = f"[AdapterLoader] 拒絕載入 {module_name}: 缺少必備屬性 {missing}"
                warnings.append(message)
                if logger:
                    logger.error(message)
                continue

            adapter_class = module.Adapter
            if not isinstance(adapter_class, type):
                message = f"[AdapterLoader] 拒絕載入 {module_name}: 'Adapter' 不是一個 Class 類別"
                warnings.append(message)
                if logger:
                    logger.error(message)
                continue

            adapter_name = module.ADAPTER_NAME.lower()
            if adapter_name in factory:
                message = f"[AdapterLoader] 拒絕載入 {module_name}: 命名衝突"
                warnings.append(message)
                if logger:
                    logger.error(message)
                continue

            factory[adapter_name] = adapter_class
            if announce and logger:
                logger.info(f"[AdapterLoader] ✅ 載入 adapter: [{adapter_name}] (來源: {module_name}.py)")
        except Exception:
            warnings.append(f"[AdapterLoader] 載入 {module_name} 崩潰，已跳過")
            if logger:
                logger.exception(f"[AdapterLoader] 載入 {module_name} 崩潰，已跳過")

    if announce and logger:
        logger.info(f"[AdapterLoader] 掃描完畢，共掛載 {len(factory)} 個轉譯器外掛")
    return factory, warnings


def load_adapters(logger=None) -> dict:
    """提供 Gateway 啟動使用的既有 ``ADAPTER_FACTORY`` contract。"""
    factory, _ = _discover_adapters(logger=logger, announce=True)
    return factory


def discover_adapter_catalog() -> dict:
    """提供 WebUI 使用的唯讀 catalog；單一失效外掛不會中斷其他選項。"""
    factory, warnings = _discover_adapters()
    return {"adapters": sorted(factory), "warnings": warnings}
