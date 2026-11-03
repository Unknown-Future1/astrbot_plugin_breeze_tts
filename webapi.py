"""「服务控制」页面的 Web API。

页面在 pages/服务控制/，通过 AstrBotPluginPage.apiGet / apiPost 调这里。
所有路由以插件名为前缀：/api/plug/<插件名>/<route>

设计原则：
  · 页面上任何按钮都不能把后端卡死 —— 启动/停止这类阻塞操作扔进线程池
  · 每个接口都给出中文 message，前端直接显示，不用猜
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from quart import jsonify, request

from .service.launcher import IS_WINDOWS, pick_directory

# 设置页允许改的配置项（key, 中文名）
EDITABLE_CONFIG = [
    ("model_dir", "模型目录"),
    ("model_file", "模型文件"),
    ("service_port", "服务端口"),
    ("auto_start_service", "启动时自动拉起服务"),
]


def _as_int(raw, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(raw))))
    except (TypeError, ValueError):
        return default


def _as_bool(raw, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on", "是")
    return default


async def _run(func, *args):
    """把阻塞调用丢到线程池，避免卡住事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def register_web_apis(plugin) -> None:
    base = f"/{plugin.name}"

    def add(route: str, handler, methods, desc: str) -> None:
        plugin.context.register_web_api(f"{base}/{route}", handler, methods, desc)

    def manager():
        return plugin.service

    # ------------------------------------------------------------- 状态
    async def status():
        payload = await _run(manager().status)
        payload["config"] = {
            "model_dir": plugin.model_dir,
            "model_file": plugin.model_file,
            "service_port": plugin.service_port,
            "auto_start_service": bool(plugin.auto_start_service),
            "voice_id": getattr(plugin, "voice_id", ""),
        }
        return jsonify(payload)

    async def log():
        lines = _as_int(request.args.get("lines", 60), 60, 1, 500)
        return jsonify({"lines": await _run(manager().log_tail, lines)})

    async def validate():
        # 支持 ?dir=... 检查还没保存的目录
        override = str(request.args.get("dir") or "").strip()
        target = manager().clone(model_dir=override) if override else manager()
        return jsonify(await _run(target.validate))

    async def detect():
        return jsonify({"candidates": await _run(manager().detect_roots)})

    # ------------------------------------------------------------- 目录选择
    async def pick():
        body = await request.get_json(silent=True) or {}
        if not IS_WINDOWS:
            return jsonify({"ok": False, "path": "", "message": "只有 Windows 用得着这个"}), 400
        initial = str(body.get("initial") or plugin.model_dir or "")
        path, error = await _run(pick_directory, initial)
        if error:
            return jsonify({"ok": False, "path": "", "message": error})
        return jsonify({"ok": True, "path": path, "message": "已选中目录" if path else "没有选择"})

    # ------------------------------------------------------------- 启动/停止
    async def start():
        body = await request.get_json(silent=True) or {}
        result = await _run(manager().start, str(body.get("model") or ""))
        return jsonify(result)

    async def stop():
        return jsonify(await _run(manager().stop))

    async def restart():
        body = await request.get_json(silent=True) or {}
        result = await _run(manager().restart, str(body.get("model") or ""))
        return jsonify(result)

    # ------------------------------------------------------------- 配置
    async def config():
        if request.method == "GET":
            return jsonify(
                {
                    "fields": [
                        {"key": key, "label": label, "value": plugin._cfg(key, "")}
                        for key, label in EDITABLE_CONFIG
                    ]
                }
            )

        body = await request.get_json(silent=True) or {}
        changes = body.get("changes")
        if not isinstance(changes, dict):
            return jsonify({"ok": False, "message": "请求格式不对"}), 400

        merged = dict(plugin.config)
        if "model_dir" in changes:
            merged["model_dir"] = str(changes["model_dir"] or "").strip()
        if "model_file" in changes:
            merged["model_file"] = str(changes["model_file"] or "").strip()
        if "service_port" in changes:
            merged["service_port"] = _as_int(changes["service_port"], 9000, 1, 65535)
        if "auto_start_service" in changes:
            merged["auto_start_service"] = _as_bool(changes["auto_start_service"])

        try:
            plugin.config.save_config(merged)
        except Exception:  # noqa: BLE001
            logger.exception("[BreezeTTS] 保存配置失败")
            return jsonify({"ok": False, "message": "保存配置文件失败，看一下 AstrBot 日志"}), 500

        star_manager = getattr(plugin.context, "_star_manager", None)
        applied = False
        if star_manager is not None:
            try:
                success, _ = await star_manager.reload(plugin.name)
                applied = bool(success)
            except Exception:  # noqa: BLE001
                logger.exception("[BreezeTTS] 配置已保存，但插件重载失败")
        return jsonify(
            {
                "ok": True,
                "applied": applied,
                "message": "已保存并生效" if applied else "已保存，重载插件后生效",
            }
        )

    # ------------------------------------------------------------- 自动拉起开关
    async def auto_start():
        """「随插件自动拉起」一体开关。

        关：先停掉正在运行的服务（失败则回滚不改设置），再保存 auto_start_service=false，
            热重载后不会再自动拉起。
        开：保存 auto_start_service=true 并热重载——重载时 initialize() 探测不到服务
            会自己把服务拉起来（复用现有链路，这里不重复造轮子）。
        """
        body = await request.get_json(silent=True) or {}
        raw = body.get("enabled")
        text = str(raw).strip().lower() if raw is not None else ""
        if isinstance(raw, bool):
            enabled = raw
        elif text in ("1", "true", "yes", "on", "是"):
            enabled = True
        elif text in ("0", "false", "no", "off", "否"):
            enabled = False
        else:
            return jsonify({"ok": False, "message": "缺少 enabled 参数"}), 400

        stop_note = ""
        if not enabled:
            stop_result = await _run(manager().stop)
            if not stop_result.get("ok"):
                # 服务没停成：设置保持原样，避免「配置关了服务却还活着」的不一致
                return jsonify(
                    {
                        "ok": False,
                        "message": f"服务停止失败，设置未改动：{stop_result.get('message')}",
                    }
                ), 500
            stop_note = str(stop_result.get("message") or "")

        merged = dict(plugin.config)
        merged["auto_start_service"] = enabled
        try:
            plugin.config.save_config(merged)
        except Exception:  # noqa: BLE001
            logger.exception("[BreezeTTS] 保存自动拉起开关失败")
            return jsonify({"ok": False, "message": "保存设置失败，看一下 AstrBot 日志"}), 500

        star_manager = getattr(plugin.context, "_star_manager", None)
        applied = False
        if star_manager is not None:
            try:
                success, _ = await star_manager.reload(plugin.name)
                applied = bool(success)
            except Exception:  # noqa: BLE001
                logger.exception("[BreezeTTS] 设置已保存，但插件重载失败")

        if enabled:
            message = "已允许自动拉起，插件重载后会自动把服务带起来"
        else:
            message = "已停止服务并禁止自动拉起，显存已释放"
            if stop_note:
                message += f"（{stop_note}）"
        if not applied:
            message += "；插件重载失败，重载后完全生效"
        return jsonify({"ok": True, "enabled": enabled, "applied": applied, "message": message})

    # ------------------------------------------------------------- 音色切换
    async def voice():
        """切换音色：只改运行中的客户端 + 落盘配置，不重载插件、不碰服务。"""
        body = await request.get_json(silent=True) or {}
        raw = body.get("voice_id")
        if raw is None:
            return jsonify({"ok": False, "message": "缺少 voice_id 参数"}), 400
        voice_id = str(raw).strip()

        available = [v["id"] for v in await _run(manager().voices)]
        if voice_id and available and voice_id not in available:
            return jsonify(
                {"ok": False, "message": f"服务端没有这个音色：{voice_id}（现有：{', '.join(available)}）"}
            ), 400

        merged = dict(plugin.config)
        merged["voice_id"] = voice_id
        try:
            plugin.config.save_config(merged)
        except Exception:  # noqa: BLE001
            logger.exception("[BreezeTTS] 保存音色失败")
            return jsonify({"ok": False, "message": "保存配置失败，看一下 AstrBot 日志"}), 500

        plugin.voice_id = voice_id
        plugin.client.voice = voice_id
        if voice_id:
            message = f"音色已切换：{voice_id}"
        else:
            await plugin._refresh_voice()
            message = f"已设为自动，当前使用：{plugin.voice_id or '（暂无可用音色）'}"
        return jsonify({"ok": True, "voice_id": plugin.voice_id, "message": message})

    add("status", status, ["GET"], "本地服务状态")
    add("log", log, ["GET"], "服务日志尾巴")
    add("validate", validate, ["GET"], "检查模型目录")
    add("detect", detect, ["GET"], "自动探测模型目录")
    add("pick", pick, ["POST"], "弹出原生目录选择框")
    add("start", start, ["POST"], "启动本地服务")
    add("stop", stop, ["POST"], "停止本地服务")
    add("restart", restart, ["POST"], "重启本地服务")
    add("config", config, ["GET", "POST"], "读写服务相关配置")
    add("auto_start", auto_start, ["POST"], "自动拉起一体开关")
    add("voice", voice, ["POST"], "切换音色")

    logger.debug("[BreezeTTS] Web API 已注册")
