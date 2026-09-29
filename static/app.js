// ── i18n helper (graceful: works even if i18n.js somehow not loaded) ──────────
function t(key) {
  return window.i18n ? window.i18n.t(key) : key;
}

const chat = document.getElementById("chat");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const commentInput = document.getElementById("comment");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");

let sessionId = null;

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.parentElement.classList.toggle("error", isError);
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Lightweight markdown-ish rendering: code fences, inline code, bold, lists.
function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre>${code.trim()}</pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\n/g, "<br>");
  return html;
}

function addBubble(role, text, { typing = false } = {}) {
  const div = document.createElement("div");
  div.className = "bubble " + role + (typing ? " typing" : "");
  div.innerHTML = role === "bot" ? renderMarkdown(text) : escapeHtml(text).replace(/\n/g, "<br>");
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function parseSidFromUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get("sid");
}

async function loadExistingSession(sid) {
  setStatus(t("chat.status.opening"));
  sendBtn.disabled = true;
  try {
    const res = await fetch(`/api/session/${encodeURIComponent(sid)}`);
    if (res.status === 404) {
      // сессии нет — отправим обратно на интейк
      window.location.href = "/intake";
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    sessionId = data.session_id;

    // Apply language from session; fall back to localStorage hint set by intake.js.
    const sessionLang = data.language || null;
    if (window.i18n) {
      const lang = sessionLang || window.i18n.getLang();
      localStorage.setItem("bizware.lang", lang);
      window.i18n.applyLang(lang);
    }

    // Гидратируем существующий transcript: bot-сообщения как "bot", user — как "user"
    for (const m of data.transcript || []) {
      if (m.role === "assistant") addBubble("bot", m.content);
      else if (m.role === "user") addBubble("user", m.content);
    }
    setStatus(data.done ? t("chat.status.done") : t("chat.status.ready"));
    sendBtn.disabled = data.done === true;
    if (data.done === true) {
      // Re-render the CTA on a returning session — same flow as live done=True.
      renderPaymentCta(data.payment_cta_url || null);
    }
    input.focus();
  } catch (err) {
    setStatus(t("chat.error.connect") + err.message, true);
  }
}

async function startFreshSession() {
  setStatus(t("chat.status.opening_session"));
  sendBtn.disabled = true;
  try {
    const lang = window.i18n ? window.i18n.getLang() : "ru";
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ language: lang }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    sessionId = data.session_id;
    addBubble("bot", data.greeting);
    setStatus(t("chat.status.ready_fresh"));
    sendBtn.disabled = false;
    input.focus();
  } catch (err) {
    setStatus(t("chat.error.connect") + err.message, true);
  }
}

async function init() {
  // Apply language immediately from localStorage before session fetch completes.
  if (window.i18n) {
    window.i18n.applyLang(window.i18n.getLang());
  }

  const sid = parseSidFromUrl();
  if (sid) {
    await loadExistingSession(sid);
  } else {
    // Если зашли на /chat без sid — отправим на /intake.
    window.location.href = "/intake";
  }
}

function renderConfirmButton() {
  // Drop any previously-rendered confirm button (avoid duplicates after PM
  // re-prompts on a question/correction).
  const old = document.getElementById("confirm-btn");
  if (old) old.remove();
  const btn = document.createElement("button");
  btn.id = "confirm-btn";
  btn.type = "button";
  btn.textContent = t("chat.confirm_btn");
  btn.className = "confirm-btn";
  btn.addEventListener("click", () => {
    btn.disabled = true;
    sendMessage(t("chat.confirm_send"), null);
    btn.remove();
  });
  // Append below the last bot bubble.
  const log = document.getElementById("chat");
  if (log) log.appendChild(btn);
}

function renderAnalysisOptions(options) {
  // Drop any previously-rendered ANALYSIS option block.
  const old = document.getElementById("analysis-options");
  if (old) old.remove();
  const log = document.getElementById("chat");
  if (!log || !options || !options.length) return;
  const wrap = document.createElement("div");
  wrap.id = "analysis-options";
  wrap.className = "analysis-options";
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "confirm-btn analysis-option-btn";
    const label = (opt.shape || t("chat.analysis.fallback").replace("{index}", opt.index)).trim();
    btn.textContent = t("chat.analysis.variant")
      .replace("{index}", opt.index)
      .replace("{label}", label);
    btn.addEventListener("click", () => {
      btn.disabled = true;
      // Drop the button group so it doesn't double-fire.
      wrap.remove();
      sendMessage(t("chat.analysis.choose").replace("{index}", opt.index), null);
    });
    wrap.appendChild(btn);
  });
  log.appendChild(wrap);
}

function renderPaymentCta(url) {
  // Drop any previously-rendered CTA so we don't end up with two buttons.
  const old = document.getElementById("payment-cta");
  if (old) old.remove();
  const log = document.getElementById("chat");
  if (!log) return;
  if (url) {
    const a = document.createElement("a");
    a.id = "payment-cta";
    a.className = "confirm-btn";
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = t("chat.payment_cta");
    log.appendChild(a);
  } else {
    // Placeholder while PAYMENT_CTA_URL isn't configured yet.
    const btn = document.createElement("button");
    btn.id = "payment-cta";
    btn.type = "button";
    btn.className = "confirm-btn";
    btn.disabled = true;
    btn.textContent = t("chat.payment_cta");
    btn.title = t("chat.payment_cta_title");
    log.appendChild(btn);
  }
}

async function sendMessage(text, comment) {
  if (!sessionId) return;
  addBubble("user", text);
  if (comment) addBubble("user-comment", "💬 " + comment);
  const typing = addBubble("bot", t("chat.typing"), { typing: true });
  sendBtn.disabled = true;
  setStatus(t("chat.status.thinking"));
  try {
    const body = { session_id: sessionId, message: text };
    if (comment) body.comment = comment;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    typing.remove();
    if (!res.ok) {
      addBubble("bot", t("chat.error.server") + (data.detail || res.statusText));
      setStatus(t("chat.error.server_status"), true);
    } else {
      addBubble("bot", data.reply);
      setStatus(t("chat.status.ready"));
      if (data.awaiting_confirm) {
        renderConfirmButton();
      }
      if (data.analysis_options && data.analysis_options.length > 0) {
        renderAnalysisOptions(data.analysis_options);
      }
      if (data.done) {
        renderPaymentCta(data.payment_cta_url || null);
        sendBtn.disabled = true;
        setStatus(t("chat.status.done"));
      }
    }
  } catch (err) {
    typing.remove();
    addBubble("bot", t("chat.error.network_bubble") + err.message);
    setStatus(t("chat.error.network_status"), true);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  const comment = (commentInput && commentInput.value.trim()) || "";
  if (!text) return;
  input.value = "";
  if (commentInput) commentInput.value = "";
  sendMessage(text, comment || null);
});

input.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    form.requestSubmit();
  }
});

init();
