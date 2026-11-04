const state = {
  status: null,
  busy: false,
  wasHealthy: null,
  voices: [],
  timer: null,
  voicesAt: 0,
  previewDefaultsApplied: false,
};

function service() {
  return window.AstrBotPluginPage || window.AstrBotPluginView || null;
}

function apiGet(endpoint, params) {
  const svc = service();
  if (!svc) return Promise.reject(new Error("页面桥接没加载出来，刷新一下试试"));
  return svc.apiGet(endpoint, params || {});
}

function apiPost(endpoint, body) {
  const svc = service();
  if (!svc) return Promise.reject(new Error("页面桥接没加载出来，刷新一下试试"));
  return svc.apiPost(endpoint, body || {});
}

const $ = (id) => document.getElementById(id);

function toast(text) {
  const box = $("toast");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    box.hidden = true;
  }, 3200);
}

function banner(text) {
  const box = $("banner");
  box.textContent = text || "";
  box.hidden = !text;
}

function setBusy(busy, label) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
  if (label) $("subtitle").textContent = label;
}

function statusLabel(st) {
  if (!st) return ["未知", "unknown"];
  if (st.healthy) return ["在线", "ok"];
  if (st.running) return ["进程在但没响应", "warn"];
  return ["离线", "off"];
}

/* ------------------------------------------------------------ 音色工作台 */

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind || ""}`;
  span.textContent = text;
  return span;
}

function renderVoices(payload) {
  const tbody = $("voice-body");
  const list = (payload && payload.voices) || [];
  const current = (payload && payload.current) || "";
  const healthy = Boolean(state.status && state.status.healthy);
  state.voices = list;
  state.voicesAt = Date.now();

  tbody.innerHTML = "";
  if (!list.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "muted";
    td.textContent = healthy ? "还没有音色，用下面的「克隆新音色」造一个" : "服务没在线，音色库读不到";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  list.forEach((v) => {
    const tr = document.createElement("tr");
    tr.dataset.id = v.id;

    const nameCell = document.createElement("td");
    const name = document.createElement("span");
    name.className = "voice-name";
    name.textContent = v.id;
    nameCell.appendChild(name);
    if (v.id === current) nameCell.appendChild(badge("使用中", "ok"));
    nameCell.appendChild(v.saved ? badge("已保存") : badge("仅内存", "warn"));

    const secCell = document.createElement("td");
    secCell.textContent = v.seconds ? `${v.seconds.toFixed(2)} s` : "—";

    const descCell = document.createElement("td");
    descCell.className = "voice-desc";
    descCell.textContent = v.description || "—";

    const actCell = document.createElement("td");
    actCell.className = "voice-actions";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "tiny";
    play.dataset.action = "preview";
    play.textContent = "试听";
    play.disabled = !healthy;
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "tiny";
    edit.dataset.action = "edit";
    edit.textContent = "编辑";
    edit.disabled = !healthy;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "tiny danger";
    del.dataset.action = "delete";
    del.textContent = "删除";
    del.disabled = !healthy || v.id === current;
    actCell.append(play, edit, del);

    tr.append(nameCell, secCell, descCell, actCell);
    tbody.appendChild(tr);
  });

  // 试听区的音色下拉
  const select = $("preview-voice");
  const keep = select.value;
  select.innerHTML = "";
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（没有可用音色）";
    select.appendChild(opt);
  }
  list.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = v.id === current ? `${v.id}（当前）` : v.id;
    select.appendChild(opt);
  });
  if (list.some((v) => v.id === keep)) select.value = keep;
  else if (list.some((v) => v.id === current)) select.value = current;
}

async function loadVoices(force) {
  if (state.busy) return;
  const fresh = Date.now() - state.voicesAt < 12000;
  if (!force && fresh) return;
  try {
    renderVoices(await apiGet("voices"));
  } catch (err) {
    renderVoices({ voices: [], current: "" });
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读不出这个文件"));
    reader.readAsDataURL(file);
  });
}

async function cloneVoice() {
  const fileInput = $("clone-file");
  const file = fileInput.files && fileInput.files[0];
  const name = $("clone-name").value.trim();
  const refText = $("clone-text").value.trim();
  const desc = $("clone-desc").value.trim();

  if (!file) return toast("先选一段 WAV 音频");
  if (!/\.wav$/i.test(file.name)) return toast("目前只支持 WAV，先转成 wav 再传");
  if (file.size > 10 * 1024 * 1024) return toast("音频太大了（上限 10 MB）");
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) return toast("名称只能用英文、数字、- 和 _");
  if (!refText) return toast("要填这段音频里说的每一个字");

  setBusy(true, "正在编码音色（几秒钟）…");
  try {
    const audioB64 = await fileToBase64(file);
    const res = await apiPost("voice_add", {
      name,
      ref_text: refText,
      description: desc,
      audio_b64: audioB64,
    });
    if (!res.ok) {
      toast(res.message || "克隆失败");
      return;
    }
    $("clone-file").value = "";
    $("clone-name").value = "";
    $("clone-desc").value = "";
    toast(res.message || "音色已保存");
    await loadVoices(true);
  } catch (err) {
    toast(`出错了：${err.message || err}`);
  } finally {
    setBusy(false);
    await refresh();
  }
}

async function deleteVoice(id) {
  if (!window.confirm(`删除音色 ${id}？\n会从服务里移除，并尝试删掉 .breeze 文件。`)) return;
  setBusy(true, `正在删除 ${id}…`);
  try {
    const res = await apiPost("voice_delete", { id });
    toast(res.message || (res.ok ? "已删除" : "删除失败"));
    await loadVoices(true);
  } catch (err) {
    toast(`出错了：${err.message || err}`);
  } finally {
    setBusy(false);
    await refresh();
  }
}

async function editVoice(id, current) {
  const next = window.prompt(`给「${id}」写个描述（留空则清除）：`, current || "");
  if (next === null) return;
  setBusy(true, `正在保存 ${id} 的描述…`);
  try {
    const res = await apiPost("voice_update", { id, description: next.trim() });
    toast(res.message || (res.ok ? "已保存" : "保存失败"));
    await loadVoices(true);
  } catch (err) {
    toast(`出错了：${err.message || err}`);
  } finally {
    setBusy(false);
    await refresh();
  }
}

async function preview() {
  const text = $("preview-text").value.trim();
  if (!text) return toast("先写点要读的内容");
  const voice = $("preview-voice").value || "";
  const speed = Math.max(-50, Math.min(100, Number($("preview-speed").value || 0)));
  const pitch = Math.max(-12, Math.min(12, Number($("preview-pitch").value || 0)));

  const btn = $("preview-btn");
  btn.disabled = true;
  btn.textContent = "合成中…";
  try {
    const res = await apiPost("preview", {
      text,
      voice_id: voice,
      speed,
      pitch,
      style_preset: $("preview-style").value || "",
      custom_instruction: $("preview-instruction").value.trim(),
    });
    if (!res.ok) {
      toast(res.message || "合成失败");
      return;
    }
    const player = $("player");
    const audio = $("preview-audio");
    player.hidden = false;
    audio.src = `data:audio/wav;base64,${res.audio_b64}`;
    audio.play().catch(() => {});
  } catch (err) {
    toast(`出错了：${err.message || err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "合成试听";
  }
}

