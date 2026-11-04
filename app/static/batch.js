// "Varios códigos": reads barcodes from the live camera one after another and keeps a list of ISBNs, so many books
// can be scanned without waiting for a lookup between them. The list lives in localStorage until it is sent, so a
// reload or a locked phone doesn't lose it. Without a camera stream (plain http, or permission denied) each photo
// is read on the phone and added to the list instead.
(() => {
  const KEY = 'isbn-lote';
  const form = document.getElementById('batch-form');
  const $ = sel => form.querySelector(sel);
  const live = $('[data-live]'), video = live.querySelector('video'), flash = $('[data-flash]');
  const startBtn = $('[data-start]'), stopBtn = $('[data-stop]'), photo = $('[data-photo]');
  const statusLine = $('[data-status]'), list = $('[data-queue]'), count = $('[data-count]');
  const submit = $('[data-submit]'), clear = $('[data-clear]'), manual = document.getElementById('manual-isbn');

  let queue = [];
  try { queue = JSON.parse(localStorage.getItem(KEY) || '[]').filter(c => typeof c === 'string'); } catch (e) { queue = []; }

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(queue)); } catch (e) { /* the list still works on this page */ }
  }

  function render() {
    list.replaceChildren(...queue.map(code => {
      const li = document.createElement('li');
      const text = document.createElement('span');
      text.textContent = code;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'link';
      remove.textContent = 'Quitar';
      remove.setAttribute('aria-label', 'Quitar ' + code);
      remove.addEventListener('click', () => { queue = queue.filter(c => c !== code); save(); render(); });
      li.append(text, remove);
      return li;
    }));
    const n = queue.length;
    count.textContent = n ? (n === 1 ? '1 código en la lista' : n + ' códigos en la lista') : 'Lista vacía';
    submit.disabled = !n;
    submit.textContent = n ? (n === 1 ? 'Buscar 1 libro' : 'Buscar ' + n + ' libros') : 'Buscar libros';
    clear.hidden = !n;
  }

  // --- Feedback: a beep and a buzz, so the phone can stay pointed at the books ---------------------------------
  let audio = null;
  function beep(ok) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = ok ? 1320 : 440;
      gain.gain.value = 0.15;
      osc.connect(gain).connect(audio.destination);
      osc.start();
      osc.stop(audio.currentTime + (ok ? 0.09 : 0.18));
    } catch (e) { /* no sound */ }
    if (navigator.vibrate) navigator.vibrate(ok ? 60 : [40, 60, 40]);
  }

  let flashTimer = null;
  function show(text, kind) {
    flash.textContent = text;
    flash.className = 'live-flash on ' + (kind || '');
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { flash.className = 'live-flash'; }, 1600);
  }

  function add(code, from) {
    if (queue.includes(code)) {
      beep(false);
      show('Ya estaba en la lista: ' + code, 'repeat');
      return false;
    }
    queue.push(code);
    save();
    render();
    beep(true);
    show('Sumado: ' + code + ' (' + queue.length + ')', 'ok');
    if (from !== 'camera') statusLine.textContent = 'Sumado ' + code + '. Sigue con el próximo.';
    return true;
  }

  // --- Live camera ------------------------------------------------------------------------------------------
  let stream = null, timer = null, busy = false, lastCode = '', lastAt = 0;
  const canvas = document.createElement('canvas');

  // zxing has to be handed pixels: the part of the frame around the guide box, at a modest size. The video fills
  // its box with object-fit: cover, so only a centered part of the camera's frame is on screen (most of a tall
  // phone frame is cut off); the guide sits in the middle of that visible part.
  function frame() {
    const vw = video.videoWidth, vh = video.videoHeight, bw = video.clientWidth, bh = video.clientHeight;
    if (!vw || !bw || !bh) return null;
    const cover = Math.max(bw / vw, bh / vh);
    const shownW = bw / cover, shownH = bh / cover;
    const sw = shownW * 0.9, sh = shownH * 0.5;
    const sx = (vw - sw) / 2, sy = (vh - sh) / 2;
    const scale = Math.min(1, 960 / sw);
    canvas.width = Math.round(sw * scale);
    canvas.height = Math.round(sh * scale);
    canvas.getContext('2d', { willReadFrequently: true }).drawImage(video, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  async function tick() {
    if (busy || !stream || video.readyState < 2) return;
    busy = true;
    try {
      const code = await Barcode.readSource(video, Barcode.native ? null : frame());
      if (!code) return;
      const now = Date.now();
      // The same book stays in view for a moment after its beep; don't report it again and again.
      if (code === lastCode && now - lastAt < 3000) { lastAt = now; return; }
      lastCode = code; lastAt = now;
      if (!Barcode.isIsbn(code)) { show('Ese código no es un ISBN: ' + code, 'repeat'); return; }
      add(code, 'camera');
    } finally {
      busy = false;
    }
  }

  async function start() {
    statusLine.textContent = 'Abriendo la cámara…';
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
      stream = await mainBackCamera(stream);
    } catch (e) {
      stream = null;
      usePhotos('No pude abrir la cámara (¿falta el permiso?). Puedes sacar una foto por libro: cada una se suma a la lista.');
      return;
    }
    video.srcObject = stream;
    await video.play().catch(() => {});
    await tuneCamera();
    live.hidden = false;
    live.scrollIntoView({ block: 'start', behavior: 'smooth' });
    startBtn.hidden = true;
    stopBtn.hidden = false;
    statusLine.textContent = 'Pon el código dentro del recuadro, horizontal, a un palmo de distancia. Si se ve borroso, ' +
                             'aleja un poco el libro o toca la imagen para enfocar. Cuando suene, pasa al siguiente.';
    timer = setInterval(tick, Barcode.native ? 120 : 220);
  }

  // Phones often open the camera focused far away, and can't focus closer than ~10 cm at all. Where the browser
  // lets us (Chrome on Android), ask for continuous autofocus and a little zoom, so the book can be held at a
  // comfortable distance and still fill the guide box. Safari ignores these, and higher resolution is what helps there.
  function track() { return stream && stream.getVideoTracks()[0]; }
  function caps() {
    const t = track();
    try { return (t && t.getCapabilities && t.getCapabilities()) || {}; } catch (e) { return {}; }
  }

  async function tuneCamera() {
    const c = caps(), wanted = {};
    if ((c.focusMode || []).includes('continuous')) wanted.focusMode = 'continuous';
    if (c.zoom && c.zoom.max >= 1.5) wanted.zoom = Math.min(2, c.zoom.max);
    if (!Object.keys(wanted).length) return;
    try { await track().applyConstraints({ advanced: [wanted] }); } catch (e) { /* keep the defaults */ }
  }

  // Android phones with several back cameras label them "camera2 0, facing back", "camera2 2, facing back", …
  // facingMode can land on the ultra-wide one, which can't focus up close. The lowest number is the main camera.
  async function mainBackCamera(current) {
    try {
      const backs = (await navigator.mediaDevices.enumerateDevices())
        .filter(d => d.kind === 'videoinput' && /facing back/i.test(d.label))
        .map(d => ({ id: d.deviceId, n: +((d.label.match(/(\d+), facing back/i) || [])[1] ?? 99) }))
        .sort((a, b) => a.n - b.n);
      const now = current.getVideoTracks()[0].getSettings().deviceId;
      if (backs.length < 2 || backs[0].id === now) return current;
      current.getTracks().forEach(t => t.stop());
      return await navigator.mediaDevices.getUserMedia({
        audio: false, video: { deviceId: { exact: backs[0].id }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
    } catch (e) {
      return current.active ? current : navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: 'environment' } });
    }
  }

  // Tap the picture to focus there, then go back to continuous focus.
  video.addEventListener('click', async e => {
    const c = caps();
    const modes = c.focusMode || [];
    if (!modes.includes('single-shot') && !modes.includes('continuous')) return;
    const r = video.getBoundingClientRect();
    const point = { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
    try {
      await track().applyConstraints({ advanced: [{ focusMode: modes.includes('single-shot') ? 'single-shot' : 'continuous',
                                                    pointsOfInterest: [point] }] });
      show('Enfocando…', 'ok');
      if (modes.includes('single-shot') && modes.includes('continuous')) {
        setTimeout(() => { if (track()) track().applyConstraints({ advanced: [{ focusMode: 'continuous' }] }).catch(() => {}); }, 1500);
      }
    } catch (err) { /* this camera can't focus on a point */ }
  });

  function stop() {
    clearInterval(timer);
    timer = null;
    if (stream) stream.getTracks().forEach(t => t.stop());
    stream = null;
    video.srcObject = null;
    live.hidden = true;
    startBtn.hidden = false;
    stopBtn.hidden = true;
  }

  function usePhotos(message) {
    startBtn.hidden = true;
    stopBtn.hidden = true;
    photo.hidden = false;
    photo.classList.add('primary', 'big-pick');
    statusLine.textContent = message;
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', () => { stop(); statusLine.textContent = 'Cámara en pausa.'; });
  document.addEventListener('visibilitychange', () => { if (document.hidden && stream) stop(); });

  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    usePhotos('La cámara continua necesita abrir la app con https (por ejemplo con Tailscale Serve). ' +
              'Mientras tanto, saca una foto por libro: se lee en el celular y se suma a la lista sin esperar.');
  }

  // --- One photo per book, when there is no live camera -----------------------------------------------------
  const photoInput = photo.querySelector('input');
  photoInput.addEventListener('change', async () => {
    if (!photoInput.files.length) return;
    statusLine.textContent = 'Leyendo el código…';
    try {
      const code = await Barcode.readFile(photoInput.files[0]);
      if (Barcode.isIsbn(code)) add(code, 'photo');
      else statusLine.textContent = 'Ese código no es un ISBN (' + code + '). Prueba con el código de la contratapa.';
    } catch (e) {
      statusLine.textContent = 'No pude leer el código. Prueba con otra foto más de cerca y con buena luz, o escribe el ISBN.';
    }
    photoInput.value = '';
  });

  // --- Typed ISBNs --------------------------------------------------------------------------------------------
  function addTyped() {
    const code = manual.value.replace(/[^0-9Xx]/g, '').toUpperCase();
    if (!code) return;
    // ISBN-10s are turned into ISBN-13 on the server; here they only need the right length.
    if (!(Barcode.isIsbn(code) || /^\d{9}[\dX]$/.test(code))) {
      statusLine.textContent = 'Ese número no parece un ISBN. Revisa que sea el que empieza con 978 o 979.';
      return;
    }
    add(code, 'typed');
    manual.value = '';
    manual.focus();
  }
  $('[data-manual]').addEventListener('click', addTyped);
  manual.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addTyped(); } });

  clear.addEventListener('click', () => {
    if (!confirm('¿Vaciar la lista de ' + queue.length + ' códigos?')) return;
    queue = [];
    save();
    render();
  });

  form.addEventListener('submit', e => {
    if (!queue.length) { e.preventDefault(); return; }
    stop();
    form.querySelector('[name=isbns]').value = queue.join('\n');
    submit.disabled = true;
    submit.textContent = 'Buscando ' + queue.length + (queue.length === 1 ? ' libro…' : ' libros…') + ' puede tardar un minuto';
    // The list is emptied by the review page once the server has them, so a failed send keeps it.
  });

  render();
})();
