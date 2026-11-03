const state = {
  status: null,
  busy: false,
  dirty: false,
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

function banner(text, kind) {
  const box = $("banner");
  if (!text) {
    box.hidden = true;
    return;
  }
  box.textContent = text;
  box.className = `banner ${kind || ""}`;
  box.hidden = false;
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

function renderStatus(st) {
  state.status = st;

  const [text, kind] = statusLabel(st);
  const pill = $("state-pill");
  pill.className = `pill ${kind}`;
  $("state-text").textContent = text;

  $("fact-port").textContent = st.port || "—";
  $("fact-pid").textContent = st.pid ? `PID ${st.pid}` : "—";
  $("fact-model").textContent = st.model_name
    ? `${st.model_name}${st.model_size ? `（${st.model_size}）` : ""}`
    : "没找到 .gguf";
  const voices = st.voices || [];
  $("fact-voices").textContent = voices.length ? `${voices.length} 个` : "—";
  $("service-hint").textContent = st.healthy
    ? `服务在 ${st.api} 上正常响应`
    : st.running
      ? "进程还在，但 /health 没响应，可能正在加载模型"
      : "服务没在运行";

  if (!state.dirty) {
    $("dir-input").value = st.model_dir || "";
  }

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

  const autoToggle = $("auto-toggle");
  if (document.activeElement !== autoToggle) {
    autoToggle.checked = Boolean(st.config && st.config.auto_start_service);
  }

  const link = $("webui-link");
  if (st.healthy) {
    link.href = st.webui || st.api;
    link.classList.remove("hidden");
  } else {
    link.removeAttribute("href");
    link.classList.add("hidden");
  }

  const lines = st.log_tail || [];
  $("log").textContent = lines.length ? lines.join("\n") : "（还没有日志，启动一次就有了）";
  $("log").scrollTop = $("log").scrollHeight;

  $("start-btn").disabled = state.busy || Boolean(st.healthy);
  $("stop-btn").disabled = state.busy || !st.running;
  $("restart-btn").disabled = state.busy;

  const missing = [];
  if (!st.model_dir) missing.push("还没设置「本地模型目录」");
  else if (!st.exe) missing.push("目录里没找到 breeze-server.exe");
  if (st.model_dir && !st.model_name) missing.push("目录里没找到 .gguf 模型");
  if (missing.length && !st.healthy) banner(missing.join(" · "), "warn");
  else if (!st.platform_ok) banner("本地服务管理只在 Windows 上可用，其他系统请自己跑服务并用「服务地址」连接。", "warn");
  else banner("", "");
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
  const value = $("model-select").value || "";
  return value || "";
}

/* ------------------------------------------------------------------ 事件 */

$("refresh-btn").addEventListener("click", () => refresh());

$("dir-input").addEventListener("input", () => {
  state.dirty = true;
});

$("save-btn").addEventListener("click", () =>
  withBusy("正在保存配置…", async () => {
    const changes = {
      model_dir: $("dir-input").value.trim(),
      model_file: selectedModel(),
    };
    const res = await apiPost("config", { changes });
    state.dirty = false;
    toast(res.message || (res.ok ? "已保存" : "保存失败"));
    // 重载插件后实例是新的，稍等一下再刷状态
    await new Promise((r) => setTimeout(r, 1200));
  })
);

$("pick-btn").addEventListener("click", () =>
  withBusy("等你在弹出的窗口里选目录…", async () => {
    const res = await apiPost("pick", { initial: $("dir-input").value.trim() });
    if (res.path) {
      $("dir-input").value = res.path;
      state.dirty = true;
      toast("已选中，记得点「保存并应用」");
    } else if (!res.ok) {
      toast(res.message || "没选到目录");
    }
  })
);

$("detect-btn").addEventListener("click", () =>
  withBusy("正在扫描常见位置…", async () => {
    const res = await apiGet("detect");
    const list = $("candidates");
    list.innerHTML = "";
    const items = res.candidates || [];
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "没扫到。可以直接把路径粘进输入框，或用「浏览…」。";
      list.appendChild(li);
    }
    items.forEach((item) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "linkish";
      btn.textContent = item.path;
      btn.addEventListener("click", () => {
        $("dir-input").value = item.path;
        state.dirty = true;
        toast("已填入，记得点「保存并应用」");
      });
      const meta = document.createElement("span");
      meta.className = "muted";
      const count = (item.models || []).length;
      meta.textContent = count ? ` · ${count} 个模型` : " · 没有 .gguf";
      li.appendChild(btn);
      li.appendChild(meta);
      list.appendChild(li);
    });
    $("candidates-wrap").hidden = false;
  })
);

$("check-btn").addEventListener("click", () =>
  withBusy("正在检查目录…", async () => {
    const dir = $("dir-input").value.trim();
    const res = await apiGet("validate", dir ? { dir } : {});
    const list = $("checks");
    list.innerHTML = "";
    (res.checks || []).forEach((check) => {
      const li = document.createElement("li");
      li.className = check.ok ? "ok" : "bad";
      const name = document.createElement("strong");
      name.textContent = check.name;
      const detail = document.createElement("span");
      detail.textContent = check.detail;
      li.appendChild(name);
      li.appendChild(detail);
      list.appendChild(li);
    });
    $("checks-wrap").hidden = false;
    toast(res.ok ? "目录可以直接用" : "有项目没通过，看下面的检查结果");
  })
);

$("start-btn").addEventListener("click", () =>
  withBusy("正在启动服务（首次加载模型要几秒）…", async () => {
    const res = await apiPost("start", { model: selectedModel() });
    toast(res.message || "已发出启动请求");
    if (res.ok && !res.ready) {
      // 还没就绪，多等一会儿再刷
      await new Promise((r) => setTimeout(r, 4000));
    }
  })
);

$("stop-btn").addEventListener("click", () => {
  if (!window.confirm("确定停止本地服务吗？显存会被释放，语音功能会暂时不可用。")) return;
  withBusy("正在停止服务…", async () => {
    const res = await apiPost("stop", {});
    toast(res.message || "已发出停止请求");
  });
});

$("restart-btn").addEventListener("click", () =>
  withBusy("正在重启服务…", async () => {
    const res = await apiPost("restart", { model: selectedModel() });
    toast(res.message || "已发出重启请求");
  })
);

$("auto-toggle").addEventListener("change", async (e) => {
  const enable = e.target.checked;
  if (!enable && !window.confirm(
    "停止正在运行的服务，并禁止随 AstrBot 自动拉起？\n显存会立刻释放；想再用语音时把这个开关打开即可。"
  )) {
    e.target.checked = true;
    return;
  }
  e.target.disabled = true;
  try {
    const res = await apiPost("auto_start", { enabled: enable });
    if (!res.ok) {
      e.target.checked = !enable; // 失败回滚开关显示
      toast(res.message || "操作失败");
    } else {
      toast(res.message || "已保存");
    }
  } catch (err) {
    e.target.checked = !enable;
    toast(`出错了：${err.message || err}`);
  } finally {
    e.target.disabled = false;
    await refresh();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

refresh();
state.timer = setInterval(() => {
  if (!document.hidden) refresh();
}, 3000);
