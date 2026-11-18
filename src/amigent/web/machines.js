// Shared machine picker and explicit registry management, in every viewer.
(async () => {
  "use strict";
  const token = new URLSearchParams(location.search).get("t") || "";
  const api = (path) => `${path}?t=${encodeURIComponent(token)}`;
  const load = (lang) => fetch(`locales/${lang}.json`).then(r => r.ok ? r.json() : {}).catch(() => ({}));
  const en = await load("en");
  let lang = "auto";
  try { lang = localStorage.getItem("amigent.lang") || "auto"; } catch { /* optional */ }
  if (lang === "auto") lang = (navigator.language || "en").split("-")[0];
  const messages = lang === "en" ? en : await load(lang);
  const t = (key) => messages[key] || en[key] || key;
  let snapshot = { machines: [], current: "", overview: "" }, editing = null;
  const switcher = document.createElement("div");
  switcher.className = "machine-switcher";
  switcher.innerHTML = '<select aria-label=""></select><button type="button"></button>';
  const select = switcher.querySelector("select"), manage = switcher.querySelector("button");
  select.setAttribute("aria-label", t("machines_select"));
  manage.textContent = t("machines_manage");
  document.getElementById("bar").append(switcher);

  const dialog = document.createElement("dialog");
  dialog.className = "machine-dialog";
  dialog.setAttribute("aria-labelledby", "machines-heading");
  dialog.innerHTML = `<header><h2 id="machines-heading"></h2><button class="close" type="button"></button></header>
    <p class="machine-error" role="alert" hidden></p>
    <section class="machine-list"></section><p class="empty" hidden></p>
    <form class="machine-connect"><label for="machine-target"></label><div><input id="machine-target" autocomplete="off" required><button type="submit"></button></div><small></small></form>
    <form class="machine-editor" hidden><h3></h3><label for="machine-value"></label><input id="machine-value" autocomplete="off"><p></p><div><button class="cancel" type="button"></button><button class="save primary" type="submit"></button></div></form>`;
  document.body.append(dialog);
  const list = dialog.querySelector(".machine-list"), form = dialog.querySelector(".machine-editor");
  const input = form.querySelector("input"), error = dialog.querySelector(".machine-error");
  const direct = dialog.querySelector(".machine-connect"), tiles = new Map();
  dialog.querySelector("h2").textContent = t("machines_title");
  dialog.querySelector(".close").textContent = t("machines_close");
  dialog.querySelector(".empty").textContent = t("machines_empty");
  direct.querySelector("label").textContent = t("machines_connect_to");
  direct.querySelector("button").textContent = t("machines_connect");
  direct.querySelector("small").textContent = t("machines_one_off");
  form.querySelector(".cancel").textContent = t("machines_cancel");
  const showError = (message) => { error.textContent = message; error.hidden = !message; };
  const state = (a) => a.conflicts.length ? t("machines_conflict") : a.online === null
    ? t("machines_checking") : a.online ? t("machines_online") : t("machines_offline");

  async function post(path, body) {
    const response = await fetch(api(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || response.statusText);
    return value;
  }
  async function connect(target) {
    const result = await post("/api/connect", { target });
    location.assign(result.url);
  }
  function edit(a, action) {
    editing = { id: a.id, action };
    list.hidden = direct.hidden = true;
    form.hidden = false;
    showError("");
    const label = t(`machines_${action}`);
    form.querySelector("h3").textContent = `${label}: ${a.name}`;
    form.querySelector("label").textContent = label;
    input.hidden = action === "remove";
    input.required = action !== "remove";
    input.value = action === "rename" ? a.name : a.host;
    input.maxLength = action === "rename" ? 80 : 253;
    form.querySelector("label").hidden = input.hidden;
    form.querySelector("p").textContent = t(action === "remove" ? "machines_remove_hint" : action === "address" ? "machines_address_hint" : "machines_rename_hint");
    form.querySelector(".save").textContent = t(action === "remove" ? "machines_remove" : "machines_save");
    (input.hidden ? form.querySelector(".cancel") : input).focus();
    if (!input.hidden) input.select();
  }
  function cancelEdit() {
    editing = null;
    form.hidden = true;
    list.hidden = direct.hidden = false;
    showError("");
  }
  async function refresh() {
    const response = await fetch(api("/api/machines"));
    if (!response.ok) throw new Error(t("machines_unavailable"));
    snapshot = await response.json();
    const options = snapshot.machines.map(a => ({ id: a.id, label: `${a.name}${a.default ? " ★" : ""} — ${a.host} — ${state(a)}`, conflict: a.conflicts.length > 0 }));
    if (snapshot.current && !options.some(o => o.id === snapshot.current)) {
      options.push({ id: snapshot.current, label: snapshot.current });
    }
    const signature = JSON.stringify([options, snapshot.current]);
    if (select.dataset.options !== signature) {
      select.replaceChildren(new Option(t("machines_title"), ""));
      for (const o of options) {
        const option = new Option(o.label, o.id);
        option.disabled = o.conflict;
        select.append(option);
      }
      select.value = snapshot.current;
      select.dataset.options = signature;
    }
    const current = snapshot.machines.find(a => a.id === snapshot.current);
    if (current) {
      document.title = `Amigent · ${current.name}`;
      document.querySelector("#bar .title b").textContent = current.name;
    }
    const live = new Set();
    for (const a of snapshot.machines) {
      live.add(a.id);
      let row = tiles.get(a.id);
      if (!row) {
        row = document.createElement("article");
        row.className = "machine-row";
        row.innerHTML = '<div><strong></strong><span class="address"></span><span class="connection"></span><p class="conflict" hidden></p></div><div class="machine-actions"></div>';
        for (const action of ["connect", "rename", "default", "address", "remove"]) {
          const button = document.createElement("button");
          button.type = "button";
          button.dataset.action = action;
          button.textContent = t(`machines_${action}`);
          button.addEventListener("click", async () => {
            try {
              if (action === "connect") await connect(row.amiga.id);
              else if (action === "default") { await post("/api/machines", { id: row.amiga.id, action }); await refresh(); }
              else edit(row.amiga, action);
            } catch (e) { showError(e.message); }
          });
          row.querySelector(".machine-actions").append(button);
        }
        tiles.set(a.id, row);
        list.append(row);
      }
      row.amiga = a;
      row.querySelector("strong").textContent = a.name + (a.default ? ` · ${t("machines_is_default")}` : "");
      row.querySelector(".address").textContent = a.host;
      row.querySelector(".connection").textContent = state(a);
      row.querySelector(".connection").className = "connection " + (a.online ? "online" : "offline");
      row.querySelector(".conflict").hidden = !a.conflicts.length;
      row.querySelector(".conflict").textContent = a.error;
      row.querySelector('[data-action="connect"]').disabled = a.conflicts.length > 0;
      row.querySelector('[data-action="default"]').disabled = a.default;
    }
    for (const [id, row] of tiles) if (!live.has(id)) { row.remove(); tiles.delete(id); }
    dialog.querySelector(".empty").hidden = snapshot.machines.length > 0 || !!editing;
  }
  async function open(id) {
    cancelEdit();
    if (!dialog.open) dialog.showModal();
    try { await refresh(); } catch (e) { showError(e.message); }
    if (id && tiles.has(id)) tiles.get(id).querySelector("button").focus();
  }
  window.amigentMachines = { open };
  manage.addEventListener("click", () => open());
  dialog.querySelector(".close").addEventListener("click", () => dialog.close());
  form.querySelector(".cancel").addEventListener("click", cancelEdit);
  select.addEventListener("change", async () => {
    try {
      if (!select.value) location.assign(snapshot.overview);
      else await connect(select.value);
    } catch (e) { await open(); showError(e.message); }
  });
  direct.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await connect(direct.querySelector("input").value); } catch (e) { showError(e.message); }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!editing) return;
    const change = { ...editing };
    if (change.action === "rename") change.name = input.value;
    if (change.action === "address") change.address = input.value;
    const save = form.querySelector(".save");
    save.disabled = true;
    try {
      await post("/api/machines", change);
      cancelEdit();
      if (change.id === snapshot.current && change.action === "remove") location.assign(snapshot.overview);
      else if (change.id === snapshot.current && change.action === "address") await connect(change.id);
      else await refresh();
    } catch (e) { showError(e.message); } finally { save.disabled = false; }
  });
  try { await refresh(); } catch { select.append(new Option(t("machines_unavailable"), "")); }
  setInterval(() => refresh().catch(() => { if (dialog.open) showError(t("machines_unavailable")); }), 5000);
})();
