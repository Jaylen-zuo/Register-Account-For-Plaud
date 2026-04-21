let taskId = null, es = null, allResults = [], running = false, resultsOpen = false;

async function startTask() {
  const cfg = {
    env:      document.getElementById('env').value,
    provider: document.getElementById('provider').value,
    count:    parseInt(document.getElementById('count').value) || 1,
    password: document.getElementById('password').value.trim() || 'Abc123456',
  };
  clearLogs();
  allResults = [];
  resultsOpen = false;
  document.getElementById('result-body').innerHTML = '';
  document.getElementById('results-card').style.display = 'none';
  document.getElementById('results-body-wrap').style.display = 'none';
  document.getElementById('results-toggle').textContent = '展开';
  document.getElementById('result-count').textContent = '0 条';
  document.getElementById('stats-card').style.display = 'none';
  setBusy(true);

  const res = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  const { task_id } = await res.json();
  taskId = task_id;

  es = new EventSource(`/api/stream/${task_id}`);
  es.onmessage = e => handleEvent(JSON.parse(e.data));
  es.onerror   = () => { es.close(); setBusy(false); };
}

async function stopTask() {
  if (!taskId) return;
  await fetch(`/api/stop/${taskId}`, { method: 'POST' });
  toast('停止请求已发送');
}

function handleEvent(d) {
  if (d.type === 'log') {
    addLog(d.level, d.msg, d.time);
  } else if (d.type === 'progress') {
    const pct = Math.round((d.current / d.total) * 100);
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-label').textContent = `账号 ${d.current} / ${d.total}`;
  } else if (d.type === 'result') {
    allResults.push(d.result);
    addResultRow(d.result, allResults.length);
    document.getElementById('result-count').textContent = allResults.length + ' 条';
    document.getElementById('results-card').style.display = 'block';
  } else if (d.type === 'start') {
    document.getElementById('env-badge').textContent = `${d.env} | ${d.provider}`;
    document.getElementById('progress-wrap').style.display = 'block';
    document.getElementById('progress-label').textContent = '准备中…';
  } else if (d.type === 'done') {
    es.close();
    setBusy(false);
    showStats(d.success, d.total);
    document.getElementById('progress-fill').style.width = '100%';
    document.getElementById('progress-label').textContent = `完成：${d.success}/${d.total} 成功`;
    toast(`注册完成！成功 ${d.success}/${d.total}`);
  }
}

function addLog(level, msg, time) {
  const box = document.getElementById('log-box');
  if (box.querySelector('.log-placeholder')) box.innerHTML = '';
  const d = document.createElement('div');
  d.className = `log-line lvl-${level}`;
  d.innerHTML = `<span class="log-time">${time || ''}</span><span class="log-lvl">${level}</span><span class="log-msg">${escHtml(msg)}</span>`;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function addResultRow(r, idx) {
  const tb = document.getElementById('result-body');
  const tr = document.createElement('tr');
  const tok = r.token || '';
  const tokShort = tok.length > 40 ? tok.slice(0, 40) + '…' : (tok || 'N/A');
  tr.innerHTML = `
    <td style="color:var(--muted)">${idx}</td>
    <td>${escHtml(r.email)}</td>
    <td><span style="font-family:monospace">${r.password}</span></td>
    <td class="token-cell" title="${escHtml(tok)}" onclick="copyText('${tok}')">${escHtml(tokShort)}</td>
    <td>${r.country || 'N/A'}</td>
    <td>${r.env || 'N/A'}</td>
    <td>${r.status === 'SUCCESS' ? '<span class="badge-ok">SUCCESS</span>' : '<span class="badge-fail">FAILED</span>'}</td>`;
  tb.appendChild(tr);
}

function showStats(ok, total) {
  const fail = total - ok;
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-ok').textContent    = ok;
  document.getElementById('s-fail').textContent  = fail;
  document.getElementById('s-rate').textContent  = total ? Math.round(ok / total * 100) + '%' : '0%';
  document.getElementById('stats-card').style.display = 'block';
}

function setBusy(on) {
  running = on;
  document.getElementById('start-btn').disabled = on;
  document.getElementById('btn-icon').textContent = on ? '⏳' : '▶';
  document.getElementById('btn-text').textContent = on ? '注册中…' : '开始注册';
  document.getElementById('stop-btn').style.display = on ? 'block' : 'none';
  if (!on) document.getElementById('env-badge').textContent = '已完成';
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '<div class="log-placeholder">日志已清空</div>';
}

function exportJSON() {
  const blob = new Blob([JSON.stringify(allResults, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `plaud_accounts_${Date.now()}.json`;
  a.click();
}

function copyText(t) {
  navigator.clipboard.writeText(t).then(() => toast('Token 已复制'));
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

function toggleResults() {
  resultsOpen = !resultsOpen;
  document.getElementById('results-body-wrap').style.display = resultsOpen ? 'block' : 'none';
  document.getElementById('results-toggle').textContent = resultsOpen ? '收起' : '展开';
}

function escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
