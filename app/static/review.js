// Review page: writing a title for a book nobody recognized (a doubtful spine, an unknown ISBN) means you want it
// added, so its box gets ticked, unless you already ticked or unticked it yourself.
document.querySelectorAll('#review-form [data-add]').forEach(input => {
  const box = input.form.elements[input.dataset.add];
  let touched = false;
  box.addEventListener('change', () => { touched = true; });
  input.addEventListener('input', () => { if (input.value.trim() && !touched) box.checked = true; });
});