function previewLocalFile() {
  const fileInput = $("clone-file");
  const file = fileInput.files && fileInput.files[0];
  if (!file) return toast("先选一段音频");
  const player = $("player");
  const audio = $("preview-audio");
  player.hidden = false;
  audio.src = URL.createObjectURL(file);
  audio.play().catch(() => {});
}

/* ------------------------------------------------------------------ 渲染 */

function renderStatus(st) {
  state.status = st;

  const [text, kind] = statusLabel(st);
  $("state-pill").className = `pill ${kind}`;
  $("state-text").textContent = text;

  $("fact-port").textContent = st.port || "—";
  $("fact-pid").textContent = st.pid ? `PID ${st.pid}` : "—";
  $("fact-model").textContent = st.model_name
    ? `${st.model_name}${st.model_size ? `（${st.model_size}）` : ""}`
    : "没找到 .gguf";
  $("service-hint").textContent = st.healthy
    ? `服务在 ${st.api} 上正常响应`
    : st.running
      ? "进程还在，但 /health 没响应，可能正在加载模型"
      : "服务没在运行";

  const select = $("model-select");
  const models = st.models || [];
  const current = select.value;
  const wanted = st.model_name || (models[0] && models[0].name) || "";
  select.innerHTML = "";
  if (!models.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（目录里没有 .gguf）";
    select.appendChild(opt);
  } else {
    models.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = `${m.name} · ${m.size}`;
      select.appendChild(opt);
    });
    select.value = models.some((m) => m.name === current) ? current : wanted;
  }

  $("start-btn").disabled = state.busy || Boolean(st.healthy);
  $("stop-btn").disabled = state.busy || !st.running;
  $("restart-btn").disabled = state.busy;

  // 音色下拉：服务端列表优先，没有就用本地 .breeze 文件名兜底
  const voiceSelect = $("voice-select");
  const voices = (st.voices && st.voices.length) ? st.voices : (st.voice_files || []);
  const currentVoice = (st.config && st.config.voice_id) || "";
  voiceSelect.innerHTML = "";
  const autoOpt = document.createElement("option");
  autoOpt.value = "";
  autoOpt.textContent = "自动（用服务端第一个音色）";
  voiceSelect.appendChild(autoOpt);
  voices.forEach((id) => {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    voiceSelect.appendChild(opt);
  });
  if (currentVoice && !voices.includes(currentVoice)) {
    const opt = document.createElement("option");
    opt.value = currentVoice;
    opt.textContent = `${currentVoice}（服务端未加载）`;
    voiceSelect.appendChild(opt);
  }
  if (document.activeElement !== voiceSelect) {
    voiceSelect.value = currentVoice;
  }
  voiceSelect.disabled = state.busy || !st.healthy;

  // 原生界面兜底链接
  const native = $("native-link");
  const url = st.webui || st.api || "";
  native.dataset.url = url;
  native.hidden = !url;

  // 工作台只在服务在线时显示
  $("studio-card").hidden = !st.healthy;

  // 风格预设下拉：选项来自后端，首次加载时套用插件当前设置
  const styleSelect = $("preview-style");
  const presets = st.presets || [];
  if (styleSelect.options.length !== presets.length) {
    styleSelect.innerHTML = "";
    presets.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      styleSelect.appendChild(opt);
    });
  }
  if (!state.previewDefaultsApplied && st.defaults) {
    styleSelect.value = st.defaults.style_preset || "normal";
    if (!$("preview-instruction").value) {
      $("preview-instruction").value = st.defaults.custom_instruction || "";
    }
    state.previewDefaultsApplied = true;
  }

  // 从离线变在线时刷新音色库
  if (st.healthy && state.wasHealthy !== true) loadVoices(true);
  state.wasHealthy = Boolean(st.healthy);

  const missing = [];
  if (!st.model_dir) missing.push("设置里还没填「本地模型目录」");
  else if (!st.exe) missing.push("目录里没找到 breeze-server.exe");
  if (st.model_dir && !st.model_name) missing.push("目录里没找到 .gguf 模型");
  if (missing.length && !st.healthy) banner(missing.join(" · "));
  else if (!st.platform_ok) banner("本地服务管理只在 Windows 上可用，其他系统请自己跑服务。");
  else banner("");
}

