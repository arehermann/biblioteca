// Reads a book's barcode from a photo, in the browser: native BarcodeDetector when present, zxing otherwise.
// Any <input type="file" data-barcode> fills its form's [name=isbn] with the code and submits the form;
// the form's [data-barcode-status] element shows progress. window.Barcode exposes the readers for other pages.
(() => {
  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = URL.createObjectURL(file);
    });
  }

  function drawn(img, size, turn) {
    const scale = Math.min(1, size / Math.max(img.naturalWidth, img.naturalHeight));
    const w = Math.round(img.naturalWidth * scale), h = Math.round(img.naturalHeight * scale);
    const canvas = document.createElement('canvas');
    canvas.width = turn ? h : w;
    canvas.height = turn ? w : h;
    const ctx = canvas.getContext('2d');
    if (turn) { ctx.translate(h, 0); ctx.rotate(Math.PI / 2); }
    ctx.drawImage(img, 0, 0, w, h);
    return canvas;
  }

  // Book barcodes are EAN-13 starting with 978/979; a second, smaller code next to it is often the price.
  const bookCode = codes => codes.find(c => /^97[89]\d{10}$/.test(c)) || codes[0];

  const FORMATS = ['ean_13', 'ean_8', 'upc_a'];
  let detector = null;
  if ('BarcodeDetector' in window) {
    try { detector = new BarcodeDetector({ formats: FORMATS }); } catch (e) { /* zxing only */ }
  }

  function zxingReader(tryHarder) {
    const hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [ZXing.BarcodeFormat.EAN_13, ZXing.BarcodeFormat.EAN_8, ZXing.BarcodeFormat.UPC_A]);
    if (tryHarder) hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    const reader = new ZXing.MultiFormatReader();
    reader.setHints(hints);
    return reader;
  }

  // Whatever code is in an image, video or canvas right now, or null. Quick enough to call on every video frame.
  let liveReader = null;
  async function readSource(source, canvas) {
    if (detector) {
      try {
        const found = await detector.detect(source);
        return found.length ? bookCode(found.map(f => f.rawValue)) : null;
      } catch (e) { /* fall back to zxing */ }
    }
    if (!canvas) return null;
    liveReader = liveReader || zxingReader(false);
    try {
      const lum = new ZXing.HTMLCanvasElementLuminanceSource(canvas);
      return liveReader.decode(new ZXing.BinaryBitmap(new ZXing.HybridBinarizer(lum))).getText();
    } catch (e) {
      return null;
    } finally {
      liveReader.reset();
    }
  }

  async function readBarcode(file) {
    const img = await loadImage(file);
    if (detector) {
      try {
        const found = await detector.detect(img);
        if (found.length) return bookCode(found.map(f => f.rawValue));
      } catch (e) { /* fall back to zxing */ }
    }
    const reader = zxingReader(true);
    for (const size of [1400, 900, 2200]) {
      for (const turn of [false, true]) {
        const source = new ZXing.HTMLCanvasElementLuminanceSource(drawn(img, size, turn));
        try {
          return reader.decode(new ZXing.BinaryBitmap(new ZXing.HybridBinarizer(source))).getText();
        } catch (e) { /* try the next size or orientation */ }
      }
    }
    throw new Error('sin código');
  }

  // Same rule as lookup.clean_isbn: a 978/979 EAN-13 with a valid check digit.
  function isIsbn(code) {
    if (!/^97[89]\d{10}$/.test(code || '')) return false;
    const sum = [...code.slice(0, 12)].reduce((t, c, i) => t + (i % 2 ? 3 : 1) * +c, 0);
    return (10 - sum % 10) % 10 === +code[12];
  }

  window.Barcode = { readFile: readBarcode, readSource, isIsbn, native: !!detector };

  document.querySelectorAll('input[type=file][data-barcode]').forEach(photo => {
    const form = photo.form;
    const statusLine = form.querySelector('[data-barcode-status]');
    const say = text => { if (statusLine) statusLine.textContent = text; };
    photo.addEventListener('change', async () => {
      if (!photo.files.length) return;
      say('Leyendo el código…');
      try {
        const code = await readBarcode(photo.files[0]);
        form.querySelector('[name=isbn]').value = code;
        say('Código leído: ' + code + '. Buscando el libro…');
        photo.value = '';
        form.submit();
      } catch (e) {
        say('No pude leer el código. Prueba con otra foto más de cerca y con buena luz, o escribe el ISBN.');
        photo.value = '';
      }
    });
  });
})();
