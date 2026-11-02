// amigent viewer: draws the Amiga's tiles, sends mouse and keys back.
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("t") || "";
  const canvas = $("screen");
  const ctx = canvas.getContext("2d", { alpha: false });
  const stage = $("stage");

  // ---------------------------------------------------------------- text

  const DE = {
    connecting: "Verbinde mit dem Amiga…",
    offline: "Der Amiga antwortet nicht. Neuer Versuch läuft…",
    disconnected: "Verbindung zu amigent getrennt. Neuer Versuch läuft…",
    state_offline: "nicht verbunden",
    send_file: "Datei senden",
    send_tip: "Dateien von diesem PC auf den Amiga kopieren",
    type_text: "Text tippen",
    type_tip: "Text auf dem Amiga tippen lassen",
    fit: "An Fenster anpassen",
    fullscreen: "Vollbild",
    screenshot: "Bildschirmfoto speichern",
    restart: "Amiga neu starten…",
    panel: "Tastatur-Leiste ein/aus",
    drop_here: "Loslassen, um es zum Amiga zu senden",
    screens: "Bildschirme",
    wb_front: "Workbench vorn",
    next_screen: "Schirm wechseln",
    programs: "Programme",
    open: "Öffnen",
    save: "Speichern",
    quit: "Beenden",
    copy: "Kopieren",
    cut: "Ausschneiden",
    paste: "Einfügen",
    select_all: "Alle wählen",
    undo: "Widerrufen",
    break: "Abbrechen",
    end_input: "Eingabe-Ende",
    keys: "Tasten",
    modifiers: "Für die nächste Taste halten",
    lamiga: "Linke Amiga-Taste",
    ramiga: "Rechte Amiga-Taste",
    mod_tip: "Klick: für die nächste Taste. Doppelklick: einrasten.",
    mouse: "Maus",
    hold_right: "Rechte Taste halten",
    middle_click: "Mittelklick",
    keyboard: "Tastatur",
    kb_text: "Text",
    kb_direct: "Direkt (Spiele)",
    kb_text_tip: "Zeichen über die Tastaturbelegung des Amiga",
    kb_direct_tip: "Tastenpositionen, gehaltene Tasten bleiben gehalten",
    send_title: "Zum Amiga senden",
    target_folder: "Ordner auf dem Amiga",
    cancel: "Abbrechen",
    send: "Senden",
    type_title: "Auf dem Amiga tippen",
    type_hint: "Der Text geht ins aktive Fenster, Zeilenumbrüche drücken Return.",
    type_go: "Tippen",
    restart_title: "Amiga neu starten?",
    restart_text: "Offene Dateien werden vorher auf die Platte geschrieben. Ungesicherte Arbeit in Programmen geht verloren.",
    restart_go: "Neu starten",
    restarting: "Amiga startet neu…",
    restart_failed: "Neustart fehlgeschlagen",
    exists: "{path} gibt es schon. Ersetzen?",
    saved: "Gespeichert: {path}",
    failed: "{path}: {error}",
    queued: "wartet",
    loading: "lädt…",
    no_folders: "keine Unterordner",
    folder_error: "Ordner nicht lesbar",
  };

  const EN = {
    connecting: "Connecting to the Amiga…",
    offline: "The Amiga is not answering. Trying again…",
    disconnected: "Lost the connection to amigent. Trying again…",
    state_offline: "not connected",
    fit: "Fit to window",
    fullscreen: "Full screen",
    screenshot: "Save screenshot",
    restart: "Restart Amiga…",
    panel: "Show or hide the keyboard panel",
    lamiga: "Left Amiga",
    ramiga: "Right Amiga",
    mod_tip: "Click: next key only. Double-click: lock.",
    restarting: "Restarting the Amiga…",
    restart_failed: "Restart failed",
    exists: "{path} exists. Replace it?",
    saved: "Saved {path}",
    failed: "{path}: {error}",
    queued: "waiting",
    loading: "loading…",
    no_folders: "no folders",
    folder_error: "cannot read this folder",
  };

  const german = (navigator.language || "").toLowerCase().startsWith("de");

  for (const el of document.querySelectorAll("[data-i18n]")) EN[el.dataset.i18n] ??= el.textContent;
  for (const el of document.querySelectorAll("[data-i18n-title]")) EN[el.dataset.i18nTitle] ??= el.title;
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) EN[el.dataset.i18nPlaceholder] ??= el.placeholder;

  function t(key, values = {}) {
    let text = (german && DE[key]) || EN[key] || key;
    for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, value);
    return text;
  }

  if (german) {
    document.documentElement.lang = "de";
    for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
    for (const el of document.querySelectorAll("[data-i18n-title]")) el.title = t(el.dataset.i18nTitle);
    for (const el of document.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = t(el.dataset.i18nPlaceholder);
  }
  for (const el of document.querySelectorAll("[data-i18n-label]")) {
    el.title = t(el.dataset.i18nLabel);
    el.setAttribute("aria-label", el.title);
  }
  for (const el of document.querySelectorAll("[data-mod]")) el.title = `${el.title || el.textContent}. ${t("mod_tip")}`;

  for (const el of document.querySelectorAll("[data-combo]")) {
    el.title = el.dataset.combo.replace("lamiga", "Amiga links").replace("ramiga", "Amiga rechts")
      .replace(/\+(.)$/, (_, k) => "+" + k.toUpperCase());
    if (!german) el.title = el.title.replace("Amiga links", "Left Amiga").replace("Amiga rechts", "Right Amiga");
  }

  const store = {
    get(key, fallback) { try { return localStorage.getItem("amigent." + key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem("amigent." + key, value); } catch { /* private window */ } },
  };

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
    $("state-text").textContent = socketOpen && amigaOnline ? host : t("state_offline");
  }

  function onEvent(m) {
    switch (m.type) {
      case "hello":
        host = m.host;
        document.title = `Amigent · ${host}`;
        if (m.version) $("st-version").textContent = `amigent ${m.version}`;
        amigaOnline = m.online;
        if (m.w) setSize(m.w, m.h);
        showState();
        break;
      case "size":
        setSize(m.w, m.h);
        break;
      case "online":
        amigaOnline = m.online;
        showState();
        break;
      case "stats":
        $("st-size").textContent = `${m.w}×${m.h}`;
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

  function onBinary(buffer) {
    const kind = new Uint8Array(buffer, 0, 1)[0];
    if (kind === 1) drawTiles(buffer);
    else if (kind === 2) {
      createImageBitmap(new Blob([new Uint8Array(buffer, 1)], { type: "image/png" }))
        .then((bitmap) => ctx.drawImage(bitmap, 0, 0));
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
    canvas.setPointerCapture(event.pointerId);
    pendingMove = null;
    send({ t: "move", ...amigaPoint(event) });
    send({ t: "buttons", mask: buttons(event) });
    event.preventDefault();
  });

  canvas.addEventListener("pointerup", (event) => {
    send({ t: "move", ...amigaPoint(event) });
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
    if (event.ctrlKey && !altGraph(event) && event.key.toLowerCase() === "v" && !(sticky | locked)) return; // paste below
    event.preventDefault();
    let q = useSticky();
    if (!altGraph(event)) {
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
      if (!data.folders.length) list.insertAdjacentHTML("beforeend", `<li class="note">${t("no_folders")}</li>`);
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

  function openSend(files) {
    chosen = [...files].filter((f) => f.size >= 0);
    if (!chosen.length) return;
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
    if ($("send-dialog").returnValue !== "send") return;
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
  showState();
  layout();
  connect();
  canvas.focus();
})();
