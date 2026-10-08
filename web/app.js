'use strict';
(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const stage = $('#stage'), view = $('#view'), canvas = $('#screen');
  const ctx = canvas.getContext('2d', { alpha: false });
  const cursorEl = $('#cursor'), kbd = $('#kbd'), hud = $('#hud'), dot = $('#touch-dot');
  const toolbar = $('#toolbar'), toolbarTab = $('#toolbar-tab');
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const now = () => performance.now();

  // ------------------------------------------------------------------ 偏好设置
  const store = {
    get(k, d) { try { const v = localStorage.getItem('rd.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('rd.' + k, JSON.stringify(v)); } catch (e) { /* 隐私模式 */ } },
  };
  const PRESETS = { saver: { scale: 0.5, quality: 55, fps: 15 }, balanced: { scale: 0.75, quality: 65, fps: 30 }, sharp: { scale: 1, quality: 75, fps: 30 } };
  const prefs = {
    mode: store.get('mode', 'trackpad'),
    speed: store.get('speed', 1.6),
    hud: store.get('hud', false),
    cmdAsCtrl: store.get('cmdAsCtrl', true),       // Windows 电脑：⌘ 默认当 Ctrl
    cmdAsCtrlMac: store.get('cmdAsCtrlMac', false), // Mac 电脑：⌘ 默认就是 ⌘
    toolbar: store.get('toolbar', true),
    privacy: store.get('privacy', true),
    stream: Object.assign({}, PRESETS.balanced, store.get('stream', {})),
  };

  // ------------------------------------------------------------------ 状态
  let ws = null, geom = null, monitors = [], retry = 0, reconnectTimer = null, appStarted = false;
  let drawQueue = Promise.resolve(), lockedShown = false;
  let hostOS = 'win'; // 'win' | 'mac'，由服务端告诉我们
  const cur = { x: 0, y: 0, vis: true, hx: 0, hy: 0, w: 0, h: 0, localUntil: 0, hasShape: false };
  const vt = { fit: 1, zoom: 1, tx: 0, ty: 0, W: 1, H: 1 };
  const stats = { bytes: 0, frames: 0, rtt: null };

  // ------------------------------------------------------------------ 提示 / 覆盖层
  let toastTimer = null;
  function toast(msg, ms = 2600) {
    const el = $('#toast');
    el.textContent = msg; el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, ms);
  }
  function showOverlay(text) { $('#overlay-text').textContent = text; $('#overlay').hidden = false; }
  function hideOverlay() { $('#overlay').hidden = true; lockedShown = false; }

  // ------------------------------------------------------------------ 电脑是 Windows 还是 Mac
  // 快捷键面板、⌘ 的映射、几处提示文字跟着变；Windows 电脑上一切和原来一样
  function cmdAsCtrl() { return hostOS === 'mac' ? prefs.cmdAsCtrlMac : prefs.cmdAsCtrl; }
  let appliedOS = null;
  function applyHostOS(os) {
    hostOS = os === 'mac' ? 'mac' : 'win';
    if (hostOS === appliedOS) return;
    appliedOS = hostOS;
    $$('[data-host]').forEach((el) => { el.hidden = el.dataset.host !== hostOS; });
    $$('[data-mac]').forEach((el) => {
      if (el.dataset.win === undefined) el.dataset.win = el.textContent;
      el.textContent = hostOS === 'mac' ? el.dataset.mac : el.dataset.win;
    });
    $('#opt-cmd').checked = cmdAsCtrl();
  }

  // ------------------------------------------------------------------ 视图变换（缩放/平移）
  function toolbarClear() {
    if (toolbar.hidden) return 0;
    const r = toolbar.getBoundingClientRect();
    return r.bottom + 4;
  }
  function layout() {
    const vv = window.visualViewport;
    const W = vv ? vv.width : innerWidth, vh = vv ? vv.height : innerHeight, top = vv ? vv.offsetTop : 0;
    // 快捷键面板贴在“可见区域”底部（iPad 键盘弹出时也不会被挡住），画面只占剩下的空间
    const keys = $('#keys');
    keys.style.bottom = Math.max(0, innerHeight - vh - top) + 'px';
    const keysH = keys.hidden ? 0 : keys.getBoundingClientRect().height + 16;
    const H = Math.max(120, vh - keysH);
    stage.style.width = W + 'px'; stage.style.height = H + 'px';
    stage.style.top = top + 'px'; stage.style.left = (vv ? vv.offsetLeft : 0) + 'px';
    vt.W = W; vt.H = H;
    if (!geom) return;
    vt.fit = Math.min(W / geom.sw, H / geom.sh);
    clampView(); applyView(); renderCursor();
  }
  const scaleNow = () => vt.fit * vt.zoom;
  function clampView() {
    const s = scaleNow(), cw = geom.sw * s, ch = geom.sh * s;
    if (cw <= vt.W + 0.5) vt.tx = (vt.W - cw) / 2; else vt.tx = clamp(vt.tx, vt.W - cw, 0);
    if (ch <= vt.H + 0.5) {
      // 画面比屏幕矮时，尽量往下放，别被工具栏挡住
      const centered = (vt.H - ch) / 2;
      vt.ty = Math.max(centered, Math.min(vt.H - ch, toolbarClear()));
    } else vt.ty = clamp(vt.ty, vt.H - ch, 0);
  }
  function applyView() { view.style.transform = `translate(${vt.tx}px, ${vt.ty}px) scale(${scaleNow()})`; }
  function stagePt(t) { const r = stage.getBoundingClientRect(); return [t.clientX - r.left, t.clientY - r.top]; }
  function stageToRemote(p) {
    const s = scaleNow();
    const cx = (p[0] - vt.tx) / s, cy = (p[1] - vt.ty) / s;
    return [Math.round(clamp(cx * geom.width / geom.sw, 0, geom.width - 1)),
            Math.round(clamp(cy * geom.height / geom.sh, 0, geom.height - 1))];
  }
  function remoteToStage(x, y) {
    const s = scaleNow();
    return [vt.tx + x * geom.sw / geom.width * s, vt.ty + y * geom.sh / geom.height * s];
  }
  function followCursor() {
    if (vt.zoom <= 1.01 || !geom) return;
    const [sx, sy] = remoteToStage(cur.x, cur.y);
    const mx = vt.W * 0.15, my = vt.H * 0.15;
    if (sx < mx) vt.tx += mx - sx; else if (sx > vt.W - mx) vt.tx -= sx - (vt.W - mx);
    if (sy < my) vt.ty += my - sy; else if (sy > vt.H - my) vt.ty -= sy - (vt.H - my);
    clampView(); applyView();
  }

  // ------------------------------------------------------------------ 光标
  function renderCursor() {
    if (!geom || !cur.vis || !cur.hasShape) { cursorEl.hidden = true; return; }
    cursorEl.hidden = false;
    const k = geom.sw / geom.width, s = scaleNow();
    const shown = cur.h * k * s;
    const boost = shown > 0 && shown < 20 ? 20 / shown : 1; // 太小看不清时放大一点
    cursorEl.style.width = (cur.w * k * boost) + 'px';
    cursorEl.style.height = (cur.h * k * boost) + 'px';
    cursorEl.style.transform = `translate(${(cur.x - cur.hx * boost) * k}px, ${(cur.y - cur.hy * boost) * k}px)`;
  }
  function setLocalCursor(r) { cur.x = r[0]; cur.y = r[1]; cur.localUntil = now() + 400; renderCursor(); }
  function showDot(p) { dot.hidden = false; dot.style.left = p[0] + 'px'; dot.style.top = p[1] + 'px'; }
  function hideDot() { dot.hidden = true; dot.classList.remove('hold'); }

  // ------------------------------------------------------------------ 发送（鼠标移动按帧合并）
  function sendRaw(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
  const pend = { rx: 0, ry: 0, ax: null, ay: null, wx: 0, wy: 0, scheduled: false };
  function flush() {
    pend.scheduled = false;
    if (pend.ax !== null) { sendRaw({ t: 'mm', x: pend.ax, y: pend.ay }); pend.ax = pend.ay = null; }
    if (pend.rx || pend.ry) { sendRaw({ t: 'mr', dx: +pend.rx.toFixed(2), dy: +pend.ry.toFixed(2) }); pend.rx = pend.ry = 0; }
    const wx = Math.trunc(pend.wx), wy = Math.trunc(pend.wy);
    if (wx || wy) { sendRaw({ t: 'wh', dx: wx, dy: wy }); pend.wx -= wx; pend.wy -= wy; }
  }
  function schedule() { if (!pend.scheduled) { pend.scheduled = true; requestAnimationFrame(flush); } }
  function send(o) { flush(); sendRaw(o); }

  // ---- 粘滞修饰键（快捷键面板里的 Ctrl/Shift/Alt/Win）
  const sticky = new Map();
  function modsDown() { const m = [...sticky.keys()]; m.forEach((c) => send({ t: 'key', code: c, d: true })); return m; }
  function modsUp(m) { m.slice().reverse().forEach((c) => send({ t: 'key', code: c, d: false })); clearSticky(); }
  function stickyUI() {
    $$('#keys .mod').forEach((b) => { const st = sticky.get(b.dataset.mod); b.classList.toggle('on', st === 'on'); b.classList.toggle('lock', st === 'lock'); });
  }
  function clearSticky() { // 用过一次就松开（双击锁定的除外）
    for (const [c, st] of sticky) if (st !== 'lock') sticky.delete(c);
    stickyUI();
  }
  function click(b = 0, n = 1, at = null) {
    const m = modsDown();
    send(Object.assign({ t: 'click', b, n }, at ? { x: at[0], y: at[1] } : {}));
    modsUp(m);
  }
  let heldMods = null;
  function buttonDown(b, at = null) {
    heldMods = modsDown();
    send(Object.assign({ t: 'mb', b, d: true }, at ? { x: at[0], y: at[1] } : {}));
  }
  function buttonUp(b, at = null) {
    send(Object.assign({ t: 'mb', b, d: false }, at ? { x: at[0], y: at[1] } : {}));
    if (heldMods) { modsUp(heldMods); heldMods = null; }
  }
  function pressCombo(codes) {
    const mods = [...sticky.keys()].filter((c) => !codes.includes(c));
    send({ t: 'combo', codes: [...mods, ...codes] });
    clearSticky();
  }

  // ------------------------------------------------------------------ 触摸手势
  const TAP_MOVE = 10, LONG_MS = 480, DTAP_MS = 250;
  let g = null, lastTap = { t: 0, p: [0, 0] }, inertia = null;

  function touchList(e) { return Array.from(e.touches).map((t) => ({ p: stagePt(t), stylus: t.touchType === 'stylus' })); }

  function relMove(dx, dy, dt) {
    const s = scaleNow(), rpc = geom.width / (geom.sw * s); // 每个屏幕点对应的远端像素
    const v = Math.hypot(dx, dy) / Math.max(dt, 1);
    const k = prefs.speed * (1 + Math.min(v, 2.5) * 0.55) * rpc;
    const rx = dx * k, ry = dy * k;
    pend.rx += rx; pend.ry += ry; schedule();
    cur.x = clamp(cur.x + rx, 0, geom.width - 1); cur.y = clamp(cur.y + ry, 0, geom.height - 1);
    cur.localUntil = now() + 400;
    renderCursor(); followCursor();
  }
  function absMove(p) {
    const r = stageToRemote(p);
    pend.ax = r[0]; pend.ay = r[1]; schedule();
    setLocalCursor(r);
  }
  function scrollBy(dx, dy) {
    const s = scaleNow(), rpc = geom.width / (geom.sw * s), k = 1.15 * rpc;
    pend.wy += dy * k; pend.wx -= dx * k; schedule();
  }
  function stopInertia() { if (inertia) cancelAnimationFrame(inertia); inertia = null; }
  function startInertia(T) {
    const t1 = now(), recent = (T.samples || []).filter((s) => t1 - s.t < 90);
    if (recent.length < 2) return;
    const span = Math.max(16, t1 - recent[0].t);
    let vx = recent.reduce((a, s) => a + s.dx, 0) / span, vy = recent.reduce((a, s) => a + s.dy, 0) / span;
    if (Math.hypot(vx, vy) < 0.3) return;
    let last = now();
    const step = (t) => {
      const dt = Math.min(50, t - last); last = t;
      scrollBy(vx * dt, vy * dt);
      const decay = Math.pow(0.9965, dt); vx *= decay; vy *= decay;
      inertia = Math.hypot(vx, vy) < 0.04 ? null : requestAnimationFrame(step);
    };
    inertia = requestAnimationFrame(step);
  }

  function onLong() {
    if (!g || g.kind !== null || g.n !== 1) return;
    if (g.direct) { g.kind = 'long'; dot.classList.add('hold'); click(2, 1, stageToRemote(g.start)); }
    else { g.kind = 'drag'; buttonDown(0); toast('拖拽中…松手结束', 1200); }
  }

  stage.addEventListener('touchstart', (e) => {
    e.preventDefault();
    focusRemoteSurface(); // touchstart is a user gesture; focus does not invoke the software keyboard
    if (!geom) return;
    stopInertia();
    const list = touchList(e), t = now();
    if (g && list.length === 1) { // 上一个手势没收到 touchend（系统打断等），先收尾
      clearTimeout(g.longTimer);
      clearTimeout(g.uiTimer);
      if (g.kind === 'drag') buttonUp(0);
      g = null;
    }
    if (!g) {
      const p = list[0].p, direct = prefs.mode === 'direct' || list[0].stylus;
      g = { t0: t, n: list.length, kind: null, direct, start: p, last: p, lastT: t, two: null,
            tapDrag: !direct && t - lastTap.t < DTAP_MS && dist(p, lastTap.p) < 45 };
      if (list.length === 1) g.longTimer = setTimeout(onLong, LONG_MS);
      if (direct) showDot(p);
    }
    g.n = Math.max(g.n, list.length);
    if (list.length >= 2 && (g.kind === null || (g.kind === 'move' && t - g.t0 < 220))) {
      clearTimeout(g.longTimer);
      g.kind = null;
      const a = list[0].p, b = list[1].p;
      g.two = { m0: mid(a, b), d0: Math.max(dist(a, b), 1), m: mid(a, b), t: t, zoom0: vt.zoom, tx0: vt.tx, ty0: vt.ty, samples: [] };
      hideDot();
      // 双指按住不动 0.8 秒 = 打开/收起工具栏（全屏时找不到工具栏也能用）
      const gg = g;
      clearTimeout(g.uiTimer);
      g.uiTimer = setTimeout(() => { if (g === gg && g.kind === null && g.n === 2) { g.kind = 'ui'; toggleToolbar(); } }, 800);
    }
  }, { passive: false });

  stage.addEventListener('touchmove', (e) => {
    e.preventDefault();
    if (!g || !geom) return;
    const list = touchList(e), t = now();
    if (g.two) {
      if (list.length < 2) return; // 双指手势途中抬起一指：忽略
      const T = g.two, a = list[0].p, b = list[1].p, m = mid(a, b), d = dist(a, b);
      if (g.kind === null) {
        const dm = dist(m, T.m0), dd = Math.abs(d - T.d0);
        if (dm < 8 && dd < 8) return;
        g.kind = dd >= 8 && dd > dm * 0.9 ? 'pinch' : 'scroll';
      }
      if (g.kind === 'scroll') {
        const dx = m[0] - T.m[0], dy = m[1] - T.m[1];
        scrollBy(dx, dy);
        T.samples.push({ t, dx, dy }); if (T.samples.length > 8) T.samples.shift();
        T.m = m; T.t = t;
      } else if (g.kind === 'pinch') {
        const s0 = vt.fit * T.zoom0, cx = (T.m0[0] - T.tx0) / s0, cy = (T.m0[1] - T.ty0) / s0;
        vt.zoom = clamp(T.zoom0 * d / T.d0, 1, 6);
        const s = scaleNow();
        vt.tx = m[0] - cx * s; vt.ty = m[1] - cy * s;
        clampView(); applyView(); renderCursor();
      }
      return;
    }
    const p = list[0].p;
    let dx = p[0] - g.last[0], dy = p[1] - g.last[1];
    const dt = t - g.lastT;
    g.last = p; g.lastT = t;
    if (g.kind === null) {
      if (dist(p, g.start) < TAP_MOVE) return;
      clearTimeout(g.longTimer);
      dx = p[0] - g.start[0]; dy = p[1] - g.start[1];
      if (g.direct) { g.kind = 'drag'; const r = stageToRemote(g.start); setLocalCursor(r); buttonDown(0, r); }
      else if (g.tapDrag) { g.kind = 'drag'; buttonDown(0); }
      else g.kind = 'move';
    }
    if (g.kind === 'move' || (g.kind === 'drag' && !g.direct)) relMove(dx, dy, dt);
    else if (g.kind === 'drag') absMove(p);
    if (g.direct) showDot(p);
  }, { passive: false });

  function onTouchEnd(e) {
    e.preventDefault();
    if (!g) return;
    if (e.touches.length > 0) return;
    clearTimeout(g.longTimer);
    clearTimeout(g.uiTimer);
    const gg = g, dur = now() - gg.t0;
    g = null; hideDot();
    if (gg.kind === null) {
      if (gg.n === 1 && dur < 450) {
        if (gg.direct) { const r = stageToRemote(gg.start); setLocalCursor(r); click(0, 1, r); }
        else { click(0, 1); lastTap = { t: now(), p: gg.start }; }
      } else if (gg.n === 2 && dur < 400) {
        click(2, 1, gg.direct && gg.two ? stageToRemote(gg.two.m0) : null);
      } else if (gg.n >= 3 && dur < 450) {
        click(1, 1);
      }
      return;
    }
    if (gg.kind === 'drag') buttonUp(0);
    else if (gg.kind === 'scroll') startInertia(gg.two);
  }
  stage.addEventListener('touchend', onTouchEnd, { passive: false });
  stage.addEventListener('touchcancel', onTouchEnd, { passive: false });

  // ------------------------------------------------------------------ 外接鼠标 / 触控板
  const btnOf = (e) => (e.button === 2 ? 2 : e.button === 1 ? 1 : 0);
  stage.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'mouse') return;
    focusRemoteSurface();
    if (!geom) return;
    e.preventDefault();
    const r = stageToRemote(stagePt(e)); setLocalCursor(r); buttonDown(btnOf(e), r);
  });
  stage.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse' && geom) absMove(stagePt(e)); });
  stage.addEventListener('pointerup', (e) => {
    if (e.pointerType !== 'mouse' || !geom) return;
    buttonUp(btnOf(e), stageToRemote(stagePt(e)));
  });
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!geom) return;
    if (e.ctrlKey) { // 触控板捏合
      vt.zoom = clamp(vt.zoom * Math.exp(-e.deltaY * 0.01), 1, 6); clampView(); applyView(); renderCursor(); return;
    }
    const k = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 400 : 1;
    pend.wy -= e.deltaY * k * 1.2; pend.wx += e.deltaX * k * 1.2; schedule();
  }, { passive: false });
  stage.addEventListener('contextmenu', (e) => e.preventDefault());
  ['gesturestart', 'gesturechange', 'gestureend', 'dblclick'].forEach((ev) => document.addEventListener(ev, (e) => e.preventDefault(), { passive: false }));

  // ------------------------------------------------------------------ 键盘
  // A focusable remote surface gives Safari a real keyboard event target
  // without opening the iPad software keyboard.
  function focusRemoteSurface() {
    if (!appStarted || anyDialogOpen()) return;
    if (document.activeElement === stage) return;
    try { stage.focus({ preventScroll: true }); } catch (e) { stage.focus(); }
  }
  const SENT = '​';
  let composing = false;
  function resetKbd() { kbd.value = SENT; try { kbd.setSelectionRange(1, 1); } catch (e) { /* ignore */ } }
  const PUNCT = { ' ': 'Space', '-': 'Minus', '=': 'Equal', '[': 'BracketLeft', ']': 'BracketRight', '\\': 'Backslash', ';': 'Semicolon',
    "'": 'Quote', '`': 'Backquote', ',': 'Comma', '.': 'Period', '/': 'Slash' };
  function charToCode(ch) {
    if (/^[a-z]$/i.test(ch)) return 'Key' + ch.toUpperCase();
    if (/^[0-9]$/.test(ch)) return 'Digit' + ch;
    return PUNCT[ch] || null;
  }
  function flushKbd() {
    if (composing) return;
    const text = kbd.value.split(SENT).join('');
    resetKbd();
    if (!text) return;
    if (sticky.size && text.length === 1 && charToCode(text)) { pressCombo([charToCode(text)]); return; }
    send({ t: 'text', s: text });
  }
  kbd.addEventListener('compositionstart', () => { composing = true; });
  kbd.addEventListener('compositionend', () => { composing = false; setTimeout(flushKbd, 0); });
  kbd.addEventListener('input', (e) => {
    if (composing || e.isComposing) return;
    if (e.inputType === 'deleteContentBackward') { pressCombo(['Backspace']); resetKbd(); return; }
    if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') { pressCombo(['Enter']); resetKbd(); return; }
    flushKbd();
  });
  const SPECIAL = new Set(['Enter', 'Backspace', 'Tab', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
    'Delete', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'CapsLock']);
  const KEY_CODE = { Enter: 'Enter', Backspace: 'Backspace', Tab: 'Tab', Escape: 'Escape', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight',
    ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', Delete: 'Delete', Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
    Insert: 'Insert', CapsLock: 'CapsLock', UIKeyInputEscape: 'Escape', UIKeyInputUpArrow: 'ArrowUp', UIKeyInputDownArrow: 'ArrowDown',
    UIKeyInputLeftArrow: 'ArrowLeft', UIKeyInputRightArrow: 'ArrowRight' };
  function onKeyDown(e) {
    if (e.isComposing || e.keyCode === 229 || composing) return;
    if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
    const ctrl = e.ctrlKey || (cmdAsCtrl() && e.metaKey);
    const win = e.metaKey && !cmdAsCtrl(); // MetaLeft：Windows 上是 Win 键，Mac 上是 ⌘
    const special = SPECIAL.has(e.key) || /^F\d{1,2}$/.test(e.key) || e.key in KEY_CODE;
    if (!ctrl && !e.altKey && !win && !special) return; // 普通字符交给 input 事件（支持中文输入法）
    e.preventDefault();
    let code = e.code && e.code !== 'Unidentified' ? e.code : (KEY_CODE[e.key] || (/^F\d+$/.test(e.key) ? e.key : charToCode(e.key.toLowerCase())));
    if (!code) return;
    const codes = [];
    if (ctrl) codes.push('ControlLeft');
    if (e.altKey) codes.push('AltLeft');
    if (e.shiftKey) codes.push('ShiftLeft');
    if (win) codes.push('MetaLeft');
    codes.push(code);
    pressCombo(codes);
  }
  kbd.addEventListener('keydown', onKeyDown);
  // Catch hardware-keyboard shortcuts in the capture phase, before Safari's
  // focused canvas/remote surface has any opportunity to handle navigation.
  // Form controls and toolbar buttons keep their normal local key behaviour.
  document.addEventListener('keydown', (e) => {
    if (!appStarted || !ws || ws.readyState !== 1 || document.hidden || anyDialogOpen()) return;
    if (e.target === kbd) return; // IME, deletion and composition use the textarea handlers above.
    if (e.target instanceof Element && e.target.closest('input, textarea, select, button, a, [contenteditable]')) return;
    if (composing || e.isComposing || e.keyCode === 229 || e.key === 'Dead' || e.key === 'Process') return;
    if (e.key.length === 1 && (!e.ctrlKey && !e.metaKey && !e.altKey || e.getModifierState?.('AltGraph'))) {
      e.preventDefault();
      send({ t: 'text', s: e.key });
      return;
    }
    onKeyDown(e); // ⌘ on an iPad maps to Ctrl on Windows, Command on a Mac.
  }, true);
  kbd.addEventListener('focus', () => { setBtn('keyboard', true); resetKbd(); });
  kbd.addEventListener('blur', () => setBtn('keyboard', false));

  // ------------------------------------------------------------------ 画面解码
  async function decode(blob) {
    if (window.createImageBitmap) {
      try { return await createImageBitmap(blob); } catch (e) { /* 走下面的兼容路径 */ }
    }
    return new Promise((res, rej) => {
      const u = URL.createObjectURL(blob), im = new Image();
      im.onload = () => { URL.revokeObjectURL(u); res(im); };
      im.onerror = (err) => { URL.revokeObjectURL(u); rej(err); };
      im.src = u;
    });
  }
  async function drawFrame(buf) {
    const dv = new DataView(buf);
    if (dv.getUint8(0) !== 1) return;
    const id = dv.getUint32(1, true), n = dv.getUint16(5, true);
    try {
      let off = 7; const items = [];
      for (let i = 0; i < n; i++) {
        const x = dv.getUint16(off, true), y = dv.getUint16(off + 2, true), len = dv.getUint32(off + 8, true);
        off += 12;
        items.push({ x, y, blob: new Blob([new Uint8Array(buf, off, len)], { type: 'image/jpeg' }) });
        off += len;
      }
      const imgs = await Promise.all(items.map((it) => decode(it.blob)));
      imgs.forEach((im, i) => { ctx.drawImage(im, items[i].x, items[i].y); if (im.close) im.close(); });
      stats.frames++;
      if (lockedShown || !$('#overlay').hidden) hideOverlay();
    } catch (err) {
      console.warn('frame error', err);
    } finally {
      sendRaw({ t: 'ack', id });
    }
  }
  function onHello(m) {
    const changed = !geom || geom.sw !== m.geom.sw || geom.sh !== m.geom.sh;
    geom = m.geom; monitors = m.monitors || [];
    applyHostOS(m.os);
    if (changed) { canvas.width = geom.sw; canvas.height = geom.sh; ctx.fillStyle = '#000'; ctx.fillRect(0, 0, geom.sw, geom.sh); }
    document.title = '远程桌面 · ' + (m.host || '');
    if (document.activeElement === document.body) focusRemoteSurface();
    setPrivacy(m.privacy);
    layout(); syncSettingsUI();
  }
  let privacyOn = null; // null = 电脑不支持
  function setPrivacy(on) {
    privacyOn = on;
    $('#btn-privacy').hidden = on === null || on === undefined;
    setBtn('privacy', !!on);
  }

  // ------------------------------------------------------------------ 连接
  function wsUrl() {
    const s = prefs.stream, q = new URLSearchParams({ scale: s.scale, quality: s.quality, fps: s.fps, privacy: prefs.privacy ? 1 : 0 });
    if (s.monitor) q.set('monitor', s.monitor);
    return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws?${q}`;
  }
  function connect() {
    clearTimeout(reconnectTimer);
    if (ws && ws.readyState <= 1) return;
    showOverlay(retry ? `连接断开，正在重连…（第 ${retry} 次）` : '正在连接电脑…');
    const sock = new WebSocket(wsUrl());
    ws = sock;
    sock.binaryType = 'arraybuffer';
    sock.onopen = () => { retry = 0; };
    sock.onmessage = (ev) => {
      if (typeof ev.data !== 'string') {
        stats.bytes += ev.data.byteLength;
        drawQueue = drawQueue.then(() => drawFrame(ev.data));
        return;
      }
      const m = JSON.parse(ev.data);
      switch (m.t) {
        case 'hello': drawQueue = drawQueue.then(() => onHello(m)); break;
        case 'c':
          cur.vis = m.v;
          if (now() > cur.localUntil) { cur.x = m.x; cur.y = m.y; }
          renderCursor(); break;
        case 'cs':
          cursorEl.src = 'data:image/png;base64,' + m.png;
          Object.assign(cur, { hx: m.hx, hy: m.hy, w: m.w, h: m.h, hasShape: true }); renderCursor(); break;
        case 'pong': stats.rtt = now() - m.ts; break;
        case 'clip':
          $('#clip-text').value = m.text;
          if (navigator.clipboard && window.isSecureContext && m.text) navigator.clipboard.writeText(m.text).then(() => toast('已复制到 iPad 剪贴板'), () => toast('已读取，可在框里长按复制'));
          else toast(m.text ? '已读取，可在框里长按复制' : '电脑剪贴板是空的');
          break;
        case 'toast': toast(m.msg, m.ms || 2600); break;
        case 'privacy': setPrivacy(m.on); if (m.msg) toast(m.msg, 3500); break;
        case 'locked':
          lockedShown = true;
          showOverlay(hostOS === 'mac' ? 'Mac 已锁屏，暂时看不到画面（需要有人在这台 Mac 前用登录密码解锁）'
            : '电脑当前处于锁屏或系统安全界面（例如管理员权限弹窗），暂时看不到画面');
          break;
      }
    };
    sock.onclose = () => {
      if (ws === sock) ws = null;
      handleClose();
    };
  }
  async function handleClose() {
    try {
      const r = await fetch('/api/me', { cache: 'no-store', credentials: 'same-origin' });
      if (r.status === 401) { showLogin(); return; }
    } catch (e) { /* 网络不通，继续重试 */ }
    if (document.hidden) return; // 切回来时会重连
    retry++;
    let hint = `连接断开，正在重连…（第 ${retry} 次）`;
    if (retry >= 4 && /trycloudflare\.com$/.test(location.hostname)) hint += '\n电脑可能重启过，备用地址已经变了：请看 ntfy 推送里的新地址，或改用 Tailscale 地址。';
    else if (retry >= 4) hint += '\n请确认 iPad 的 Tailscale 已连接；电脑需要开机且没有睡眠。';
    showOverlay(hint);
    reconnectTimer = setTimeout(connect, Math.min(800 * retry, 5000));
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { sendRaw({ t: 'release' }); return; }
    if (appStarted && (!ws || ws.readyState > 1)) { retry = 0; connect(); }
  });
  setInterval(() => sendRaw({ t: 'ping', ts: now() }), 2000);
  let lastStat = now();
  setInterval(() => {
    const t = now(), dt = (t - lastStat) / 1000; lastStat = t;
    if (prefs.hud && geom) {
      hud.textContent = `${stats.rtt == null ? '--' : Math.round(stats.rtt)}ms · ${Math.round(stats.frames / dt)}fps · ${Math.round(stats.bytes / 1024 / dt)}KB/s · ${geom.sw}×${geom.sh}`;
    }
    stats.frames = 0; stats.bytes = 0;
  }, 1000);

  // ------------------------------------------------------------------ 登录
  function showLogin(msg) {
    appStarted = false;
    hideOverlay();
    $('#login').hidden = false;
    $('#login-err').textContent = msg || '';
    setTimeout(() => $('#pw').focus(), 50);
  }
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button'); btn.disabled = true;
    $('#login-err').textContent = '';
    try {
      const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
        body: JSON.stringify({ password: $('#pw').value, remember: $('#remember').checked }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) { $('#login').hidden = true; $('#pw').value = ''; startApp(); }
      else $('#login-err').textContent = j.error || ('登录失败 (' + r.status + ')');
    } catch (err) {
      $('#login-err').textContent = '连不上电脑：' + err.message;
    } finally { btn.disabled = false; }
  });
  function startApp() {
    appStarted = true;
    retry = 0;
    connect();
    focusRemoteSurface();
  }
  async function boot() {
    try {
      const r = await fetch('/api/me', { cache: 'no-store', credentials: 'same-origin' });
      if (r.ok) { applyHostOS((await r.json().catch(() => ({}))).os); startApp(); } else showLogin();
    } catch (e) {
      showOverlay('连不上电脑，3 秒后重试…');
      setTimeout(boot, 3000);
    }
  }

  // ------------------------------------------------------------------ 工具栏 / 面板
  function setBtn(act, on) { const b = toolbar.querySelector(`[data-act="${act}"]`); if (b) b.classList.toggle('on', !!on); }
  function anyDialogOpen() { return $$('.dialog').some((d) => !d.hidden); }
  function openDialog(id) { kbd.blur(); $$('.dialog').forEach((d) => { d.hidden = d.id !== id; }); }
  function closeDialogs() { $$('.dialog').forEach((d) => { d.hidden = true; }); }
  $$('.dialog').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d || e.target.hasAttribute('data-close')) closeDialogs(); }));

  function applyMode() {
    $('#btn-mode').classList.toggle('direct', prefs.mode === 'direct');
    $('#mode-label').textContent = prefs.mode === 'direct' ? '直接点' : '触控板';
  }
  function applyToolbar() {
    toolbar.hidden = !prefs.toolbar; toolbarTab.hidden = prefs.toolbar;
    if (geom) { clampView(); applyView(); renderCursor(); }
  }
  const isStandalone = window.navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  if (isStandalone) $('#btn-fs').hidden = true;

  toolbar.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    switch (b.dataset.act) {
      case 'keyboard':
        if (document.activeElement === kbd) kbd.blur(); else { resetKbd(); kbd.focus(); }
        break;
      case 'keys': { const k = $('#keys'); k.hidden = !k.hidden; setBtn('keys', !k.hidden); layout(); break; }
      case 'mode':
        prefs.mode = prefs.mode === 'direct' ? 'trackpad' : 'direct'; store.set('mode', prefs.mode); applyMode();
        toast(prefs.mode === 'direct' ? '直接点击模式：点哪里就点哪里，长按＝右键' : '触控板模式：滑动移动鼠标，轻点＝左键，双指轻点＝右键');
        break;
      case 'privacy':
        prefs.privacy = !privacyOn; store.set('privacy', prefs.privacy);
        send({ t: 'privacy', on: prefs.privacy });
        break;
      case 'url': openDialog('dlg-url'); renderRecent(); setTimeout(() => $('#url-input').focus(), 50); break;
      case 'clip': openDialog('dlg-clip'); break;
      case 'upload': openDialog('dlg-upload'); break;
      case 'settings': openDialog('dlg-settings'); syncSettingsUI(); break;
      case 'fullscreen': toggleFullscreen(); break;
      case 'hide': toggleToolbar(); break;
    }
  });
  toolbarTab.addEventListener('click', toggleToolbar);
  function toggleToolbar() {
    prefs.toolbar = !prefs.toolbar; store.set('toolbar', prefs.toolbar);
    if (!prefs.toolbar) {
      $('#keys').hidden = true; setBtn('keys', false);
      toast('工具栏已收起：点顶部的小箭头，或双指按住屏幕 1 秒，就能重新打开', 4000);
    }
    applyToolbar(); layout();
  }

  function toggleFullscreen() {
    const d = document, el = d.documentElement;
    if (d.fullscreenElement || d.webkitFullscreenElement) { (d.exitFullscreen || d.webkitExitFullscreen).call(d); return; }
    const req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!req) { toast('这个浏览器不支持网页全屏。可以点 Safari 的“分享 → 添加到主屏幕”，从主屏幕图标打开就是全屏。', 5000); return; }
    try { const p = req.call(el); if (p && p.catch) p.catch(() => toast('全屏被拒绝：可改用“分享 → 添加到主屏幕”', 4000)); } catch (e) { toast('全屏失败'); }
  }
  ['fullscreenchange', 'webkitfullscreenchange'].forEach((ev) => document.addEventListener(ev, () => setTimeout(layout, 120)));

  // ---- 快捷键面板
  let modTapT = {};
  $('#keys').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.mod) {
      const c = b.dataset.mod, st = sticky.get(c), t = now();
      if (!st) sticky.set(c, t - (modTapT[c] || 0) < 350 ? 'lock' : 'on');
      else if (st === 'on' && t - (modTapT[c] || 0) < 350) sticky.set(c, 'lock');
      else sticky.delete(c);
      modTapT[c] = t;
      stickyUI();
      return;
    }
    if (b.dataset.code) { pressCombo([b.dataset.code]); return; }
    if (b.dataset.combo) { pressCombo(b.dataset.combo.split('+')); return; }
    if (b.dataset.launch) { send({ t: 'launch', app: b.dataset.launch }); return; }
    if (b.dataset.act === 'rightclick') click(2);
    else if (b.dataset.act === 'middleclick') click(1);
    else if (b.dataset.act === 'dblclick') click(0, 2);
  });

  // ---- 打开网址
  function renderRecent() {
    const box = $('#url-recent'); box.innerHTML = '';
    store.get('recentUrls', []).forEach((u) => {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = u.replace(/^https?:\/\//, '');
      b.onclick = () => openUrl(u); box.appendChild(b);
    });
  }
  function openUrl(u) {
    u = u.trim(); if (!u) return;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(u)) u = 'https://' + u;
    send({ t: 'open', url: u });
    const list = [u, ...store.get('recentUrls', []).filter((x) => x !== u)].slice(0, 8);
    store.set('recentUrls', list);
    closeDialogs();
  }
  $('#url-form').addEventListener('submit', (e) => { e.preventDefault(); openUrl($('#url-input').value); $('#url-input').value = ''; });

  // ---- 剪贴板
  $('#clip-type').onclick = () => { const s = $('#clip-text').value; if (s) { send({ t: 'text', s }); closeDialogs(); toast('已输入到电脑'); } };
  $('#clip-set').onclick = () => send({ t: 'clip_set', text: $('#clip-text').value });
  $('#clip-get').onclick = () => send({ t: 'clip_get' });

  // ---- 上传
  $('#file-input').addEventListener('change', (e) => {
    const files = Array.from(e.target.files || []); if (!files.length) return;
    const fd = new FormData(); files.forEach((f) => fd.append('file', f, f.name));
    const xhr = new XMLHttpRequest(), bar = $('#upload-bar'), msg = $('#upload-msg');
    bar.hidden = false; bar.firstElementChild.style.width = '0%';
    msg.textContent = `正在上传 ${files.length} 个文件…`;
    xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = (ev.loaded / ev.total * 100).toFixed(1) + '%'; };
    xhr.onload = () => {
      let j = {}; try { j = JSON.parse(xhr.responseText); } catch (err) { /* ignore */ }
      msg.textContent = xhr.status === 200 && j.ok ? `完成！已保存到 ${j.dir}` : '上传失败：' + (j.error || xhr.status);
      e.target.value = '';
    };
    xhr.onerror = () => { msg.textContent = '上传失败：网络错误'; e.target.value = ''; };
    xhr.open('POST', '/api/upload'); xhr.send(fd);
  });

  // ---- 设置
  function syncSettingsUI() {
    const s = prefs.stream;
    const preset = Object.keys(PRESETS).find((k) => PRESETS[k].scale === s.scale && PRESETS[k].quality === s.quality && PRESETS[k].fps === s.fps);
    $$('#preset button').forEach((b) => b.classList.toggle('on', b.dataset.preset === preset));
    $$('#scale button').forEach((b) => b.classList.toggle('on', +b.dataset.v === s.scale));
    $$('#fps button').forEach((b) => b.classList.toggle('on', +b.dataset.v === s.fps));
    $('#quality').value = s.quality; $('#v-quality').textContent = s.quality;
    $('#v-scale').textContent = geom ? `${geom.sw}×${geom.sh}` : '';
    $('#v-fps').textContent = s.fps + ' fps';
    $('#speed').value = prefs.speed; $('#v-speed').textContent = (+prefs.speed).toFixed(1) + 'x';
    $('#opt-hud').checked = prefs.hud; $('#opt-cmd').checked = cmdAsCtrl();
    hud.hidden = !prefs.hud;
    const fm = $('#field-monitor'); fm.hidden = monitors.length < 2;
    if (monitors.length >= 2) {
      const box = $('#monitor'); box.innerHTML = '';
      monitors.forEach((m) => {
        const b = document.createElement('button'); b.textContent = `${m.index}（${m.width}×${m.height}）`;
        b.classList.toggle('on', geom && geom.monitor === m.index);
        b.onclick = () => pushStream({ monitor: m.index }); box.appendChild(b);
      });
    }
  }
  function pushStream(change) {
    Object.assign(prefs.stream, change); store.set('stream', prefs.stream);
    send(Object.assign({ t: 'settings' }, prefs.stream));
    syncSettingsUI();
  }
  $('#preset').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) pushStream(Object.assign({}, PRESETS[b.dataset.preset])); });
  $('#scale').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) pushStream({ scale: +b.dataset.v }); });
  $('#fps').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) pushStream({ fps: +b.dataset.v }); });
  $('#quality').addEventListener('input', (e) => { $('#v-quality').textContent = e.target.value; });
  $('#quality').addEventListener('change', (e) => pushStream({ quality: +e.target.value }));
  $('#speed').addEventListener('input', (e) => { prefs.speed = +e.target.value; store.set('speed', prefs.speed); $('#v-speed').textContent = prefs.speed.toFixed(1) + 'x'; });
  $('#opt-hud').addEventListener('change', (e) => { prefs.hud = e.target.checked; store.set('hud', prefs.hud); hud.hidden = !prefs.hud; });
  $('#opt-cmd').addEventListener('change', (e) => {
    const k = hostOS === 'mac' ? 'cmdAsCtrlMac' : 'cmdAsCtrl';
    prefs[k] = e.target.checked; store.set(k, prefs[k]);
  });
  $('#btn-refresh').onclick = () => { send({ t: 'refresh' }); closeDialogs(); };
  $('#btn-release').onclick = () => { sticky.clear(); stickyUI(); send({ t: 'release' }); toast('已松开所有按键'); };
  $('#btn-logout').onclick = async () => {
    await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
    closeDialogs(); appStarted = false; if (ws) ws.close(); showLogin('已退出登录');
  };

  // ------------------------------------------------------------------ 启动
  window.addEventListener('resize', layout);
  if (window.visualViewport) { visualViewport.addEventListener('resize', layout); visualViewport.addEventListener('scroll', layout); }
  window.addEventListener('orientationchange', () => setTimeout(layout, 300));
  applyMode(); applyToolbar(); resetKbd(); layout(); syncSettingsUI();
  boot();
})();
