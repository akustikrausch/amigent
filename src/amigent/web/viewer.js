// Amigent viewer: draws the Amiga's tiles, sends mouse and keys back.
(() => {
  "use strict";

  const UI = window.amigentUI;
  const { store, t, MAC } = UI;
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("t") || "";
  const canvas = $("screen");
  const ctx = canvas.getContext("2d", { alpha: false });
  const stage = $("stage");
  const toast = UI.toast;

  // ---------------------------------------------------------------- text

  function applyLanguage() {
    UI.translate();
    for (const el of document.querySelectorAll("[data-mod]")) {
      const name = t(el.dataset.name);
      el.setAttribute("aria-label", name);
      el.title = `${name}. ${t("mod_tip")}`;
    }
    // "Right Amiga+O on the Amiga": what a panel button presses
    for (const el of document.querySelectorAll("[data-combo]")) {
      const keys = el.dataset.combo.split("+").map((part) => (
        { lamiga: t("lamiga"), ramiga: t("ramiga"), ctrl: t("key_ctrl") }[part] ?? part.toUpperCase()));
      el.title = t("combo_tip", { key: keys.join("+") });
    }
    for (const el of document.querySelectorAll("kbd[data-ctrl]")) el.textContent = `${t("key_ctrl")}-${el.dataset.ctrl}`;
    for (const el of document.querySelectorAll('[data-combo^="ramiga+"] kbd')) {
      el.insertAdjacentHTML("afterbegin", '<svg class="a" aria-hidden="true"><use href="#a-outline"/></svg>');
    }
    const select = $("language");
    select.replaceChildren(new Option(t("language_auto"), "auto"));
    for (const [code, name] of Object.entries(UI.LANGUAGES)) select.append(new Option(name, code));
    select.value = store.get("lang", "auto");
    const menu = $("lang-menu");
    menu.replaceChildren();
    for (const [code, name] of [["auto", t("language_auto")], ...Object.entries(UI.LANGUAGES)]) {
      const item = document.createElement("button");
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", String(select.value === code));
      item.textContent = name;
      if (code !== "auto") item.lang = code;
      item.addEventListener("click", () => setLanguage(code));
      menu.append(item);
    }
    $("m-lang-now").textContent = select.value === "auto" ? t("language_auto") : UI.LANGUAGES[select.value];
    paintTheme();
  }

  function setLanguage(code) {
    store.set("lang", code);
    location.reload();
  }

  function paintTheme() {
    const now = UI.theme();
    $("theme").value = now;
    for (const item of document.querySelectorAll("[data-theme-value]")) {
      item.setAttribute("aria-checked", String(item.dataset.themeValue === now));
    }
    $("m-theme-now").textContent = t(`theme_${now}`);
  }

  function setTheme(value) {
    UI.setTheme(value);
    paintTheme();
  }

  // ---------------------------------------------------------------- update

  // A newer Amigent: a banner until answered; Later keeps it in the More menu and in About.
  let offer = null;
  let bannerShown = "";
  let version = "";

  function showUpdate(info) {
    if (!info || !info.version) return;
    offer = info;
    const title = t("update_available", { version: info.version, current: info.current });
    $("update-title").textContent = title;
    $("update-page").href = info.page;
    $("about-update-text").textContent = title;
    $("about-update-page").href = info.page;
    const list = $("about-update-notes");
    list.replaceChildren();
    for (const line of (info.notes || "").split("\n").filter(Boolean).slice(0, 5)) {
      const item = document.createElement("li");
      item.textContent = line.replace(/^- /, "");
      list.append(item);
    }
    if (bannerShown !== info.version) {
      bannerShown = info.version;
      $("update").hidden = false;
    }
    paintUpdate();
  }

  function paintUpdate() {
    const on = !!offer;
    $("m-update").hidden = $("m-update-sep").hidden = !on;
    $("more-dot").hidden = !on;
    $("about-update").hidden = !on;
    $("about-update-notes").hidden = !on;
    if (on) $("m-update-text").textContent = t("menu_update", { version: offer.version });
    $("more").setAttribute("aria-label", t(on ? "more_update" : "more"));
    $("more").title = $("more").getAttribute("aria-label");
    $("st-version").textContent = version ? `Amigent ${version}${on ? " · " + t("update_ready") : ""}` : "";
    $("st-version").classList.toggle("ready", on);
  }

  async function answerUpdate(action) {
    $("update").hidden = true;
    if (action === "skip") offer = null;
    paintUpdate();
    const reply = await fetch(api("/api/update"), { method: "POST", body: action }).catch(() => null);
    if (!reply || !reply.ok) {
      const said = reply ? (await reply.json().catch(() => ({}))).error : "";
      if (action === "now") toast(said || t("update_failed"), true);
      return;
    }
    if (action === "now") toast(t("update_running"));
  }
  for (const action of ["now", "later", "skip"]) {
    $(`update-${action}`).addEventListener("click", () => answerUpdate(action));
  }
  $("about-update-now").addEventListener("click", () => {
    $("about-dialog").close();
    answerUpdate("now");
  });

  // ---------------------------------------------------------------- link

  let ws = null;
  let socketOpen = false;
  let amigaOnline = null;
  let host = "";
  let address = "";
  let reason = "";  // why the Amiga does not answer: a reason_* key from the PC side
  let amiga = {};
  let stats = {};

  // The PC side sends a key (reason_*) for why the Amiga does not answer; only
  // keys the locales know are shown, anything else stays off the page.
  function shownLinkReason(key) {
    return /^reason_[a-z_]+$/.test(key) && UI.knows(key) ? t(key) : "";
  }
  // Bumped when the connection identity changes so a late amiga/SYSINFO
  // payload from a previous session cannot re-offer Hear.
  let capabilityEpoch = 0;

  function send(op) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(op));
  }

  function invalidateHearCapabilities() {
    capabilityEpoch += 1;
    amiga = {};
    showHear(null);
  }

  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { socketOpen = true; showState(); showHear(amiga); };
    ws.onclose = () => {
      socketOpen = false;
      stopHear(false);
      invalidateHearCapabilities();
      showState();
      setTimeout(connect, 1000);
    };
    ws.onmessage = (event) => {
      if (typeof event.data === "string") onEvent(JSON.parse(event.data));
      else onBinary(event.data);
    };
  }

  function where() {
    return address && address !== host ? `${host} (${address})` : host;
  }

  function showState() {
    const overlay = $("overlay");
    let message = "";
    if (!socketOpen) message = t("disconnected");
    else if (amigaOnline === false) message = t("offline");
    else if (amigaOnline === null) message = t("connecting");
    overlay.classList.toggle("show", message !== "");
    $("overlay-text").textContent = message;
    // Say where it connects to, and why it failed: a missing key looks like a hang otherwise.
    $("overlay-reason").textContent = !socketOpen ? "" : amigaOnline === false ? shownLinkReason(reason)
      : amigaOnline === null && address ? where() : "";
    $("offline-actions").hidden = !(socketOpen && amigaOnline === false);
    const up = socketOpen && amigaOnline;
    const waiting = socketOpen && amigaOnline === null;
    $("machine-dot").className = "dot " + (up ? "ok" : waiting ? "" : "bad");
    $("machine-name").textContent = host || "Amiga";
    const state = up ? t("machines_online") : waiting ? t("machines_checking") : t("state_offline");
    $("machine-sub").textContent = up ? [amiga.os, amiga.cpu && amiga.cpu + (amiga.fpu_state && !["none", ""].includes(amiga.fpu_state) ? " + FPU" : "")].filter(Boolean).join(" · ") || state : state;
    $("machine").setAttribute("aria-label", t("machine_switch", { name: host || "Amiga", state }));
    $("machine").title = up ? [where(), systemLine(amiga), cpuLine(amiga)].filter(Boolean).join("\n") : "";
    // the machine menu shows this window's own view of its Amiga, which is fresher than the list's
    window.amigentCurrent = { online: socketOpen ? amigaOnline : false, os: amiga.os || "" };
    document.dispatchEvent(new Event("amigent-current"));
  }

  function showPaused(seconds) {
    $("paused").hidden = !seconds;
    const m = Math.floor(seconds / 60), s = String(seconds % 60).padStart(2, "0");
    if (seconds) $("paused-text").textContent = t("paused", { time: `${m}:${s}` });
    $("about-picture").textContent = seconds ? t("picture_paused", { time: `${m}:${s}` }) : t("picture_live");
  }

  // null when SYSINFO did not send an integer. 0 is a real reading.
  function knownBytes(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    if (typeof value === "number") return Number.isInteger(value) && value >= 0 ? value : null;
    if (typeof value !== "string" || !/^\d+$/.test(value.trim())) return null;
    return Number(value.trim());
  }

  function bytes(n) {
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
    if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(n >= 100 * 1024 ** 2 ? 0 : 1)} MB`;
    return `${Math.round(n / 1024)} KB`;
  }

  function systemLine(info) {
    return [info.os, info.kickstart && t("kickstart_n", { version: info.kickstart }),
      info.workbench && t("workbench_n", { version: info.workbench })].filter(Boolean).join(", ");
  }

  const cpuLine = UI.cpuLine;

  function apolloMachine(info) {
    return !!(info && (info.os === "ApolloOS" || String(info.cpu || "").includes("68080")));
  }

  // Hear is visible only for an active Apollo connection with protocol ≥ 6
  // and SYSINFO audio=1. Protocol 6 alone is not Apollo evidence.
  function infoHearEligible(info) {
    if (!info || typeof info !== "object") return false;
    if (!apolloMachine(info)) return false;
    const proto = Number(info.daemon_protocol);
    if (!Number.isFinite(proto) || proto < 6) return false;
    return info.audio === "1";
  }

  function hearEligible(info) {
    return !!(socketOpen && amigaOnline === true && infoHearEligible(info));
  }


  let toldOutdated = false;

  function showAmiga(info) {
    const epoch = capabilityEpoch;
    if (!info || typeof info !== "object") {
      showHear(null);
      return;
    }
    amiga = info;
    if (info.pc_outdated && !toldOutdated) {
      toldOutdated = true;
      toast(t("pc_outdated"), true);
    }
    const chip = knownBytes(info.chip_free);
    const fast = bytes(knownBytes(info.fast_free) ?? 0);
    const memory = chip === null ? t("mem_fast_only", { fast }) : t("mem_line", { chip: bytes(chip), fast });
    $("about-system").textContent = systemLine(info) || "–";
    $("about-cpu").textContent = cpuLine(info) || "–";
    $("about-memory").textContent = memory;
    $("about-tcp").textContent = info.tcp ? `bsdsocket.library ${info.tcp}` : "–";
    const list = $("about-volumes");
    list.replaceChildren();
    for (const v of info.volumes || []) {
      const row = document.createElement("div");
      row.className = "vol";
      row.textContent = `${v.name}:`;
      const free = document.createElement("span");
      free.textContent = t("free_of", { free: bytes(v.free), size: bytes(v.size) });
      row.append(free);
      list.append(row);
    }
    if (!list.children.length) list.textContent = "–";
    $("st-mem").textContent = bytes((chip ?? 0) + (knownBytes(info.fast_free) ?? 0));
    $("st-free").hidden = false;
    // Drop capabilities that arrived after a disconnect / address change.
    if (epoch !== capabilityEpoch) return;
    showHear(amiga);
    showLink();
    showState();
  }

  // the Connection tab: what the PC knows about the line to this Amiga
  function showLink() {
    const link = amiga.link || {};
    $("about-address").textContent = where() || "–";
    $("about-port").textContent = link.port || "–";
    $("about-tls").textContent = link.tls === undefined ? "–" : t(link.tls ? "link_tls_on" : "link_tls_off");
    $("about-protocol").textContent = link.protocol || "–";
    const warn = amiga.pc_outdated ? t("pc_outdated") : link.amiga_outdated ? t("amiga_outdated") : "";
    $("about-warn").textContent = warn;
    $("about-warn").hidden = !warn;
    $("about-versions").textContent = version
      ? amiga.amigentd ? t("version_line", { pc: version, amiga: amiga.amigentd }) : t("version_pc", { pc: version })
      : "–";
  }

  // The screen picker: Intuition's screens, front one first, plus the two Amiga screen keys.
  let screenMenu = null;

  function showHear(info) {
    const button = $("hear");
    const offered = hearEligible(info);
    button.hidden = !offered;
    if (!offered && hearing) stopHear(true);
  }

  function showScreens(list) {
    const pick = $("screen-pick");
    pick.hidden = list.length < 2;
    const box = $("screen-list");
    box.replaceChildren();
    for (const s of list) {
      const item = document.createElement("button");
      item.setAttribute("role", "menuitemradio");
      item.setAttribute("aria-checked", String(!!s.front));
      item.textContent = `${s.title || t("screen_untitled")} · ${s.w}×${s.h}`;
      item.addEventListener("click", () => {
        send({ t: "screen", handle: s.handle });
        canvas.focus();
      });
      box.append(item);
      if (s.front) $("screen-name").textContent = item.textContent;
    }
    pick.setAttribute("aria-label", `${t("screen_select")}: ${$("screen-name").textContent}`);
    screenMenu?.refresh();
  }

  function onEvent(m) {
    switch (m.type) {
      case "hello":
        host = m.host;
        document.title = `Amigent · ${host}`;
        if (m.version) {
          version = m.version;
          $("about-version").textContent = m.version;
        }
        address = m.address || host;
        $("about-host").textContent = where();
        reason = m.reason || "";
        if (m.update_done) toast(m.update_done.ok ? t("update_done") : t("update_failed"), !m.update_done.ok);
        if (m.update) showUpdate(m.update);
        paintUpdate();
        amigaOnline = m.online;
        showScreens(m.screens || []);
        if (m.online && m.amiga && m.amiga.os !== undefined) showAmiga(m.amiga);
        else invalidateHearCapabilities();
        if (m.w) setSize(m.w, m.h);
        showLink();
        showState();
        showHear(amiga);
        break;
      case "size":
        setSize(m.w, m.h);
        break;
      case "update":
        showUpdate(m.update);
        break;
      case "amiga":
        if (amigaOnline !== true) {
          // Stale capability while disconnected / connecting — ignore for Hear.
          return;
        }
        showAmiga(m);
        break;
      case "screens":
        showScreens(m.screens);
        break;
      case "online":
        amigaOnline = m.online;
        reason = m.reason || "";
        if (!m.online) {
          stopHear(false);
          invalidateHearCapabilities();
        } else {
          showHear(amiga);
        }
        showState();
        break;
      case "audio":
        if (!m.on) stopHear(false);
        break;
      case "address":
        stopHear(false);
        invalidateHearCapabilities();
        address = m.address;
        $("about-host").textContent = where();
        showLink();
        break;
      case "stats":
        stats = m;
        $("st-size").textContent = `${m.w}×${m.h}`;
        $("about-screen").textContent = `${m.w} × ${m.h}`;
        $("st-rate").textContent = `${m.kbps.toFixed(1)} KB/s`;
        $("st-rtt").textContent = `${m.rtt.toFixed(0)} ms`;
        $("about-speed").textContent = $("st-rate").textContent;
        $("about-ping").textContent = $("st-rtt").textContent;
        showPaused(m.paused || 0);
        break;
      case "upload":
        onUpload(m);
        break;
      case "toast":
        toast(m.text ? `${t(m.key)}: ${m.text}` : t(m.key), m.key.endsWith("failed"));
        break;
    }
  }

  // ---------------------------------------------------------------- picture

  const LUT = new Uint32Array(256);
  for (let v = 0; v < 256; v++) {
    const r = Math.floor((v & 7) * 255 / 7);
    const g = Math.floor(((v >> 3) & 7) * 255 / 7);
    const b = Math.floor(((v >> 6) & 3) * 255 / 3);
    LUT[v] = 0xff000000 | (b << 16) | (g << 8) | r;
  }

  // Pictures and tiles must land in the order they came: a snapshot decodes
  // asynchronously, so everything after it waits for it.
  let drawing = Promise.resolve();

  function onBinary(buffer) {
    const kind = new Uint8Array(buffer, 0, 1)[0];
    if (kind === 1) {
      drawing = drawing.then(() => drawTiles(buffer));
    } else if (kind === 3) {
      drawing = drawing.then(() => drawPixels(buffer));
    } else if (false) {
    } else if (kind === 5 && hearing && buffer.byteLength >= 5) {
      const view = new DataView(buffer);
      playPcm(view.getUint32(1), new Uint8Array(buffer, 5));
    } else if (kind === 2) {
      drawing = drawing
        .then(() => createImageBitmap(new Blob([new Uint8Array(buffer, 1)], { type: "image/png" })))
        .then((bitmap) => ctx.drawImage(bitmap, 0, 0))
        .catch(() => {});
    }
  }

  // One ImageData per tile shape, reused: no garbage per tile, the collector stays in its cave.
  const scratch = new Map();

  function tileImage(w, h) {
    const key = w << 16 | h;
    let entry = scratch.get(key);
    if (!entry) {
      const image = ctx.createImageData(w, h);
      entry = { image, pixels: new Uint32Array(image.data.buffer) };
      scratch.set(key, entry);
    }
    return entry;
  }

  function drawTiles(buffer) {
    const view = new DataView(buffer);
    const bytes = new Uint8Array(buffer);
    let at = 1;
    while (at < buffer.byteLength) {
      const x = view.getUint16(at), y = view.getUint16(at + 2);
      const w = view.getUint16(at + 4), h = view.getUint16(at + 6);
      const size = view.getUint32(at + 8);
      const data = bytes.subarray(at + 12, at + 12 + size);
      at += 12 + size;
      const { image, pixels } = tileImage(w, h);
      if (size === w * h && size !== 0) {
        for (let i = 0; i < size; i++) pixels[i] = LUT[data[i]];
      } else {
        let p = 0;
        for (let i = 0; i + 1 < size; i += 2) {
          const run = data[i] + 1;
          pixels.fill(LUT[data[i + 1]], p, p + run);
          p += run;
        }
      }
      ctx.putImageData(image, x, y);
    }
  }

  // Message 3: regions the PC already decoded (bitplanes, HAM), three bytes a pixel.
  function drawPixels(buffer) {
    const view = new DataView(buffer);
    const bytes = new Uint8Array(buffer);
    let at = 1;
    while (at < buffer.byteLength) {
      const x = view.getUint16(at), y = view.getUint16(at + 2);
      const w = view.getUint16(at + 4), h = view.getUint16(at + 6);
      at += 8;
      const { image, pixels } = tileImage(w, h);
      for (let i = 0, n = w * h; i < n; i++, at += 3) {
        pixels[i] = 0xff000000 | (bytes[at + 2] << 16) | (bytes[at + 1] << 8) | bytes[at];
      }
      ctx.putImageData(image, x, y);
    }
  }

  let hearing = false;
  let audioCtx = null, player = null, playerBoot = null;
  const playerQueue = [];
  const WORKLET = `
class AmigentPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.cap = 48000 * 8;
    this.L = new Float32Array(this.cap);
    this.R = new Float32Array(this.cap);
    this.w = 0;
    this.r = 0;
    this.rate = 48000;
    this.started = false;
    this.port.onmessage = (e) => {
      const pcm = e.data.pcm;
      const n = pcm.length >> 1;
      if (e.data.rate) this.rate = e.data.rate;
      if (this.w - this.r + n >= this.cap) this.r = this.w + n - this.cap + 1;
      for (let i = 0; i < n; i++) {
        const at = this.w % this.cap;
        this.L[at] = pcm[i * 2];
        this.R[at] = pcm[i * 2 + 1];
        this.w++;
      }
    };
  }
  process(_inputs, outputs) {
    const ch = outputs[0];
    if (!ch || !ch[0]) return true;
    const L = ch[0];
    const R = ch[1] || ch[0];
    if (!this.started) {
      if (this.w - this.r < this.rate * 0.25) {
        L.fill(0);
        if (ch[1]) R.fill(0);
        return true;
      }
      this.started = true;
    }
    const step = this.rate / sampleRate;
    for (let i = 0; i < L.length; i++) {
      if (this.w - this.r < 2) {
        L[i] = 0;
        if (ch[1]) R[i] = 0;
        continue;
      }
      const i0 = Math.floor(this.r);
      const f = this.r - i0;
      const a = i0 % this.cap;
      const b = (i0 + 1) % this.cap;
      L[i] = this.L[a] * (1 - f) + this.L[b] * f;
      if (ch[1]) R[i] = this.R[a] * (1 - f) + this.R[b] * f;
      this.r += step;
    }
    return true;
  }
}
registerProcessor("amigent-pcm", AmigentPcm);
`;

  function playPcm(rate, pcm) {
    if (!hearing || !pcm.byteLength || pcm.byteLength % 4) return;
    const samples = pcm.byteLength / 4;
    const out = new Float32Array(samples * 2);
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    for (let i = 0; i < samples; i++) {
      out[i * 2] = view.getInt16(i * 4, true) / 32768;
      out[i * 2 + 1] = view.getInt16(i * 4 + 2, true) / 32768;
    }
    const item = { rate: rate || 48000, pcm: out };
    if (player) {
      player.port.postMessage(item, [out.buffer]);
      return;
    }
    playerQueue.push(item);
    if (!playerBoot) {
      if (!audioCtx) audioCtx = new AudioContext();
      if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      playerBoot = audioCtx.audioWorklet.addModule(url).then(() => {
        URL.revokeObjectURL(url);
        if (!hearing) return;
        player = new AudioWorkletNode(audioCtx, "amigent-pcm", { outputChannelCount: [2] });
        player.connect(audioCtx.destination);
        for (const queued of playerQueue) player.port.postMessage(queued, [queued.pcm.buffer]);
        playerQueue.length = 0;
      }).catch(() => { URL.revokeObjectURL(url); playerBoot = null; });
    }
  }


  function stopHear(tell) {
    hearing = false;
    const button = $("hear");
    if (button) button.setAttribute("aria-pressed", "false");
    playerQueue.length = 0;
    playerBoot = null;
    if (player) {
      try { player.disconnect(); } catch (_) {}
      player = null;
    }
    if (audioCtx) {
      const ctx = audioCtx;
      audioCtx = null;
      try { ctx.close(); } catch (_) {}
    }
    if (tell) send({ t: "audio", on: false });
  }

  function setSize(w, h) {
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, w, h);
    }
    layout();
  }

  let fit = store.get("fit", "1") === "1";

  function layout() {
    const w = canvas.width, h = canvas.height;
    const scale = fit ? Math.min(stage.clientWidth / w, stage.clientHeight / h) : 1;
    canvas.style.width = `${Math.max(1, Math.floor(w * scale))}px`;
    canvas.style.height = `${Math.max(1, Math.floor(h * scale))}px`;
    canvas.classList.toggle("sharp", scale >= 1);
    stage.classList.toggle("actual", !fit);
    $("fit").setAttribute("aria-pressed", String(fit));
  }

  new ResizeObserver(layout).observe(stage);

  // ---------------------------------------------------------------- mouse

  let holdRight = false;
  let pendingMove = null;
  let moveFrame = 0;

  function amigaPoint(event) {
    const r = canvas.getBoundingClientRect();
    const x = Math.round((event.clientX - r.left) * canvas.width / r.width);
    const y = Math.round((event.clientY - r.top) * canvas.height / r.height);
    return { x: Math.max(0, Math.min(canvas.width - 1, x)), y: Math.max(0, Math.min(canvas.height - 1, y)) };
  }

  function buttons(event) {
    // Browser and Amiga use the same bits: 1 left, 2 right, 4 middle.
    return ((event ? event.buttons : 0) & 7) | (holdRight ? 2 : 0);
  }

  canvas.addEventListener("pointermove", (event) => {
    if (relative) return;
    pendingMove = amigaPoint(event);
    if (!moveFrame) {
      moveFrame = requestAnimationFrame(() => {
        moveFrame = 0;
        if (pendingMove) send({ t: "move", ...pendingMove });
        pendingMove = null;
      });
    }
  });

  canvas.addEventListener("pointerdown", (event) => {
    canvas.focus();
    pendingMove = null;
    if (!relative) {
      canvas.setPointerCapture(event.pointerId);
      send({ t: "move", ...amigaPoint(event) });
    }
    send({ t: "buttons", mask: buttons(event) });
    event.preventDefault();
  });

  canvas.addEventListener("pointerup", (event) => {
    if (!relative) send({ t: "move", ...amigaPoint(event) });
    send({ t: "buttons", mask: buttons(event) });
  });

  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  let wheel = 0;
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    wheel += event.deltaMode === 0 ? event.deltaY / 100 : event.deltaY;
    const steps = Math.trunc(wheel);
    if (steps) {
      wheel -= steps;
      send({ t: "wheel", dy: steps });
    }
  }, { passive: false });

  $("hold-right").addEventListener("click", () => {
    holdRight = !holdRight;
    $("hold-right").setAttribute("aria-pressed", String(holdRight));
    send({ t: "buttons", mask: buttons() });
  });

  $("middle").addEventListener("click", () => {
    send({ t: "buttons", mask: 4 | buttons() });
    send({ t: "buttons", mask: buttons() });
    canvas.focus();
  });

  // Games read mouse movement, not the pointer position: capture the mouse and send deltas.
  let relative = false;
  let relX = 0, relY = 0, relFrame = 0;
  // Game mode = captured mouse + direct keys; letting the mouse go (Esc) ends it.
  let gameMode = false;
  let directBefore = false;

  for (const button of document.querySelectorAll("[data-pause]")) {
    button.addEventListener("click", () => {
      send({ t: "pause", seconds: Number(button.dataset.pause) });
      canvas.focus();
    });
  }
  $("resume").addEventListener("click", () => send({ t: "pause", seconds: 0 }));

  function capture(on) {
    if (on && document.pointerLockElement !== canvas) {
      const asked = canvas.requestPointerLock();
      if (asked && asked.catch) asked.catch(() => {});
    } else if (!on && document.pointerLockElement === canvas) {
      document.exitPointerLock();
    }
  }

  function setGame(on) {
    if (on === gameMode) return;
    gameMode = on;
    if (on) {
      directBefore = direct;
      setDirect(true, false);
      capture(true);
    } else {
      setDirect(directBefore, false);
      capture(false);
    }
    paintGame();
  }

  function paintGame() {
    $("game-mode").setAttribute("aria-checked", String(gameMode));
    $("rel-mouse").setAttribute("aria-checked", String(relative));
    $("kb-direct").setAttribute("aria-checked", String(direct));
    const pill = gameMode || relative;
    $("game-pill").hidden = !pill;
    $("game-pill-text").textContent = t(gameMode ? "game_pill" : "mouse_pill");
    stage.classList.toggle("game", gameMode);
  }

  $("game-mode").addEventListener("click", () => setGame(!gameMode));
  $("rel-mouse").addEventListener("click", () => capture(!relative));

  document.addEventListener("pointerlockchange", () => {
    relative = document.pointerLockElement === canvas;
    if (relative) canvas.focus();
    else {
      send({ t: "buttons", mask: buttons() });
      if (gameMode) setGame(false);
    }
    paintGame();
  });

  document.addEventListener("pointerlockerror", () => {
    if (gameMode) setGame(false);
    toast(t("not_supported"), true);
  });

  canvas.addEventListener("mousemove", (event) => {
    if (!relative) return;
    relX += event.movementX;
    relY += event.movementY;
    if (!relFrame) {
      relFrame = requestAnimationFrame(() => {
        relFrame = 0;
        if (relX || relY) send({ t: "rel", dx: Math.round(relX), dy: Math.round(relY) });
        relX = relY = 0;
      });
    }
  });

  // ---------------------------------------------------------------- keys

  const Q = { lshift: 1, rshift: 2, caps: 4, ctrl: 8, lalt: 16, ralt: 32, lamiga: 64, ramiga: 128 };

  const SPECIAL = {
    Enter: 0x44, NumpadEnter: 0x43, Backspace: 0x41, Tab: 0x42, Escape: 0x45, Delete: 0x46,
    ArrowUp: 0x4c, ArrowDown: 0x4d, ArrowRight: 0x4e, ArrowLeft: 0x4f, Insert: 0x5f, Help: 0x5f,
    F1: 0x50, F2: 0x51, F3: 0x52, F4: 0x53, F5: 0x54, F6: 0x55, F7: 0x56, F8: 0x57, F9: 0x58, F10: 0x59,
  };
  // Amiga editors use Shift with the cursor keys for these.
  const SHIFTED = { PageUp: 0x4c, PageDown: 0x4d, Home: 0x4f, End: 0x4e };

  const MODIFIERS = {
    ShiftLeft: [0x60, Q.lshift], ShiftRight: [0x61, Q.rshift], CapsLock: [0x62, Q.caps],
    ControlLeft: [0x63, Q.ctrl], ControlRight: [0x63, Q.ctrl], AltLeft: [0x64, Q.lalt],
    AltRight: [0x65, Q.ralt], MetaLeft: [0x66, Q.lamiga], MetaRight: [0x67, Q.ramiga],
    ContextMenu: [0x67, Q.ramiga],
  };
  // Cmd is the shortcut key on a Mac, as Right Amiga is on the Amiga.
  if (MAC) MODIFIERS.MetaLeft = MODIFIERS.MetaRight;
  const CMD = MAC ? Q.ramiga : Q.lamiga;

  // Key positions for direct mode. The key left of Return is the ISO one (# on German keyboards).
  const POSITIONS = {
    Backquote: 0x00, Digit1: 0x01, Digit2: 0x02, Digit3: 0x03, Digit4: 0x04, Digit5: 0x05,
    Digit6: 0x06, Digit7: 0x07, Digit8: 0x08, Digit9: 0x09, Digit0: 0x0a, Minus: 0x0b, Equal: 0x0c,
    IntlYen: 0x0d, Numpad0: 0x0f, KeyQ: 0x10, KeyW: 0x11, KeyE: 0x12, KeyR: 0x13, KeyT: 0x14,
    KeyY: 0x15, KeyU: 0x16, KeyI: 0x17, KeyO: 0x18, KeyP: 0x19, BracketLeft: 0x1a, BracketRight: 0x1b,
    Numpad1: 0x1d, Numpad2: 0x1e, Numpad3: 0x1f, KeyA: 0x20, KeyS: 0x21, KeyD: 0x22, KeyF: 0x23,
    KeyG: 0x24, KeyH: 0x25, KeyJ: 0x26, KeyK: 0x27, KeyL: 0x28, Semicolon: 0x29, Quote: 0x2a,
    Backslash: 0x2b, Numpad4: 0x2d, Numpad5: 0x2e, Numpad6: 0x2f, IntlBackslash: 0x30, KeyZ: 0x31,
    KeyX: 0x32, KeyC: 0x33, KeyV: 0x34, KeyB: 0x35, KeyN: 0x36, KeyM: 0x37, Comma: 0x38,
    Period: 0x39, Slash: 0x3a, NumpadDecimal: 0x3c, Numpad7: 0x3d, Numpad8: 0x3e, Numpad9: 0x3f,
    Space: 0x40, NumpadSubtract: 0x4a, NumpadDivide: 0x5c, NumpadMultiply: 0x5d, NumpadAdd: 0x5e,
  };

  let direct = false;
  let held = 0;            // qualifiers of physically held modifier keys
  const down = new Set();  // codes we pressed in direct mode
  let sticky = 0, locked = 0;

  function useSticky() {
    const q = sticky | locked;
    if (sticky) {
      sticky = 0;
      paintMods();
    }
    return q;
  }

  function paintMods() {
    for (const el of document.querySelectorAll("[data-mod]")) {
      const bit = Number(el.dataset.mod);
      el.setAttribute("aria-pressed", String(!!((sticky | locked) & bit)));
      el.classList.toggle("locked", !!(locked & bit));
    }
  }

  for (const el of document.querySelectorAll("[data-mod]")) {
    const bit = Number(el.dataset.mod);
    el.addEventListener("click", () => {
      if (locked & bit) locked &= ~bit;
      else sticky ^= bit;
      paintMods();
      canvas.focus();
    });
    el.addEventListener("dblclick", () => {
      sticky &= ~bit;
      locked |= bit;
      paintMods();
    });
  }

  function altGraph(event) {
    return event.getModifierState && event.getModifierState("AltGraph");
  }

  // The Amiga keys held down; also when the browser saw no keydown for them.
  function amigaKeys(event) {
    const q = held & (Q.lamiga | Q.ramiga);
    return q || (event.metaKey ? CMD : 0);
  }

  function qualifiers(event) {
    let q = amigaKeys(event) | (held & Q.caps);
    if (event.shiftKey) q |= held & Q.rshift ? Q.rshift : Q.lshift;
    if (!altGraph(event)) {
      if (event.ctrlKey) q |= Q.ctrl;
      if (event.altKey) q |= Q.lalt;
    }
    return q;
  }

  function onKey(event, isDown) {
    if (document.activeElement !== canvas) return;
    const code = event.code;

    if (MODIFIERS[code]) {
      const [raw, bit] = MODIFIERS[code];
      held = isDown ? held | bit : held & ~bit;
      if (MAC && !isDown && code.startsWith("Meta")) {
        // macOS sends no keyup for keys let go while Cmd was held
        for (const k of down) send({ t: "key", code: k, down: false, q: held });
        down.clear();
      }
      if (direct && !(isDown && event.repeat)) send({ t: "key", code: raw, down: isDown, q: held });
      event.preventDefault();
      return;
    }

    if (direct) {
      const raw = POSITIONS[code] ?? SPECIAL[code] ?? SHIFTED[code];
      if (raw === undefined) return;
      event.preventDefault();
      if (isDown) down.add(raw); else down.delete(raw);
      send({ t: "key", code: raw, down: isDown, q: held | locked, repeat: event.repeat });
      return;
    }

    // text mode
    if (SPECIAL[code] !== undefined || SHIFTED[code] !== undefined) {
      event.preventDefault();
      const shifted = SHIFTED[code] !== undefined;
      const raw = shifted ? SHIFTED[code] : SPECIAL[code];
      const q = qualifiers(event) | (shifted ? Q.lshift : 0) | (isDown ? useSticky() : locked);
      send({ t: "key", code: raw, down: isDown, q, repeat: event.repeat });
      return;
    }
    if (!isDown || event.key.length !== 1) return;
    const pasteKey = MAC ? event.metaKey : event.ctrlKey && !altGraph(event);
    if (pasteKey && event.key.toLowerCase() === "v" && !(sticky | locked)) return; // paste below
    event.preventDefault();
    let q = useSticky();
    if (!altGraph(event) && !(MAC && event.altKey)) {
      if (event.ctrlKey) q |= Q.ctrl;
      if (event.altKey) q |= Q.lalt;
    }
    q |= amigaKeys(event);
    // repeats are marked so a thin wire can drop them instead of queueing a runaway cursor
    send({ t: "char", c: event.key, q, repeat: event.repeat });
  }

  window.addEventListener("keydown", (event) => onKey(event, true));
  window.addEventListener("keyup", (event) => onKey(event, false));

  document.addEventListener("paste", (event) => {
    if (document.activeElement !== canvas) return;
    const text = event.clipboardData.getData("text");
    if (text) send({ t: "text", s: text });
    event.preventDefault();
  });

  function releaseAll() {
    for (const raw of down) send({ t: "key", code: raw, down: false, q: 0 });
    down.clear();
    held = 0;
    send({ t: "buttons", mask: holdRight ? 2 : 0 });
  }

  canvas.addEventListener("blur", releaseAll);
  window.addEventListener("blur", releaseAll);


  function setDirect(on, remember = true) {
    direct = on;
    if (remember) store.set("direct", on ? "1" : "0");
    releaseAll();
    paintGame();
    canvas.focus();
  }

  $("kb-direct").addEventListener("click", () => setDirect(!direct));

  // Program shortcuts: a fixed chord. Panel keys: with the modifiers picked for the next key.
  const MOD_NAMES = [[Q.lshift, "shift"], [Q.ctrl, "ctrl"], [Q.lalt, "alt"], [Q.lamiga, "lamiga"], [Q.ramiga, "ramiga"]];

  for (const el of document.querySelectorAll("[data-combo]")) {
    el.addEventListener("click", () => {
      send({ t: "combo", k: el.dataset.combo });
      canvas.focus();
    });
  }
  for (const el of document.querySelectorAll("[data-key]")) {
    el.addEventListener("click", () => {
      const q = useSticky();
      const mods = MOD_NAMES.filter(([bit]) => q & bit).map(([, name]) => name);
      send({ t: "combo", k: [...mods, el.dataset.key].join("+") });
      canvas.focus();
    });
  }

  function openType() {
    $("type-dialog").showModal();
    $("type-text").focus();
  }
  $("type").addEventListener("click", openType);
  $("m-type").addEventListener("click", openType);

  $("type-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) $("type-dialog").close("type");
  });

  $("type-dialog").addEventListener("close", () => {
    const text = $("type-text").value;
    if ($("type-dialog").returnValue === "type" && text) {
      send({ t: "text", s: text });
      $("type-text").value = "";
    }
    canvas.focus();
  });

  // ---------------------------------------------------------------- view

  $("hear").addEventListener("click", () => {
    if (hearing) {
      stopHear(true);
      return;
    }
    // Visibility is not the only gate: refuse activation unless still eligible.
    if (!hearEligible(amiga)) {
      showHear(amiga);
      return;
    }
    hearing = true;
    $("hear").setAttribute("aria-pressed", "true");
    if (!audioCtx) audioCtx = new AudioContext();
    audioCtx.resume().catch(() => {});
    send({ t: "audio", on: true });
  });

  function toggleFit() {
    fit = !fit;
    store.set("fit", fit ? "1" : "0");
    layout();
    canvas.focus();
  }
  $("fit").addEventListener("click", toggleFit);
  $("m-fit").addEventListener("click", toggleFit);
  new MutationObserver(() => $("m-fit").setAttribute("aria-checked", $("fit").getAttribute("aria-pressed")))
    .observe($("fit"), { attributes: true, attributeFilter: ["aria-pressed"] });

  $("fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  });

  document.addEventListener("fullscreenchange", () => {
    // In full screen the browser can hand over the Windows keys, which become the Amiga keys.
    if (!navigator.keyboard || !navigator.keyboard.lock) return;
    if (document.fullscreenElement) navigator.keyboard.lock().catch(() => {});
    else navigator.keyboard.unlock();
  });

  function screenshot() {
    canvas.toBlob((blob) => {
      const a = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      a.href = URL.createObjectURL(blob);
      a.download = `amiga-${stamp}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  }
  $("shot").addEventListener("click", screenshot);
  $("m-shot").addEventListener("click", screenshot);

  function setPanel(on) {
    document.body.classList.toggle("no-panel", !on);
    $("panel-toggle").setAttribute("aria-pressed", String(on));
    store.set("panel", on ? "1" : "0");
    layout();
  }

  $("panel-toggle").addEventListener("click", () => setPanel(document.body.classList.contains("no-panel")));

  UI.tabs($("panel-tabs"), { remember: "tab", onChange: () => requestAnimationFrame(layout) });

  // ---------------------------------------------------------------- menus and About

  const aboutTabs = UI.tabs($("about-tabs"), { remember: "about-tab" });

  function openAbout(tab) {
    if (tab) aboutTabs.select(tab);
    showLink();
    if (!$("about-dialog").open) $("about-dialog").showModal();
  }

  screenMenu = UI.menu($("screen-pick"), $("screen-menu"), { align: "start" });
  UI.menu($("more"), $("more-menu"), { align: "end" });

  $("m-update").addEventListener("click", () => openAbout("ab-tab-amigent"));
  $("m-keys").addEventListener("click", () => openAbout("ab-tab-keys"));
  $("m-about").addEventListener("click", () => openAbout("ab-tab-amigent"));
  $("status-link").addEventListener("click", () => openAbout("ab-tab-link"));
  $("st-version").addEventListener("click", () => openAbout("ab-tab-amigent"));
  $("about-close").addEventListener("click", () => $("about-dialog").close());
  $("about-dialog").addEventListener("close", () => canvas.focus());
  $("language").addEventListener("change", () => setLanguage($("language").value));
  $("theme").addEventListener("change", () => setTheme($("theme").value));
  for (const item of document.querySelectorAll("[data-theme-value]")) {
    item.addEventListener("click", () => setTheme(item.dataset.themeValue));
  }

  // the offline overlay: somewhere else to go
  $("try-other").addEventListener("click", () => window.amigentMachines?.openMenu());
  $("try-address").addEventListener("click", () => window.amigentMachines?.open({ connect: true }));
  $("try-manage").addEventListener("click", () => window.amigentMachines?.open());

  // ---------------------------------------------------------------- restart

  $("m-restart").addEventListener("click", () => $("restart-dialog").showModal());
  $("restart-dialog").addEventListener("close", () => {
    if ($("restart-dialog").returnValue === "ok") send({ t: "restart" });
    canvas.focus();
  });

  // ---------------------------------------------------------------- files

  let chosen = [];
  let folderPath = store.get("folder", "RAM:");

  function api(path, params = {}) {
    const query = new URLSearchParams({ t: token, ...params });
    return `${path}?${query}`;
  }

  function sizeText(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function parentOf(path) {
    if (path.endsWith(":")) return null;
    const trimmed = path.replace(/\/$/, "");
    const cut = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf(":"));
    return cut < 0 ? null : trimmed.slice(0, cut + (trimmed[cut] === ":" ? 1 : 0));
  }

  function joinPath(folder, name) {
    return folder.endsWith(":") || folder.endsWith("/") ? folder + name : `${folder}/${name}`;
  }

  function note(text) {
    const li = document.createElement("li");
    li.className = "note";
    li.textContent = text;
    return li;
  }

  async function browse(path) {
    folderPath = path;
    $("folder").value = path;
    pickFile(null);
    const list = $("folders");
    list.replaceChildren(note(t("loading")));
    try {
      const reply = await fetch(api("/api/folders", { path }));
      const data = await reply.json();
      if (!reply.ok) throw new Error(data.error);
      const chips = $("volumes");
      chips.replaceChildren();
      for (const volume of ["RAM:", ...data.volumes.filter((v) => v !== "RAM Disk:")]) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.textContent = volume;
        if (path.toLowerCase().startsWith(volume.toLowerCase())) chip.setAttribute("aria-current", "true");
        chip.addEventListener("click", () => browse(volume));
        chips.append(chip);
      }
      list.replaceChildren();
      const parent = parentOf(path);
      if (parent) list.append(folderItem("..", parent, true));
      for (const name of data.folders) list.append(folderItem(name, joinPath(path, name)));
      if (getting) for (const file of data.files) list.append(fileItem(file, joinPath(path, file.name)));
      if (!data.folders.length && !(getting && data.files.length)) list.append(note(t(getting ? "no_files" : "no_folders")));
    } catch {
      list.replaceChildren(note(t("folder_error")));
      const parent = parentOf(path);
      if (parent) list.prepend(folderItem("..", parent, true));
    }
  }

  function folderItem(name, path, up = false) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = up ? "row up" : "row";
    button.textContent = name;
    button.addEventListener("click", () => browse(path));
    li.append(button);
    return li;
  }

  // A file in the folder list while getting: select it, then Get (or double-click).
  let picked = null;

  function pickFile(item) {
    picked = item;
    for (const el of $("folders").querySelectorAll("button.file")) {
      el.setAttribute("aria-pressed", String(item !== null && el === item.button));
    }
    $("get-go").disabled = !item;
  }

  function fileItem(file, path) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "row file";
    button.setAttribute("aria-pressed", "false");
    button.textContent = file.name;
    const size = document.createElement("span");
    size.textContent = sizeText(file.size);
    button.append(size);
    const item = { button, file, path };
    button.addEventListener("click", () => pickFile(item));
    button.addEventListener("dblclick", () => { pickFile(item); getPicked(); });
    li.append(button);
    return li;
  }

  async function getPicked() {
    if (!picked) return;
    const { file, path } = picked;
    toast(t("getting", { path }));
    try {
      const reply = await fetch(api("/api/download", { path }));
      if (!reply.ok) throw new Error((await reply.json()).error);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await reply.blob());
      link.download = file.name;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    } catch (error) {
      toast(t("failed", { path, error: error.message }), true);
    }
  }
  $("get-go").addEventListener("click", getPicked);

  let getting = false;

  function setMode(get) {
    getting = get;
    $("send-title").textContent = t(get ? "get_title" : "send_title");
    $("send-files").hidden = get;
    $("send-go").hidden = get;
    $("get-go").hidden = !get;
    $("get-hint").hidden = !get;
    $("send-cancel").textContent = t(get ? "close" : "cancel");
  }

  function openGet() {
    setMode(true);
    $("send-dialog").showModal();
    browse(folderPath);
  }

  function openSend(files) {
    chosen = [...files].filter((f) => f.size >= 0);
    if (!chosen.length) return;
    setMode(false);
    const list = $("send-files");
    list.replaceChildren();
    for (const f of chosen) {
      const li = document.createElement("li");
      li.textContent = f.name;
      const size = document.createElement("span");
      size.textContent = sizeText(f.size);
      li.append(size);
      list.append(li);
    }
    $("send-dialog").showModal();
    browse(folderPath);
  }

  $("folder").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      browse($("folder").value.trim() || "RAM:");
    }
  });

  $("send-dialog").addEventListener("close", async () => {
    canvas.focus();
    if (getting) store.set("folder", folderPath);
    if (getting || $("send-dialog").returnValue !== "send") return;
    const folder = $("folder").value.trim() || "RAM:";
    store.set("folder", folder);
    folderPath = folder;
    for (const file of chosen) await upload(file, folder);
  });

  async function upload(file, folder, overwrite = false) {
    try {
      const reply = await fetch(api("/api/upload", { folder, name: file.name, overwrite: overwrite ? "1" : "0" }), {
        method: "POST",
        body: file,
      });
      const data = await reply.json();
      if (reply.status === 409) {
        const yes = await UI.ask({ title: t("exists_title"), text: t("exists", { path: data.path }), ok: t("replace") });
        canvas.focus();
        if (yes) await upload(file, folder, true);
        return;
      }
      if (!reply.ok) throw new Error(data.error);
    } catch (error) {
      toast(t("failed", { path: file.name, error: error.message }), true);
    }
  }

  const rows = new Map();

  function onUpload(m) {
    let row = rows.get(m.id);
    if (!row) {
      row = document.createElement("div");
      row.className = "upload";
      row.innerHTML = `<div class="name"><b></b><span></span></div><div class="bar"><i></i></div>`;
      row.querySelector("b").textContent = m.path;
      $("uploads").prepend(row);
      rows.set(m.id, row);
    }
    const percent = m.size ? Math.round(100 * (m.done || 0) / m.size) : 100;
    row.querySelector(".bar i").style.width = `${m.state === "queued" ? 0 : percent}%`;
    const status = row.querySelector("span");
    if (m.state === "queued") status.textContent = t("queued");
    else if (m.state === "sending") status.textContent = `${percent}% · ${sizeText(m.size)}`;
    else if (m.state === "done") {
      row.classList.add("done");
      status.textContent = t("upload_done", { size: sizeText(m.size) });
      toast(t("saved", { path: m.path }));
      setTimeout(() => { row.remove(); rows.delete(m.id); }, 10000);
    } else if (m.state === "failed") {
      row.classList.add("failed");
      status.textContent = t("upload_failed");
      toast(t("failed", { path: m.path, error: m.error }), true);
    }
  }

  $("upload").addEventListener("click", () => $("file").click());
  $("download").addEventListener("click", openGet);
  $("m-get").addEventListener("click", openGet);

  // ---------------------------------------------------------------- clipboard

  async function clipGet() {
    try {
      const reply = await fetch(api("/api/clipboard"));
      const data = await reply.json();
      if (!reply.ok) throw new Error(data.error);
      if (!data.text) return toast(t("clip_empty"));
      await navigator.clipboard.writeText(data.text);
      toast(t("clip_got"));
    } catch (error) {
      toast(t("clip_failed", { error: error.message }), true);
    } finally {
      canvas.focus();
    }
  }

  async function clipPut() {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return toast(t("clip_empty"));
      const reply = await fetch(api("/api/clipboard"), { method: "POST", body: text });
      if (!reply.ok) throw new Error((await reply.json()).error);
      toast(t("clip_put_done"));
    } catch (error) {
      toast(t("clip_failed", { error: error.message }), true);
    } finally {
      canvas.focus();
    }
  }

  for (const el of document.querySelectorAll("[data-clip]")) {
    el.addEventListener("click", el.dataset.clip === "get" ? clipGet : clipPut);
  }

  $("file").addEventListener("change", () => {
    openSend($("file").files);
    $("file").value = "";
  });

  let dragDepth = 0;
  stage.addEventListener("dragenter", (event) => {
    if (![...event.dataTransfer.types].includes("Files")) return;
    dragDepth++;
    $("drop").classList.add("show");
  });
  stage.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) { dragDepth = 0; $("drop").classList.remove("show"); }
  });
  stage.addEventListener("dragover", (event) => event.preventDefault());
  stage.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    $("drop").classList.remove("show");
    openSend(event.dataTransfer.files);
  });

  // ---------------------------------------------------------------- start

  if (store.get("direct", "0") === "1") setDirect(true);
  if (store.get("panel", "1") === "0") setPanel(false);
  layout();
  canvas.focus();
  // Texts first: state messages before that would show their keys.
  UI.ready.then(() => {
    applyLanguage();
    paintGame();
    paintUpdate();
    showPaused(0);
    showState();
    connect();
  });
})();
