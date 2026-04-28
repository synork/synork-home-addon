/* Synork Home setup wizard — vanilla JS, no framework, no build step.
 *
 * Renders 4 main views:
 *   already_paired  — addon already has a device_secret on disk
 *   sign_in         — email/password → Synork mobile auth token
 *   household       — pick existing household or create new
 *   pairing         — in-flight self_install call
 *   done            — success state
 *
 * All HTTP calls are RELATIVE so they resolve correctly under HA's
 * `/api/hassio_ingress/<token>/...` proxy prefix. Don't prepend "/".
 *
 * Uses DOM construction (createElement / textContent) rather than innerHTML
 * to make XSS impossible by construction even if upstream payloads go bad.
 */

(() => {
  "use strict";

  /** @type {{strings: Record<string,string>, paired: boolean, device_id: string,
   *          household_id: string|null, household_name: string|null,
   *          signed_in: boolean, signed_in_user: any}} */
  let state = null;
  let households = [];
  /** Selected household_id, or "__new__" to indicate "create new". */
  let selectedHouseholdId = "__new__";
  let newHouseholdName = "";
  let deviceLocation = "";

  const root = document.getElementById("content");
  const stepIndicator = document.getElementById("step-indicator");
  const deviceIdTag = document.getElementById("device-id-tag");

  // ── Tiny DOM builder (safer than template strings) ─────────────────

  /**
   * Create an element with attributes and children.
   * Children may be strings (treated as textNode), elements, or null/false (skipped).
   */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "dataset") {
          for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
        } else if (k.startsWith("on") && typeof v === "function") {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          el.setAttribute(k, String(v));
        }
      }
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      if (typeof c === "string") el.appendChild(document.createTextNode(c));
      else el.appendChild(c);
    }
    return el;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(...children) { clear(root); for (const c of children) root.appendChild(c); }

  // ── Network helpers ────────────────────────────────────────────────

  async function api(method, path, body) {
    const init = { method, headers: { "Accept": "application/json" } };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(path, init);
    let data;
    try { data = await resp.json(); } catch { data = {}; }
    return { ok: resp.ok, status: resp.status, data };
  }

  // ── State plumbing ─────────────────────────────────────────────────

  async function loadState() {
    const { ok, data } = await api("GET", "api/wizard/state");
    if (!ok || !data.ok) {
      throw new Error("could not load wizard state");
    }
    state = data;
    return state;
  }

  function s(key) {
    return (state && state.strings && state.strings[key]) || key;
  }

  function setStepIndicator(step) {
    if (!stepIndicator) return;
    const order = ["sign_in", "household", "pairing", "done"];
    const idx = order.indexOf(step);
    clear(stepIndicator);
    if (idx === -1) return;
    order.forEach((_, i) => {
      const cls = i < idx ? "done" : i === idx ? "active" : "";
      stepIndicator.appendChild(h("span", cls ? { class: cls } : null));
    });
  }

  function setDeviceTag(id) {
    if (deviceIdTag && id) deviceIdTag.textContent = id;
  }

  // ── Reusable bits ──────────────────────────────────────────────────

  function spinner() { return h("span", { class: "spinner" }); }

  function errorBanner(text) {
    return h("div", { class: "error" }, text);
  }

  function field({ label, input }) {
    return h("div", { class: "field" }, h("label", null, label), input);
  }

  function summary(items) {
    const dl = h("dl", { class: "summary" });
    for (const [k, v] of items) {
      dl.appendChild(h("dt", null, k));
      dl.appendChild(h("dd", null, v || "—"));
    }
    return dl;
  }

  // ── Renderers ──────────────────────────────────────────────────────

  function renderLoading() {
    show(h("div", { class: "loading" }, spinner(), s("loading") || "Loading…"));
  }

  function renderError(msg) {
    show(h("div", { class: "step" },
      h("h1", null, s("error.unknown")),
      errorBanner(msg || ""),
      h("div", { class: "actions" },
        h("button", { id: "retry", onclick: boot }, "Retry"))));
  }

  function renderAlreadyPaired() {
    setStepIndicator("done");
    show(h("div", { class: "step" },
      h("h1", null, s("already_paired.title")),
      h("p", null, s("already_paired.body")),
      summary([
        [s("already_paired.household"), state.household_name || ""],
        [s("step.done.device"), state.device_id || ""],
      ])));
  }

  function renderSignIn(errorKey) {
    setStepIndicator("sign_in");

    const emailInput = h("input", {
      type: "email", id: "email", name: "email",
      autocomplete: "username", required: "required",
    });
    const passwordInput = h("input", {
      type: "password", id: "password", name: "password",
      autocomplete: "current-password", required: "required",
    });
    const submitBtn = h("button", { type: "submit", id: "signin-submit" }, s("step.signin.cta"));

    const form = h("form", {
      id: "signin-form", autocomplete: "on",
      onsubmit: async (e) => {
        e.preventDefault();
        await handleSignInSubmit(emailInput.value.trim(), passwordInput.value, submitBtn);
      },
    },
      field({ label: s("step.signin.email"), input: emailInput }),
      field({ label: s("step.signin.password"), input: passwordInput }),
      errorKey ? errorBanner(s(errorKey)) : null,
      h("div", { class: "actions" }, submitBtn),
    );

    show(h("div", { class: "step" },
      h("h1", null, s("step.signin.title")),
      h("p", null, s("step.signin.body")),
      form));
  }

  async function handleSignInSubmit(email, password, btn) {
    btn.disabled = true;
    clear(btn);
    btn.appendChild(spinner());
    btn.appendChild(document.createTextNode(s("step.signin.cta")));

    const { status, data } = await api("POST", "api/wizard/sign-in", { email, password });
    if (status === 200 && data.ok) {
      await loadState();
      await goToHouseholds();
      return;
    }
    if (status === 401 && data.error === "tfa_required") { renderSignIn("step.signin.error.tfa"); return; }
    if (status === 401) { renderSignIn("step.signin.error.invalid"); return; }
    renderSignIn("step.signin.error.network");
  }

  async function goToHouseholds() {
    setStepIndicator("household");
    show(h("div", { class: "loading" }, spinner(), "Loading households…"));
    const { ok, data } = await api("GET", "api/wizard/households");
    if (!ok || !data.ok) {
      if (data && data.error === "session_expired") { renderSignIn("step.signin.error.invalid"); return; }
      renderError(data && data.detail ? data.detail : "Could not load households.");
      return;
    }
    households = data.households || [];
    selectedHouseholdId = households.length > 0 ? households[0].household_id : "__new__";
    renderHouseholdPick();
  }

  function renderHouseholdPick(errorMsg) {
    setStepIndicator("household");

    const existingList = h("ul", { class: "household-list" });
    for (const hh of households) {
      const isSel = hh.household_id === selectedHouseholdId;
      const li = h("li", {
        class: isSel ? "selected" : "",
        dataset: { id: hh.household_id },
        onclick: () => { selectInto("__id__", hh.household_id); },
      },
        h("span", { class: "name" }, hh.name),
        h("span", { class: "role" }, hh.household_id),
      );
      existingList.appendChild(li);
    }

    const newRow = h("li", {
      class: selectedHouseholdId === "__new__" ? "selected" : "",
      dataset: { id: "__new__" },
      onclick: () => { selectInto("__id__", "__new__"); },
    },
      h("span", { class: "name" }, "+ " + s("step.household.create")),
    );
    const newWrapper = h("ul", { class: "household-list" }, newRow);

    const newNameInput = h("input", {
      type: "text", id: "new-household-name",
      placeholder: s("step.household.create.placeholder"),
      value: newHouseholdName,
      oninput: (e) => { newHouseholdName = e.target.value; },
    });
    const newFields = h("div", { id: "new-household-fields" },
      h("div", { class: "field" }, newNameInput));
    newFields.style.display = selectedHouseholdId === "__new__" ? "block" : "none";

    const locationInput = h("input", {
      type: "text", id: "device-location",
      placeholder: s("step.household.location.placeholder"),
      value: deviceLocation,
      oninput: (e) => { deviceLocation = e.target.value; },
    });

    const backBtn = h("button", {
      class: "secondary", id: "back-to-signin",
      onclick: async () => {
        await api("POST", "api/wizard/sign-out");
        await loadState();
        renderSignIn();
      },
    }, "←");
    const pairBtn = h("button", { id: "pair-submit", onclick: handlePairSubmit }, s("step.household.cta"));

    show(h("div", { class: "step" },
      h("h1", null, s("step.household.title")),
      h("p", null, s("step.household.body")),
      households.length > 0 ? existingList : null,
      newWrapper,
      newFields,
      field({ label: s("step.household.location.label"), input: locationInput }),
      errorMsg ? errorBanner(errorMsg) : null,
      h("div", { class: "actions" }, backBtn, pairBtn),
    ));
  }

  /**
   * Capture current input values and switch the selected household, then
   * re-render. The "__id__" first-arg is a marker for "switching the
   * highlighted row" — it exists so the call site reads naturally.
   */
  function selectInto(_marker, newId) {
    const nameEl = document.getElementById("new-household-name");
    if (nameEl) newHouseholdName = nameEl.value;
    const locEl = document.getElementById("device-location");
    if (locEl) deviceLocation = locEl.value;
    selectedHouseholdId = newId;
    renderHouseholdPick();
  }

  async function handlePairSubmit() {
    const nameEl = document.getElementById("new-household-name");
    if (nameEl) newHouseholdName = nameEl.value.trim();
    const locEl = document.getElementById("device-location");
    if (locEl) deviceLocation = locEl.value.trim();

    const isNew = selectedHouseholdId === "__new__";
    if (isNew && !newHouseholdName) {
      renderHouseholdPick("Please name the new household.");
      return;
    }

    setStepIndicator("pairing");
    show(h("div", { class: "step" },
      h("h1", null, s("step.pairing.title")),
      h("p", null, spinner(), s("step.pairing.body"))));

    const payload = {
      household_id: isNew ? null : selectedHouseholdId,
      household_name: isNew ? newHouseholdName : null,
      device_location: deviceLocation || null,
      device_label: null,
    };
    const { status, data } = await api("POST", "api/wizard/pair", payload);

    if (status === 200 && data.ok) {
      await loadState();
      renderDone(data);
      return;
    }
    if (status === 401) { renderSignIn("step.signin.error.invalid"); return; }
    if (status === 409) { await loadState(); renderAlreadyPaired(); return; }
    renderHouseholdPick(data && data.detail ? data.detail : s("error.unknown"));
  }

  function renderDone(payload) {
    setStepIndicator("done");
    show(h("div", { class: "step" },
      h("h1", null, s("step.done.title")),
      h("p", null, s("step.done.body")),
      summary([
        [s("step.done.household"), payload.household_name || ""],
        [s("step.done.device"), payload.device_id || ""],
      ])));
  }

  // ── Boot ───────────────────────────────────────────────────────────

  async function boot() {
    renderLoading();
    try {
      await loadState();
    } catch (err) {
      renderError(String(err && err.message ? err.message : err));
      return;
    }
    setDeviceTag(state.device_id);
    if (state.paired) { renderAlreadyPaired(); return; }
    if (state.signed_in) { await goToHouseholds(); return; }
    renderSignIn();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
