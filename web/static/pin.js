/* The panel's PIN gate, and the api() helper both pages call.
 *
 * Every gated request funnels through api(), so the lock lives in exactly one
 * place: a 401 raises the keypad, and the request that hit it is retried once
 * the PIN lands — so a half-finished action completes instead of being silently
 * dropped. The alternative, each caller checking for itself, is how you end up
 * with three that forgot to.
 *
 * Shared by index.html and admin.html rather than pasted into both. Two copies
 * of a control that decides who may drive a 350 lb base is two chances to fix
 * only one of them.
 */
(function () {
  const PIN_LENGTH = 4;
  let entry = '';
  let asking = null;            // promise open while the keypad is up
  let resolveAsk = null;

  function dom() {
    let wrap = document.getElementById('pinwrap');
    if (wrap) return wrap;
    wrap = document.createElement('div');
    wrap.id = 'pinwrap';
    wrap.innerHTML =
      '<div id="pinbox">' +
        '<h3>Panel locked</h3>' +
        '<div class="sub">Enter the ' + PIN_LENGTH + '-digit PIN</div>' +
        '<div id="pindots"></div><div id="pinerr"></div><div id="pinkeys"></div>' +
      '</div>';
    document.body.appendChild(wrap);
    const dots = wrap.querySelector('#pindots');
    for (let i = 0; i < PIN_LENGTH; i++) dots.appendChild(document.createElement('i'));
    const keys = wrap.querySelector('#pinkeys');
    for (const k of ['1','2','3','4','5','6','7','8','9','Clear','0','Cancel']) {
      const b = document.createElement('button');
      b.textContent = k;
      if (k.length > 1) b.className = 'wide';
      b.onclick = () => key(k);
      keys.appendChild(b);
    }
    return wrap;
  }

  function paintDots() {
    document.querySelectorAll('#pindots i').forEach((el, i) =>
      el.classList.toggle('on', i < entry.length));
  }

  function err(msg) { document.getElementById('pinerr').textContent = msg || ''; }

  function key(k) {
    if (k === 'Cancel') return close(false);
    if (k === 'Clear') { entry = ''; err(''); return paintDots(); }
    if (entry.length >= PIN_LENGTH) return;
    entry += k; err(''); paintDots();
    if (entry.length === PIN_LENGTH) submit();
  }

  async function submit() {
    const pin = entry;
    entry = ''; paintDots();
    // Deliberately not through api(): this IS the unlock, so a 401 here must
    // not recurse into a second keypad.
    let r, data = {};
    try {
      r = await fetch('/api/auth/login', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pin: pin})
      });
      data = await r.json();
    } catch (e) { err('The panel did not answer'); return; }
    if (r.ok && data.unlocked) { close(true); return; }
    err(data.error || 'Wrong PIN');
    if (data.locked_for > 0) lockout(data.locked_for);
  }

  function lockout(seconds) {
    const keys = document.querySelectorAll('#pinkeys button');
    keys.forEach(b => { if (b.textContent !== 'Cancel') b.disabled = true; });
    let left = Math.ceil(seconds);
    const tick = () => {
      err('Too many tries — wait ' + left + 's');
      if (--left < 0) {
        clearInterval(t);
        keys.forEach(b => b.disabled = false);
        err('');
      }
    };
    tick();
    const t = setInterval(tick, 1000);
  }

  function open() {
    if (asking) return asking;          // one keypad, however many 401s land
    dom().classList.add('show');
    entry = ''; paintDots(); err('');
    asking = new Promise(res => { resolveAsk = res; });
    return asking;
  }

  function close(ok) {
    dom().classList.remove('show');
    entry = ''; paintDots();
    const res = resolveAsk;
    asking = null; resolveAsk = null;
    if (res) res(!!ok);
  }

  window.pinOpen = open;
  window.pinLocked = () => !!asking;

  window.api = async function (path, body, retried) {
    const opt = {method: body ? 'POST' : 'GET'};
    if (body) {
      opt.headers = {'Content-Type': 'application/json'};
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(path, opt);
    if (r.status === 401 && !retried) {
      if (await open()) return window.api(path, body, true);
    }
    return r.json();
  };
})();
