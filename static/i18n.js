/**
 * i18n.js — BizWare bilingual support (RU / EN)
 * Loaded by the intake flow.
 * Exposes window.i18n.
 */
(function () {
  "use strict";

  var STRINGS = {
    ru: {
      // ── Intake page ───────────────────────────────────────────────────────
      "intake.page.title": "Расскажите о себе",

      "intake.q1.label": "Как могу к вам обращаться?",
      "intake.q1.placeholder": "Ваше имя",

      "intake.q2.label": "Email",
      "intake.q2.hint": "оставьте email — и через 5 минут у вас будет готов план",
      "intake.q2.placeholder": "you@example.com",

      "intake.q3.label": "Сколько вам лет?",
      "intake.q3.opt1": "до 25",
      "intake.q3.opt2": "25–35",
      "intake.q3.opt3": "35–45",
      "intake.q3.opt4": "45–55",
      "intake.q3.opt5": "55 и старше",

      "intake.q4.label": "Какой у вас пол?",
      "intake.q4.opt1": "женский",
      "intake.q4.opt2": "мужской",
      "intake.q4.opt3": "пропустить",

      "intake.q5.label": "Чем ваш бизнес занимается?",
      "intake.q5.opt1": "услуги (мастер, репетитор, консультант)",
      "intake.q5.opt2": "товары / магазин",
      "intake.q5.opt3": "онлайн-курсы / инфопродукт",
      "intake.q5.opt4": "производство / ремесло",
      "intake.q5.opt5": "услуги для других бизнесов (b2b)",
      "intake.q5.opt6": "другое",

      "intake.q6.label": "Что у вас сейчас съедает больше всего времени?",
      "intake.q6.opt1": "общение с клиентами",
      "intake.q6.opt2": "продажи и переговоры",
      "intake.q6.opt3": "сама работа / производство",
      "intake.q6.opt4": "документы, отчёты, рутина",
      "intake.q6.opt5": "маркетинг и привлечение",
      "intake.q6.opt6": "другое",

      "intake.submit": "Пройти опрос и получить решение сегодня",
      "intake.submitting": "Отправляю…",

      // ── Intake errors ─────────────────────────────────────────────────────
      "intake.error.name": "Заполните имя.",
      "intake.error.email": "Email выглядит неправильно — проверьте формат.",
      "intake.error.fields": "Выберите вариант во всех вопросах.",
      "intake.error.captcha": "Подождите — проверка безопасности ещё идёт. Попробуйте через секунду.",
      "intake.error.server": "Ошибка {code}. Попробуйте ещё раз.",
      "intake.error.network": "Сеть упала. Попробуйте ещё раз.",

      // ── Chat page ─────────────────────────────────────────────────────────
      "chat.title": "BizWare — диалог",
      "chat.input.placeholder": "Ваш ответ…",
      "chat.comment.toggle": "Хочу оставить комментарий о вопросе",
      "chat.comment.placeholder": "Что-то непонятно в вопросе, бот забыл что-то, или просто заметка для нас — напишите здесь",
      "chat.send_button": "Отправить",

      "chat.status.connecting": "Подключаюсь…",
      "chat.status.opening": "Открываю диалог…",
      "chat.status.ready": "Готов к следующему вопросу.",
      "chat.status.ready_fresh": "Готов к диалогу.",
      "chat.status.thinking": "Думаю…",
      "chat.status.done": "Диалог завершён.",
      "chat.status.opening_session": "Открываю сессию…",

      "chat.typing": "печатает…",
      "chat.error.connect": "Ошибка подключения: ",
      "chat.error.server": "⚠ Ошибка: ",
      "chat.error.server_status": "Ошибка от сервера",
      "chat.error.network_bubble": "⚠ Сеть упала: ",
      "chat.error.network_status": "Ошибка сети",

      "chat.confirm_btn": "✅ Да, вопросов нет",
      "chat.confirm_send": "да, вопросов нет, подтверждаю",
      "chat.analysis.variant": "Вариант {index}: {label}",
      "chat.analysis.fallback": "вариант {index}",
      "chat.analysis.choose": "выбираю вариант {index}",
      "chat.payment_cta": "Перейти в чат для согласования выполнения",
      "chat.payment_cta_title": "Уточняем у команды, скоро появится ссылка.",

      // ── Mode toggle + Journey ────────────────────────────────────────────
      "mode.chat": "Чат",
      "mode.journey": "Путь",
      "journey.point_a": "Точка А — Где вы сейчас",
      "journey.point_b": "Точка Б — Куда хотите прийти",
      "journey.step": "Шаг",
      "journey.not_selected": "Не выбрано",
      "journey.send": "Отправить",
      "journey.placeholder": "Ваш ответ...",
      "journey.checkpoint": "Подтверждение",
      "journey.clarity": "Ясность",
      "journey.resources": "Ресурсы",
    },

    en: {
      // ── Intake page ───────────────────────────────────────────────────────
      "intake.page.title": "Tell us about yourself",

      "intake.q1.label": "What name should I use for you?",
      "intake.q1.placeholder": "Your name",

      "intake.q2.label": "Email",
      "intake.q2.hint": "leave your email — your plan will be ready in 5 minutes",
      "intake.q2.placeholder": "you@example.com",

      "intake.q3.label": "Your age?",
      "intake.q3.opt1": "under 25",
      "intake.q3.opt2": "25–35",
      "intake.q3.opt3": "35–45",
      "intake.q3.opt4": "45–55",
      "intake.q3.opt5": "55+",

      "intake.q4.label": "Your gender?",
      "intake.q4.opt1": "female",
      "intake.q4.opt2": "male",
      "intake.q4.opt3": "skip",

      "intake.q5.label": "What does your business do?",
      "intake.q5.opt1": "services (consultant, tutor, specialist)",
      "intake.q5.opt2": "products / store",
      "intake.q5.opt3": "online courses / info products",
      "intake.q5.opt4": "manufacturing / crafts",
      "intake.q5.opt5": "services for other businesses (b2b)",
      "intake.q5.opt6": "other",

      "intake.q6.label": "What eats up the most of your time right now?",
      "intake.q6.opt1": "customer chats",
      "intake.q6.opt2": "sales and negotiations",
      "intake.q6.opt3": "the work itself / production",
      "intake.q6.opt4": "paperwork, reports & routine",
      "intake.q6.opt5": "marketing and outreach",
      "intake.q6.opt6": "other",

      "intake.submit": "Take the survey and get a plan today",
      "intake.submitting": "Sending…",

      // ── Intake errors ─────────────────────────────────────────────────────
      "intake.error.name": "Please enter your name.",
      "intake.error.email": "That email doesn't look right — check the format.",
      "intake.error.fields": "Please choose an answer for every question.",
      "intake.error.captcha": "Security check still in progress — try again in a moment.",
      "intake.error.server": "Error {code}. Please try again.",
      "intake.error.network": "Connection lost. Please try again.",

      // ── Chat page ─────────────────────────────────────────────────────────
      "chat.title": "BizWare — conversation",
      "chat.input.placeholder": "Your answer…",
      "chat.comment.toggle": "Leave a comment about this question",
      "chat.comment.placeholder": "Something unclear, something the bot missed, or just a note for us — write it here",
      "chat.send_button": "Send",

      "chat.status.connecting": "Connecting…",
      "chat.status.opening": "Opening conversation…",
      "chat.status.ready": "Ready for the next question.",
      "chat.status.ready_fresh": "Ready to chat.",
      "chat.status.thinking": "Working on it, one moment…",
      "chat.status.done": "Conversation finished.",
      "chat.status.opening_session": "Opening session…",

      "chat.typing": "typing…",
      "chat.error.connect": "Connection error: ",
      "chat.error.server": "⚠ Error: ",
      "chat.error.server_status": "Server error",
      "chat.error.network_bubble": "⚠ Connection lost: ",
      "chat.error.network_status": "Network error",

      "chat.confirm_btn": "✅ No questions, looks good",
      "chat.confirm_send": "no questions, I confirm",
      "chat.analysis.variant": "Option {index}: {label}",
      "chat.analysis.fallback": "option {index}",
      "chat.analysis.choose": "I choose option {index}",
      "chat.payment_cta": "Go to chat to confirm and get started",
      "chat.payment_cta_title": "We're checking with the team — a link will appear shortly.",

      // ── Mode toggle + Journey ────────────────────────────────────────────
      "mode.chat": "Chat",
      "mode.journey": "Journey",
      "journey.point_a": "Point A — Where you are now",
      "journey.point_b": "Point B — Where you want to be",
      "journey.step": "Step",
      "journey.not_selected": "Not selected",
      "journey.send": "Send",
      "journey.placeholder": "Your answer...",
      "journey.checkpoint": "Confirmation",
      "journey.clarity": "Clarity",
      "journey.resources": "Resources",
    },
  };

  // ── Core lang resolution ─────────────────────────────────────────────────

  function getLang() {
    var stored = localStorage.getItem("bizware.lang");
    if (stored === "en" || stored === "ru") return stored;
    var nav = (navigator.language || navigator.userLanguage || "ru").toLowerCase();
    return nav.startsWith("en") ? "en" : "ru";
  }

  function setLang(lang) {
    if (lang !== "en" && lang !== "ru") return;
    localStorage.setItem("bizware.lang", lang);
    applyLang(lang);
    document.dispatchEvent(new CustomEvent("languagechange", { detail: { lang: lang } }));
  }

  // ── String lookup ────────────────────────────────────────────────────────

  function t(key) {
    var lang = getLang();
    var dict = STRINGS[lang] || STRINGS.ru;
    return Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : (STRINGS.ru[key] || key);
  }

  // ── DOM application ──────────────────────────────────────────────────────

  function applyLang(lang) {
    if (lang !== "en" && lang !== "ru") lang = "ru";
    var dict = STRINGS[lang] || STRINGS.ru;

    // Swap <html lang="...">
    document.documentElement.lang = lang;

    // Swap data-i18n text content
    var elems = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < elems.length; i++) {
      var el = elems[i];
      var key = el.getAttribute("data-i18n");
      if (Object.prototype.hasOwnProperty.call(dict, key)) {
        el.textContent = dict[key];
      }
    }

    // Swap data-i18n-placeholder
    var pElems = document.querySelectorAll("[data-i18n-placeholder]");
    for (var j = 0; j < pElems.length; j++) {
      var pEl = pElems[j];
      var pKey = pEl.getAttribute("data-i18n-placeholder");
      if (Object.prototype.hasOwnProperty.call(dict, pKey)) {
        pEl.placeholder = dict[pKey];
      }
    }

    // Update toggle button states if mounted
    var btns = document.querySelectorAll(".lang-toggle button[data-lang]");
    for (var k = 0; k < btns.length; k++) {
      var btn = btns[k];
      var isActive = btn.getAttribute("data-lang") === lang;
      btn.setAttribute("aria-pressed", String(isActive));
      btn.classList.toggle("lang-toggle__btn--active", isActive);
    }

    // Swap rotator words if present (index.html)
    var rotatorWords = document.querySelectorAll(".rotator__word[data-i18n]");
    for (var r = 0; r < rotatorWords.length; r++) {
      var rEl = rotatorWords[r];
      var rKey = rEl.getAttribute("data-i18n");
      if (Object.prototype.hasOwnProperty.call(dict, rKey)) {
        rEl.textContent = dict[rKey];
      }
    }

    // Swap rotator prefix text node (the text before the .rotator span)
    var rotatorLine = document.querySelector(".hero__rotator-line");
    if (rotatorLine) {
      var prefixNode = null;
      var nodes = rotatorLine.childNodes;
      for (var n = 0; n < nodes.length; n++) {
        if (nodes[n].nodeType === Node.TEXT_NODE && nodes[n].textContent.trim()) {
          prefixNode = nodes[n];
          break;
        }
      }
      if (prefixNode && dict["hero.rotator.prefix"]) {
        prefixNode.textContent = dict["hero.rotator.prefix"];
      }
    }
  }

  // ── Toggle widget ────────────────────────────────────────────────────────

  function mountLangToggle(slotEl) {
    if (!slotEl) return;
    var wrap = document.createElement("div");
    wrap.className = "lang-toggle";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "Language / Язык");

    ["en", "ru"].forEach(function (lang) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "lang-toggle__btn";
      btn.setAttribute("data-lang", lang);
      btn.textContent = lang.toUpperCase();
      var cur = getLang();
      btn.setAttribute("aria-pressed", String(cur === lang));
      if (cur === lang) btn.classList.add("lang-toggle__btn--active");
      btn.addEventListener("click", function () {
        setLang(lang);
      });
      wrap.appendChild(btn);
    });

    slotEl.appendChild(wrap);
  }

  // ── Mode toggle widget ─────────────────────────────────────────────────

  function getMode() {
    var stored = localStorage.getItem("bizware.mode");
    if (stored === "chat" || stored === "journey") return stored;
    return "chat";
  }

  function setMode(mode) {
    if (mode !== "chat" && mode !== "journey") return;
    localStorage.setItem("bizware.mode", mode);
    // Update button states
    var btns = document.querySelectorAll(".mode-toggle button[data-mode]");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      var isActive = btn.getAttribute("data-mode") === mode;
      btn.setAttribute("aria-pressed", String(isActive));
      btn.classList.toggle("mode-toggle__btn--active", isActive);
    }
    document.dispatchEvent(new CustomEvent("modechange", { detail: { mode: mode } }));
  }

  function mountModeToggle(slotEl) {
    if (!slotEl) return;
    var wrap = document.createElement("div");
    wrap.className = "mode-toggle";
    wrap.setAttribute("role", "group");
    wrap.setAttribute("aria-label", "View mode");

    ["chat", "journey"].forEach(function (mode) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mode-toggle__btn";
      btn.setAttribute("data-mode", mode);
      btn.textContent = t("mode." + mode);
      var cur = getMode();
      btn.setAttribute("aria-pressed", String(cur === mode));
      if (cur === mode) btn.classList.add("mode-toggle__btn--active");
      btn.addEventListener("click", function () {
        setMode(mode);
      });
      wrap.appendChild(btn);
    });

    slotEl.appendChild(wrap);
  }

  // ── Export ───────────────────────────────────────────────────────────────

  window.i18n = {
    STRINGS: STRINGS,
    getLang: getLang,
    setLang: setLang,
    applyLang: applyLang,
    t: t,
    mountLangToggle: mountLangToggle,
    mountModeToggle: mountModeToggle,
    getMode: getMode,
    setMode: setMode,
  };
})();
