(() => {
  const panel = document.getElementById('port-panel');
  const script = document.currentScript;
  if (!panel) return;
  const refresh = Math.max(5, Number(script.dataset.refresh || 30)) * 1000;
  async function update() {
    const response = await fetch(panel.dataset.statusUrl, {headers: {'Accept': 'application/json'}});
    const type = response.headers.get('content-type') || '';
    if (!response.ok || !type.includes('application/json')) throw new Error(`status API ${response.status} ${type}`);
    const data = await response.json();
    let up = 0, down = 0, unknown = 0;
    for (const port of data.ports) {
      const el = document.getElementById(`port-${port.id}`);
      if (!el) continue;
      el.classList.remove('up', 'down', 'unknown');
      el.classList.add(port.oper_status);
      el.classList.toggle('disabled', !port.enabled);
      el.querySelector('.port-state').textContent = port.oper_status.toUpperCase();
      const speed = el.querySelectorAll('.interface-meta')[1];
      if (speed) speed.textContent = `${port.speed_mbps ? `${port.speed_mbps} Mbps` : 'Speed unknown'}${port.mtu ? ` · MTU ${port.mtu}` : ''}`;
      if (port.oper_status === 'up') up++; else if (port.oper_status === 'down') down++; else unknown++;
    }
    document.getElementById('panel-summary').textContent = `UP ${up} · DOWN ${down} · UNKNOWN ${unknown}`;
  }
  update().catch(error => { document.getElementById('panel-summary').textContent = `Status unavailable: ${error.message}`; });
  window.setInterval(() => update().catch(() => {}), refresh);
})();
