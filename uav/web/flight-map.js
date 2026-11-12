/* Leaflet is vendored locally; only basemap images need an external connection. */
class FlightMap {
  constructor({api, getCsrf}) {
    this.api = api;
    this.getCsrf = getCsrf;
    this.el = id => document.getElementById(id);
    this.text = (id, value) => { this.el(id).textContent = value; };
    this.points = [];
    this.state = null;
    this.generation = 0;
    this.busy = false;
    this.exporting = false;
    this.synced = false;
    this.positionValid = false;
    this.lastFlightAt = 0;
    this.freshFor = 3000;
    this.recordingEnabled = false;
    this.map = L.map('flight-map', {scrollWheelZoom:false}).setView([23.7, 121], 7);
    this.tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom:19, crossOrigin:'anonymous', referrerPolicy:'strict-origin-when-cross-origin',
      attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
    });
    this.tiles.on('tileerror', () => { this.el('map-tile-error').hidden = false; });
    this.tiles.addTo(this.map);
    this.route = null;
    this.ends = null;
    this.map.on('dragstart', () => { this.el('map-follow').checked = false; });
    this.el('map-follow').addEventListener('change', () => {
      if (this.marker && this.el('map-follow').checked) this.map.panTo(this.marker.getLatLng());
    });
    this.el('map-fit-track').addEventListener('click', () => this.fitTrack());
    this.el('map-retry-tiles').addEventListener('click', () => {
      this.el('map-tile-error').hidden = true;
      this.tiles.redraw();
    });
    this.el('track-start').addEventListener('click', () => this.action('start'));
    this.el('track-stop').addEventListener('click', () => this.action('stop'));
    this.el('track-download').addEventListener('click', () => this.download());
    // A pending HTTP request must not leave a stale aircraft marked as live.
    setInterval(() => {
      if (this.positionValid && this.lastFlightAt && Date.now() - this.lastFlightAt > this.freshFor) this.offline();
    }, 500);
    new ResizeObserver(() => this.map.invalidateSize()).observe(this.el('flight-map'));
  }

  configureRecording(enabled) {
    if (enabled === this.recordingEnabled) return;
    this.recordingEnabled = enabled;
    this.el('flight-track-panel').hidden = !enabled;
    this.el('map-fit-track').hidden = !enabled;
    this.el('track-disabled-note').hidden = enabled;
    this.el('map-workspace').classList.toggle('live-position-only', !enabled);
    document.querySelectorAll('[data-track-only]').forEach(el => { el.hidden = !enabled; });
    if (enabled) {
      this.route = L.polyline([], {color:'#1875d1', weight:4, opacity:.95}).addTo(this.map);
      this.ends = L.layerGroup().addTo(this.map);
    } else {
      this.generation++;
      this.synced = false;
      this.state = null;
      this.points = [];
      this.route?.remove();
      this.ends?.remove();
      this.route = this.ends = null;
    }
    this.map.invalidateSize();
    this.renderSummary();
  }

  updateFlight(status) {
    this.configureRecording(status.flight_tracks_enabled === true);
    const flight = status.flight;
    this.lastFlightAt = Date.now();
    const ages = flight.ages_s || {};
    this.freshFor = Math.max(0, (status.telemetry_stale_s - Math.max(
      ages.GLOBAL_POSITION_INT ?? Infinity, ages.GPS_RAW_INT ?? Infinity)) * 1000);
    this.positionValid = status.flight_position_valid === true;
    if (!this.positionValid) {
      this.offline(flight.connected ? 'GPS 無效或已過期' : '飛控離線');
      return;
    }
    const longitude = flight.longitude + 360 * Math.round((this.map.getCenter().lng - flight.longitude) / 360);
    const latlng = [flight.latitude, longitude];
    if (!this.marker) {
      this.marker = L.marker(latlng, {icon:L.divIcon({className:'aircraft-icon',
        html:'<span class="aircraft-symbol">✈</span>', iconSize:[38,38], iconAnchor:[19,19]}),
        title:'飛機即時位置', zIndexOffset:1000}).addTo(this.map);
      this.marker.bindTooltip('飛機即時位置');
      if (this.el('map-follow').checked) this.map.setView(latlng, 17);
    } else {
      this.marker.setLatLng(latlng);
      this.marker.setTooltipContent('飛機即時位置');
      if (this.el('map-follow').checked) this.map.panTo(latlng, {animate:false});
    }
    const icon = this.marker.getElement();
    icon.classList.remove('stale');
    const heading = Number.isFinite(flight.heading_deg) ? flight.heading_deg :
      Number.isFinite(flight.yaw) ? flight.yaw * 180 / Math.PI : 0;
    icon.firstElementChild.style.transform = `rotate(${heading - 90}deg)`;
    this.text('map-gps-status', status.demo ? '模擬 GPS' : 'GPS 即時定位');
    this.el('map-gps-status').className = 'badge online';
    this.text('map-coordinate', `${flight.latitude.toFixed(7)}, ${flight.longitude.toFixed(7)} · WGS84`);
    this.renderSummary();
  }

  offline(message='連線中斷，等待新位置') {
    this.positionValid = false;
    this.text('map-gps-status', message);
    this.el('map-gps-status').className = 'badge';
    if (this.marker) {
      this.marker.getElement().classList.add('stale');
      this.marker.setTooltipContent('最後有效位置（非即時）');
      this.text('map-coordinate', '顯示最後有效位置（非即時）');
    } else this.text('map-coordinate', '等待有效飛機座標');
    this.renderSummary();
  }

  async refresh() {
    if (!this.recordingEnabled || this.busy) return;
    const generation = this.generation;
    let batches = 0;
    do {
      const id = this.state?.track?.id || 0;
      const after = this.points.at(-1)?.seq || 0;
      const data = await this.api(`/api/flight-tracks?track_id=${id}&after=${after}`);
      if (generation !== this.generation) return;
      if (data.enabled === false) { this.configureRecording(false); return; }
      this.synced = true;
      this.apply(data);
      if (!data.has_more) break;
    } while (++batches < 10);
  }

  apply(data, reset=false) {
    const changed = this.state?.track?.id !== data.track?.id;
    if (changed || reset) this.points = [];
    const after = this.points.at(-1)?.seq || 0;
    this.points.push(...data.points.filter(point => point.seq > after));
    const wasActive = this.state?.active?.id;
    if (!data.active && (changed || wasActive)) this.pendingFit = true;
    this.state = data;
    this.renderRoute();
    this.renderSummary();
    if (this.pendingFit && !data.has_more && this.points.length) {
      this.fitTrack();
      this.pendingFit = false;
    }
  }

  syncFailed() {
    this.synced = false;
    this.renderSummary();
    this.text('track-message', '軌跡狀態暫時無法確認，正在重試；主機上的紀錄不受網頁斷線影響。');
  }

  renderSummary() {
    if (!this.recordingEnabled) {
      for (const id of ['track-start', 'track-stop', 'track-download']) this.el(id).disabled = true;
      return;
    }
    const track = this.state?.track, recording = this.state?.active, latest = this.state?.latest;
    this.el('track-start').disabled = this.busy || !this.synced || !this.getCsrf() || !!recording;
    this.el('track-stop').disabled = this.busy || !this.synced || !this.getCsrf() || !recording;
    this.el('track-download').disabled = this.exporting || !latest?.point_count;
    this.text('track-status', recording ? '● 紀錄中' : track ? '已停止' : '尚未紀錄');
    this.el('track-status').className = 'badge' + (recording ? ' recording' : '');
    this.text('track-points', track?.point_count ?? 0);
    this.text('track-distance', track ? FlightMap.distance(track.distance_m) : '—');
    this.text('track-duration', track ? FlightMap.duration(track) : '—');
    this.text('track-started', track ? FlightMap.date(track.started_at) : '—');
    this.text('track-stopped', track?.stopped_at ? FlightMap.date(track.stopped_at) : '—');
    if (this.synced && !this.busy) this.text('track-message', this.state?.error || (
      recording ? this.positionValid ? '紀錄中 · 每秒保存有效 GPS' : '紀錄中 · 等待有效 GPS，缺失路段不連線' :
      track?.end_reason === 'service_restart' ? '服務重啟已結束紀錄，已保存的軌跡仍可下載。' :
      track ? track.point_count ? '軌跡已保存，可下載 JPG。' : '此次沒有有效 GPS 座標，無可下載軌跡。' : '紀錄預設關閉'));
    this.text('track-last-summary', latest ?
      `#${latest.id} · ${FlightMap.date(latest.started_at)} · ${latest.point_count} 點${latest.demo ? ' · 模擬資料' : ''}` : '尚無已完成的紀錄');
  }

  renderRoute() {
    const segments = FlightMap.segments(this.points);
    this.route.setLatLngs(segments);
    this.ends.clearLayers();
    if (segments.length) {
      const start = segments[0][0], end = segments.at(-1).at(-1);
      L.circleMarker(start, {radius:7, color:'#fff', weight:2, fillColor:'#168653', fillOpacity:1})
        .bindTooltip('紀錄起點').addTo(this.ends);
      if (!this.state.active) L.circleMarker(end, {radius:7, color:'#fff', weight:2, fillColor:'#db7524', fillOpacity:1})
        .bindTooltip('紀錄終點').addTo(this.ends);
    }
    this.el('map-fit-track').disabled = !this.points.length;
  }

  fitTrack() {
    if (!this.points.length) return;
    this.el('map-follow').checked = false;
    this.map.fitBounds(this.route.getBounds(), {padding:[35,35], maxZoom:18, animate:false});
  }

  async action(kind) {
    if (!this.recordingEnabled || this.busy) return;
    const id = this.state?.active?.id;
    if (kind === 'stop' && !id) return;
    this.busy = true;
    this.generation++;
    this.renderSummary();
    this.text('track-message', kind === 'start' ? '正在開始紀錄…' : '正在停止並保存軌跡…');
    try {
      const data = await this.api(`/api/flight-tracks/${kind}`, {method:'POST',
        headers:{'X-CSRF-Token':this.getCsrf(), 'Content-Type':'application/json'},
        ...(kind === 'stop' ? {body:JSON.stringify({track_id:id})} : {})});
      this.synced = true;
      this.apply(data, true);
      if (kind === 'start') this.el('map-follow').checked = true;
      this.text('track-export-message', '');
    } catch (error) {
      this.synced = false;
      this.text('track-message', `操作尚未確認：${error.message}；正在重新確認主機狀態。`);
    } finally {
      this.busy = false;
      this.renderSummary();
    }
  }

  async download() {
    if (!this.recordingEnabled) return;
    const id = this.state?.latest?.id;
    if (!id || this.exporting) return;
    this.exporting = true;
    this.renderSummary();
    this.text('track-export-message', '正在準備完整軌跡與地圖…');
    try {
      const points = [];
      let data;
      do {
        data = await this.api(`/api/flight-tracks/${id}?after=${points.at(-1)?.seq || 0}`);
        points.push(...data.points);
      } while (data.has_more);
      if (!points.length) throw new Error('此次沒有有效 GPS 座標');
      const blob = await FlightMap.jpeg(data.track, points);
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url;
      link.download = `uav-flight-track-${id}${data.track.demo ? '-demo' : ''}.jpg`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      this.text('track-export-message', `已下載第 ${id} 次完整飛行軌跡 JPG。`);
    } catch (error) {
      this.text('track-export-message', `下載失敗：${error.message}。請稍後重試，軌跡仍保存在主機。`);
    } finally {
      this.exporting = false;
      this.renderSummary();
    }
  }

  static date(value) {
    return new Date(value).toLocaleString('zh-TW', {timeZone:'Asia/Taipei', hour12:false});
  }

  static distance(value) {
    return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${value.toFixed(1)} m`;
  }

  static duration(track) {
    const seconds = Math.max(0, Math.floor(((track.stopped_at ? new Date(track.stopped_at).getTime() : Date.now()) -
      new Date(track.started_at).getTime()) / 1000));
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(v => String(v).padStart(2,'0')).join(':');
  }

  static segments(points) {
    const segments = [];
    let previous, longitude;
    for (const point of points) {
      if (previous !== point.segment) segments.push([]);
      longitude = point.longitude + (longitude === undefined ? 0 : 360 * Math.round((longitude - point.longitude) / 360));
      segments.at(-1).push([point.latitude, longitude]);
      previous = point.segment;
    }
    return segments;
  }

  static loadTile(z, x, y) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.referrerPolicy = 'strict-origin-when-cross-origin';
      const timer = setTimeout(() => { img.src = ''; reject(new Error('地圖載入逾時，請確認網路連線')); }, 15000);
      img.onload = () => { clearTimeout(timer); resolve(img); };
      img.onerror = () => { clearTimeout(timer); reject(new Error('無法載入完整底圖，請確認網路連線')); };
      img.src = `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
    });
  }

  static async jpeg(track, points) {
    // Render the complete recorded bounds at fixed resolution, independent of screen size/pan.
    const width = 1400, height = 1000, top = 112, mapHeight = 788, padding = 70;
    const segments = FlightMap.segments(points);
    let zoom = 19, projected, minX, maxX, minY, maxY;
    for (; zoom >= 0; zoom--) {
      projected = segments.map(segment => segment.map(point => L.CRS.EPSG3857.latLngToPoint(L.latLng(point), zoom)));
      minX = minY = Infinity;
      maxX = maxY = -Infinity;
      for (const segment of projected) for (const p of segment) {
        minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
      }
      if ((maxX - minX <= width - padding * 2 && maxY - minY <= mapHeight - padding * 2) || zoom === 0) break;
    }
    const origin = {x:Math.floor((minX + maxX - width) / 2), y:Math.floor((minY + maxY - mapHeight) / 2)};
    const canvas = document.createElement('canvas');
    canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#e1e8eb'; ctx.fillRect(0, 0, width, height);
    const count = 2 ** zoom, jobs = [];
    // Request only the tiles used by this explicit, single-image export; browser caching applies.
    for (let y = Math.max(0, Math.floor(origin.y / 256)); y <= Math.min(count - 1, Math.floor((origin.y + mapHeight - 1) / 256)); y++) {
      for (let x = Math.floor(origin.x / 256); x <= Math.floor((origin.x + width - 1) / 256); x++) {
        jobs.push(FlightMap.loadTile(zoom, ((x % count) + count) % count, y).then(img => {
          ctx.drawImage(img, x * 256 - origin.x, y * 256 - origin.y + top, 256, 256);
        }));
      }
    }
    await Promise.all(jobs);
    ctx.lineJoin = ctx.lineCap = 'round';
    for (const [color, lineWidth] of [['#ffffff',8], ['#1875d1',4]]) {
      ctx.strokeStyle = color; ctx.lineWidth = lineWidth;
      ctx.beginPath();
      for (const segment of projected) segment.forEach((p, i) => {
        ctx[i ? 'lineTo' : 'moveTo'](p.x - origin.x, p.y - origin.y + top);
      });
      ctx.stroke();
    }
    const dot = (point, color, label, offset) => {
      const x = point.x - origin.x, y = point.y - origin.y + top;
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill(); ctx.strokeStyle = '#fff'; ctx.lineWidth = 3; ctx.stroke();
      ctx.font = 'bold 18px sans-serif'; ctx.lineWidth = 4; ctx.strokeText(label, x + 14, y + offset);
      ctx.fillStyle = '#173142'; ctx.fillText(label, x + 14, y + offset);
    };
    dot(projected[0][0], '#168653', '起點', -12);
    dot(projected.at(-1).at(-1), '#db7524', '終點', 25);
    ctx.fillStyle = '#14212b'; ctx.fillRect(0, 0, width, top); ctx.fillRect(0, top + mapHeight, width, height - top - mapHeight);
    ctx.fillStyle = '#eaf4f8'; ctx.font = 'bold 27px sans-serif';
    ctx.fillText(`LAITING UAV / 飛行軌跡 #${track.id}${track.demo ? ' · 模擬資料，非真實飛行' : ''}`, 28, 39);
    ctx.font = '18px sans-serif';
    ctx.fillText(`${FlightMap.date(track.started_at)} — ${FlightMap.date(track.stopped_at)}  (Asia/Taipei)`, 28, 73);
    ctx.fillStyle = '#aec3ce'; ctx.font = '16px sans-serif';
    ctx.fillText(`紀錄時間 ${FlightMap.duration(track)}  /  ${points.length} 點  /  已紀錄距離 ${FlightMap.distance(track.distance_m)}  /  GPS 缺失處不連線`, 28, 99);
    const first = points[0], last = points.at(-1);
    ctx.fillStyle = '#eaf4f8'; ctx.font = '16px sans-serif';
    ctx.fillText(`起點 ${first.latitude.toFixed(7)}, ${first.longitude.toFixed(7)}  /  終點 ${last.latitude.toFixed(7)}, ${last.longitude.toFixed(7)}  /  WGS84`, 28, 934);
    ctx.fillStyle = '#aec3ce'; ctx.font = '15px sans-serif';
    ctx.fillText('© OpenStreetMap contributors · https://www.openstreetmap.org/copyright', 28, 972);
    if (track.end_reason === 'service_restart') ctx.fillText('服務重啟結束紀錄', 1110, 972);
    return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('無法產生 JPG')), 'image/jpeg', .93));
  }
}
