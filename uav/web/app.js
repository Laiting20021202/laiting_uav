const $ = (id) => document.getElementById(id);
const set = (id, value) => { $(id).textContent = value; };
const fmt = (v, n=1) => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(n) : '—';
let csrf = '', imageUrl = null, lastPreviewAt = 0, previewDelay = Number($('preview-rate').value), active = true, eventsKey = '';
let recoveringUntil = 0, restarting = false;
let authCheck = null;
let eventsGeneration=0, clearingEvents=false, nextEventsBefore=null, eventsInitialized=false, eventPages=new Map();
let serverBoot = null, restartBoot = null, restartTimer = null, restartAwaitingFrame = false;
let confidenceDirty = false, confidenceSaving = false;
const reasons = {camera_not_calibrated:'相機尚未校正', telemetry_disconnected:'飛控離線', gps_fix_below_gate:'GPS 精度不足', no_ground_height:'缺少地面高度', simulation:'模擬資料', stale_global_position_int:'飛機位置過期', stale_gps_raw_int:'GPS 資料過期', stale_attitude:'姿態資料過期', ray_above_or_near_horizon:'視線未朝向地面', projection_too_far:'推估距離超出範圍', calibration_aspect_mismatch:'影像比例與校正不符'};
const reason = g => g.valid ? [g.quality==='uncalibrated_ground_plane_estimate'?'估算座標（鏡頭未校正）':g.quality==='assumed_nadir_ground_plane'?'假設垂直下視推估':'地面平面推估',g.gps_quality,g.height_source==='assumed_takeoff_ground'?'假設地面與起飛點同高':null].filter(Boolean).join(' · ') : (reasons[g.reason] || g.reason || '待定位');
function fetchResponse(path, options={}) {
  return fetch(path, {...options, cache:'no-store', credentials:'same-origin', signal:options.signal || AbortSignal.timeout(12000)});
}
function identify(me) {csrf=me.csrf;set('user',me.username);$('clear-events').disabled=clearingEvents;}
async function confirmSession() {
  // Concurrent preview/status failures share one check; a proxy error is not logout.
  if (!authCheck) authCheck=(async()=>{
    const response=await fetchResponse('/api/me');
    const body=await response.json().catch(()=>({}));
    if(response.status===401 && body.detail==='Login required') {
      active=false;location.replace('/login?reason=expired');
      throw new Error('登入已到期，請重新登入');
    }
    if(!response.ok || !body.csrf)throw new Error('登入狀態確認暫時失敗，正在重試');
    identify(body);
  })().finally(()=>{authCheck=null;});
  return authCheck;
}
async function request(path, options={}) {
  let response=await fetchResponse(path,options);
  if(response.status===401) {
    await confirmSession();
    // Only reads can be replayed automatically; actions remain an explicit click.
    if(options.method && options.method!=='GET')throw new Error('操作未完成，請重試');
    response=await fetchResponse(path,options);
  }
  if(!response.ok) {
    const error=await response.json().catch(()=>({}));
    const failure=new Error(typeof error.detail==='string'?error.detail:`連線暫時無法使用（${response.status}），正在重試`);
    failure.status=response.status;throw failure;
  }
  return response;
}
async function api(path, options={}) {
  const response = await request(path, options);
  return response.json();
}
function repeat(task, delay, onError=()=>{}) {
  let running=false,timer;
  async function tick() {
    if(!active || running)return;
    clearTimeout(timer);running=true;let ok=false;
    try {if(!document.hidden){await task();ok=true;}}
    catch(e) {if(active)onError(e);}
    finally {running=false;if(active)timer=setTimeout(tick,typeof delay==='function'?delay(ok):delay);}
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)tick();});
  window.addEventListener('online',tick);
  tick();
}
function textElement(tag, content, className) {const el = document.createElement(tag); el.textContent=content; if(className) el.className=className; return el;}
function notice(message) {$('action-error').hidden = !message; set('action-error', message);}
function displayStatus(s) {
  flightMap.updateFlight(s);
  if(restarting && restartBoot && s.boot_id!==restartBoot) {
    restarting=false;restartAwaitingFrame=true;clearTimeout(restartTimer);
    set('recovery-message','服務已重啟，正在載入模型並等待影像。');
  }
  serverBoot=s.boot_id;
  if(restartAwaitingFrame && !s.model_loading) {
    if(s.model_error) {
      set('recovery-message','服務已重啟，但模型載入失敗：'+s.model_error);restartAwaitingFrame=false;
    } else if(s.camera_connected && s.preview_age_s!==null && s.preview_age_s<3) {
      set('recovery-message','影像辨識服務已重啟，畫面已恢復。');restartAwaitingFrame=false;
    } else set('recovery-message','服務已重啟，模型已就緒，等待相機影像，系統會自動重連。');
  }
  const f=s.flight, age=f.ages_s || {}, stale=s.telemetry_stale_s;
  const fresh = kind => f.connected && age[kind] !== undefined && age[kind] >= 0 && age[kind] <= stale;
  set('connection', s.demo ? '模擬展示' : '地面站已連線'); $('connection').className='badge online';
  set('clock',new Date().toLocaleTimeString('zh-TW',{timeZone:'Asia/Taipei',hour12:false}));
  set('flight-status', f.connected ? '已連線' : '離線'); $('flight-status').className='badge '+(f.connected?'online':'');
  set('mode', f.connected ? f.flight_mode || '—' : '—'); set('armed', f.connected ? (f.armed?'已解鎖':'未解鎖') : '等待飛控');
  set('altitude', fmt(fresh('GLOBAL_POSITION_INT')?f.relative_altitude_m:null));
  set('speed',fmt(fresh('VFR_HUD')?f.groundspeed:null));
  set('battery',fmt(fresh('SYS_STATUS')?f.battery_pct:null,0)); set('voltage',fmt(fresh('SYS_STATUS')?f.voltage_v:null));
  const positionValid = fresh('GLOBAL_POSITION_INT') && fresh('GPS_RAW_INT') && f.fix_type >= 3;
  set('latitude',fmt(positionValid?f.latitude:null,7));set('longitude',fmt(positionValid?f.longitude:null,7));
  const fixes={0:'無 GPS',1:'未定位',2:'2D',3:'3D',4:'DGPS',5:'RTK Float',6:'RTK Fixed'};
  set('gps',fresh('GPS_RAW_INT')?`${fixes[f.fix_type] || f.fix_type} / ${f.satellites ?? '—'}`:'—');set('hdop',fmt(fresh('GPS_RAW_INT')?f.hdop:null,2));
  set('attitude',fresh('ATTITUDE')?[f.roll,f.pitch,f.yaw].map(v=>fmt(typeof v==='number'?v*180/Math.PI:null,0)+'°').join(' / '):'—');
  let flightError=f.error || '';
  if(flightError.includes('USB is not present'))flightError='小電腦尚未辨識到 AP6 USB 裝置，正在等待接回。';
  else if(flightError.includes('No flight-controller heartbeat') && f.transport?.bytes_received===0)
    flightError='AP6 USB 裝置仍在，但未收到遙測資料，正在自動重連。';
  else if(flightError.includes('heartbeat expired'))flightError='飛控心跳中斷，正在自動重連。';
  set('flight-message',f.message || flightError || '—');
  const streamNames={GLOBAL_POSITION_INT:'位置',GPS_RAW_INT:'GPS',ATTITUDE:'姿態',VFR_HUD:'速度',SYS_STATUS:'電池'};
  set('telemetry-integrity',s.demo?'模擬資料':!f.connected?'等待飛控連線':f.complete?'資料完整 · 即時接收':
    `等待 ${(f.missing_messages || []).map(name=>streamNames[name] || name).join('、')} · 自動重新請求中`);
  set('camera-status',s.camera_connected?'即時影像':'相機未連線'); $('demo-tag').hidden=!s.demo;
  const phaseNames={waiting:'等待設備',waiting_usb:'等待 USB 接上',connecting:'連線中',recovering_encoder:'正在恢復相機串流，等待編碼器就緒',waiting_heartbeat:'等待飛控心跳',waiting_frame:'等待第一張影像',streaming:'影像接收中',connected:'遙測接收中',retry_wait:'等待自動重連',reconnecting:'重新連線中',stopped:'已停止'};
  const connectedAt = value => value ? new Date(value*1000).toLocaleTimeString('zh-TW',{timeZone:'Asia/Taipei',hour12:false}) : '尚未成功連線';
  const cam=s.camera || {};
  let cameraError=cam.error || '';
  if(cameraError.includes('no decodable video dimensions'))cameraError='相機串流未提供可解碼影像';
  else if(cameraError.includes('Camera frame timeout'))cameraError='超過 5 秒未收到完整影像';
  const controlError=!cam.connected && cam.control_error ? ` · 最近控制回覆：${cam.control_error}` : '';
  const controlStatus=cam.control ? ` · 相機控制${cam.control.connected?'連線正常':(['waiting','stopped'].includes(cam.control.phase)?'待命':'重連中')}` : '';
  const refreshStatus=cam.refreshes?` · 預防更新 ${cam.refreshes} 次`:'';
  const recoveryStatus=cam.power_cycle_required?' · 相機持續拒絕恢復（-21），請將相機斷電 10 秒再上電；系統持續重連':(!cam.connected && cam.recovery_failures?` · 編碼器恢復將於 ${fmt(cam.recovery_retry_in_s,0)} 秒後重試`:'');
  set('camera-detail',s.demo?'模擬展示':`${phaseNames[cam.phase] || '等待設備'} · ${fmt(cam.fps,1)} FPS · 成功連線 ${cam.connections ?? 0} 次${refreshStatus} · 最近 ${connectedAt(cam.last_connected)}${controlStatus}${cameraError ? ' · '+cameraError : ''}${controlError}${recoveryStatus}`);
  set('flight-detail',s.demo?'模擬展示':`${phaseNames[f.phase] || '等待設備'} · 成功連線 ${f.connections ?? 0} 次 · 最近 ${connectedAt(f.last_connected)}${flightError ? ' · '+flightError : ''}`);
  $('reconnect-camera').disabled=!csrf || s.demo || restarting || Date.now()<recoveringUntil;
  $('reconnect-flight').disabled=!csrf || s.demo || restarting || Date.now()<recoveringUntil;
  $('restart-service').disabled=!csrf || !s.service_restart_available || restarting;
  set('model-status',s.model_loading?'正在載入模型…':s.model_error || `使用中：${s.model || '未載入'}`);
  $('switch-model').disabled=!csrf || !$('model').options.length || s.model_loading || s.demo;
  set('confidence-status',`新目標 ${fmt(s.detection_confidence*100,0)}% · 續追 ${fmt(s.tracking_confidence*100,0)}% · 保留 ${fmt(s.track_ttl_s,2)} 秒`);
  if(!confidenceDirty && !confidenceSaving && Number.isFinite(s.detection_confidence)) {
    $('confidence').value=Math.round(s.detection_confidence*100);
    $('confidence-range').value=$('confidence').value;
    $('track-confidence').value=Math.round(s.tracking_confidence*100);
    $('track-ttl').value=s.track_ttl_s;
  }
  for(const id of ['confidence','confidence-range','track-confidence','track-ttl','apply-confidence'])$(id).disabled=!csrf || s.demo || confidenceSaving;
  const lensStatus=s.geolocation_calibrated?'校正已設定':s.geolocation_estimated_intrinsics?'估算鏡頭參數':'待校正';
  set('geo-status',lensStatus);
  if(s.orientation_source==='mavlink_gimbal')set('geo-status',lensStatus+' / '+(fresh('GIMBAL_DEVICE_ATTITUDE_STATUS')?'雲台姿態已接收':'等待雲台姿態'));
  if(s.orientation_source==='assumed_nadir')set('geo-status',lensStatus+' / 假設垂直下視');
  set('system-info',`${fmt(s.inference_ms,0)} ms / ${fmt(s.free_disk_mb/1024,1)} GB`);
  const warnings=[];
  if(s.demo)warnings.push('目前為模擬展示，影像與飛控數值均非實機資料。');
  else if(!s.geolocation_calibrated)warnings.push(s.geolocation_estimated_intrinsics?'目標座標為估算值：使用未校正鏡頭參數，精度尚未量測。':'相機尚未校正：可辨識並紀錄事件與飛機 GPS，目標座標將標示無效。');
  if(!s.demo && s.geolocation_assume_ground_at_takeoff)warnings.push('無測距時假設目標地面與起飛點同高。');
  if(s.storage_error)warnings.push('紀錄寫入失敗：'+s.storage_error);
  if(s.model_error)warnings.push('模型異常：'+s.model_error);
  if(s.camera_error)warnings.push('相機尚未取得影像，系統會自動重連。');
  if(!s.demo && s.orientation_source==='mavlink_gimbal' && !fresh('GIMBAL_DEVICE_ATTITUDE_STATUS'))warnings.push('等待雲台姿態：鏡頭轉動時需有角度回傳才能定位目標。');
  set('notice',warnings.join(' ')); $('notice').hidden=!warnings.length;
  const targets=s.camera_connected && !s.model_error ? s.detections : [];
  set('target-count',`${targets.length} 個目標`);$('targets').replaceChildren();
  if(!targets.length)$('targets').append(textElement('p','等待符合條件的車輛','empty'));
  for(const d of targets){const row=textElement('div','','target-row');row.append(textElement('strong',`#${d.track_id} · ${d.class_name}`),textElement('span',d.retained?`漏檢保留 ${fmt(d.missed_s,2)} 秒`:d.confirmed?'已辨識':`持續 ${fmt(d.visible_s || 0,1)} / ${fmt(s.record_after_s,1)} 秒`,'badge'),textElement('small',d.retained?'上次模型信心 '+fmt(d.confidence*100,0)+'% · 暫無新定位':reason(d.location),'muted'));$('targets').append(row);}
  set('event-total',`${s.event_count} 筆保留紀錄`);
}
function eventCard(e){
  const card=textElement('article','', 'event-card'+(e.example?' example-record':''));card.dataset.eventId=e.id;
  const d=e.detection || {},g=e.target_gps || {},twd=g.valid?g.twd97:null;
  const hasGps=g.valid && Number.isFinite(g.latitude) && Number.isFinite(g.longitude) && Math.abs(g.latitude)<=90 && Math.abs(g.longitude)<=180;
  if(e.has_image){
    const preview=document.createElement('a');preview.href=`/api/events/${e.id}/image.jpg`;preview.target='_blank';preview.rel='noopener';preview.className='event-preview';preview.setAttribute('aria-label',`查看第 ${e.id} 筆完整照片`);
    const img=document.createElement('img');img.src=`/api/events/${e.id}/image.jpg?thumbnail=true`;img.alt=`${e.example?'範例 ':''}${d.class_name} 目標紀錄照片`;img.loading='lazy';preview.append(img);card.append(preview);
  }else card.append(textElement('div','舊紀錄未保存照片','event-no-photo'));
  const body=textElement('div','','event-body');
  const confidence=d.confidence>0 && d.confidence<0.001?'<0.1%':fmt(d.confidence*100,1)+'%';
  const heading=textElement('div','','event-heading');heading.append(textElement('strong',`${d.class_name || '未知類型'} · #${e.id}`),textElement('span',e.example?'格式範例':confidence,'badge'));body.append(heading);
  if(d.candidate_count>1)body.append(textElement('p',`同一目標 #${d.track_id} · 可能類別 ${d.candidate_rank}/${d.candidate_count} · 最高機率：${d.primary_class_name}`,'muted'));
  body.append(textElement('time',new Date(e.timestamp).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false})+' · 台灣時間'));
  body.append(textElement('p',hasGps?`目標緯度 ${fmt(g.latitude,7)}° / 經度 ${fmt(g.longitude,7)}°（WGS84）`:'目標經緯度（WGS84）：無有效座標','event-coordinate'));
  body.append(textElement('p',twd?`E ${fmt(twd.easting_m,2)} m / N ${fmt(twd.northing_m,2)} m`:'TWD97：無有效目標座標','event-coordinate'));
  body.append(textElement('p',e.example?'範例框、類型與座標為示範資料，不是真實偵測。':reason(g),'muted'));
  const actions=textElement('div','','event-card-actions');
  if(e.has_image){const a=textElement('a','下載 JPG ↓','button');a.href=`/api/events/${e.id}/image.jpg?download=true`;a.download=`uav-target-${e.id}.jpg`;actions.append(a);}
  if(hasGps && !e.example){const a=textElement('a','查看地圖 ↗','button quiet');a.href=`https://www.openstreetmap.org/?mlat=${encodeURIComponent(g.latitude)}&mlon=${encodeURIComponent(g.longitude)}#map=19/${encodeURIComponent(g.latitude)}/${encodeURIComponent(g.longitude)}`;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}
  body.append(actions);card.append(body);return card;
}
function renderEvents(){
  const events=[...eventPages.values()].sort((a,b)=>Number(!!b.example)-Number(!!a.example)||b.id-a.id);
  const key=JSON.stringify(events);if(key!==eventsKey){eventsKey=key;$('events').replaceChildren(...events.map(eventCard));}
  $('no-events').hidden=events.length>0;$('more-events').hidden=nextEventsBefore===null;
}
async function refreshEvents(older=false){
  if(clearingEvents)return;
  const generation=eventsGeneration;
  const data=await api('/api/events?limit=20'+(older && nextEventsBefore?`&before=${nextEventsBefore}`:''));
  if(generation!==eventsGeneration || clearingEvents)return;
  $('events-error').hidden=true;
  if(!older && !data.events.length)eventPages.clear();
  for(const e of data.events)eventPages.set(e.id,e);
  for(const id of eventPages.keys())if(data.oldest_id===null || id<data.oldest_id)eventPages.delete(id);
  if(older || !eventsInitialized){nextEventsBefore=data.next_before;eventsInitialized=true;}
  renderEvents();
}
$('more-events').addEventListener('click',async()=>{
  $('more-events').disabled=true;
  try{await refreshEvents(true);}catch(e){set('events-error',e.message);$('events-error').hidden=false;}
  finally{$('more-events').disabled=false;}
});
$('clear-events').addEventListener('click',async()=>{
  clearingEvents=true;eventsGeneration++;$('clear-events').disabled=true;
  set('events-message','正在清除紀錄與照片…');
  try{
    const result=await api('/api/events',{method:'DELETE',headers:{'X-CSRF-Token':csrf},signal:AbortSignal.timeout(60000)});
    eventsGeneration++;eventPages.clear();nextEventsBefore=null;eventsInitialized=false;eventsKey='';renderEvents();
    set('events-message',`已清除 ${result.deleted} 筆紀錄及照片。下次辨識到目標即重新紀錄。`);
    set('event-total','0 筆保留紀錄');
  }catch(e){set('events-message','清除未確認完成：'+e.message);}
  finally{clearingEvents=false;$('clear-events').disabled=!csrf;}
});
async function poll(){displayStatus(await api('/api/status'));}
async function preview(){
  const r=await request('/api/frame.jpg',{signal:AbortSignal.timeout(5000)});
  const blob=await r.blob();
  if(!active)return;
  const next=URL.createObjectURL(blob);
  $('camera').src=next;$('camera').hidden=false;$('camera-placeholder').hidden=true;
  if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=next;lastPreviewAt=Date.now();
  set('preview-info',`JPEG · ${(blob.size/1024).toFixed(0)} KB / 畫面`);
}
$('preview-rate').addEventListener('change',e=>{previewDelay=Number(e.target.value);});
async function reconnectDevice(device){
  recoveringUntil=Date.now()+4000;
  $('reconnect-camera').disabled=true;$('reconnect-flight').disabled=true;
  set('recovery-message','正在重新建立連線，設備接上後會自動恢復。');
  try{await api('/api/system/reconnect',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({device})});}
  catch(e){set('recovery-message',e.message);}
}
$('reconnect-camera').addEventListener('click',()=>reconnectDevice('camera'));
$('reconnect-flight').addEventListener('click',()=>reconnectDevice('flight'));
$('restart-service').addEventListener('click',async()=>{
  restarting=true;restartBoot=serverBoot;restartAwaitingFrame=false;$('restart-service').disabled=true;
  set('recovery-message','正在重啟影像辨識服務，登入會保留，畫面將自動恢復。');
  clearTimeout(restartTimer);
  restartTimer=setTimeout(()=>{
    if(active && restarting){restarting=false;set('recovery-message','尚未確認服務重啟完成，請檢查連線或重試。');}
  },90000);
  try{await api('/api/system/restart',{method:'POST',headers:{'X-CSRF-Token':csrf}});}
  catch(e){if(active){
    if(e.status){restarting=false;clearTimeout(restartTimer);set('recovery-message','無法重啟服務：'+e.message);}
    else set('recovery-message','重啟請求暫時未收到回應，正在確認服務是否恢復。');
  }}
});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST',headers:{'X-CSRF-Token':csrf}});active=false;location.replace('/login');}catch(e){notice(e.message);}});
$('switch-model').addEventListener('click',async()=>{notice('');$('switch-model').disabled=true;try{await api('/api/models/select',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({name:$('model').value})});}catch(e){notice(e.message);$('switch-model').disabled=false;}});
$('confidence-range').addEventListener('input',()=>{
  $('confidence').value=$('confidence-range').value;confidenceDirty=true;
  set('confidence-message','按「套用信心值」儲存門檻。');
});
$('confidence').addEventListener('input',()=>{
  if($('confidence').validity.valid)$('confidence-range').value=$('confidence').value;
  confidenceDirty=true;set('confidence-message','按「套用信心值」儲存門檻。');
});
for(const id of ['track-confidence','track-ttl'])$(id).addEventListener('input',()=>{
  confidenceDirty=true;set('confidence-message','按「套用信心值」儲存門檻。');
});
$('confidence-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const percent=$('confidence').valueAsNumber;
  if(confidenceSaving || !csrf || !$('confidence-form').reportValidity() || !Number.isFinite(percent))return;
  confidenceSaving=true;
  for(const id of ['confidence','confidence-range','track-confidence','track-ttl','apply-confidence'])$(id).disabled=true;
  set('confidence-message','正在套用並儲存…');
  try{
    const result=await api('/api/detection/confidence',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({confidence:percent/100,track_confidence:$('track-confidence').valueAsNumber/100,track_ttl_s:$('track-ttl').valueAsNumber})});
    $('confidence').value=Math.round(result.confidence*100);$('confidence-range').value=$('confidence').value;
    confidenceDirty=false;
    set('confidence-status',`新目標 ${fmt(result.confidence*100,0)}% · 續追 ${fmt(result.track_confidence*100,0)}% · 保留 ${fmt(result.track_ttl_s,2)} 秒`);
    set('confidence-message',`已套用 ${fmt(result.confidence*100,0)}%，續追 ${fmt(result.track_confidence*100,0)}%，保留 ${fmt(result.track_ttl_s,2)} 秒，重新開機保留。`);
  }catch(error){set('confidence-message','套用未確認完成：'+error.message);}
  finally{
    confidenceSaving=false;
    for(const id of ['confidence','confidence-range','track-confidence','track-ttl','apply-confidence'])$(id).disabled=false;
  }
});
repeat(async()=>{identify(await api('/api/me'));},ok=>ok?60000:2000);
const flightMap = new FlightMap({api, getCsrf:()=>csrf});
const networkPanel = new NetworkPanel({api, getCsrf:()=>csrf});
repeat(()=>networkPanel.refresh(),ok=>ok?2000:1000,()=>networkPanel.offline());
repeat(()=>flightMap.refresh(),()=>flightMap.recordingEnabled?1000:5000,e=>flightMap.syncFailed(e));
repeat(poll,1000,()=>{set('connection','連線中斷 · 自動重試中');$('connection').className='badge';flightMap.offline();});
repeat(preview,ok=>ok?previewDelay:Math.max(previewDelay,1000),()=>{
  if(!imageUrl){$('camera').hidden=true;$('camera-placeholder').hidden=false;set('preview-info','等待第一張影像 · 自動重試中');return;}
  $('camera').hidden=false;$('camera-placeholder').hidden=true;
  const age=Math.max(1,Math.round((Date.now()-lastPreviewAt)/1000));
  set('preview-info',`連線暫時中斷 · 顯示 ${age} 秒前最後畫面 · 自動重試中`);
});
repeat(refreshEvents,3000,()=>{set('events-error','紀錄暫時無法更新，正在自動重試。');$('events-error').hidden=false;});
let modelsLoaded=false;
repeat(async()=>{
  if(modelsLoaded)return;
  const m=await api('/api/models');
  $('model').replaceChildren();
  for(const [id,spec] of Object.entries(m.models)){const o=textElement('option',`${id} · ${spec.description}`);o.value=id;$('model').append(o);}
  if(m.active || m.default)$('model').value=m.active || m.default;
  modelsLoaded=true;
  if($('action-error').textContent.startsWith('模型清單載入失敗：'))notice('');
},5000,e=>notice('模型清單載入失敗：'+e.message));
