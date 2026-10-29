'use strict';
const join = location.hash.startsWith('#join=') ? location.hash.slice(6) : '';
if (/^[A-Za-z0-9_-]{20,256}$/.test(join)) sessionStorage.setItem('classroom-invite', join);
fetch('/api/auth/me').then(response => response.json()).then(data => {
  document.querySelector('#login-status').textContent = new URLSearchParams(location.search).has('login_error') ? '로그인을 완료하지 못했어요. 다시 시도해 주세요.' : data.google_configured ? 'Google 계정으로 로그인하면 수업별 권한이 적용됩니다.' : '관리자가 Google 로그인 설정을 준비 중입니다.';
  if (!data.google_configured) document.querySelector('#google-login').hidden = true;
}).catch(() => { document.querySelector('#login-status').textContent = '서버에 연결하지 못했어요.'; });
