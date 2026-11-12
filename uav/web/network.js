/* Actual 4G egress and fixed-path switching. Actions are never replayed. */
class NetworkPanel {
  constructor({api, getCsrf}) {
    this.api = api;
    this.getCsrf = getCsrf;
    this.status = null;
    this.pending = null;
    try {
      const saved = JSON.parse(sessionStorage.getItem('uav-network-switch'));
      if (saved && ['auto', 'usb', 'wifi'].includes(saved.target) &&
          this.canonicalId(saved.request_id) && Number.isFinite(saved.started_at) &&
          Date.now() - saved.started_at >= 0 && Date.now() - saved.started_at < 120000) {
        this.pending = {target:saved.target, request_id:saved.request_id,
          started_at:saved.started_at, route_complete:false};
      }
    } catch (_) {}
    for (const target of ['auto', 'usb', 'wifi']) {
      document.getElementById('network-use-' + target).addEventListener('click', () => this.switch(target));
    }
  }

  text(id, value) { document.getElementById(id).textContent = value; }
  canonicalId(value) {
    return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
  }
  label(path) { return {usb: 'USB 4G', wifi: '網卡 Wi-Fi 4G', none: '暫無 4G 出口', unknown: '等待確認目前出口'}[path] || '等待確認目前出口'; }
  healthy(state) { return state?.available === true && state.connected === true && state.internet === true; }
  atomicBusy(state) {
    return ['preparing', 'switching'].includes(state?.phase) ||
      (state?.switching === true && state.phase !== 'recovering_tunnel');
  }
  operationLocked() { return !!this.pending && !this.pending.route_complete; }
  savePending() {
    try {
      if (this.pending) sessionStorage.setItem('uav-network-switch', JSON.stringify(this.pending));
      else sessionStorage.removeItem('uav-network-switch');
    } catch (_) {}
  }
  pathState(path, state) {
    if (this.healthy(state)) return {badge:'可使用', kind:'ready',
      detail:'對外連線正常' + (this.status?.active_path !== path ? ' · 備援就緒' : ' · 目前使用中'),
      hint:'可選擇此路徑；系統已確認對外連線正常。'};
    if (state?.available === false) return {badge:'故障', kind:'fault',
      detail:path === 'usb' ? 'USB 網卡尚未連接，系統持續等待裝置恢復。' : '網卡 Wi-Fi 尚未連接，系統持續重連指定 AP。',
      hint:'此路徑目前不可用；修復並確認外網後會自動開放選擇。'};
    if (state?.available === true && state.connected === false) return {badge:'修復中', kind:'recovering',
      detail:'已偵測介面，正在恢復與網卡的連線。', hint:'尚未連上網卡；確認連線與外網恢復後即可選擇。'};
    if (state?.available === true && state.connected === true && state.internet === false) return {badge:'修復中', kind:'recovering',
      detail:'已連上網卡，但外網連線失敗，系統持續修復。', hint:'外網尚未恢復；確認可上網後會自動開放選擇。'};
    return {badge:'確認中', kind:'unknown', detail:'尚未取得完整的連線與外網狀態。',
      hint:'連線狀態尚未確認；確認正常後即可選擇。'};
  }
  controls() {
    const state = this.status || {};
    for (const target of ['auto', 'usb', 'wifi']) {
      const button = document.getElementById('network-use-' + target);
      button.setAttribute('aria-pressed', state.preferred_path === target ? 'true' : 'false');
      const available = target === 'auto' ? this.healthy(state.usb) || this.healthy(state.wifi) : this.healthy(state[target]);
      let hint = target === 'auto' ? 'USB 優先，自動使用已確認可上網的機上備援。' : this.pathState(target, state[target]).hint;
      if (!this.getCsrf()) hint = '正在確認登入權限，完成後即可操作。';
      else if (state.controller_available !== true) hint = '切換服務尚未就緒，畫面持續查詢最新狀態。';
      else if (this.atomicBusy(state)) hint = '正在建立出口，完成後即可選擇其他健康路徑。';
      else if (this.operationLocked()) hint = '正在確認切換是否已建立出口，請稍候。';
      else if (target === 'auto' && !available) hint = '兩條 4G 路徑尚未確認可用，系統持續自動修復。';
      button.disabled = !this.getCsrf() || state.controller_available !== true ||
        this.atomicBusy(state) || this.operationLocked() || !available;
      button.textContent = target === 'auto' ? '自動選擇 · USB 優先' :
        (available ? '使用 ' + this.label(target) : this.label(target) + ' · 暫不可用');
      this.text('network-' + target + '-action-hint', hint);
    }
  }
  render(state) {
    this.status = state;
    this.text('network-active-path', this.label(state.active_path));
    const active = state[state.active_path] || {};
    const phase = {preparing: '正在準備備援連線', switching: '正在切換出口',
      recovering_tunnel: '出口已建立，網頁重連中', offline: '正在恢復 4G 連線'}[state.phase];
    this.text('network-phase', phase || (this.healthy(active) && state.tunnel_ready === true ? '網頁連線正常' : '正在確認外網與網頁連線'));
    const badge = document.getElementById('network-active-path');
    badge.className = 'badge' + (this.healthy(active) ? ' online' : '');
    for (const path of ['usb', 'wifi']) {
      const health = this.pathState(path, state[path]);
      this.text('network-' + path + '-detail', health.detail);
      this.text('network-' + path + '-health', health.badge);
      document.getElementById('network-' + path + '-health').className = 'badge path-' + health.kind;
    }
    const last = state.last_switch;
    const duration = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toFixed(1) + ' 秒' : null;
    const measured = last ? [duration(last.route_seconds) && '出口 ' + duration(last.route_seconds),
      duration(last.website_seconds) && '網頁 ' + duration(last.website_seconds)].filter(Boolean) : [];
    this.text('network-timing', measured.length ? '最近切換實測：' + measured.join(' · ') : '切換完成後顯示本次實測時間');
    let message = state.error || '';
    if (this.pending) {
      const lastTargetMatches = last?.target === this.pending.target ||
        (this.pending.target === 'auto' && ['usb', 'wifi'].includes(last?.target));
      const sameRequest = last?.request_id === this.pending.request_id && lastTargetMatches;
      const actualTarget = this.pending.target === 'auto' ? ['usb', 'wifi'].includes(state.active_path) : state.active_path === this.pending.target;
      const ownResult = state.controller_available === true && Array.isArray(state.recent_requests) ?
        state.recent_requests.slice(-16).reverse().find(record => record && this.canonicalId(record.request_id) &&
          record.request_id === this.pending.request_id && record.target === this.pending.target &&
          ['queued', 'route_ready', 'failed'].includes(record.state)) : null;
      if (ownResult?.state === 'failed') {
        message = '此次切換未完成；目前出口為 ' + this.label(state.active_path) + '。' + (state.error || '系統持續修復可用路徑。');
        this.pending = null;
        this.savePending();
      } else if (ownResult?.state === 'route_ready' && !sameRequest) {
        message = '此次出口切換已完成；目前出口為 ' + this.label(state.active_path) + '。';
        this.pending = null;
        this.savePending();
      } else if (sameRequest && state.error && state.controller_available === true && !state.switching && ownResult?.state !== 'queued') {
        message = state.error + '；目前出口為 ' + this.label(state.active_path) + '。';
        this.pending = null;
        this.savePending();
      } else if (sameRequest && state.controller_available === true && duration(last.finished_at) !== null &&
                 !state.switching && ownResult?.state !== 'queued') {
        message = actualTarget && this.healthy(active) ? '已確認目前使用 ' + this.label(state.active_path) + '。' :
          '切換結果已更新；目前出口為 ' + this.label(state.active_path) + '。' + (state.error || '');
        this.pending = null;
        this.savePending();
      } else if (Date.now() - this.pending.started_at >= 120000) {
        message = '尚未確認切換完成。請查看目前出口與連線狀態，再重新選擇。';
        this.pending = null;
        this.savePending();
      } else if (ownResult?.state === 'route_ready' || (sameRequest && state.controller_available === true &&
                 !ownResult && duration(last.route_seconds) !== null && actualTarget && !this.atomicBusy(state))) {
        this.pending.route_complete = true;
        this.savePending();
        message = actualTarget ? '出口已切換，網頁重連中；仍可選擇其他可使用路徑。' :
          '此次出口曾確認建立；目前出口為 ' + this.label(state.active_path) + '，仍可選擇其他可使用路徑。';
      } else if (this.pending.route_complete) {
        message = '已確認出口切換，持續查詢網頁恢復狀態；可選擇其他可使用路徑。';
      } else message = '正在確認切換結果；連線恢復後，畫面會自行更新。';
    }
    this.text('network-message', message);
    this.controls();
  }
  async refresh() { this.render(await this.api('/api/network')); }
  offline() {
    this.status = null;
    this.text('network-active-path', '連線中斷 · 等待確認出口');
    document.getElementById('network-active-path').className = 'badge';
    this.text('network-phase', this.pending?.route_complete ? '出口曾確認，正在重新確認網頁連線' :
      this.pending ? '切換中，正在自動重新連線' : '正在自動重新連線');
    for (const path of ['usb', 'wifi']) {
      this.text('network-' + path + '-detail', '連線中斷，等待最新狀態。');
      this.text('network-' + path + '-health', '確認中');
      document.getElementById('network-' + path + '-health').className = 'badge path-unknown';
    }
    this.controls();
    if (this.pending) this.text('network-message', '切換請求未重複送出，等待確認目前出口。');
  }
  async switch(target) {
    if (this.operationLocked() || !this.getCsrf() || this.status?.controller_available !== true) return;
    const button = document.getElementById('network-use-' + target);
    if (button.disabled) return;
    const operation = {target, request_id: crypto.randomUUID(), started_at: Date.now(), route_complete:false};
    this.pending = operation;
    this.savePending();
    this.controls();
    this.text('network-message', '正在提交切換，畫面會持續確認連線。');
    try {
      await this.api('/api/network/switch', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': this.getCsrf()},
        body: JSON.stringify({target, request_id: operation.request_id})});
      if (this.pending?.request_id === operation.request_id && !this.pending.route_complete) {
        this.text('network-message', '已接受切換，正在確認新的 4G 出口。');
      }
    } catch (error) {
      if (this.pending?.request_id !== operation.request_id) return;
      if (this.pending.route_complete) return;
      if (error.status && error.status >= 400 && error.status < 500) {
        this.pending = null;
        this.savePending();
        this.text('network-message', '切換未接受：' + error.message);
        this.controls();
      } else {
        this.text('network-message', '切換回覆暫時中斷，正在確認目前出口；不會重複送出操作。');
      }
    }
  }
}

if (typeof module !== 'undefined' && module.exports) module.exports = NetworkPanel;
