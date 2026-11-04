// Cover photos for Claude (see _cover_photo.html): after the cover, offer a photo of the copyright page, which has
// the publisher, year and ISBN, or reading right away. Either way the form is sent once, with a waiting message.
document.querySelectorAll('[data-cover]').forEach(box => {
  const front = box.querySelector('[data-cover-front]');
  const legal = box.querySelector('[data-cover-legal]');
  const next = box.querySelector('[data-cover-next]');
  const status = box.querySelector('[data-cover-status]');
  const form = front.form;
  const say = text => { if (status) status.textContent = text; };

  form.addEventListener('submit', event => {
    if (form.dataset.sending) { event.preventDefault(); return; }
    form.dataset.sending = '1';
    next.hidden = true;
    say('Claude está leyendo la foto…');
  });
  front.addEventListener('change', () => {
    if (!front.files.length) return;
    next.hidden = false;
    say('Portada lista. Si sumas la página legal (la de los datos de impresión), salen la editorial, el año y el ISBN.');
  });
  legal.addEventListener('change', () => { if (legal.files.length) form.requestSubmit(); });
});
