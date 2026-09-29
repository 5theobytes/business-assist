/**
 * journey.js — BizWare Journey Mode
 * Renders the conversation as a horizontal left-to-right path from Point A → Point B.
 * Uses inline inputs within step cards (no bottom composer).
 */
(function () {
  "use strict";

  // ── i18n helper ───────────────────────────────────────────────────────────
  function t(key) {
    return window.i18n ? window.i18n.t(key) : key;
  }

  // ── DOM refs ──────────────────────────────────────────────────────────────
  var viewport = document.getElementById("journey-viewport");
  var pathContainer = document.getElementById("journey-path");
  var pointA = document.getElementById("point-a");
  var pointB = document.getElementById("point-b");
  var pointBNode = document.getElementById("point-b-node");
  var phasesBar = document.getElementById("journey-phases");
  var statusEl = document.getElementById("status-text");
  var statusWrap = document.getElementById("journey-status");

  var sessionId = null;
  var isDone = false;
  var stepCount = 0;
  var PHASE_ORDER = ["point_a", "point_b", "resources", "analysis", "dev_coverage", "spec_review"];

  // ── Utilities ─────────────────────────────────────────────────────────────

  function setStatus(text, isError) {
    statusEl.textContent = text;
    statusWrap.classList.toggle("error", !!isError);
  }

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function scrollToEnd() {
    viewport.scrollLeft = viewport.scrollWidth;
  }

  function parseSidFromUrl() {
    var params = new URLSearchParams(window.location.search);
    return params.get("sid");
  }

  // ── Phase bar ─────────────────────────────────────────────────────────────

  function updatePhaseBar(phaseMarker) {
    if (!phaseMarker || !phasesBar) return;
    var phaseEls = phasesBar.querySelectorAll(".journey-phase");
    var activeIndex = phaseMarker === "done"
      ? PHASE_ORDER.length
      : PHASE_ORDER.indexOf(phaseMarker);
    phaseEls.forEach(function (el) {
      var phase = el.getAttribute("data-phase");
      var idx = PHASE_ORDER.indexOf(phase);
      el.classList.remove("journey-phase--active", "journey-phase--completed");
      if (idx === activeIndex) {
        el.classList.add("journey-phase--active");
      } else if (idx >= 0 && idx < activeIndex) {
        el.classList.add("journey-phase--completed");
      }
    });
    // Show Point B anchor when we've moved past point_a phase
    if (pointB) {
      var showB = activeIndex > 0; // any phase after point_a
      pointB.style.display = showB ? "" : "none";
    }
  }

  // ── Clarity scores ─────────────────────────────────────────────────────────

  function updateClarityScores(scores) {
    // No-op: clarity score bars removed from UI
  }

  // ── Payment CTA ───────────────────────────────────────────────────────────

  function showPaymentCta(url) {
    var existing = document.getElementById("journey-payment-cta");
    if (existing) return;
    var cta = document.createElement("a");
    cta.id = "journey-payment-cta";
    cta.href = url;
    cta.target = "_blank";
    cta.className = "journey-payment-cta";
    cta.textContent = t("chat.payment_cta");
    cta.title = t("chat.payment_cta_title");
    cta.style.display = "inline-block";
    cta.style.marginTop = "8px";
    cta.style.padding = "10px 20px";
    cta.style.borderRadius = "100px";
    cta.style.background = "var(--j-accent)";
    cta.style.color = "#fff";
    cta.style.fontFamily = "'Hind Madurai', sans-serif";
    cta.style.fontWeight = "600";
    cta.style.fontSize = "14px";
    cta.style.textDecoration = "none";
    cta.style.cursor = "pointer";
    statusWrap.appendChild(cta);
  }

  // ── Path rendering ────────────────────────────────────────────────────────

  function createConnector(isLong) {
    var el = document.createElement("div");
    el.className = "journey-connector" + (isLong ? " journey-connector--long" : "");
    el.setAttribute("aria-hidden", "true");
    return el;
  }

  function createStepNode(question, answer, suggestedOptions, isActive, extraOpts) {
    stepCount++;
    var step = document.createElement("div");
    step.className = "journey-step" + (isActive ? " journey-step--active" : "");
    step.setAttribute("role", "article");
    step.setAttribute("data-step-index", String(stepCount - 1));

    if (extraOpts && extraOpts.isCheckpoint) {
      step.classList.add("journey-step--checkpoint");
    }

    // Node circle
    var node = document.createElement("div");
    node.className = "journey-step__node" + (answer ? " journey-step__node--filled" : "");
    step.appendChild(node);

    // Label
    var label = document.createElement("span");
    label.className = "journey-step__label";
    label.textContent = t("journey.step") + " " + stepCount;
    step.appendChild(label);

    // Card
    var card = document.createElement("div");
    card.className = "journey-step__card";
    card.style.position = "relative";

    // Checkpoint badge
    if (extraOpts && extraOpts.isCheckpoint) {
      var badge = document.createElement("span");
      badge.className = "journey-step__checkpoint-badge";
      badge.textContent = t("journey.checkpoint");
      card.appendChild(badge);
    }

    // Question
    if (question) {
      var qEl = document.createElement("div");
      qEl.className = "journey-step__question";
      qEl.textContent = question;
      card.appendChild(qEl);
    }

    // Answer (shown for completed steps)
    if (answer) {
      var aEl = document.createElement("div");
      aEl.className = "journey-step__answer";
      aEl.textContent = answer;
      card.appendChild(aEl);
    }

    // Suggested options (only on active step)
    if (isActive && suggestedOptions && suggestedOptions.length) {
      var optsWrap = document.createElement("div");
      optsWrap.className = "journey-step__options";
      suggestedOptions.forEach(function (optText) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "journey-step__option";
        btn.textContent = optText;
        btn.addEventListener("click", function () {
          handleOptionClick(optText);
        });
        optsWrap.appendChild(btn);
      });
      card.appendChild(optsWrap);
    }

    // Analysis options (only on active step)
    if (isActive && extraOpts && extraOpts.analysisOptions && extraOpts.analysisOptions.length) {
      var analysisWrap = document.createElement("div");
      analysisWrap.className = "journey-step__options";
      extraOpts.analysisOptions.forEach(function (opt) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "journey-step__option";
        var labelText = opt.shape || t("chat.analysis.fallback").replace("{index}", opt.index);
        btn.textContent = t("chat.analysis.variant").replace("{index}", opt.index).replace("{label}", labelText);
        btn.addEventListener("click", function () {
          handleAnalysisOptionClick(opt.index);
        });
        analysisWrap.appendChild(btn);
      });
      card.appendChild(analysisWrap);
    }

    // Inline custom input (only on active step when allowed)
    if (isActive && extraOpts && extraOpts.allowCustomInput) {
      var inputWrap = document.createElement("div");
      inputWrap.className = "journey-step__inline-input";
      inputWrap.style.display = "flex";
      inputWrap.style.gap = "6px";
      inputWrap.style.marginTop = "8px";

      var input = document.createElement("input");
      input.type = "text";
      input.className = "journey-step__inline-text";
      input.placeholder = t("journey.placeholder");
      input.style.flex = "1";
      input.style.padding = "6px 10px";
      input.style.borderRadius = "8px";
      input.style.border = "1px solid var(--j-border)";
      input.style.fontFamily = "'Inter', sans-serif";
      input.style.fontSize = "13px";
      input.style.outline = "none";
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          var text = input.value.trim();
          if (text) handleCustomInput(text);
        }
      });
      inputWrap.appendChild(input);

      var sendBtn = document.createElement("button");
      sendBtn.type = "button";
      sendBtn.className = "journey-step__inline-send";
      sendBtn.innerHTML = "&#8594;"; // right arrow
      sendBtn.title = t("journey.send");
      sendBtn.style.appearance = "none";
      sendBtn.style.webkitAppearance = "none";
      sendBtn.style.border = "none";
      sendBtn.style.borderRadius = "8px";
      sendBtn.style.background = "var(--j-accent)";
      sendBtn.style.color = "#fff";
      sendBtn.style.width = "32px";
      sendBtn.style.height = "32px";
      sendBtn.style.cursor = "pointer";
      sendBtn.style.fontSize = "16px";
      sendBtn.style.display = "flex";
      sendBtn.style.alignItems = "center";
      sendBtn.style.justifyContent = "center";
      sendBtn.addEventListener("click", function () {
        var text = input.value.trim();
        if (text) handleCustomInput(text);
      });
      inputWrap.appendChild(sendBtn);

      card.appendChild(inputWrap);
    }

    // Undo button (only on completed steps, shown on hover via CSS)
    if (!isActive && answer) {
      var undoBtn = document.createElement("button");
      undoBtn.type = "button";
      undoBtn.className = "journey-step__undo";
      undoBtn.innerHTML = "&#8617;"; // ↩
      undoBtn.title = "Undo";
      undoBtn.setAttribute("aria-label", "Undo this step");
      undoBtn.style.position = "absolute";
      undoBtn.style.top = "6px";
      undoBtn.style.right = "6px";
      undoBtn.style.appearance = "none";
      undoBtn.style.webkitAppearance = "none";
      undoBtn.style.border = "none";
      undoBtn.style.background = "transparent";
      undoBtn.style.color = "var(--j-text-muted)";
      undoBtn.style.cursor = "pointer";
      undoBtn.style.fontSize = "14px";
      undoBtn.style.padding = "2px 4px";
      undoBtn.style.opacity = "0";
      undoBtn.style.transition = "opacity 0.15s";
      undoBtn.addEventListener("click", function () {
        handleUndo(stepCount - 1);
      });
      card.appendChild(undoBtn);

      // Show undo on card hover
      card.addEventListener("mouseenter", function () { undoBtn.style.opacity = "1"; });
      card.addEventListener("mouseleave", function () { undoBtn.style.opacity = "0"; });
    }

    // Loading state overlay
    if (isActive && extraOpts && extraOpts.isLoading) {
      var loadingOverlay = document.createElement("div");
      loadingOverlay.className = "journey-step__loading";
      loadingOverlay.textContent = t("chat.status.thinking");
      loadingOverlay.style.position = "absolute";
      loadingOverlay.style.inset = "0";
      loadingOverlay.style.background = "rgba(255,255,255,0.85)";
      loadingOverlay.style.display = "flex";
      loadingOverlay.style.alignItems = "center";
      loadingOverlay.style.justifyContent = "center";
      loadingOverlay.style.borderRadius = "12px";
      loadingOverlay.style.fontSize = "13px";
      loadingOverlay.style.color = "var(--j-text-muted)";
      loadingOverlay.style.zIndex = "2";
      card.appendChild(loadingOverlay);
    }

    step.appendChild(card);
    return step;
  }

  function insertStepBeforeB(stepEl) {
    pathContainer.insertBefore(createConnector(false), pointB);
    pathContainer.insertBefore(stepEl, pointB);
    scrollToEnd();
  }

  // ── Interaction handlers ──────────────────────────────────────────────────

  function disableActiveCardInputs() {
    var activeStep = pathContainer.querySelector(".journey-step--active");
    if (!activeStep) return;
    var buttons = activeStep.querySelectorAll("button");
    buttons.forEach(function (btn) { btn.disabled = true; });
    var input = activeStep.querySelector("input[type='text']");
    if (input) { input.disabled = true; }
  }

  function showAnswerInActiveCard(text) {
    var activeStep = pathContainer.querySelector(".journey-step--active");
    if (!activeStep) return;
    var card = activeStep.querySelector(".journey-step__card");
    if (!card) return;

    // Remove inline input and options from the active card
    var optsWrap = card.querySelector(".journey-step__options");
    if (optsWrap) card.removeChild(optsWrap);
    var inputWrap = card.querySelector(".journey-step__inline-input");
    if (inputWrap) card.removeChild(inputWrap);
    var loading = card.querySelector(".journey-step__loading");
    if (loading) card.removeChild(loading);

    // Add answer element
    var aEl = document.createElement("div");
    aEl.className = "journey-step__answer";
    aEl.textContent = text;
    card.appendChild(aEl);

    // Mark as completed
    activeStep.classList.remove("journey-step--active");
    var node = activeStep.querySelector(".journey-step__node");
    if (node) node.classList.add("journey-step__node--filled");
  }

  function handleOptionClick(text) {
    if (!sessionId || isDone) return;
    disableActiveCardInputs();
    showAnswerInActiveCard(text);
    sendMessage(text);
  }

  function handleAnalysisOptionClick(index) {
    if (!sessionId || isDone) return;
    var msg = t("chat.analysis.choose").replace("{index}", index);
    disableActiveCardInputs();
    showAnswerInActiveCard(msg);
    sendMessage(msg);
  }

  function handleCustomInput(text) {
    if (!sessionId || isDone) return;
    disableActiveCardInputs();
    showAnswerInActiveCard(text);
    sendMessage(text);
  }

  function handleUndo(stepIndex) {
    if (!sessionId || isDone) return;
    setStatus(t("chat.status.thinking"));

    fetch("/api/chat/undo", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, target_step: stepIndex }),
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data.success) {
          setStatus(t("chat.error.server_status"), true);
          return;
        }
        // Remove all steps at or after the target step, plus their preceding connectors
        var steps = pathContainer.querySelectorAll(".journey-step");
        steps.forEach(function (step) {
          var idx = parseInt(step.getAttribute("data-step-index"), 10);
          if (idx >= stepIndex) {
            var prev = step.previousElementSibling;
            if (prev && prev.classList.contains("journey-connector")) {
              pathContainer.removeChild(prev);
            }
            pathContainer.removeChild(step);
          }
        });
        stepCount = stepIndex;

        // Re-create the target step as active with returned data
        var extraOpts = {
          isCheckpoint: data.is_checkpoint,
          allowCustomInput: data.allow_custom_input,
          suggestedOptions: data.suggested_options,
          analysisOptions: data.analysis_options,
        };
        var stepEl = createStepNode(data.reply, null, data.suggested_options, true, extraOpts);
        insertStepBeforeB(stepEl);

        updatePhaseBar(data.phase_marker);

        if (data.done) {
          markDone();
          setStatus(t("chat.status.done"));
        } else {
          setStatus(t("chat.status.ready"));
        }
      })
      .catch(function (err) {
        setStatus(t("chat.error.network_status"), true);
      });
  }

  // ── API calls ─────────────────────────────────────────────────────────────

  function sendMessage(text) {
    if (!sessionId || isDone) return;
    setStatus(t("chat.status.thinking"));

    fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          setStatus(t("chat.error.server_status"), true);
          return;
        }
        var data = result.data;

        // Create new step for the bot reply
        var isActive = !data.done;
        var extraOpts = {
          isCheckpoint: data.is_checkpoint,
          allowCustomInput: data.allow_custom_input,
          analysisOptions: data.analysis_options,
        };
        var stepEl = createStepNode(data.reply, null, data.suggested_options, isActive, extraOpts);
        insertStepBeforeB(stepEl);

        // Update phase bar and clarity scores
        updatePhaseBar(data.phase_marker);

        if (data.done) {
          markDone();
          setStatus(t("chat.status.done"));
          if (data.payment_cta_url) {
            showPaymentCta(data.payment_cta_url);
          }
        } else {
          setStatus(t("chat.status.ready"));
        }
      })
      .catch(function (err) {
        setStatus(t("chat.error.network_status"), true);
      });
  }

  function loadSession(sid) {
    setStatus(t("chat.status.opening"));

    fetch("/api/session/" + encodeURIComponent(sid))
      .then(function (res) {
        if (res.status === 404) {
          window.location.href = "/intake";
          return null;
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data) return;
        sessionId = data.session_id;

        // Apply language
        if (window.i18n) {
          var lang = data.language || window.i18n.getLang();
          localStorage.setItem("bizware.lang", lang);
          window.i18n.applyLang(lang);
        }

        // Clear existing steps (keep pointA and pointB)
        var children = Array.prototype.slice.call(pathContainer.children);
        children.forEach(function (child) {
          if (child !== pointA && child !== pointB) {
            pathContainer.removeChild(child);
          }
        });
        stepCount = 0;
        pathContainer.appendChild(pointB);
        pathContainer.insertBefore(createConnector(true), pointB);

        // Hydrate transcript
        if (data.transcript && data.transcript.length) {
          var pairs = [];
          for (var j = 0; j < data.transcript.length; j++) {
            if (data.transcript[j].role === "assistant") {
              var question = data.transcript[j].content;
              var answer = null;
              if (j + 1 < data.transcript.length && data.transcript[j + 1].role === "user") {
                answer = data.transcript[j + 1].content;
                j++;
              }
              pairs.push({ question: question, answer: answer });
            }
          }

          pairs.forEach(function (pair, idx) {
            var stepEl = createStepNode(pair.question, pair.answer, null, false);
            insertStepBeforeB(stepEl);
          });

          // Last assistant message without answer becomes active step
          var lastPair = pairs[pairs.length - 1];
          if (lastPair && !lastPair.answer && !data.done) {
            // Remove the last created step (which was non-active) and recreate as active
            var steps = pathContainer.querySelectorAll(".journey-step");
            var lastStep = steps[steps.length - 1];
            if (lastStep) {
              var prevConn = lastStep.previousElementSibling;
              if (prevConn && prevConn.classList.contains("journey-connector")) {
                pathContainer.removeChild(prevConn);
              }
              pathContainer.removeChild(lastStep);
              stepCount--;
            }
            var activeExtra = {
              allowCustomInput: true,
              suggestedOptions: data.last_suggested_options,
            };
            var activeStep = createStepNode(lastPair.question, null, data.last_suggested_options, true, activeExtra);
            insertStepBeforeB(activeStep);
          }
        }

        updatePhaseBar(data.phase_marker);

        if (data.done) {
          markDone();
          setStatus(t("chat.status.done"));
          if (data.payment_cta_url) {
            showPaymentCta(data.payment_cta_url);
          }
        } else {
          setStatus(t("chat.status.ready"));
        }
      })
      .catch(function (err) {
        setStatus(t("chat.error.connect") + err.message, true);
      });
  }

  function markDone() {
    isDone = true;
    if (pointBNode) {
      pointBNode.classList.add("journey-anchor__node--completed");
    }
    // Disable any remaining active inputs
    var activeStep = pathContainer.querySelector(".journey-step--active");
    if (activeStep) {
      activeStep.classList.remove("journey-step--active");
      var node = activeStep.querySelector(".journey-step__node");
      if (node) node.classList.add("journey-step__node--filled");
      var buttons = activeStep.querySelectorAll("button");
      buttons.forEach(function (btn) { btn.disabled = true; });
      var input = activeStep.querySelector("input[type='text']");
      if (input) { input.disabled = true; }
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  function init() {
    // Apply language
    if (window.i18n) {
      window.i18n.applyLang(window.i18n.getLang());
    }

    // Mount toggles
    var modeSlot = document.getElementById("mode-toggle-slot");
    if (window.i18n && window.i18n.mountModeToggle) {
      window.i18n.mountModeToggle(modeSlot);
    }
    var langSlot = document.getElementById("lang-toggle-slot");
    if (window.i18n && window.i18n.mountLangToggle) {
      window.i18n.mountLangToggle(langSlot);
    }

    // Parse session
    var sid = parseSidFromUrl();
    if (!sid) {
      window.location.href = "/intake";
      return;
    }

    loadSession(sid);

    // Mode change listener → redirect to chat
    document.addEventListener("modechange", function (e) {
      if (e.detail && e.detail.mode === "chat") {
        var currentSid = parseSidFromUrl();
        window.location.href = "/chat?sid=" + encodeURIComponent(currentSid || "");
      }
    });
  }

  init();
})();
