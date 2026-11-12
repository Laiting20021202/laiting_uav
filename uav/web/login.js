if(new URLSearchParams(location.search).get('reason')==='expired') {
  document.getElementById('error').textContent='登入已到期或已失效，請重新登入以恢復畫面。';
}
document.getElementById('login').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const error = document.getElementById('error');
  button.disabled = true; error.textContent = '';
  try {
    const result = await fetch('/api/login', {method: 'POST', cache:'no-store', signal:AbortSignal.timeout(12000), headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: document.getElementById('username').value, password: document.getElementById('password').value})});
    if (!result.ok) { const body = await result.json().catch(()=>({})); throw new Error(typeof body.detail === 'string' ? body.detail : '服務暫時無法連線，請重試'); }
    location.replace('/');
  } catch (e) { error.textContent = e.name==='TimeoutError' || e.name==='TypeError' ? '連線逾時或網路中斷，請重試登入。' : e.message || '連線失敗，請重試'; }
  finally { button.disabled = false; }
});
