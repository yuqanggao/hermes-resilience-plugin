"""Resilience plugin backend — FastAPI routes for the desktop badge & config UI.

Mounted at /api/plugins/resilience/ by the dashboard backend loader
(manifest: dashboard/manifest.json, api: plugin_api.py).

The desktop plugin (id `resilience`) calls these via ctx.rest, which is
scoped to /api/plugins/resilience/<path> by construction.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# 允许写入 config.yaml 的 resilience 配置键（白名单）
_ALLOWED_CONFIG_KEYS = {
    "enabled",
    "api_retry_on_interrupt",
    "model_rotation_enabled",
    "model_rotation_on_errors",
    "model_rotation_fallback_order",
    "model_auto_rollback_enabled",
    "model_rollback_after_failures",
    "auto_screenshot_on_error",
    "ocr_engine",
    "auto_approve_enabled",
    "stall_watchdog_enabled",
    "stall_detect_seconds",
    "stall_max_auto_resumes",
    "smart_auto_choice_enabled",
    "smart_choice_timeout_seconds",
}

# 与 __init__.py 的 DEFAULTS 保持一致（用于 UI 预填 + 缺省补全）。
# 运行时优先从主插件模块取 DEFAULTS（单点维护），导入失败时退回本副本。
_DEFAULTS: Dict[str, Any] = {
    "api_retry_on_interrupt": True,
    "heartbeat_auto_continue": True,
    "heartbeat_interval_minutes": 10,
    "interrupt_log_path": None,
    "sleep_mode_enabled": False,
    "sleep_mode_force": False,
    "sleep_mode_auto_approve_tools": ["clarify", "confirm_dialog"],
    "sleep_mode_default_reply": "按最优推荐执行",
    "sleep_mode_error_continue": True,
    "sleep_mode_error_message": "请继续完成任务",
    "sleep_mode_max_error_retries": 3,
    "sleep_windows": [
        {"start": "23:00", "end": "08:00", "enabled": True},
    ],
    "model_rotation_enabled": True,
    "model_rotation_on_errors": [
        "rate_limit", "timeout", "connection_error", "server_error",
    ],
    "model_rotation_fallback_order": [],
    "screen_error_analysis_enabled": True,
    "auto_screenshot_on_error": True,
    "ocr_engine": "rapidocr",
}


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def _plugin_dir() -> Path:
    """resilience 插件目录（HERMES_HOME/plugins/resilience，跟随实际 home）。"""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "plugins" / "resilience"
    except Exception:
        return Path.home() / ".hermes" / "plugins" / "resilience"


def _status_file() -> Path:
    return _plugin_dir() / "status.json"


def _load_main_plugin_module():
    """获取主插件模块（resilience/__init__.py）。

    按候选模块名探测 sys.modules（gateway 加载器或本模块此前按路径加载的缓存）；
    全部缺失则按路径新建加载并缓存到 sys.modules。
    """
    import importlib.util
    import sys

    for mod_name in ("resilience", "hermes_resilience_main"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "DEFAULTS") and hasattr(mod, "_GLOBAL_CONFIG"):
            return mod

    main_path = _plugin_dir() / "__init__.py"
    mod_name = "hermes_resilience_main"
    spec = importlib.util.spec_from_file_location(mod_name, main_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {main_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 配置读写（独立，不依赖主插件模块实例）
# ---------------------------------------------------------------------------
def _defaults() -> Dict[str, Any]:
    """单点维护 DEFAULTS：优先取主插件模块的 DEFAULTS（若已加载），否则用本地副本。"""
    try:
        main = _load_main_plugin_module()
        if hasattr(main, "DEFAULTS"):
            return dict(main.DEFAULTS)
    except Exception:
        pass
    return dict(_DEFAULTS)


def _read_resilience_section() -> Dict[str, Any]:
    """读取 config.yaml 中的 resilience 段（磁盘为准，独立于运行实例）。

    走官方 load_config()（DEFAULT_CONFIG + config.yaml + managed + env 合并），
    与 `hermes config get` 同一条读取路径，避免缓存与语义分叉。
    """
    try:
        import hermes_cli.config as hcfg
        raw = hcfg.load_config().get("resilience")
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    # 与 DEFAULTS 合并（DEFAULTS 兜底，磁盘值优先）
    merged = _defaults()
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def _write_resilience_section(updates: Dict[str, Any]) -> Dict[str, Any]:
    """把白名单键写回 config.yaml（通过官方 save_config API，不破坏其他段）。"""
    import hermes_cli.config as hcfg
    sect = dict(hcfg.load_config().get("resilience") or {})
    sect.update(updates)
    hcfg.save_config({"resilience": sect}, merge_existing=True)
    return updates


# ---------------------------------------------------------------------------
# 主插件热更新（best-effort：找到运行实例则推，找不到则仅落盘）
# ---------------------------------------------------------------------------
def _push_to_live_instance(applied: Dict[str, Any]) -> bool:
    """把配置变更推给 gateway 内的活实例（若可定位），失败不影响落盘。"""
    try:
        main = _load_main_plugin_module()
        main._apply_config(applied)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("resilience: 热更新运行实例失败（配置已落盘）: %s", e)
        return False


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------
class ConfigUpdate(BaseModel):
    """PUT /config 的 body：{"resilience": {...}} 或直接 {...}。"""
    resilience: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _read_interrupts_log(limit: int = 5) -> List[Dict[str, Any]]:
    path = _plugin_dir() / "interrupts.log"
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return entries[:limit]


def _sleep_mode_active(cfg: Dict[str, Any]) -> bool:
    """REST 端独立计算：与 __init__.py 的 sleep_mode_active 保持相同判定优先级。

    1. sleep_mode_force=True → True（手动强制）
    2. sleep_mode_enabled=False → False（系统关闭）
    3. 无 sleep_windows → True（旧行为兜底）
    4. 否则 → 按时间窗口判定
    """
    if cfg.get("sleep_mode_force", False):
        return True
    if not cfg.get("sleep_mode_enabled", False):
        return False
    windows = cfg.get("sleep_windows") or []
    if not windows:
        return True
    from datetime import datetime
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    for w in windows:
        if not isinstance(w, dict) or not w.get("enabled", False):
            continue
        try:
            sh, sm = (w.get("start", "00:00").split(":") + ["0"])[:2]
            eh, em = (w.get("end", "00:00").split(":") + ["0"])[:2]
            s, e = int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
        except (ValueError, AttributeError):
            continue
        if s == e:
            continue
        if s < e:
            if s <= cur < e:
                return True
        else:
            if cur >= s or cur < e:
                return True
    return False


@router.get("/health")
def health() -> Dict[str, Any]:
    """状态徽章轮询：睡眠模式 + 模型轮换 + 自动审批 + 停滞看门狗 + 智能选项 + 最近中断。"""
    cfg = _read_resilience_section()
    # 读取自动审批状态（从 status.json 取运行态）
    auto_approve_info = {"enabled": cfg.get("auto_approve_enabled", False), "monitor_thread_active": False}
    stall_info = {
        "enabled": cfg.get("stall_watchdog_enabled", True),
        "detect_seconds": cfg.get("stall_detect_seconds", 180),
        "max_auto_resumes": cfg.get("stall_max_auto_resumes", 3),
        "thread_active": False,
    }
    smart_info = {
        "enabled": cfg.get("smart_auto_choice_enabled", True),
        "timeout_seconds": cfg.get("smart_choice_timeout_seconds", 300),
        "thread_active": False,
    }
    try:
        status_file = _plugin_dir() / "status.json"
        if status_file.exists():
            with open(status_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            aa = sdata.get("auto_approve", {})
            auto_approve_info["monitor_thread_active"] = aa.get("monitor_thread_active", False)
            sw = sdata.get("stall_watchdog", {})
            stall_info["thread_active"] = sw.get("thread_active", False)
            sc = sdata.get("smart_auto_choice", {})
            smart_info["thread_active"] = sc.get("thread_active", False)
    except Exception:
        pass
    return {
        "plugin": "resilience",
        "version": "2.8.0",
        "sleep_mode": {
            "enabled": cfg.get("sleep_mode_enabled", False),
            "force": cfg.get("sleep_mode_force", False),
            "active_now": _sleep_mode_active(cfg),
            "sleep_windows": cfg.get("sleep_windows", []),
        },
        "model_rotation": {
            "enabled": cfg.get("model_rotation_enabled", True),
            "fallback_order": cfg.get("model_rotation_fallback_order", []),
            "auto_rollback_enabled": cfg.get("model_auto_rollback_enabled", True),
            "rollback_after_failures": cfg.get("model_rollback_after_failures", 2),
        },
        "ocr": {
            "engine": cfg.get("ocr_engine", "rapidocr"),
            "auto_screenshot_on_error": cfg.get("auto_screenshot_on_error", True),
        },
        "auto_approve": auto_approve_info,
        "stall_watchdog": stall_info,
        "smart_auto_choice": smart_info,
        "recent": _read_interrupts_log(limit=5),
    }


@router.get("/config")
def get_config() -> Dict[str, Any]:
    """返回当前完整配置（磁盘为准，供 UI 配置面板预填）。"""
    return {"resilience": _read_resilience_section()}


@router.put("/config")
def put_config(payload: ConfigUpdate) -> Dict[str, Any]:
    """热更新配置：body 形如 {"resilience": {...}}，白名单键生效并落盘。"""
    raw = payload.resilience or {}
    applied: Dict[str, Any] = {}
    skipped: List[str] = []
    for k, v in raw.items():
        if k in _ALLOWED_CONFIG_KEYS:
            applied[k] = v
        else:
            skipped.append(k)
    if applied:
        _write_resilience_section(applied)
        _push_to_live_instance(applied)
        # 立即刷新状态文件，让徽章不必等 15s 轮询
        try:
            _write_status_file_sync(applied)
        except Exception:  # noqa: BLE001
            pass
    return {"success": True, "applied": applied, "skipped": skipped}


@router.post("/refresh-status")
def refresh_status() -> Dict[str, Any]:
    """立即刷新 status.json（供配置保存后徽章即时反映）。"""
    _write_status_file_sync({})
    return {"success": True, "updated_at": datetime.now().isoformat()}


@router.get("/models")
def get_available_models() -> Dict[str, Any]:
    """返回所有可用模型（config.yaml + catalog 免费模型），供 UI 勾选。

    格式：{ models: [{ id, provider, name, context_length, supports_vision,
      supports_tools, supports_reasoning, pricing, description }] }
    """
    try:
        import yaml
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "config.yaml"

        # 1. 从 config.yaml 读取手动配置的模型
        all_models = []
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                c = yaml.safe_load(f)
            providers = c.get("providers", {})
            for prov_key, prov_val in providers.items():
                if not isinstance(prov_val, dict):
                    continue
                models = prov_val.get("models", {})
                for model_key, model_info in models.items():
                    ctx_len = model_info.get("context_length", 0)
                    supports_vision = model_info.get("supports_vision", False)
                    supports_tools = model_info.get("supports_tools", False)
                    supports_reasoning = model_info.get("supports_reasoning", False)
                    pricing = model_info.get("pricing", "FREE")
                    tags = []
                    if ctx_len >= 100000:
                        tags.append(f"{ctx_len//1024}K上下文")
                    if supports_vision:
                        tags.append("视觉")
                    if supports_tools:
                        tags.append("工具")
                    if supports_reasoning:
                        tags.append("推理")
                    if "FREE" in str(pricing).upper():
                        tags.append("免费")
                    desc = " · ".join(tags) if tags else "通用"
                    all_models.append({
                        "id": f"{prov_key}/{model_key}",
                        "provider": prov_key,
                        "model_key": model_key,
                        "name": model_info.get("name", model_key),
                        "context_length": ctx_len,
                        "supports_vision": supports_vision,
                        "supports_tools": supports_tools,
                        "supports_reasoning": supports_reasoning,
                        "pricing": pricing,
                        "description": desc,
                    })

        # 2. 从 catalog 读取免费模型（补充）
        catalog_path = Path(get_hermes_home()) / "cache" / "model_catalog.json"
        if catalog_path.exists():
            with open(catalog_path, encoding="utf-8") as f:
                cat = json.load(f)
            seen_ids = {m["id"] for m in all_models}
            providers_cat = cat.get("providers", {})
            for pk, pv in providers_cat.items():
                if not isinstance(pv, dict): continue
                for m in pv.get("models", []):
                    if not isinstance(m, dict): continue
                    raw_id = str(m.get("id", ""))
                    mid = f"{pk}/{raw_id}"
                    if mid in seen_ids: continue
                    # 免费判定：
                    #  ① id 以 ':free' 结尾（OpenRouter 免费模型标准写法；catalog 里
                    #     免费模型只存在 ':free' 后缀条目，bare 同名词条是付费版）
                    #  ② id 含 'free' 子串（历史 catalog 写法）
                    #  ③ pricing 字段为 FREE
                    # 不再按「pricing 缺失」放行——catalog 付费模型同样无 pricing 字段，
                    # 全量放行会让列表混入大量付费模型（v2.8 修复 openrouter 模型缺失）
                    pricing = str(m.get("pricing") or "").strip().upper()
                    is_free = raw_id.lower().endswith(":free") or "free" in raw_id.lower() or pricing == "FREE"
                    if not is_free: continue
                    display = m.get("display_name") or m.get("name", "") or raw_id
                    # catalog 里免费模型的 context_length 全部为 None，
                    # 根据 OpenRouter 公开文档补上合理估算值，让 UI 能显示上下文
                    ctx_len = m.get('context_length')
                    if ctx_len is None:
                        _id_lower = raw_id.lower()
                        if _id_lower.endswith("glm-5.2"):
                            ctx_len = 256000
                        elif _id_lower.startswith("minimax/minimax-m3"):
                            ctx_len = 131072
                        elif _id_lower.startswith("thinkingmachines/inkling"):
                            ctx_len = 131072
                        elif _id_lower.startswith("nvidia/nemotron"):
                            ctx_len = 131072
                        elif _id_lower.startswith("poolside/laguna"):
                            ctx_len = 32768
                        else:
                            ctx_len = 131072  # 兜底默认 128K
                    seen_ids.add(mid)
                    all_models.append({
                        "id": mid,
                        "provider": pk,
                        "model_key": raw_id,
                        "name": display,
                        "context_length": ctx_len,
                        "supports_vision": m.get("supports_vision", False),
                        "supports_tools": m.get("supports_tools", False),
                        "supports_reasoning": m.get("supports_reasoning", False),
                        "pricing": "FREE",
                        "description": "免费 · " + (m.get("description", "") or f"{pk} 免费模型"),
                    })

        return {"models": all_models}
    except Exception as e:
        logger.warning("读取可用模型失败: %s", e)
        return {"models": [], "error": str(e)}


class ResumeRequest(BaseModel):
    """POST /resume-task 的 body：可携带 last_prompt / force_model。"""
    last_prompt: str = ""
    force_model: str = ""


@router.post("/resume-task")
def resume_task(payload: ResumeRequest = ResumeRequest()) -> Dict[str, Any]:
    """断点恢复：由后端直接执行 continue_session 工具，不依赖桌面端 host.request('tools.dispatch')。

    桌面端恢复按钮走这里：服务端定位活实例（若可），否则直接调用工具 handler。
    """
    args = {"status": "resume"}
    if payload.last_prompt:
        args["last_prompt"] = payload.last_prompt
    if payload.force_model:
        args["force_model"] = payload.force_model

    # 优先定位运行实例并调用 handle_continue（最贴近真实执行路径）
    try:
        main = _load_main_plugin_module()
        result = main.handle_continue(args, session_id="desktop-resume")
        return {"success": True, "result": result, "source": "live-instance"}
    except Exception as e:  # noqa: BLE001
        logger.warning("resilience: live-instance resume failed, fallback to disk: %s", e)

    # 兜底：磁盘调用（无运行实例时）
    try:
        main = _load_main_plugin_module()
        result = main.handle_continue(args, session_id="desktop-resume")
        return {"success": True, "result": result, "source": "fallback"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


def _write_status_file_sync(applied: Dict[str, Any]) -> None:
    """直接写 status.json（独立实现，不依赖主插件线程）。"""
    cfg = _read_resilience_section()
    if applied:
        cfg.update(applied)
    recent_all = _read_interrupts_log(limit=10)
    status = {
        "plugin": "resilience",
        "version": "2.8.0",
        "updated_at": datetime.now().isoformat(),
        "sleep_mode": {
            "enabled": cfg.get("sleep_mode_enabled", False),
            "force": cfg.get("sleep_mode_force", False),
            "active_now": _sleep_mode_active(cfg),
            "sleep_windows": cfg.get("sleep_windows", []),
            "auto_approve_tools": cfg.get("sleep_mode_auto_approve_tools", []),
            "default_reply": cfg.get("sleep_mode_default_reply", "按最优推荐执行"),
        },
        "model_rotation": {
            "enabled": cfg.get("model_rotation_enabled", True),
            "fallback_order": cfg.get("model_rotation_fallback_order", []),
        },
        "ocr": {
            "engine": cfg.get("ocr_engine", "rapidocr"),
            "auto_screenshot_on_error": cfg.get("auto_screenshot_on_error", True),
        },
        "auto_approve": {
            "enabled": cfg.get("auto_approve_enabled", False),
            "monitor_thread_active": False,
        },
        "stall_watchdog": {
            "enabled": cfg.get("stall_watchdog_enabled", True),
            "detect_seconds": cfg.get("stall_detect_seconds", 180),
            "max_auto_resumes": cfg.get("stall_max_auto_resumes", 3),
            "thread_active": False,
        },
        "smart_auto_choice": {
            "enabled": cfg.get("smart_auto_choice_enabled", True),
            "timeout_seconds": cfg.get("smart_choice_timeout_seconds", 300),
            "thread_active": False,
        },
        "recent_interrupts": recent_all,
    }
    out = _status_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
