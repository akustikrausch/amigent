// Amigent fleet: one tile per Amiga, one click to its remote desktop,
// one command line to drive the whole party.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("t") || "";
  const LANGUAGES = ["en", "de", "pl", "fr", "es", "it", "fi", "sv", "da", "cs", "sk", "hu"];
  let EN = {}, TEXT = {};

  function pick() {
    let chosen = "auto";
    try { chosen = localStorage.getItem("amigent.lang") || "auto"; } catch { /* private window */ }
    if (LANGUAGES.includes(chosen)) return chosen;
    for (const tag of navigator.languages || [navigator.language || "en"]) {
      const code = tag.toLowerCase().split("-")[0];
      if (LANGUAGES.includes(code)) return code;
    }
    return "en";
  }

  const language = pick();
  const t = (key, values = {}) => {
    let text = TEXT[key] ?? EN[key] ?? key;
    for (const [k, v] of Object.entries(values)) text = text.replaceAll(`{${k}}`, v);
    return text;
  };

  function bytes(n) {
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
    if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(0)} MB`;
    return `${Math.round(n / 1024)} KB`;
  }

  const tiles = new Map();
  const api = (path) => `${path}?t=${encodeURIComponent(token)}`;

  // the specs behind a tile, folded away until someone asks
  function details(a) {
    const i = a.info || {};
    const rows = [
      [t("about_system"), [i.os, i.kickstart && `Kickstart ${i.kickstart}`, i.workbench && `Workbench ${i.workbench}`].filter(Boolean).join(", ")],
      [t("about_cpu"), [i.cpu, i.fpu, i.ppc].filter(Boolean).join(" + ")],
      [t("about_memory"), i.chip_total ? `Chip ${bytes(i.chip_free)} / ${bytes(i.chip_total)}, Fast ${bytes(i.fast_free)} / ${bytes(i.fast_total)}` : ""],
      [t("about_network"), i.tcp ? `bsdsocket.library ${i.tcp}` : ""],
      ...(i.volumes || []).map((v) => [v.name + ":", `${bytes(v.free)} / ${bytes(v.size)}`]),
    ].filter(([, v]) => v);
    const dl = document.createElement("dl");
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.append(dt, dd);
    }
    return dl;
  }

  function tile(a) {
    let el = tiles.get(a.id);
    if (!el) {
      el = document.createElement("article");
      el.className = "tile";
      el.innerHTML = `<button class="open"><div class="shot"><img alt=""></div>
        <div class="meta"><b></b><span class="state"><i></i><em></em></span><small></small></div></button>
        <div class="tools"><button class="more" aria-expanded="false"></button><button class="reboot"></button></div>`;
      el.querySelector(".open").addEventListener("click", async () => {
        const reply = await fetch(api(`/api/open/${a.id}`));
        if (reply.ok) window.open((await reply.json()).url, `amiga-${a.id}`, "popup,width=1440,height=900");
      });
      el.querySelector(".more").addEventListener("click", (event) => {
        const open = event.currentTarget.getAttribute("aria-expanded") !== "true";
        event.currentTarget.setAttribute("aria-expanded", String(open));
        el.querySelector("dl")?.remove();
        if (open) el.append(details(el.amiga));
      });
      el.querySelector(".reboot").addEventListener("click", async () => {
        if (!confirm(t("fleet_restart_confirm", { name: el.amiga.name }))) return;
        const reply = await fetch(api(`/api/restart/${a.id}`), { method: "POST", body: "{}" });
        const data = reply.ok ? await reply.json() : { ok: false, error: reply.statusText };
        if (!data.ok) alert(`${el.amiga.name}: ${data.error}`);
      });
      $("tiles").append(el);
      tiles.set(a.id, el);
    }
    el.amiga = a;
    el.querySelector("b").textContent = a.name;
    const state = el.querySelector(".state");
    state.className = "state " + (a.online ? "ok" : a.online === false ? "bad" : "");
    state.querySelector("em").textContent = a.online ? a.host : a.online === false ? t("fleet_offline") : t("connecting");
    el.querySelector("small").textContent = a.online
      ? [a.os, a.cpu, t("status_free", { size: bytes(a.free) })].filter(Boolean).join(" · ")
      : a.host;
    const open = el.querySelector(".open");
    open.title = t("fleet_open", { name: a.name });
    open.setAttribute("aria-label", open.title);
    el.querySelector(".more").textContent = t("fleet_details");
    el.querySelector(".reboot").textContent = t("fleet_restart");
    el.querySelector(".reboot").disabled = !a.online;
    if (el.querySelector(".more").getAttribute("aria-expanded") === "true") {
      el.querySelector("dl")?.remove();
      el.append(details(a));
    }
    if (a.online && a.seen) el.querySelector("img").src = api(`/api/preview/${a.id}`) + `&s=${a.seen}`;
    el.classList.toggle("offline", a.online === false);
  }

  // one line for the whole fleet; answers stack up per Amiga, rc > 5 glows red
  $("run").addEventListener("submit", async (event) => {
    event.preventDefault();
    const command = $("command").value.trim();
    if (!command) return;
    const go = $("run-go");
    go.disabled = true;
    $("results").hidden = false;
    $("results").textContent = t("fleet_running", { command });
    try {
      const reply = await fetch(api("/api/run"), { method: "POST", body: JSON.stringify({ command }) });
      const list = reply.ok ? await reply.json() : [];
      $("results").textContent = "";
      for (const r of list) {
        const art = document.createElement("article");
        art.className = r.ok && r.rc <= 5 ? "" : "bad";
        const head = document.createElement("header");
        const name = document.createElement("b"); name.textContent = r.name;
        const rc = document.createElement("span"); rc.className = "rc";
        rc.textContent = r.ok ? `rc ${r.rc}` : r.error;
        head.append(name, rc);
        art.append(head);
        if (r.ok && r.output) {
          const pre = document.createElement("pre"); pre.textContent = r.output;
          art.append(pre);
        }
        $("results").append(art);
      }
    } finally {
      go.disabled = false;
    }
  });

  async function refresh() {
    try {
      const list = await (await fetch(`/api/fleet?t=${encodeURIComponent(token)}`)).json();
      list.forEach(tile);
      const up = list.filter((a) => a.online).length;
      $("summary").textContent = t("fleet_summary", { up, total: list.length });
    } catch { /* fleet process gone; freeze the last frame */ }
  }

  (async () => {
    const load = (code) => fetch(`locales/${code}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    EN = await load("en");
    TEXT = language === "en" ? EN : await load(language);
    document.documentElement.lang = language;
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    for (const el of document.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = t(el.dataset.i18nPlaceholder);
    refresh();
    setInterval(refresh, 5000);
  })();
})();
