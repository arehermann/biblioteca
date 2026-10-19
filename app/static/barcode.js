// Reads a book's barcode from a photo, in the browser: native BarcodeDetector when present, zxing otherwise.
// Any <input type="file" data-barcode> fills its form's [name=isbn] with the code and submits the form;
// the form's [data-barcode-status] element shows progress.
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

  async function readBarcode(file) {
    const img = await loadImage(file);
    if ('BarcodeDetector' in window) {
      try {
        const found = await new BarcodeDetector({ formats: ['ean_13', 'ean_8', 'upc_a'] }).detect(img);
        if (found.length) return bookCode(found.map(f => f.rawValue));
      } catch (e) { /* fall back to zxing */ }
    }
    const hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [ZXing.BarcodeFormat.EAN_13, ZXing.BarcodeFormat.EAN_8, ZXing.BarcodeFormat.UPC_A]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    const reader = new ZXing.MultiFormatReader();
    reader.setHints(hints);
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
