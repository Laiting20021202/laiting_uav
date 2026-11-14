'use strict';
const base=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
const proxy=base==='/ros2/';
const labels={normal:'正常',waiting:'等待資料',timeout:'資料逾時',disconnected:'來源斷線',invalid:'資料無效'};
const states={idle:'尚未錄製',starting:'啟動中',recording:'錄製中',stopping:'停止並寫入 metadata',validating:'逐筆驗證中',packaging:'打包中',ready:'ZIP 已完成',failed:'失敗 · 資料已保留'};
const $=id=>document.getElementById(id);
let csrf='',pending=false,previewUrl=null,lastPreview=0,refreshAt=0,refreshHealthy=false;
const num=(v,d=2)=>v===null||v===undefined?'不可用':Number(v).toFixed(d);
const bytes=v=>v>1024**3?(v/1024**3).toFixed(2)+' GB':(v/1024**2).toFixed(1)+' MB';
const date=v=>v?new Date(v*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'}):'—';
function durationText(job,bagSpan=false){
 const seconds=bagSpan?(job.duration_s??job.elapsed_s):job.elapsed_s;
 if(seconds===null||seconds===undefined)return job.state==='idle'?'—':job.state==='failed'?'未知（異常中斷）':'未知';
 return num(seconds)+' s';
}
function cell(row,text){const c=document.createElement('td');c.textContent=text;row.append(c);return c;}
async function api(path,method='GET'){
 const h={'X-UAV-Request':'1'};if(csrf)h['X-CSRF-Token']=csrf;
 const r=await fetch(base+'api/'+path,{method,headers:h,cache:'no-store',signal:AbortSignal.timeout(5000)});
 if(r.status===401&&proxy){location.href='/login';throw Error('登入已到期');}
 const v=await r.json();if(!r.ok)throw Error(v.error||v.detail||r.status);return v;
}
function markOffline(){for(const b of document.querySelectorAll('#topics .badge')){b.className='badge timeout';b.textContent='監測中斷／非即時';}for(const v of document.querySelectorAll('#topics .values')){v.classList.add('old');v.textContent=v.textContent.replace(/^即時值/,'歷史值');}$('system').textContent='監測中斷';$('camera').textContent=$('flight').textContent='狀態未知';}
function hidePreview(text){$('preview').hidden=true;$('preview-empty').hidden=false;$('preview-empty').textContent=text;}
async function preview(t){
 if(t.state!=='normal'){hidePreview('相機非即時：'+labels[t.state]);return;}
 try{
 const r=await fetch(base+'api/preview',{cache:'no-store',signal:AbortSignal.timeout(4000)});if(!r.ok)throw Error('沒有新影像');
 const blob=await r.blob();const url=URL.createObjectURL(blob);
 $('preview').src=url;$('preview').hidden=false;$('preview-empty').hidden=true;
 if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=url;lastPreview=Date.now();
 }catch(e){hidePreview(e.message);}
}
function render(s){
 const src=s.sources,r=s.recording,j=r.current;
 const unavailable=s.topics.filter(t=>t.state!=='normal');$('preflight').textContent='開始前檢查：'+(10-unavailable.length)+'/10 項有效'+(unavailable.length?'。不可用：'+unavailable.map(t=>t.name+'（'+labels[t.state]+'）').join('、'):'。十項均有持續有效資料。');
 $('system').textContent=src.bridge?'橋接已連線':'橋接中斷';
 $('environment').textContent=`Humble · domain ${s.environment.domain_id} · ${s.environment.qos}`;
 $('camera').textContent=src.bridge?(src.camera.connected?'已連線':'未連線'):'來源狀態未知';
 $('flight').textContent=src.bridge?(src.flight.connected?'AP6 已連線':'未連線'):'來源狀態未知';
 $('gps').textContent=`${src.bridge?'GPS':'上次 GPS'} fix ${src.flight.fix_type??'—'} / 衛星 ${src.flight.satellites??'—'} / HDOP ${src.flight.hdop??'—'}`;
 $('disk').textContent=num(r.disk.free_mb/1024)+' GB';
 $('connection').textContent=`監測已更新 ${new Date().toLocaleTimeString('zh-TW')} · 橋接遺失 ${src.dropped_events} 筆`+(src.error?' · '+src.error:'');
 const body=$('topics');body.replaceChildren();
 for(const t of s.topics){
 const row=document.createElement('tr');
 const name=cell(row,t.name),type=document.createElement('small');type.textContent=t.type;name.append(type);
 const state=cell(row,''),badge=document.createElement('span');badge.className='badge '+t.state;badge.textContent=labels[t.state];state.append(badge);
 const content=document.createElement('small');content.textContent=t.count?('最後內容：'+(t.content_valid?'有效':'無效')):'尚未收到';state.append(content);
 cell(row,num(t.hz)+' / '+t.expected_hz).className=t.below_expected?'warning':'';
 cell(row,`${t.count} / ${t.valid_count} / ${t.invalid_count}`);
 cell(row,t.age_s===null?'尚未收到':num(t.age_s)+' s');
 const value=cell(row,(t.state==='invalid'?'新訊息／內容無效':t.live?'即時值':'歷史值／不可用')+'\n'+JSON.stringify(t.values,null,1)+(t.reason?'\n'+t.reason:''));value.className='values'+(t.live?'':' old');body.append(row);
 }
 $('record-state').textContent=states[j.state]||j.state;$('progress').value=j.progress||0;
 const dl=$('record-details');dl.replaceChildren();
 for(const [k,v] of Object.entries({'名稱':j.name||'—','開始時間':date(j.started_at),'持續時間':durationText(j),'bag 大小':bytes(j.bytes||0),'打包進度':num(j.progress||0,1)+' %','停止原因／錯誤':j.error||j.stop_reason||'—'})){
 const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;dl.append(dt,dd);}
 const busy=['starting','recording','stopping','validating','packaging'].includes(j.state);
 $('start').disabled=pending||busy;$('stop').disabled=pending||j.state!=='recording';
 const history=$('history');history.replaceChildren();
 for(const h of r.history){const row=document.createElement('tr');cell(row,h.name+'\n'+date(h.started_at));cell(row,durationText(h,true));cell(row,h.state==='ready'?bytes(h.zip_bytes||0):'未完成打包');cell(row,h.integrity?(h.topic_count||0)+' / 10':'未驗證');cell(row,(h.integrity==='complete'?'完整':h.integrity==='partial'?'部分缺少／內容無效':'尚未驗證')+' · '+(states[h.state]||h.state)+(h.error?'\n'+h.error:''));const action=cell(row,'');if(h.state==='ready'){const a=document.createElement('a');a.className='download';a.href=base+'api/download/'+h.name;a.textContent='下載這包 ZIP';action.append(a);}history.append(row);}
 preview(s.topics[0]);
}
async function refresh(){try{render(await api('status'));refreshAt=Date.now();refreshHealthy=true;}catch(e){refreshHealthy=false;markOffline();$('connection').textContent='監測連線中斷：'+e.message;$('start').disabled=true;$('stop').disabled=true;hidePreview('監測中斷，舊影像已隱藏');}}
async function action(name){pending=true;$('start').disabled=$('stop').disabled=true;try{await api(name,'POST');$('action-message').textContent=name==='start'?'開始錄製；請確認各 topic 的累計訊息持續增加。':'已要求正常停止，驗證與打包完成後可下載。';}catch(e){$('action-message').textContent=e.message;}finally{pending=false;await refresh();}}
$('start').onclick=()=>action('start');$('stop').onclick=()=>action('stop');
(async()=>{if(proxy){try{const r=await fetch('/api/me');if(!r.ok){location.href='/login';return;}csrf=(await r.json()).csrf;}catch(e){$('connection').textContent=e.message;return;}}await refresh();setInterval(refresh,1000);})();
setInterval(()=>{if(!refreshHealthy||Date.now()-refreshAt>4000){markOffline();$('connection').textContent='監測已逾時，請檢查連線';hidePreview('資料已逾時');$('start').disabled=$('stop').disabled=true;}if(lastPreview&&Date.now()-lastPreview>4000)hidePreview('影像資料已逾時');},1000);
