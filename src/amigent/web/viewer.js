// Amigent viewer: draws the Amiga's tiles, sends mouse and keys back.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("t") || "";
  const canvas = $("screen");
  const ctx = canvas.getContext("2d", { alpha: false });
  const stage = $("stage");

  // ---------------------------------------------------------------- text

  const store = {
    get(key, fallback) { try { return localStorage.getItem("amigent." + key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem("amigent." + key, value); } catch { /* private window */ } },
  };

  // Each language is web/locales/<code>.json; English is the default and fills gaps.
  const LANGUAGES = {
    en: "English", de: "Deutsch", pl: "Polski", fr: "Français", es: "Español", it: "Italiano",
    fi: "Suomi", sv: "Svenska", da: "Dansk", cs: "Čeština", sk: "Slovenčina", hu: "Magyar",
  };

  function pickLanguage() {
    const chosen = store.get("lang", "auto");
    if (LANGUAGES[chosen]) return chosen;
    for (const tag of navigator.languages?.length ? navigator.languages : [navigator.language || "en"]) {
      const code = tag.toLowerCase().split("-")[0];
      if (LANGUAGES[code]) return code;
    }
    return "en";
  }

  const language = pickLanguage();
  let EN = {};
  let TEXT = {};

  async function loadLanguage() {
    const load = (code) => fetch(`locales/${code}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    EN = await load("en");
    TEXT = language === "en" ? EN : await load(language);
  }

  function t(key, values = {}) {
    let text = TEXT[key] ?? EN[key] ?? key;
    for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, value);
    return text;
  }

  function applyLanguage() {
    document.documentElement.lang = language;
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    for (const el of document.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
    for (const el of document.querySelectorAll("[data-i18n-aria]")) el.setAttribute("aria-label", t(el.dataset.i18nAria));
    for (const el of document.querySelectorAll("[data-i18n-label]")) {
      el.title = t(el.dataset.i18nLabel);
      el.setAttribute("aria-label", el.title);
    }
    for (const el of document.querySelectorAll("[data-mod]")) {
      const name = el.dataset.i18nLabel ? t(el.dataset.i18nLabel) : el.textContent;
      el.title = `${name}. ${t("mod_tip")}`;
    }
    for (const el of document.querySelectorAll("[data-combo]")) {
      el.title = el.dataset.combo.replace("lamiga", t("lamiga")).replace("ramiga", t("ramiga"))
        .replace(/\+(.)$/, (_, k) => "+" + k.toUpperCase());
    }
    const select = $("language");
    select.innerHTML = "";
    select.append(new Option(t("language_auto"), "auto"));
    for (const [code, name] of Object.entries(LANGUAGES)) select.append(new Option(name, code));
    select.value = store.get("lang", "auto");
  }

  function toast(text, bad = false) {
    const el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    el.textContent = text;
    $("toasts").append(el);
    setTimeout(() => el.remove(), bad ? 7000 : 3500);
  }

  // ---------------------------------------------------------------- link

  let ws = null;
  let socketOpen = false;
  let amigaOnline = null;
  let host = "";

  function send(op) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(op));
  }

  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { socketOpen = true; showState(); };
    ws.onclose = () => {
      socketOpen = false;
      showState();
      setTimeout(connect, 1000);
    };
    ws.onmessage = (event) => {
      if (typeof event.data === "string") onEvent(JSON.parse(event.data));
      else onBinary(event.data);
    };
  }

  function showState() {
    const pill = $("state");
    const overlay = $("overlay");
    let message = "";
    if (!socketOpen) message = t("disconnected");
    else if (amigaOnline === false) message = t("offline");
    else if (amigaOnline === null) message = t("connecting");
    overlay.classList.toggle("show", message !== "");
    $("overlay-text").textContent = message;
    pill.className = "state " + (socketOpen && amigaOnline ? "ok" : socketOpen && amigaOnline === null ? "" : "bad");
    $("state-text").textContent = socketOpen && amigaOnline ? [host, amiga.os, amiga.cpu].filter(Boolean).join(" · ") : t("state_offline");
  }

  let amiga = {};

  function bytes(n) {
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
    if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(n >= 100 * 1024 ** 2 ? 0 : 1)} MB`;
    return `${Math.round(n / 1024)} KB`;
  }

  function showAmiga(info) {
    amiga = info;
    const system = [info.os, info.kickstart && `Kickstart ${info.kickstart}`, info.workbench && `Workbench ${info.workbench}`]
      .filter(Boolean).join(", ");
    const cpu = [info.cpu, info.fpu].filter(Boolean).join(" + ");
    const memory = t("mem_line", { chip: bytes(info.chip_free), fast: bytes(info.fast_free) });
    $("about-system").textContent = system || "–";
    $("about-cpu").textContent = cpu || "–";
    $("about-memory").textContent = memory;
    $("about-tcp").textContent = info.tcp ? `bsdsocket.library ${info.tcp}` : "–";
    const list = $("about-volumes");
    list.innerHTML = "";
    for (const v of info.volumes || []) {
      const row = document.createElement("div");
      row.className = "vol";
      row.textContent = `${v.name}:`;
      const free = document.createElement("span");
      free.textContent = t("free_of", { free: bytes(v.free), size: bytes(v.size) });
      row.append(free);
      list.append(row);
    }
    $("st-mem").textContent = t("status_free", { size: bytes(info.chip_free + info.fast_free) });
    $("state").title = [host, system, cpu, memory].filter(Boolean).join("\n");
    showState();
  }

  function showScreens(list) {
    const select = $("screen-select");
    select.hidden = list.length < 2;
    select.innerHTML = "";
    for (const s of list) {
      const option = new Option(`${s.title} · ${s.w}×${s.h}`, String(s.handle));
      option.selected = s.front;
      select.append(option);
    }
    select.title = t("screen_select");
  }

  function onEvent(m) {
    switch (m.type) {
      case "hello":
        host = m.host;
        document.title = `Amigent · ${host}`;
        if (m.version) {
          $("st-version").textContent = `Amigent ${m.version}`;
          $("about-version").textContent = t("version", { version: m.version });
        }
        $("about-host").textContent = host;
        if (m.amiga && m.amiga.os !== undefined) showAmiga(m.amiga);
        showScreens(m.screens || []);
        amigaOnline = m.online;
        if (m.w) setSize(m.w, m.h);
        showState();
        break;
      case "size":
        setSize(m.w, m.h);
        break;
      case "amiga":
        showAmiga(m);
        break;
      case "screens":
        showScreens(m.screens);
        break;
      case "online":
        amigaOnline = m.online;
        showState();
        break;
      case "stats":
        $("st-size").textContent = `${m.w}×${m.h}`;
        $("about-screen").textContent = `${m.w} × ${m.h}`;
        $("st-rate").textContent = `${m.kbps.toFixed(1)} KB/s`;
        $("st-rtt").textContent = `${m.rtt.toFixed(0)} ms`;
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
    } else if (kind === 2) {
      drawing = drawing
        .then(() => createImageBitmap(new Blob([new Uint8Array(buffer, 1)], { type: "image/png" })))
        .then((bitmap) => ctx.drawImage(bitmap, 0, 0))
        .catch(() => {});
    }
  }

  function drawTiles(buffer) {
    const view = new DataView(buffer);
    let at = 1;
    while (at < buffer.byteLength) {
      const x = view.getUint16(at), y = view.getUint16(at + 2);
      const w = view.getUint16(at + 4), h = view.getUint16(at + 6);
      const size = view.getUint32(at + 8);
      const data = new Uint8Array(buffer, at + 12, size);
      at += 12 + size;
      const image = ctx.createImageData(w, h);
      const pixels = new Uint32Array(image.data.buffer);
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

  $("screen-select").addEventListener("change", () => {
    send({ t: "screen", handle: Number($("screen-select").value) });
    canvas.focus();
  });

  // Games read mouse movement, not the pointer position: capture the mouse and send deltas.
  let relative = false;
  let relX = 0, relY = 0, relFrame = 0;

  $("rel-mouse").addEventListener("click", () => {
    if (document.pointerLockElement === canvas) document.exitPointerLock();
    else canvas.requestPointerLock();
  });

  document.addEventListener("pointerlockchange", () => {
    relative = document.pointerLockElement === canvas;
    $("rel-mouse").setAttribute("aria-pressed", String(relative));
    if (relative) {
      canvas.focus();
      toast(t("rel_mouse_on"));
    } else {
      send({ t: "buttons", mask: buttons() });
    }
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
  // On a Mac, Cmd pastes and Option types characters such as @ and € on its own.
  const MAC = /Mac/.test(navigator.platform || navigator.userAgent);

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

  function qualifiers(event) {
    let q = held & (Q.lamiga | Q.ramiga | Q.caps);
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
    q |= held & (Q.lamiga | Q.ramiga);
    send({ t: "char", c: event.key, q });
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

  function setDirect(on) {
    direct = on;
    $("kb-text").setAttribute("aria-checked", String(!on));
    $("kb-direct").setAttribute("aria-checked", String(on));
    store.set("direct", on ? "1" : "0");
    releaseAll();
    canvas.focus();
  }

  $("kb-text").addEventListener("click", () => setDirect(false));
  $("kb-direct").addEventListener("click", () => setDirect(true));

  for (const el of document.querySelectorAll("[data-combo]")) {
    el.addEventListener("click", () => {
      send({ t: "combo", k: el.dataset.combo });
      canvas.focus();
    });
  }

  $("type").addEventListener("click", () => {
    $("type-dialog").showModal();
    $("type-text").focus();
  });

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

  $("fit").addEventListener("click", () => {
    fit = !fit;
    store.set("fit", fit ? "1" : "0");
    layout();
    canvas.focus();
  });

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

  $("shot").addEventListener("click", () => {
    canvas.toBlob((blob) => {
      const a = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      a.href = URL.createObjectURL(blob);
      a.download = `amiga-${stamp}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
  });

  function setPanel(on) {
    document.body.classList.toggle("no-panel", !on);
    $("panel-toggle").setAttribute("aria-pressed", String(on));
    store.set("panel", on ? "1" : "0");
    layout();
  }

  $("panel-toggle").addEventListener("click", () => setPanel(document.body.classList.contains("no-panel")));

  $("about").addEventListener("click", () => $("about-dialog").showModal());
  $("language").addEventListener("change", () => {
    store.set("lang", $("language").value);
    location.reload();
  });
  $("about-dialog").addEventListener("close", () => canvas.focus());

  // ---------------------------------------------------------------- restart

  $("restart").addEventListener("click", () => $("restart-dialog").showModal());
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

  async function browse(path) {
    folderPath = path;
    $("folder").value = path;
    const list = $("folders");
    list.innerHTML = `<li class="note">${t("loading")}</li>`;
    try {
      const reply = await fetch(api("/api/folders", { path }));
      const data = await reply.json();
      if (!reply.ok) throw new Error(data.error);
      const chips = $("volumes");
      chips.innerHTML = "";
      for (const volume of ["RAM:", ...data.volumes.filter((v) => v !== "RAM Disk:")]) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.textContent = volume;
        if (path.toLowerCase().startsWith(volume.toLowerCase())) chip.setAttribute("aria-current", "true");
        chip.addEventListener("click", () => browse(volume));
        chips.append(chip);
      }
      list.innerHTML = "";
      const parent = parentOf(path);
      if (parent) list.append(folderItem("..", parent, true));
      for (const name of data.folders) list.append(folderItem(name, joinPath(path, name)));
      if (getting) for (const file of data.files) list.append(fileItem(file, joinPath(path, file.name)));
      if (!data.folders.length && !(getting && data.files.length)) {
        list.insertAdjacentHTML("beforeend", `<li class="note">${t(getting ? "no_files" : "no_folders")}</li>`);
      }
    } catch {
      list.innerHTML = `<li class="note">${t("folder_error")}</li>`;
      const parent = parentOf(path);
      if (parent) list.prepend(folderItem("..", parent, true));
    }
  }

  function folderItem(name, path, up = false) {
    const li = document.createElement("li");
    li.textContent = name;
    li.tabIndex = 0;
    if (up) li.className = "up";
    li.addEventListener("click", () => browse(path));
    li.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        browse(path);
      }
    });
    return li;
  }

  // A file in the folder list while getting: a click downloads it to the PC.
  function fileItem(file, path) {
    const li = document.createElement("li");
    li.className = "file";
    li.textContent = file.name;
    const size = document.createElement("span");
    size.textContent = sizeText(file.size);
    li.append(size);
    li.tabIndex = 0;
    const get = async () => {
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
    };
    li.addEventListener("click", get);
    li.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        get();
      }
    });
    return li;
  }

  let getting = false;

  function setMode(get) {
    getting = get;
    $("send-title").textContent = t(get ? "get_title" : "send_title");
    $("send-files").hidden = get;
    $("send-go").hidden = get;
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
    list.innerHTML = "";
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
        if (confirm(t("exists", { path: data.path }))) await upload(file, folder, true);
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
      status.textContent = `✓ ${sizeText(m.size)}`;
      toast(t("saved", { path: m.path }));
      setTimeout(() => { row.remove(); rows.delete(m.id); }, 10000);
    } else if (m.state === "failed") {
      row.classList.add("failed");
      status.textContent = "✗";
      toast(t("failed", { path: m.path, error: m.error }), true);
    }
  }

  $("upload").addEventListener("click", () => $("file").click());
  $("download").addEventListener("click", openGet);

  // ---------------------------------------------------------------- clipboard

  $("clip-get").addEventListener("click", async () => {
    try {
      const reply = await fetch(api("/api/clipboard"));
      const data = await reply.json();
      if (!reply.ok) throw new Error(data.error);
      if (!data.text) return toast(t("clip_empty"));
      await navigator.clipboard.writeText(data.text);
      toast(t("clip_got"));
    } catch (error) {
      toast(t("clip_failed", { error: error.message }), true);
    }
    canvas.focus();
  });

  $("clip-put").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return toast(t("clip_empty"));
      const reply = await fetch(api("/api/clipboard"), { method: "POST", body: text });
      if (!reply.ok) throw new Error((await reply.json()).error);
      toast(t("clip_put_done"));
    } catch (error) {
      toast(t("clip_failed", { error: error.message }), true);
    }
    canvas.focus();
  });
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
  loadLanguage().then(() => {
    applyLanguage();
    showState();
    connect();
  });
})();
