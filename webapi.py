"""「服务控制」页面的 Web API。

页面在 pages/服务控制/，通过 AstrBotPluginPage.apiGet / apiPost 调这里。
所有路由以插件名为前缀：/api/plug/<插件名>/<route>

只保留页面真正用到的 4 组能力：
  status  看服务状态（端口/进程/模型/音色）
  start / stop / restart  起停服务
  voice   切换音色（即时生效，不重载插件）

设计原则：阻塞操作（起停服务、切换音色）扔进线程池，别卡住事件循环。
"""

from __future__ import annotations

import asyncio

from quart import jsonify, request


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

    # ------------------------------------------------------------- 启动/停止
    async def start():
        body = await request.get_json(silent=True) or {}
        return jsonify(await _run(manager().start, str(body.get("model") or "")))

    async def stop():
        return jsonify(await _run(manager().stop))

    async def restart():
        body = await request.get_json(silent=True) or {}
        return jsonify(await _run(manager().restart, str(body.get("model") or "")))

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
    add("start", start, ["POST"], "启动本地服务")
    add("stop", stop, ["POST"], "停止本地服务")
    add("restart", restart, ["POST"], "重启本地服务")
    add("voice", voice, ["POST"], "切换音色")


