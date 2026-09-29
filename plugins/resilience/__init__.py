"""续跑助手 (resilience) — 自动续跑 + 模型轮换 + 截图OCR分析"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULTS: Dict[str, Any] = {
    # 总开关
    "enabled": True,
    # 基础续跑
    "api_retry_on_interrupt": True,
    "interrupt_log_path": None,
    # 模型轮换
    "model_rotation_enabled": True,
    "model_rotation_on_errors": [
        "rate_limit", "timeout", "connection_error", "server_error",
        "model_not_found", "model_unavailable", "empty_response",
    ],
    # 网关层泛化异常（Hermes 任务处理异常/请稍后重试/空响应等）视为可恢复，
    # 供 llm_request middleware 实际切换模型并重发；_is_resumeable 识别这些关键词
    "gateway_error_resume_triggers": [
        "任务处理异常", "请稍后重试", "connection aborted",
        "internal error", "503", "502", "504", "5xx",
        "rate limit", "timeout", "timed out", "empty response",
    ],
    "model_rotation_fallback_order": [],
    # 模型轮换：自动回滚（默认开启）
    "model_auto_rollback_enabled": True,
    "model_rollback_after_failures": 2,  # 备用模型连续失败此次数后回滚
    # 模型轮换：自动切换目标（会话级，立即生效，不写 config.yaml）
    "model_auto_switch": True,
    # 屏幕错误分析
    "screen_error_analysis_enabled": True,
    "auto_screenshot_on_error": True,
    "ocr_engine": "rapidocr",  # rapidocr | tesseract | off
    # 自动审批（默认关闭）：开启后所有审批弹窗自动通过
    "auto_approve_enabled": False,
    # ── v2.8：停滞看门狗 + 截图确认 + 自动续跑 ──
    "stall_watchdog_enabled": True,        # 停滞看门狗总开关
    "stall_detect_seconds": 180,          # 无进展判定阈值（秒），可手动调
    "stall_max_auto_resumes": 3,          # 单会话最多自动续跑次数（防死循环）
    # ── v2.8：智能选项（clarify 超时无响应 → 按 LLM 推荐项执行） ──
    "smart_auto_choice_enabled": True,    # 智能选项总开关
    "smart_choice_timeout_seconds": 300,  # clarify 等待多少秒无响应后自动选推荐项
}

ERROR_CONTINUE_TRIGGERS = {
    "timeout", "timed_out", "rate_limit", "429", "503", "502",
    "connection_error", "network_error", "empty_response",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════════════════════════
_GLOBAL_CONFIG: Dict[str, Any] = {}
_ERROR_STATE: Dict[str, Any] = {}  # session_id → {errors, retry_count}
_MODEL_ROTATION_STATE: Dict[str, Any] = {}  # session_id → 待执行的目标模型（由 llm_request middleware 消费）
# 模型切换历史记录：session_id → [{from_model, to_model, timestamp, success}]
_MODEL_SWITCH_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
_OCR_INSTANCE = None       # 缓存的 OCR 引擎实例
_OCR_ENGINE_NAME = None    # 当前缓存实例对应的引擎名

# ── v2.8：停滞看门狗 + 智能选项 全局状态 ──
# 会话活动追踪：session_id → last_activity_ts（LLM 轮/工具调用刷新）
_SESSION_ACTIVITY: Dict[str, float] = {}
# 会话自动续跑计数：session_id → 已自动续跑次数（防死循环）
_STALL_RESUME_COUNT: Dict[str, int] = {}
# 已判定「项目完成」的会话（不再续跑）
_STALL_COMPLETED: set = set()
# 看门狗线程
_stall_watchdog_thread: Optional[object] = None
_stall_watchdog_active = False
# 智能选项：记录每个 pending clarify 的首次发现时间
_SMART_CHOICE_SEEN: Dict[str, float] = {}
_smart_choice_thread: Optional[object] = None
_smart_choice_active = False


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════
def _get_config(ctx) -> Dict[str, Any]:
    try:
        raw = ctx.config.get("resilience", {}) or {}
    except Exception:
        raw = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if v is not None})
    return merged


def _plugin_dir() -> Path:
    """resilience 插件目录（HERMES_HOME/plugins/resilience，跟随实际 home）。"""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "plugins" / "resilience"
    except Exception:
        return Path.home() / ".hermes" / "plugins" / "resilience"


def _log_file() -> Path:
    raw = _GLOBAL_CONFIG.get("interrupt_log_path")
    if raw:
        return Path(raw)
    return _plugin_dir() / "interrupts.log"


def _append_log(entry: Dict[str, Any]) -> None:
    path = _log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _recent_logs(session_id: str, limit: int = 5) -> list:
    path = _log_file()
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    recent = [e for e in entries if e.get("session_id") == session_id]
    recent.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return recent[:limit]


def _last_interrupted_prompt(session_id: str) -> str:
    recent = _recent_logs(session_id, limit=1)
    if recent and recent[0].get("last_user_message"):
        return recent[0]["last_user_message"]
    return ""


def _truncate(text: Optional[str], n: int = 200) -> str:
    if not text:
        return ""
    return text[:n] + ("..." if len(text) > n else "")


def _is_resumeable(error_type: str, error_message: str) -> bool:
    combined = (error_type or "").lower() + " " + (error_message or "").lower()
    # ① 标准 API 中断关键词（timeout/429/503 等）
    if any(kw in combined for kw in ERROR_CONTINUE_TRIGGERS):
        return True
    # ② 网关层泛化异常（Hermes「任务处理异常/请稍后重试」等）→ 视为可自动续跑
    extra = _GLOBAL_CONFIG.get("gateway_error_resume_triggers", []) or []
    for kw in extra:
        if kw and kw.lower() in combined:
            return True
    return False


def _error_label(error_type: str, error_message: str) -> str:
    labels = {
        "rate_limit": "API速率限制(429)",
        "timeout": "API超时",
        "connection_error": "网络连接中断",
        "server_error": "服务器错误(5xx)",
        "model_not_found": "模型不可用",
        "credential_expired": "凭证过期",
        "network_error": "网络错误",
        "empty_response": "空响应",
    }
    return labels.get(error_type, error_type) or _truncate(error_message, 50)


def _get_fallback_models(current_provider: str, current_model: str) -> List[str]:
    """获取备用模型列表（从全局配置读取）。"""
    return _GLOBAL_CONFIG.get("model_rotation_fallback_order", [])


def _pick_fallback_model(fallback_order: List[str], current_model: str) -> Optional[str]:
    """从备用模型列表里选第一个「不同于当前模型」的模型。

    找不到（列表为空、或全部等于当前模型）则返回 None。
    这样即使 fallback_order[0] 恰好等于当前模型，也能切到下一个真正的备胎。
    """
    for m in fallback_order:
        if m and m != current_model:
            return m
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 模型切换历史 & 自动回滚
# ═══════════════════════════════════════════════════════════════════════════════
def _record_switch(session_id: str, from_model: str, to_model: str, success: bool) -> None:
    """记录一次模型切换到历史"""
    if session_id not in _MODEL_SWITCH_HISTORY:
        _MODEL_SWITCH_HISTORY[session_id] = []
    _MODEL_SWITCH_HISTORY[session_id].append({
        "from_model": from_model,
        "to_model": to_model,
        "timestamp": datetime.now().isoformat(),
        "success": success,
    })
    # 只保留最近10条
    _MODEL_SWITCH_HISTORY[session_id] = _MODEL_SWITCH_HISTORY[session_id][-10:]


def _get_original_model(session_id: str) -> Optional[str]:
    """从历史记录中获取原模型（最后一次成功切换前的模型）"""
    history = _MODEL_SWITCH_HISTORY.get(session_id, [])
    for entry in reversed(history):
        if entry["success"]:
            return entry["from_model"]
    return None


def _count_consecutive_failures(session_id: str) -> int:
    """统计最近连续失败次数（从最后一次成功切换后）"""
    history = _MODEL_SWITCH_HISTORY.get(session_id, [])
    if not history:
        return 0
    # 找到最后一次成功切换的位置
    last_success_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i]["success"]:
            last_success_idx = i
            break
    if last_success_idx is None:
        # 没有成功记录，统计最后几条失败
        recent = history[-5:] if len(history) > 5 else history
        return sum(1 for h in recent if not h["success"])
    # 统计成功后的失败次数
    recent = history[last_success_idx + 1:]
    return sum(1 for h in recent if not h["success"])


def _should_rollback(session_id: str, cfg: Dict) -> bool:
    """判断是否应该回滚到原模型"""
    if not cfg.get("model_auto_rollback_enabled", True):
        return False
    rollback_threshold = cfg.get("model_rollback_after_failures", 2)
    consecutive_failures = _count_consecutive_failures(session_id)
    return consecutive_failures >= rollback_threshold


def _discover_available_models() -> List[Dict[str, Any]]:
    """从 config.yaml 读取所有已配置的模型"""
    try:
        import yaml
        from hermes_constants import get_hermes_home
        config_path = Path(get_hermes_home()) / "config.yaml"
        if not config_path.exists():
            return []
        with open(config_path, encoding="utf-8") as f:
            c = yaml.safe_load(f)
        providers = c.get("providers", {})
        all_models = []
        for prov_key, prov_val in providers.items():
            if not isinstance(prov_val, dict):
                continue
            models = prov_val.get("models", {})
            for model_key, model_info in models.items():
                all_models.append({
                    "provider": prov_key,
                    "model_key": model_key,
                    "name": model_info.get("name", ""),
                    "context_length": model_info.get("context_length", 0),
                    "supports_vision": model_info.get("supports_vision", False),
                    "supports_tools": model_info.get("supports_tools", False),
                    "supports_reasoning": model_info.get("supports_reasoning", False),
                    "pricing": model_info.get("pricing", ""),
                })
        return all_models
    except Exception as e:
        logger.warning("读取可用模型失败: %s", e)
        return []




def _take_screenshot(tag: str = "error") -> Optional[str]:
    """截取当前屏幕，保存为带时间戳的 PNG，返回路径。失败返回 None。"""
    try:
        import PIL.ImageGrab
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp = _plugin_dir() / f"{tag}_screenshot_{ts}.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        img = PIL.ImageGrab.grab()
        img.save(str(tmp), "PNG")
        return str(tmp)
    except Exception as e:
        logger.warning("截图失败: %s", e)
        return None


def _ocr_screenshot(img_path: str) -> tuple:
    """对截图做 OCR，返回 (ocr_text, ocr_status)。引擎由 config ocr_engine 指定。

    引擎选择（ocr_engine）:
      - rapidocr  : RapidOCR（本地离线 ONNX，默认）
      - tesseract : Tesseract OCR（需安装 tesseract + chi_sim 语言包）
      - off       : 不做 OCR，仅记录截图路径
    引擎实例缓存到模块级 _OCR_INSTANCE，避免每次调用重复初始化（~1s）。
    """
    global _OCR_INSTANCE, _OCR_ENGINE_NAME
    engine = (_GLOBAL_CONFIG.get("ocr_engine", "rapidocr") or "rapidocr").lower()
    if engine == "off":
        return "", "ocr_disabled"

    # 引擎变化时重建实例
    if _OCR_INSTANCE is None or _OCR_ENGINE_NAME != engine:
        _OCR_INSTANCE = None
        _OCR_ENGINE_NAME = engine
        if engine == "rapidocr":
            try:
                from rapidocr_onnxruntime import RapidOCR
                _OCR_INSTANCE = RapidOCR()
                logger.info("RapidOCR 实例已初始化（本地离线）")
            except Exception as e:
                return "", f"rapidocr_init_failed: {e}"
        elif engine == "tesseract":
            try:
                import pytesseract
                _OCR_INSTANCE = pytesseract
                logger.info("Tesseract 引擎已就绪")
            except Exception as e:
                return "", f"tesseract_init_failed: {e}"
        else:
            return "", f"unknown_ocr_engine: {engine}"

    try:
        if engine == "rapidocr":
            result, _ = _OCR_INSTANCE(img_path)
            text = "\n".join(
                (line[1] if isinstance(line, (list, tuple)) else str(line))
                for line in (result or [])
            )
            return text, "rapidocr_ok" if text else "rapidocr_empty"
        elif engine == "tesseract":
            from PIL import Image
            img = Image.open(img_path)
            text = _OCR_INSTANCE.image_to_string(img, lang="chi_sim+eng")
            return text, "tesseract_ok" if text else "tesseract_empty"
    except Exception as e:
        return "", f"ocr_runtime_error: {e}"
    return "", "ocr_no_result"


# ═══════════════════════════════════════════════════════════════════════════════
# Hook: api_request_error
# ═══════════════════════════════════════════════════════════════════════════════
def on_api_error(**kwargs: Any) -> Optional[Dict[str, str]]:
    try:
        cfg = _GLOBAL_CONFIG
        # 总开关检查
        if not cfg.get("enabled", True):
            return None
        if not cfg.get("api_retry_on_interrupt", True):
            return None

        error_type = kwargs.get("error_type", "")
        error_message = (kwargs.get("error_message") or kwargs.get("error") or "")
        session_id = kwargs.get("session_id", "unknown")
        api_call_count = kwargs.get("api_call_count", 0)
        max_retries = kwargs.get("max_retries", 3)
        user_message = kwargs.get("user_message", "")
        provider = kwargs.get("provider", "unknown")
        model = kwargs.get("model", "unknown")

        entry = {
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "error_type": error_type,
            "error_message": _truncate(error_message, 300),
            "provider": provider,
            "model": model,
            "retry_count": api_call_count,
            "is_resumeable": _is_resumeable(error_type, error_message),
            "last_user_message": _truncate(user_message, 300),
        }
        _append_log(entry)

        if session_id not in _ERROR_STATE:
            _ERROR_STATE[session_id] = {"errors": [], "retry_count": 0}
        _ERROR_STATE[session_id]["errors"].append(entry)
        _ERROR_STATE[session_id]["retry_count"] = api_call_count

        # 模型下线/不可用/网关处理异常/可恢复中断：标记需要轮换（供 llm_request middleware 在下一次请求前实际切换模型并重发）
        model_unavailable = (
            error_type in (
                "model_not_found", "model_unavailable", "model_unsupported",
                "rate_limit", "timeout", "connection_error", "server_error",
                "empty_response", "internal_error", "unknown",
            )
            or "not available" in (error_message or "").lower()
            or "model_not_found" in (error_type or "")
            or _is_resumeable(error_type, error_message)  # 含「任务处理异常/请稍后重试」等网关泛化异常
        )
        if model_unavailable and cfg.get("model_rotation_enabled", True) and cfg.get("model_auto_switch", True):
            fallback_order = cfg.get("model_rotation_fallback_order", [])
            if fallback_order:
                consecutive_failures = _count_consecutive_failures(session_id)
                threshold = cfg.get("model_rollback_after_failures", 2)
                auto_rollback = cfg.get("model_auto_rollback_enabled", True)
                original_model = _get_original_model(session_id)

                if auto_rollback and original_model and model == original_model:
                    # 当前是原模型失败 → 正常切到备用模型
                    to_model = _pick_fallback_model(fallback_order, model)
                    action = "rotate"
                elif auto_rollback and original_model and model != original_model \
                        and consecutive_failures >= threshold:
                    # 当前是备用模型连续失败达到阈值 → 回滚到原模型
                    to_model = original_model
                    action = "rollback"
                else:
                    # 备用模型尚未达到回滚阈值 → 继续尝试下一个备用模型
                    to_model = _pick_fallback_model(fallback_order, model)
                    action = "rotate"

                if to_model:
                    _MODEL_ROTATION_STATE[session_id] = {
                        "from_model": model,
                        "to_model": to_model,
                        "trigger": error_type,
                        "trigger_message": _truncate(error_message, 200),
                        "at": datetime.now().isoformat(),
                        "action": action,
                    }
                    # 记录失败到切换历史
                    _record_switch(session_id, model, to_model, success=(action == "rollback"))
                    logger.info(
                        "resilience: 模型 %s 不可用 [%s] → %s (session=%s, 连续失败=%d)",
                        model, action, to_model, session_id, consecutive_failures,
                    )
                else:
                    logger.warning(
                        "resilience: fallback_order=%s 全部等于当前模型 %s，无法轮换 (session=%s)",
                        fallback_order, model, session_id,
                    )

        # 普通模式：最后一次重试时注入续跑提示
        if api_call_count >= max_retries and user_message and _is_resumeable(error_type, error_message):
            hint = (
                f"\n\n[Resilience 续跑提示] API中断: {_error_label(error_type, error_message)}。"
                f"原始任务: `{_truncate(user_message, 80)}`。"
                f"如需恢复，请说「继续」或调用 continue_session 工具。"
            )
            return {"context": hint}

    except Exception as e:
        logger.warning("on_api_error 异常: %s", e, exc_info=True)

    return None



# ═══════════════════════════════════════════════════════════════════════════════
# Middleware: llm_request — 实际执行模型轮换（在 API 请求发出前改写请求 payload）
# ═══════════════════════════════════════════════════════════════════════════════
def on_llm_request(request: Dict[str, Any], **context: Any) -> Optional[Dict[str, Any]]:
    """Hermes llm_request middleware：请求发出前实际切换模型。

    当 _MODEL_ROTATION_STATE 中存在待执行轮换（由 on_api_error 写入），
    把请求的 model 字段改写为备用模型，并清除状态。
    """
    cfg = _GLOBAL_CONFIG
    if not cfg.get("enabled", True):
        return None
    session_id = context.get("session_id", "")
    rotation = _MODEL_ROTATION_STATE.get(session_id)
    if not rotation:
        return None
    target_model = rotation.get("to_model")
    if not target_model:
        _MODEL_ROTATION_STATE.pop(session_id, None)
        return None
    current_model = request.get("model", "")
    if current_model == target_model:
        # 已经是目标模型，无需改写
        _MODEL_ROTATION_STATE.pop(session_id, None)
        return None
    # 改写请求 payload（Hermes middleware 契约：返回 {request: {...}} 即可替换）
    new_request = dict(request)
    new_request["model"] = target_model
    _MODEL_ROTATION_STATE.pop(session_id, None)
    logger.info(
        "resilience middleware: 实际切换模型 %s → %s (session=%s)",
        current_model, target_model, session_id,
    )
    # 同步记录到日志
    _append_log({
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "action": "model_switched",
        "from_model": current_model,
        "to_model": target_model,
        "reason": rotation.get("trigger", "model_unavailable"),
    })
    result = {
        "request": new_request,
        "source": "resilience",
        "reason": f"model {current_model} unavailable"
    }
    if _should_rollback(session_id, cfg):
        logger.info(
            "resilience: 备选模型连续失败 %d 次，自动回滚到原模型 (session=%s)",
            _count_consecutive_failures(session_id), session_id,
        )
        original_model = _get_original_model(session_id)
        if original_model and original_model != target_model:
            # 记录回滚
            _record_switch(session_id, target_model, original_model, success=True)
            new_request["model"] = original_model
            result["reason"] = f"rollback to original model {original_model} after {target_model} failed"
            logger.info(
                "resilience: 实际切换模型 %s → %s (session=%s)",
                target_model, original_model, session_id,
            )
    _write_status_file()
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 自动审批（auto_approve_enabled）
# 开启后：① 后台线程持续监控待审批队列，发现即自动批准（1.5s 内完成）
#          ② 临时关闭「受保护指令文件」安全门（AGENTS.md 等弹窗的根源）
#          ③ 每个会话预批准所有审批模式
# 关闭时：恢复受保护文件门，停止监控线程
# ═══════════════════════════════════════════════════════════════════════════════
_auto_approve_thread: Optional[object] = None      # 后台监控线程
_auto_approve_active = False                       # 线程运行标志
_protected_gate_state: Dict[str, Any] = {}         # 记录被关闭的安全门，关闭开关时恢复


def _auto_approve_enabled() -> bool:
    """当前自动审批是否开启（总开关 AND 插件 enabled）。"""
    cfg = _GLOBAL_CONFIG
    return bool(cfg.get("enabled", True)) and bool(cfg.get("auto_approve_enabled", False))


def _auto_approve_loop() -> None:
    """后台监控线程：扫描待审批队列（终端命令/工具/文件写入），自动批准。

    数据源：tools.approval._gateway_queues（session_key → 待审批条目）。
    条目在等待用户点击/输入期间保持入队，1.5s 轮询窗口足以抢在超时前批准。
    """
    global _auto_approve_active
    logger.info("resilience: 自动审批监控线程已启动")
    while _auto_approve_active:
        try:
            import tools.approval as _approval
            import threading
            with _approval._lock:
                pending = list(_approval._gateway_queues.keys())
            for session_key in pending:
                try:
                    # 预批准该会话所有审批模式（终端命令、文件写入等）
                    _approval.approve_session(session_key, "auto_approve_all")
                    # 批准当前队列中等待的条目（一次批准最旧一条）
                    resolved = _approval.resolve_gateway_approval(
                        session_key, "once",
                        reason="resilience 自动审批",
                    )
                    if resolved:
                        logger.info(
                            "resilience: 自动批准 session=%s 的 %d 条待审批",
                            session_key, resolved,
                        )
                        _append_log({
                            "timestamp": datetime.now().isoformat(),
                            "session_id": session_key,
                            "action": "auto_approved",
                            "count": resolved,
                            "reason": "auto_approve_enabled",
                        })
                except Exception as e:
                    logger.debug("resilience: 自动批准 session=%s 失败: %s", session_key, e)
        except Exception as e:
            logger.debug("resilience: 自动审批轮询异常: %s", e)
        time.sleep(1.5)
    logger.info("resilience: 自动审批监控线程已停止")


def _set_protected_gate(disabled: bool) -> None:
    """临时启用/关闭「受保护指令文件」安全门。

    开启自动审批时把 security.protected_instruction_files 设为 False（AGENTS.md
    弹窗的根源），关闭自动审批时恢复为 True。
    优先走官方 save_config；保存 API 不可用时回退为文本级改写 config.yaml。
    """
    target = not disabled
    try:
        import hermes_cli.config as hcfg
        current = hcfg.load_config()
        security = current.get("security")
        if not isinstance(security, dict):
            security = {}
            current["security"] = security
        sec_val = security.get("protected_instruction_files", True)
        if sec_val == target:
            return
        if _protected_gate_state.get("original") is None:
            _protected_gate_state["original"] = sec_val
        security["protected_instruction_files"] = target
        hcfg.save_config(current, merge_existing=True)
        logger.info("resilience: protected_instruction_files %s → %s（自动审批%s）",
                    sec_val, target, "关闭" if disabled else "恢复")
    except Exception:
        # 回退：文本级改写 config.yaml（不依赖 yaml 库）
        try:
            from pathlib import Path
            import re
            cfg_path = Path(_plugin_dir()).parent.parent / "config.yaml"
            with open(cfg_path, encoding="utf-8") as f:
                raw = f.read()
            if 'protected_instruction_files' in raw:
                if target is False:
                    new_raw = re.sub(r'protected_instruction_files:\s*\S+',
                                     'protected_instruction_files: false', raw)
                else:
                    new_raw = re.sub(r'protected_instruction_files:\s*\S+',
                                     'protected_instruction_files: true', raw)
            else:
                new_raw = raw
                # 在文件末尾追加 security 段
                new_raw += "\nsecurity:\n  protected_instruction_files: " + ("false" if target is False else "true") + "\n"
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(new_raw)
            logger.info("resilience: protected_instruction_files → %s（回退文本级改写）", target)
        except Exception as e:
            logger.warning("resilience: 切换 protected_instruction_files 失败: %s", e)


def _start_auto_approve() -> None:
    """启动自动审批：开启受保护文件门 + 启动监控线程。"""
    global _auto_approve_thread, _auto_approve_active
    if _auto_approve_active:
        return
    _auto_approve_active = True
    _set_protected_gate(disabled=True)   # 暂时关闭受保护文件门
    _auto_approve_thread = threading.Thread(
        target=_auto_approve_loop, name="resilience-auto-approve", daemon=True
    )
    _auto_approve_thread.start()


def _stop_auto_approve() -> None:
    """停止自动审批：关闭监控线程 + 恢复受保护文件门。"""
    global _auto_approve_thread, _auto_approve_active
    if not _auto_approve_active:
        return
    _auto_approve_active = False
    try:
        if _auto_approve_thread is not None:
            _auto_approve_thread.join(timeout=3)
    except Exception:
        pass
    _auto_approve_thread = None
    _set_protected_gate(disabled=False)  # 恢复受保护文件门


def _sync_auto_approve() -> None:
    """根据当前配置同步自动审批状态（热更新/启动时调用）。"""
    if _auto_approve_enabled():
        _start_auto_approve()
    else:
        _stop_auto_approve()


# ═══════════════════════════════════════════════════════════════════════════════
# v2.8：会话活动追踪（停滞判定数据源）
# 任何 agent 活动（LLM 轮开始/结束、工具调用）都刷新对应会话的时间戳。
# 看门狗仅在「某会话长时间无任何 agent 活动」时判定为停滞。
# ═══════════════════════════════════════════════════════════════════════════════
def _touch_session_activity(session_id: str) -> None:
    """记录某会话的 agent 活动，刷新停滞计时。"""
    if not session_id or session_id in ("unknown", ""):
        return
    _SESSION_ACTIVITY[session_id] = time.time()


# ── 会话活动钩子：on_session_start（每轮 LLM 开始） ──
def on_session_start(**kwargs: Any) -> None:
    try:
        _touch_session_activity(kwargs.get("session_id", ""))
    except Exception as e:
        logger.debug("on_session_start 异常: %s", e)


# ── 会话活动钩子：on_session_end（每轮 LLM 结束）──
def on_session_end(**kwargs: Any) -> None:
    try:
        session_id = kwargs.get("session_id", "")
        _touch_session_activity(session_id)
        # agent 正常结束一轮（completed 且未 failed/interrupted）且明确「完成」时，
        # 标记该会话为完成，看门狗不再续跑。
        completed = kwargs.get("completed", False)
        turn_exit_reason = str(kwargs.get("turn_exit_reason", ""))
        if completed and not kwargs.get("failed") and not kwargs.get("interrupted"):
            # 轮正常结束。是否「项目完成」由看门狗的截图 OCR 判定，
            # 这里不直接标完成（避免误判）——仅刷新活动。
            pass
    except Exception as e:
        logger.debug("on_session_end 异常: %s", e)


# ── 工具活动钩子：pre_tool_call（每次工具调用前刷新活动，避免长工具被误判停滞） ──
def on_pre_tool_call(**kwargs: Any) -> None:
    try:
        _touch_session_activity(kwargs.get("session_id", ""))
    except Exception as e:
        logger.debug("on_pre_tool_call 异常: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# v2.8：停滞看门狗 —— 截图确认 + 自动续跑
# 逻辑：
#   每 30s 扫描一次 _SESSION_ACTIVITY。
#   对最近活跃过（活动过且未被标记完成）的会话，若 now - last_ts > stall_detect_seconds：
#     ① 截图 + OCR
#     ② OCR 文本含完成标志词 → 标记完成，不再续跑
#     ③ 否则（未完成且 agent 已停）→ 若未达 stall_max_auto_resumes 上限，
#        通过 ctx.inject_message 注入「继续」消息，重置活动计时
# ═══════════════════════════════════════════════════════════════════════════════
# 完成标志词（OCR 命中任一即判定项目完成）
# 覆盖中英文 + 缩写形式 + 用户指定的"已完成"
_STALL_COMPLETED_MARKERS = (
    "已完成", "任务完成", "项目完成", "全部完成", "已完成。", "done", "completed",
    "任务已完成", "全部完成", "success", "成功完成", "交付完成", "已交付",
    "任务成功", "完成总结", "项目竣工", "已保存", "已生成",
    # 常见缩写/变体
    "任务完成.", "任务完成！", "已完成!", "全部完成!", "全部完成。",
    "project done", "project completed", "task done", "task completed",
    # 中文变体
    "全部完成了", "都完成了", "搞定了", "做完了", "写完了", "生成完毕",
    "已上传", "已保存", "已下载", "输出完毕",
)

# 自动续跑注入消息（用户指定）
_STALL_RESUME_MESSAGE = "请继续自动完成此项目，如果项目完成，请做好总结后回复已完成"


def _inject_session_message(session_id: str, text: str) -> bool:
    """通过官方 PluginContext.inject_message 向会话注入一条 user 消息。

    桌面端会话的 session_id 即 TUI 持久 session_key（agent.session_id），
    inject_message 会落到该会话的 prompt 队列；空闲会话立即开始新一轮。
    需要 config.yaml: plugins.entries.resilience.allow_gateway_injection: true。
    """
    try:
        # 复用 register() 时绑定的 ctx 引用
        ctx = _PLUGIN_CTX
        if ctx is None or not hasattr(ctx, "inject_message"):
            logger.warning("resilience: 无法获取插件 ctx，跳过注入 (session=%s)", session_id)
            return False
        ok = ctx.inject_message(text, role="user", session_key=session_id)
        if not ok:
            logger.warning("resilience: inject_message 失败 (session=%s)；"
                           "请确认 config.yaml 已设 plugins.entries.resilience.allow_gateway_injection: true",
                           session_id)
        else:
            logger.info("resilience: 已向会话 %s 注入续跑消息", session_id)
        return bool(ok)
    except Exception as e:
        logger.warning("resilience: inject_message 异常: %s", e)
        return False


# register() 时绑定的 PluginContext（供后台线程注入消息用）
_PLUGIN_CTX: Optional[Any] = None


def _stall_ocr_confirms_done(ocr_text: str) -> bool:
    """OCR 文本命中任一完成标志词 → 判定项目已完成。"""
    if not ocr_text:
        return False
    low = ocr_text.lower()
    for marker in _STALL_COMPLETED_MARKERS:
        if marker.lower() in low:
            return True
    return False


def _stall_watchdog_loop() -> None:
    """停滞看门狗主循环：每 30s 扫描一次会话活动。"""
    global _stall_watchdog_active
    logger.info("resilience: 停滞看门狗线程已启动（间隔 30s）")
    while _stall_watchdog_active:
        try:
            cfg = _GLOBAL_CONFIG
            if not cfg.get("enabled", True) or not cfg.get("stall_watchdog_enabled", True):
                time.sleep(30)
                continue
            threshold = int(cfg.get("stall_detect_seconds", 180))
            max_resumes = int(cfg.get("stall_max_auto_resumes", 3))
            now = time.time()

            # 快照当前活动表（避免遍历中修改）
            snapshot = dict(_SESSION_ACTIVITY)
            for session_id, last_ts in snapshot.items():
                if session_id in _STALL_COMPLETED:
                    continue
                idle = now - last_ts
                if idle <= threshold:
                    continue
                # 该会话 idle 超过阈值 → 候选停滞
                count = _STALL_RESUME_COUNT.get(session_id, 0)
                if count >= max_resumes:
                    logger.info("resilience: 会话 %s 已达自动续跑上限 %d，停止续跑", session_id, max_resumes)
                    _STALL_COMPLETED.add(session_id)  # 视为终结，不再处理
                    continue

                # ① 截图 + OCR 确认
                img_path = _take_screenshot(tag="stall_check")
                ocr_text, ocr_status = ("", "screenshot_skipped")
                if img_path:
                    ocr_text, ocr_status = _ocr_screenshot(img_path)
                else:
                    logger.warning("resilience: 停滞检测会话 %s 截图失败，按未完成处理", session_id)

                # ② OCR 判定是否完成
                if _stall_ocr_confirms_done(ocr_text):
                    logger.info("resilience: 会话 %s 截图 OCR 判定项目已完成，停止续跑", session_id)
                    _STALL_COMPLETED.add(session_id)
                    _append_log({
                        "timestamp": datetime.now().isoformat(),
                        "session_id": session_id,
                        "action": "stall_confirmed_done",
                        "ocr_status": ocr_status,
                        "screenshot": img_path,
                        "reason": "OCR 命中完成标志词，项目判定完成",
                    })
                    continue

                # ③ 未完成且 agent 已停 → 注入续跑消息
                logger.info("resilience: 会话 %s 停滞 %ds 且未完成 → 自动续跑（第 %d/%d 次）",
                            session_id, int(idle), count + 1, max_resumes)
                _STALL_RESUME_COUNT[session_id] = count + 1
                # 重置活动计时，避免下一轮扫描立即再触发
                _touch_session_activity(session_id)
                ok = _inject_session_message(session_id, _STALL_RESUME_MESSAGE)
                _append_log({
                    "timestamp": datetime.now().isoformat(),
                    "session_id": session_id,
                    "action": "stall_auto_resume",
                    "resume_count": count + 1,
                    "idle_seconds": int(idle),
                    "ocr_status": ocr_status,
                    "screenshot": img_path,
                    "injected": ok,
                    "reason": "停滞无正向输出，自动续跑",
                })
        except Exception as e:
            logger.warning("resilience: 停滞看门狗扫描异常: %s", e, exc_info=True)
        time.sleep(30)
    logger.info("resilience: 停滞看门狗线程已停止")


def _start_stall_watchdog() -> None:
    global _stall_watchdog_thread, _stall_watchdog_active
    if _stall_watchdog_active:
        return
    _stall_watchdog_active = True
    _stall_watchdog_thread = threading.Thread(
        target=_stall_watchdog_loop, name="resilience-stall-watchdog", daemon=True
    )
    _stall_watchdog_thread.start()


def _stop_stall_watchdog() -> None:
    global _stall_watchdog_thread, _stall_watchdog_active
    if not _stall_watchdog_active:
        return
    _stall_watchdog_active = False
    try:
        if _stall_watchdog_thread is not None:
            _stall_watchdog_thread.join(timeout=3)
    except Exception:
        pass
    _stall_watchdog_thread = None


def _sync_stall_watchdog() -> None:
    if _GLOBAL_CONFIG.get("enabled", True) and _GLOBAL_CONFIG.get("stall_watchdog_enabled", True):
        _start_stall_watchdog()
    else:
        _stop_stall_watchdog()


# ═══════════════════════════════════════════════════════════════════════════════
# v2.8：智能选项 —— clarify 超时无响应 → 自动按 LLM 推荐项执行
# 逻辑：
#   每 10s 扫描 tools.clarify_gateway._entries（pending clarify）。
#   记录每个 clarify_id 的首次发现时间；若 pending 超过 smart_choice_timeout_seconds
#   且用户未响应 → 取 choices[0]（推荐项，按 AGENTS.md 约定推荐项排第一）
#   通过 resolve_gateway_clarify(clarify_id, 推荐项) 解除 agent 阻塞。
# ═══════════════════════════════════════════════════════════════════════════════
def _smart_choice_loop() -> None:
    global _smart_choice_active, _SMART_CHOICE_SEEN
    logger.info("resilience: 智能选项监控线程已启动（间隔 10s）")
    while _smart_choice_active:
        try:
            cfg = _GLOBAL_CONFIG
            if not cfg.get("enabled", True) or not cfg.get("smart_auto_choice_enabled", True):
                time.sleep(10)
                continue
            timeout_s = int(cfg.get("smart_choice_timeout_seconds", 300))
            now = time.time()
            import tools.clarify_gateway as cg

            with cg._lock:
                pending = []
                for clarify_id in list(cg._entries.keys()):
                    entry = cg._entries.get(clarify_id)
                    if entry is not None and not entry.event.is_set():
                        pending.append((clarify_id, entry))
            for clarify_id, entry in pending:
                choices = entry.choices
                # 无选项（开放式）的 clarify 不自动处理，跳过
                if not choices:
                    continue
                # 记录首次发现时间
                first_seen = _SMART_CHOICE_SEEN.setdefault(clarify_id, now)
                waited = now - first_seen
                if waited >= timeout_s:
                    # 取推荐项（第一个选项）；多选项模式返回 JSON 数组
                    recommended = str(choices[0])
                    if entry.multi_select:
                        import json as _json
                        resolve_val = _json.dumps([recommended], ensure_ascii=False)
                    else:
                        resolve_val = recommended
                    resolved = cg.resolve_gateway_clarify(clarify_id, resolve_val)
                    if resolved:
                        logger.info("resilience: clarify %s 超时 %ds 未响应 → 自动选推荐项「%s」",
                                    clarify_id, int(waited), recommended)
                        _append_log({
                            "timestamp": datetime.now().isoformat(),
                            "session_id": entry.session_key,
                            "action": "smart_auto_choice",
                            "clarify_id": clarify_id,
                            "waited_seconds": int(waited),
                            "chosen": recommended,
                            "multi_select": entry.multi_select,
                            "reason": "clarify 超时无响应，按推荐项自动执行",
                        })
                    # 无论成功与否都清理首次记录，避免重复处理
                    _SMART_CHOICE_SEEN.pop(clarify_id, None)
            # 清理已不在 pending 的 clarify 的 first_seen 记录
            with cg._lock:
                alive_ids = set(cg._entries.keys())
            _SMART_CHOICE_SEEN = {k: v for k, v in _SMART_CHOICE_SEEN.items() if k in alive_ids}
        except Exception as e:
            logger.warning("resilience: 智能选项监控异常: %s", e, exc_info=True)
        time.sleep(10)
    logger.info("resilience: 智能选项监控线程已停止")


def _start_smart_choice() -> None:
    global _smart_choice_thread, _smart_choice_active
    if _smart_choice_active:
        return
    _smart_choice_active = True
    _smart_choice_thread = threading.Thread(
        target=_smart_choice_loop, name="resilience-smart-choice", daemon=True
    )
    _smart_choice_thread.start()


def _stop_smart_choice() -> None:
    global _smart_choice_thread, _smart_choice_active
    if not _smart_choice_active:
        return
    _smart_choice_active = False
    try:
        if _smart_choice_thread is not None:
            _smart_choice_thread.join(timeout=3)
    except Exception:
        pass
    _smart_choice_thread = None


def _sync_smart_choice() -> None:
    if _GLOBAL_CONFIG.get("enabled", True) and _GLOBAL_CONFIG.get("smart_auto_choice_enabled", True):
        _start_smart_choice()
    else:
        _stop_smart_choice()


# ═══════════════════════════════════════════════════════════════════════════════
# Hook: post_tool_call — 工具失败时记录 + 截图
# ═══════════════════════════════════════════════════════════════════════════════
def on_post_tool_call(**kwargs: Any) -> None:
    try:
        cfg = _GLOBAL_CONFIG
        session_id = kwargs.get("session_id", "unknown")
        # 任何工具调用（无论成败）都刷新会话活动计时，避免长工具被误判为停滞
        _touch_session_activity(session_id)

        status = kwargs.get("status", "")
        if status != "error":
            return

        error_type = kwargs.get("error_type", "")
        error_message = kwargs.get("error_message", "") or ""
        tool_name = kwargs.get("tool_name", "")

        high_impact = {"terminal", "write_file", "patch", "browser_exec", "delegate_task"}
        if tool_name not in high_impact:
            return

        is_timeout = "timeout" in error_type.lower() or "timed out" in error_message.lower()
        entry = {
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "tool_name": tool_name,
            "error_type": error_type,
            "error_message": _truncate(error_message, 200),
            "is_resumeable": is_timeout,
            "last_user_message": "",
            "provider": "tool",
            "retry_count": 0,
        }
        _append_log(entry)

        # 自动截图（受 auto_screenshot_on_error 控制）
        if cfg.get("auto_screenshot_on_error"):
            img_path = _take_screenshot()
            if img_path:
                logger.info("工具 %s 失败，截图: %s", tool_name, img_path)
                # 自动分析截图中文字
                _analyze_screenshot(img_path, tool_name, error_message, session_id)

    except Exception as e:
        logger.warning("on_post_tool_call 异常: %s", e, exc_info=True)


# 占位：分析截图函数需在 _take_screenshot 之后定义
def _analyze_screenshot(img_path: str, tool_name: str, error_message: str, session_id: str) -> None:
    """提取截图中的错误文字（引擎由 config ocr_engine 指定，默认 RapidOCR 本地离线）。"""
    try:
        # 记录截图分析事件
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "session_id": session_id,
            "tool_name": tool_name,
            "action": "screenshot_captured",
            "screenshot_path": img_path,
            "error_summary": _truncate(error_message, 100),
        }

        # 调用可配置 OCR 引擎提取文字
        ocr_text, ocr_status = _ocr_screenshot(img_path)
        if ocr_text:
            log_entry["ocr_text"] = _truncate(ocr_text, 800)
        log_entry["ocr_status"] = ocr_status

        _append_log(log_entry)
        logger.info("截图分析完成: %s (OCR: %s, %d chars)", img_path, ocr_status, len(ocr_text))
    except Exception as e:
        logger.warning("截图分析失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: continue_session
# ═══════════════════════════════════════════════════════════════════════════════
CONTINUE_TOOL = {
    "name": "continue_session",
    "description": (
        "检测会话中断状态并恢复。status='check' 仅查看状态；status='resume' 尝试恢复任务。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["check", "resume"],
                "description": "check=查看状态，resume=恢复任务",
            },
            "last_prompt": {
                "type": "string",
                "description": "手动指定要重发的用户消息（可选）",
            },
            "force_model": {
                "type": "string",
                "description": "强制切换到指定模型（睡眠模式）",
            },
        },
        "required": [],
    },
}


def handle_continue(params: Dict[str, Any], **kwargs: Any) -> str:
    mode = params.get("status", "resume")
    session_id = kwargs.get("session_id", "unknown")
    cfg = _GLOBAL_CONFIG

    if mode == "check":
        recent = _recent_logs(session_id, limit=3)
        if recent:
            last = recent[0]
            return json.dumps({
                "interrupted": True,
                "reason": last.get("error_type", "unknown"),
                "message": _truncate(last.get("error_message", ""), 100),
                "timestamp": last.get("timestamp"),
                "suggestion": (
                    "调用 status='resume' 恢复"
                    if last.get("is_resumeable")
                    else "需人工介入"
                ),
            }, ensure_ascii=False)
        return json.dumps(
            {"interrupted": False, "message": "无中断记录"}, ensure_ascii=False
        )

    # resume
    last_prompt = params.get("last_prompt") or _last_interrupted_prompt(session_id)
    force_model = params.get("force_model")

    if not last_prompt:
        return json.dumps({
            "success": False,
            "reason": "无历史消息可恢复，请手动发送 /continue",
        }, ensure_ascii=False)
    note = (
        f"[Resilience 续跑提示] 恢复中断任务\n"
        f"原始任务: {_truncate(last_prompt, 100)}\n"
        f"如需继续请回复「继续」"
    )
    return json.dumps(
        {"success": True, "note": note, "action": "hint_injected"},
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Tool: resilience_status
# ═══════════════════════════════════════════════════════════════════════════════
STATUS_TOOL = {
    "name": "resilience_status",
    "description": (
        """查看续跑助手状态：中断日志、模型轮换策略、修复建议。
        action='configure' 并传 params 可直接修改续跑助手配置（立即生效，无需重启）。"""
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "会话ID（可选）"},
            "limit": {
                "type": "integer",
                "description": "返回条数（默认5）",
                "default": 5,
            },
            "action": {
                "type": "string",
                "enum": ["get", "configure"],
                "description": "get=查看状态（默认），configure=应用新配置",
            },
            "config": {
                "type": "object",
                "description": (
                    "configure 时使用：要更新的配置项（合并到 resilience 配置）。"
                    "支持键：enabled, api_retry_on_interrupt, model_rotation_enabled, "
                    "model_auto_switch, model_rotation_fallback_order, "
                    "model_rotation_on_errors, auto_screenshot_on_error, "
                    "ocr_engine (rapidocr|tesseract|off), auto_approve_enabled (自动审批开关)"
                            ),
            },
        },
        "required": [],
    },
}


def handle_status(params: Dict[str, Any], **kwargs: Any) -> str:
    sid = params.get("session_id") or kwargs.get("session_id", "unknown")
    limit = params.get("limit", 5)
    cfg = _GLOBAL_CONFIG

    # configure 子命令：应用配置变更
    if params.get("action") == "configure":
        return _apply_config(params.get("config") or {})

    recent = _recent_logs(sid, limit=limit)
    recs = _make_recommendations(recent, cfg)

    summary = {
        "plugin": "resilience",
        "version": "2.8.0",
        "session_id": sid,
        "enabled": cfg.get("enabled", True),
        "model_rotation": {
            "enabled": cfg.get("model_rotation_enabled", True),
            "on_errors": cfg.get("model_rotation_on_errors", []),
            "fallback_order": cfg.get("model_rotation_fallback_order", []),
            "auto_rollback_enabled": cfg.get("model_auto_rollback_enabled", True),
            "rollback_after_failures": cfg.get("model_rollback_after_failures", 2),
        },
        "ocr": {
            "engine": cfg.get("ocr_engine", "rapidocr"),
            "auto_screenshot_on_error": cfg.get("auto_screenshot_on_error", True),
        },
        "auto_approve": {
            "enabled": cfg.get("auto_approve_enabled", False),
            "monitor_thread_active": _auto_approve_active,
        },
        "stall_watchdog": {
            "enabled": cfg.get("stall_watchdog_enabled", True),
            "detect_seconds": cfg.get("stall_detect_seconds", 180),
            "max_auto_resumes": cfg.get("stall_max_auto_resumes", 3),
            "thread_active": _stall_watchdog_active,
            "tracked_sessions": len(_SESSION_ACTIVITY),
        },
        "smart_auto_choice": {
            "enabled": cfg.get("smart_auto_choice_enabled", True),
            "timeout_seconds": cfg.get("smart_choice_timeout_seconds", 300),
            "thread_active": _smart_choice_active,
        },
        "recent_interrupts": recent,
        "recommendations": recs,
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


_ALLOWED_CONFIG_KEYS = {
    "enabled",
    "api_retry_on_interrupt",
    "model_rotation_enabled",
    "model_rotation_on_errors",
    "model_rotation_fallback_order",
    "model_auto_rollback_enabled",
    "model_rollback_after_failures",
    "model_auto_switch",
    "auto_screenshot_on_error",
    "ocr_engine",
    "gateway_error_resume_triggers",
    "auto_approve_enabled",
    "stall_watchdog_enabled",
    "stall_detect_seconds",
    "stall_max_auto_resumes",
    "smart_auto_choice_enabled",
    "smart_choice_timeout_seconds",
}


def _apply_config(new_values: Dict[str, Any]) -> str:
    """把配置变更应用到运行内配置并写回 config.yaml，立即生效。

    若调用时传入 _reconfigure 回调（dashboard 后端会注入），则用它把更新
    推给 gateway 的活实例，实现零重启热更新。"""
    global _GLOBAL_CONFIG
    applied = {}
    for k, v in new_values.items():
        if k not in _ALLOWED_CONFIG_KEYS:
            continue
        _GLOBAL_CONFIG[k] = v
        applied[k] = v
    # 通过官方 API 写回 config.yaml（merge_existing 保护其他段不被覆盖）
    if applied:
        try:
            import hermes_cli.config as hcfg
            sect = dict(hcfg.load_config().get("resilience") or {})
            sect.update(applied)
            hcfg.save_config({"resilience": sect}, merge_existing=True)
        except Exception as e:
            logger.warning("写回 config.yaml 失败（运行内配置已生效）: %s", e)
    # 若存在 re-register 钩子（dashboard 注入），推给活实例
    reconf = _GLOBAL_CONFIG.get("_reconfigure_fn")
    if callable(reconf):
        try:
            reconf(applied)
            logger.info("resilience: 已热更新到运行实例: %s", list(applied))
        except Exception as e:
            logger.warning("resilience: 热更新运行实例失败: %s", e)
    # OCR 引擎切换时重置缓存实例（下次 _ocr_screenshot 自动重建）
    if "ocr_engine" in applied:
        global _OCR_INSTANCE, _OCR_ENGINE_NAME
        _OCR_INSTANCE = None
        _OCR_ENGINE_NAME = None
    # 自动审批状态同步（启动/停止监控线程 + 切换受保护文件门）
    if "auto_approve_enabled" in applied:
        _sync_auto_approve()
    # v2.8：停滞看门狗 / 智能选项线程同步
    if "stall_watchdog_enabled" in applied or "enabled" in applied:
        _sync_stall_watchdog()
    if "smart_auto_choice_enabled" in applied or "enabled" in applied:
        _sync_smart_choice()
    # 同步刷新状态文件
    _write_status_file()
    return json.dumps({
        "success": True,
        "applied": applied,
        "skipped": [k for k in new_values if k not in applied],
        "note": "配置已热更新，立即生效",
    }, ensure_ascii=False)

def _reconfigure_live(applied: Dict[str, Any]) -> None:
    """由 register(ctx) 绑定到 ctx，dashboard 后端调用以热更新活实例。"""
    global _GLOBAL_CONFIG
    _GLOBAL_CONFIG.update(applied)
    # 更新后重新计算状态文件
    _write_status_file()


def _make_recommendations(interrupts: list, cfg: Dict) -> List[str]:
    if not interrupts:
        return ["一切正常，无中断记录"]
    latest = interrupts[0]
    recs = []
    if latest.get("is_resumeable"):
        recs.append(f"最近中断: {_truncate(latest.get('error_message', ''), 60)}")
        recs.append("建议: 调用 continue_session(status='resume') 恢复")
    else:
        recs.append(f"不可恢复错误: {_truncate(latest.get('error_message', ''), 60)}")
        recs.append("建议: 检查凭证/网络/模型可用性后手动继续")
    return recs


# ═══════════════════════════════════════════════════════════════════════════════
# 状态文件（供桌面徽章轮询）
# ═══════════════════════════════════════════════════════════════════════════════
def _status_file() -> Path:
    """状态文件路径：HERMES_HOME/plugins/resilience/status.json"""
    return _plugin_dir() / "status.json"


def _write_status_file() -> None:
    """写入最新状态到 status.json，供桌面徽章直接读取（无需 REST API）。"""
    try:
        cfg = _GLOBAL_CONFIG
        recent = _recent_logs("all", limit=10)
        # 合并所有 session 的最近中断
        all_entries = []
        path = _log_file()
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        all_entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        recent_all = all_entries[:10]

        status = {
            "plugin": "resilience",
            "version": "2.8.0",
            "updated_at": datetime.now().isoformat(),
            "enabled": cfg.get("enabled", True),
            "model_rotation": {
                "enabled": cfg.get("model_rotation_enabled", True),
                "fallback_order": cfg.get("model_rotation_fallback_order", []),
                "auto_rollback_enabled": cfg.get("model_auto_rollback_enabled", True),
                "rollback_after_failures": cfg.get("model_rollback_after_failures", 2),
            },
            "auto_approve": {
                "enabled": cfg.get("auto_approve_enabled", False),
                "monitor_thread_active": _auto_approve_active,
            },
            "stall_watchdog": {
                "enabled": cfg.get("stall_watchdog_enabled", True),
                "detect_seconds": cfg.get("stall_detect_seconds", 180),
                "max_auto_resumes": cfg.get("stall_max_auto_resumes", 3),
                "thread_active": _stall_watchdog_active,
            },
            "smart_auto_choice": {
                "enabled": cfg.get("smart_auto_choice_enabled", True),
                "timeout_seconds": cfg.get("smart_choice_timeout_seconds", 300),
                "thread_active": _smart_choice_active,
            },
            "recent_interrupts": recent_all,
        }
        out = _status_file()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("写入状态文件失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# 注册入口
# ═══════════════════════════════════════════════════════════════════════════════
def _ensure_status_file_fresh() -> None:
    """供 dashboard /health 等接口调用：若 status.json 缺失或超过 30s 未更新则重写。"""
    global _last_status_write
    out = _status_file()
    now = time.time()
    if out.exists():
        try:
            age = now - out.stat().st_mtime
            if age < 30:
                return  # 还新鲜，不用写
        except OSError:
            pass
    _write_status_file()
    _last_status_write = now


# ═══════════════════════════════════════════════════════════════════════════════
# 初始化：模块加载时从 config.yaml 直接读取（确保 gateway 未初始化时也有值）
# ═══════════════════════════════════════════════════════════════════════════════
try:
    import yaml
    _cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if _cfg_path.exists():
        _raw_file = yaml.safe_load(open(_cfg_path, encoding="utf-8").read())
        _res_file = (_raw_file or {}).get("resilience") or {}
        for k, v in _res_file.items():
            if v is not None and k in DEFAULTS:
                DEFAULTS[k] = v
        _GLOBAL_CONFIG = dict(DEFAULTS)
        _GLOBAL_CONFIG.update({k: v for k, v in _res_file.items() if v is not None})
except Exception:
    _GLOBAL_CONFIG = dict(DEFAULTS)


def status_badge_path() -> Path:
    return _status_file()


def register(ctx) -> None:
    global _GLOBAL_CONFIG, _PLUGIN_CTX
    _GLOBAL_CONFIG = _get_config(ctx)
    # 把热更新钩子绑定到活实例，供 dashboard 后端 _apply_config 调用
    _GLOBAL_CONFIG["_reconfigure_fn"] = lambda applied: _reconfigure_live(applied)
    # 绑定 ctx，供后台线程（停滞看门狗）通过 ctx.inject_message 注入续跑消息
    _PLUGIN_CTX = ctx

    ctx.register_tool(
        name="continue_session",
        toolset="resilience",
        schema=CONTINUE_TOOL,
        handler=handle_continue,
    )
    ctx.register_tool(
        name="resilience_status",
        toolset="resilience",
        schema=STATUS_TOOL,
        handler=handle_status,
    )

    ctx.register_hook("api_request_error", on_api_error)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    # v2.8：会话活动追踪钩子（停滞判定数据源）
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    # llm_request middleware：在请求发出前实际执行模型切换
    ctx.register_middleware("llm_request", on_llm_request)

    # 自动审批：根据配置启动/停止监控线程（热更新走 _apply_config）
    _sync_auto_approve()
    # v2.8：停滞看门狗 + 智能选项线程
    _sync_stall_watchdog()
    _sync_smart_choice()
    # 启动即写一次 status.json，桌面徽章立即反映真实版本（不等首次中断事件）
    _write_status_file()

    logger.info(
        "resilience 插件已加载 (enabled=%s, rotation=%s, auto_approve=%s, stall_watchdog=%s, smart_choice=%s)",
        _GLOBAL_CONFIG.get("enabled"),
        _GLOBAL_CONFIG.get("model_rotation_enabled"),
        _GLOBAL_CONFIG.get("auto_approve_enabled"),
        _GLOBAL_CONFIG.get("stall_watchdog_enabled"),
        _GLOBAL_CONFIG.get("smart_auto_choice_enabled"),
    )
