(function () {
  // ── i18n init ─────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    if (window.i18n) {
      window.i18n.applyLang(window.i18n.getLang());
      window.i18n.mountLangToggle(document.getElementById("lang-toggle-slot"));
    }
  });

  // ── Form refs ─────────────────────────────────────────────────────────────
  const form = document.getElementById("intake-form");
  const errorBox = document.getElementById("form-error");
  const submitBtn = document.getElementById("submit-btn");
  const turnstileDiv = document.getElementById("turnstile-widget");

  let turnstileWidgetId = null;
  let turnstileToken = null;
  let turnstileEnabled = false;

  function t(key) {
    return window.i18n ? window.i18n.t(key) : key;
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.add("visible");
  }

  function clearError() {
    errorBox.classList.remove("visible");
    errorBox.textContent = "";
  }

  function emailLooksValid(s) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s.trim());
  }

  // ---- Turnstile bootstrap ------------------------------------------------

  async function fetchConfigAndMountTurnstile() {
    try {
      const res = await fetch("/api/config");
      if (!res.ok) return;
      const cfg = await res.json();
      const siteKey = cfg.turnstile_site_key || "";
      if (!siteKey) return; // отключено в env

      turnstileEnabled = true;

      // Determine language for Turnstile widget; fall back to "auto" if i18n not loaded.
      const lang = window.i18n ? window.i18n.getLang() : "auto";

      // Если скрипт ещё не загрузился — подождём интервалом, до 5с.
      const started = Date.now();
      const tryRender = () => {
        if (window.turnstile && typeof window.turnstile.render === "function") {
          turnstileWidgetId = window.turnstile.render(turnstileDiv, {
            sitekey: siteKey,
            theme: "light",
            language: lang,
            callback: (token) => {
              turnstileToken = token;
            },
            "expired-callback": () => {
              turnstileToken = null;
            },
            "error-callback": () => {
              turnstileToken = null;
            },
          });
          return;
        }
        if (Date.now() - started > 5000) return; // больше не пробуем
        setTimeout(tryRender, 100);
      };
      tryRender();
    } catch (e) {
      // если не получили конфиг — просто работаем без капчи (бэк всё равно проверит)
    }
  }

  fetchConfigAndMountTurnstile();

  // ---- form submit --------------------------------------------------------

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();

    const fd = new FormData(form);
    const lang = window.i18n ? window.i18n.getLang() : "ru";
    const payload = {
      name: (fd.get("name") || "").toString().trim(),
      email: (fd.get("email") || "").toString().trim(),
      age_range: fd.get("age_range") || "",
      gender: fd.get("gender") || "",
      sector: fd.get("sector") || "",
      time_eater: fd.get("time_eater") || "",
      turnstile_token: turnstileToken || "",
      language: lang,
    };

    if (!payload.name) {
      showError(t("intake.error.name"));
      return;
    }
    if (!emailLooksValid(payload.email)) {
      showError(t("intake.error.email"));
      return;
    }
    for (const f of ["age_range", "gender", "sector", "time_eater"]) {
      if (!payload[f]) {
        showError(t("intake.error.fields"));
        return;
      }
    }
    if (turnstileEnabled && !payload.turnstile_token) {
      showError(t("intake.error.captcha"));
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = t("intake.submitting");

    try {
      const res = await fetch("/api/intake", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        showError(data.detail || t("intake.error.server").replace("{code}", res.status));
        submitBtn.disabled = false;
        submitBtn.textContent = t("intake.submit");
        // Сбросим Turnstile, чтобы пользователь прошёл заново.
        if (turnstileEnabled && window.turnstile && turnstileWidgetId) {
          window.turnstile.reset(turnstileWidgetId);
          turnstileToken = null;
        }
        return;
      }
      // Stash language so chat.html can read it immediately before session load.
      if (window.i18n) {
        localStorage.setItem("bizware.lang", lang);
      }
      // Готово — переходим в чат.
      window.location.href = `/journey?sid=${encodeURIComponent(data.session_id)}`;
    } catch (err) {
      showError(t("intake.error.network"));
      submitBtn.disabled = false;
      submitBtn.textContent = t("intake.submit");
    }
  });
})();
