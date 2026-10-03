// E-Mail-Liste für das Formular "Neue Abstimmung" (Einfügen, Prüfen, Entfernen)
(function () {
  var form = document.getElementById('election-form');
  var emails = JSON.parse(form.dataset.emails || '[]');
  var removeLabel = form.dataset.removeLabel || '×';
  var EMAIL_FULL = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
  var EMAIL_ANY = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g;

  function extract(text) {
    return (text.match(EMAIL_ANY) || []).map(function (m) { return m.toLowerCase(); });
  }

  function validateSubmit() {
    var ok = emails.length > 0 && emails.every(function (e) { return EMAIL_FULL.test(e); });
    document.getElementById('submit-btn').disabled = !ok;
  }

  function markValidity(input) {
    input.classList.toggle('invalid', !!input.value && !EMAIL_FULL.test(input.value));
  }

  function render() {
    var list = document.getElementById('email-list');
    list.innerHTML = '';
    emails.forEach(function (email, i) {
      var li = document.createElement('li');
      var input = document.createElement('input');
      input.type = 'text';
      input.value = email;
      input.addEventListener('input', function () {
        emails[i] = input.value.trim().toLowerCase();
        markValidity(input);
        validateSubmit();
      });
      markValidity(input);
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove';
      remove.textContent = '✕';
      remove.setAttribute('aria-label', removeLabel);
      remove.addEventListener('click', function () { emails.splice(i, 1); render(); });
      li.appendChild(input);
      li.appendChild(remove);
      list.appendChild(li);
    });
    validateSubmit();
  }

  document.getElementById('take-over').addEventListener('click', function () {
    var area = document.getElementById('paste_area');
    extract(area.value).forEach(function (e) { if (emails.indexOf(e) < 0) emails.push(e); });
    area.value = '';
    render();
  });
  document.getElementById('add-row').addEventListener('click', function () { emails.push(''); render(); });
  form.addEventListener('submit', function (event) {
    document.getElementById('emails_raw').value = emails.join('\n');
    if (!emails.length) event.preventDefault();
  });
  // Standardzeiten: Beginn morgen 00:00, Ende am letzten Tag 23:59
  function pad(n) { return String(n).padStart(2, '0'); }
  function fmt(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  var startEl = document.getElementById('starts_at');
  var endEl = document.getElementById('ends_at');
  var days = parseInt(form.dataset.defaultDays || '7', 10);
  if (!startEl.value) {
    var start = new Date();
    start.setDate(start.getDate() + 1);
    start.setHours(0, 0, 0, 0);
    var end = new Date(start);
    end.setDate(end.getDate() + days - 1);
    end.setHours(23, 59, 0, 0);
    startEl.value = fmt(start);
    endEl.value = fmt(end);
  }
  document.getElementById('start-now').addEventListener('click', function () {
    startEl.value = fmt(new Date());
  });

  render();
})();
