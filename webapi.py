"""「服务控制」页面的 Web API。

页面在 pages/服务控制/，通过 AstrBotPluginPage.apiGet / apiPost 调这里。
所有路由以插件名为前缀：/api/plug/<插件名>/<route>

能力：
  status                  看服务状态（端口/进程/模型/音色）
  start / stop / restart  起停服务
  voice                   切换音色（即时生效，不重载插件）
  voices                  音色库明细（时长/描述/文件是否存在）
  voice_add               克隆新音色（只收 WAV）
  voice_delete            移除音色（内存 + .breeze 文件）
  voice_update            改描述
  preview                 试听：指定音色合成一小段

设计原则：阻塞操作（起停服务、写文件）扔进线程池，别卡住事件循环；
网络调用走 aiohttp，且 trust_env=False（避免系统代理劫持 127.0.0.1）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re

import aiohttp
from quart import jsonify, request

MAX_UPLOAD = 10 * 1024 * 1024  # 10 MB：够放 60 秒 24k 单声道 WAV
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _run(func, *args):
    """把阻塞调用丢到线程池，避免卡住事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def _is_wav(raw: bytes) -> bool:
    return len(raw) > 44 and raw[0:4] == b"RIFF" and raw[8:12] == b"WAVE"


def register_web_apis(plugin) -> None:
    base = f"/{plugin.name}"

    def add(route: str, handler, methods, desc: str) -> None:
        plugin.context.register_web_api(f"{base}/{route}", handler, methods, desc)

    def manager():
        return plugin.service

    def api_base() -> str:
        return plugin.client.base

    def session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=120)
        )

    # ------------------------------------------------------------- 状态
    async def status():
        payload = await _run(manager().status)
        # 页面只额外需要「当前音色」；端口/模型/目录在 status() 里已经有了，不再重复回
        payload["config"] = {"voice_id": getattr(plugin, "voice_id", "")}
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

    # ------------------------------------------------------------- 音色库
    async def voices():
        details = await _run(manager().voice_details)
        folder = await _run(manager().voices_dir)
        return jsonify({
            "ok": True,
            "current": getattr(plugin, "voice_id", ""),
            "voices_dir": str(folder) if folder else "",
            "voices": details,
        })

    async def voice_add():
        """克隆新音色：只收 WAV（Breeze 只认 WAV，其它格式会被它拒掉）。"""
        body = await request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()
        ref_text = str(body.get("ref_text") or "").strip()
        description = str(body.get("description") or "").strip()
        audio_b64 = str(body.get("audio_b64") or "")

        if not NAME_RE.match(name):
            return jsonify({"ok": False, "message": "名称只能用英文、数字、- 和 _（1~64 位）"}), 400
        if not ref_text:
            return jsonify({"ok": False, "message": "要填「这段音频里说的每一个字」，否则音色会跑偏"}), 400
        if audio_b64.startswith("data:"):
            audio_b64 = audio_b64.split(",", 1)[-1]
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"ok": False, "message": "音频数据读不出来，换一个文件试试"}), 400
        if not raw:
            return jsonify({"ok": False, "message": "没有收到音频"}), 400
        if len(raw) > MAX_UPLOAD:
            return jsonify({"ok": False, "message": "音频太大（上限 10 MB），剪短一点"}), 400
        if not _is_wav(raw):
            return jsonify({
                "ok": False,
                "message": "目前只支持 WAV。mp3 / m4a / flac 需要先转成 wav（16kHz~48kHz 单声道即可）",
            }), 400

        form = aiohttp.FormData()
        form.add_field("ref_audio", raw, filename=f"{name}.wav", content_type="audio/wav")
        form.add_field("ref_text", ref_text)
        form.add_field("name", name)
        if description:
            form.add_field("description", description)

        try:
            async with session() as s:
                async with s.post(f"{api_base()}/v1/voices", data=form) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        return jsonify({"ok": False, "message": f"服务拒绝了这段音频：{text[:160]}"}), 400
        except aiohttp.ClientError as exc:
            return jsonify({"ok": False, "message": f"连不上本地服务：{type(exc).__name__}"}), 500

        detail = next((v for v in await _run(manager().voice_details) if v["id"] == name), {})
        return jsonify({
            "ok": True,
            "voice": detail,
            "message": f"音色已保存：{name}（{detail.get('seconds', 0):.2f} 秒）",
        })

    async def voice_delete():
        body = await request.get_json(silent=True) or {}
        vid = str(body.get("id") or "").strip()
        if not NAME_RE.match(vid):
            return jsonify({"ok": False, "message": "音色名不合法"}), 400
        if vid == getattr(plugin, "voice_id", ""):
            return jsonify({
                "ok": False,
                "message": f"{vid} 正在使用中，先在「当前音色」里换一个再删",
            }), 400

        mem_note = ""
        try:
            async with session() as s:
                async with s.delete(f"{api_base()}/v1/voices/{vid}") as resp:
                    if resp.status not in (200, 404):
                        mem_note = f"（服务返回 {resp.status}）"
        except aiohttp.ClientError as exc:
            return jsonify({"ok": False, "message": f"连不上本地服务：{type(exc).__name__}"}), 500

        removed, note = await _run(manager().remove_voice_file, vid)
        return jsonify({
            "ok": True,
            "file_removed": removed,
            "message": f"已移除 {vid}{mem_note}；" + ("文件也删掉了" if removed else f"文件未删（{note}）"),
        })

    async def voice_update():
        body = await request.get_json(silent=True) or {}
        vid = str(body.get("id") or "").strip()
        description = str(body.get("description") or "")
        if not NAME_RE.match(vid):
            return jsonify({"ok": False, "message": "音色名不合法"}), 400
        try:
            async with session() as s:
                async with s.patch(
                    f"{api_base()}/v1/voices/{vid}", json={"description": description}
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        return jsonify({"ok": False, "message": f"保存失败：{text[:160]}"}), 400
        except aiohttp.ClientError as exc:
            return jsonify({"ok": False, "message": f"连不上本地服务：{type(exc).__name__}"}), 500
        return jsonify({"ok": True, "message": "描述已保存"})

    # ------------------------------------------------------------- 试听
    async def preview():
        body = await request.get_json(silent=True) or {}
        text = str(body.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "message": "先写点要读的内容"}), 400
        if len(text) > 100:
            text = text[:100]
        voice_id = str(body.get("voice_id") or "").strip() or None
        try:
            speed = int(body.get("speed") or 0)
            pitch = int(body.get("pitch") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "语速/音高要是整数"}), 400
        speed = max(-50, min(100, speed))
        pitch = max(-12, min(12, pitch))

        instruction = plugin.custom_instruction or plugin.preset_instruction()
        cfg = plugin.cfg_scale if instruction else 1.0
        try:
            wav = await plugin.client.synth(
                text,
                instructions=instruction or None,
                speed_ratio=speed,
                cfg_scale=cfg,
                pitch_shift=pitch,
                voice=voice_id,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"合成失败：{type(exc).__name__}"}), 500
        if not wav:
            return jsonify({"ok": False, "message": "合成失败：服务没返回音频（看服务是否在线）"}), 500
        return jsonify({
            "ok": True,
            "audio_b64": plugin.client.to_base64(wav),
            "bytes": len(wav),
        })

    add("status", status, ["GET"], "本地服务状态")
    add("start", start, ["POST"], "启动本地服务")
    add("stop", stop, ["POST"], "停止本地服务")
    add("restart", restart, ["POST"], "重启本地服务")
    add("voice", voice, ["POST"], "切换音色")
    add("voices", voices, ["GET"], "音色库明细")
    add("voice_add", voice_add, ["POST"], "克隆音色（WAV）")
    add("voice_delete", voice_delete, ["POST"], "移除音色")
    add("voice_update", voice_update, ["POST"], "修改音色描述")
    add("preview", preview, ["POST"], "试听合成")
