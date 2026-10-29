'use strict';
const consentForm = document.querySelector('#oauth-consent');
consentForm?.addEventListener('submit', event => {
  if (consentForm.dataset.submitting === 'true') {
    event.preventDefault();
    return;
  }
  const decision = event.submitter?.value;
  if (!['allow', 'deny'].includes(decision)) {
    event.preventDefault();
    return;
  }
  // Disabled submit buttons are omitted from native form data. Preserve the
  // clicked decision before disabling controls to prevent duplicate approval.
  const field = document.createElement('input');
  field.type = 'hidden';
  field.name = 'decision';
  field.value = decision;
  consentForm.append(field);
  consentForm.dataset.submitting = 'true';
  consentForm.querySelectorAll('button').forEach(button => { button.disabled = true; });
  event.submitter.textContent = '연결 처리 중…';
});
