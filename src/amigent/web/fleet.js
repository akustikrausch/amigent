// Amigent fleet: one tile per Amiga, a click opens its remote desktop.
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

  function tile(a) {
    let el = tiles.get(a.id);
    if (!el) {
      el = document.createElement("button");
      el.className = "tile";
      el.innerHTML = `<div class="shot"><img alt=""></div>
        <div class="meta"><b></b><span class="state"><i></i><em></em></span><small></small></div>`;
      el.addEventListener("click", async () => {
        const reply = await fetch(`/api/open/${a.id}?t=${encodeURIComponent(token)}`);
        if (reply.ok) window.open((await reply.json()).url, `amiga-${a.id}`, "popup,width=1440,height=900");
      });
      $("tiles").append(el);
      tiles.set(a.id, el);
    }
    el.querySelector("b").textContent = a.name;
    const state = el.querySelector(".state");
    state.className = "state " + (a.online ? "ok" : a.online === false ? "bad" : "");
    state.querySelector("em").textContent = a.online ? a.host : a.online === false ? t("fleet_offline") : t("connecting");
    el.querySelector("small").textContent = a.online
      ? [a.os, a.cpu, t("status_free", { size: bytes(a.free) })].filter(Boolean).join(" · ")
      : a.host;
    el.title = t("fleet_open", { name: a.name });
    el.setAttribute("aria-label", el.title);
    if (a.online && a.seen) el.querySelector("img").src = `/api/preview/${a.id}?t=${encodeURIComponent(token)}&s=${a.seen}`;
    el.classList.toggle("offline", a.online === false);
  }

  async function refresh() {
    try {
      const list = await (await fetch(`/api/fleet?t=${encodeURIComponent(token)}`)).json();
      list.forEach(tile);
      const up = list.filter((a) => a.online).length;
      $("summary").textContent = t("fleet_summary", { up, total: list.length });
    } catch { /* the fleet process went away; keep the last view */ }
  }

  (async () => {
    const load = (code) => fetch(`locales/${code}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    EN = await load("en");
    TEXT = language === "en" ? EN : await load(language);
    document.documentElement.lang = language;
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    refresh();
    setInterval(refresh, 5000);
  })();
})();
