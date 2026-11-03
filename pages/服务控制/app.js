const state = {
  status: null,
  busy: false,
  wasHealthy: null,
  timer: null,
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

/* ------------------------------------------------------- 模型可视化（折叠） */

function syncViz(st) {
  const card = $("viz-card");
  const frame = $("viz-frame");
  const empty = $("viz-empty");
  const healthy = Boolean(st.healthy);
  const url = st.webui || st.api || "";

  if (healthy && url) {
    empty.hidden = true;
    frame.hidden = false;
    if (frame.dataset.src !== url) {
      frame.dataset.src = url;
      frame.src = url;
    }
    // 只在「从离线变成在线」的瞬间自动展开，之后尊重用户的手动折叠
    if (state.wasHealthy !== true) card.open = true;
  } else {
    empty.hidden = false;
    frame.hidden = true;
    frame.removeAttribute("src");
    frame.dataset.src = "";
    card.open = false;
  }

  $("viz-hint").textContent = healthy ? "已连接" : "服务未连接";
  state.wasHealthy = healthy;
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

  syncViz(st);

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

$("refresh-btn").addEventListener("click", () => refresh());

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
    await refresh();
  } catch (err) {
    toast(`出错了：${err.message || err}`);
    await refresh();
  } finally {
    e.target.disabled = false;
  }
});

// 折叠卡片被手动展开时，若服务在线则补加载
$("viz-card").addEventListener("toggle", () => {
  const st = state.status;
  if (!st || !st.healthy) return;
  const frame = $("viz-frame");
  const url = st.webui || st.api || "";
  if (url && frame.dataset.src !== url) {
    frame.dataset.src = url;
    frame.src = url;
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

refresh();
state.timer = setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);