async function refresh() {
  if (state.busy) return;
  try {
    const st = await apiGet("status");
    $("subtitle").textContent = st.healthy
      ? `服务运行中 · ${st.api}`
      : "服务未运行，点下面的按钮启动";
    renderStatus(st);
  } catch (err) {
    $("subtitle").textContent = `读取状态失败：${err.message || err}`;
  }
}

async function withBusy(label, fn) {
  if (state.busy) return;
  setBusy(true, label);
  try {
    await fn();
  } catch (err) {
    toast(`出错了：${err.message || err}`);
  } finally {
    setBusy(false);
    await refresh();
  }
}

function selectedModel() {
  return $("model-select").value || "";
}

/* ------------------------------------------------------------------ 事件 */

$("refresh-btn").addEventListener("click", () => {
  refresh();
  loadVoices(true);
});

$("start-btn").addEventListener("click", () =>
  withBusy("正在启动服务（首次加载模型要几秒）…", async () => {
    const res = await apiPost("start", { model: selectedModel() });
    toast(res.message || "已发出启动请求");
    if (res.ok && !res.ready) {
      await new Promise((r) => setTimeout(r, 4000));
    }
  })
);

$("stop-btn").addEventListener("click", () => {
  if (!window.confirm("确定关闭本地服务吗？显存会被释放，语音功能会暂时不可用。")) return;
  withBusy("正在关闭服务…", async () => {
    const res = await apiPost("stop", {});
    toast(res.message || "已发出关闭请求");
  });
});

$("restart-btn").addEventListener("click", () =>
  withBusy("正在重启服务…", async () => {
    const res = await apiPost("restart", { model: selectedModel() });
    toast(res.message || "已发出重启请求");
  })
);

$("voice-select").addEventListener("change", async (e) => {
  const value = e.target.value;
  e.target.disabled = true;
  try {
    const res = await apiPost("voice", { voice_id: value });
    toast(res.message || (res.ok ? "音色已切换" : "切换失败"));
    await loadVoices(true);
    await refresh();
  } catch (err) {
    toast(`出错了：${err.message || err}`);
    await refresh();
  } finally {
    e.target.disabled = false;
  }
});

// 音色库表格：事件委托
$("voice-body").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const row = btn.closest("tr");
  const id = row && row.dataset.id;
  if (!id) return;
  const voice = state.voices.find((v) => v.id === id) || {};
  if (btn.dataset.action === "preview") {
    $("preview-voice").value = id;
    preview();
  } else if (btn.dataset.action === "edit") {
    editVoice(id, voice.description || "");
  } else if (btn.dataset.action === "delete") {
    deleteVoice(id);
  }
});

$("clone-btn").addEventListener("click", cloneVoice);
$("clone-preview-btn").addEventListener("click", previewLocalFile);
$("preview-btn").addEventListener("click", preview);

$("native-link").addEventListener("click", (e) => {
  e.preventDefault();
  const url = $("native-link").dataset.url;
  if (url) window.open(url, "_blank", "noopener");
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refresh();
    loadVoices(true);
  }
});

refresh();
loadVoices(true);
state.timer = setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);
