// Bestätigungsdialog für Formulare mit data-confirm (ohne Inline-Skript wegen CSP)
document.querySelectorAll('form[data-confirm]').forEach(function (form) {
  form.addEventListener('submit', function (event) {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
