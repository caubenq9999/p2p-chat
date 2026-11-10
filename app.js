const el = id => document.getElementById(id);
const escapeText = s => String(s ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const avatars = ['Chiikawa','Usagi','Furuhonya','Kurimanju','Hachiware','Momonga'];
const avatar = id => `./assets/${avatars[Array.from(id || '').reduce((n,c)=>(n+c.charCodeAt(0))%6,0)]}.png`;
let state = {peers:[],messages:{},transfers:[],connections:[]}, currentId = null, token = '', busy = false;
let lastMessages = '', lastPeers = '', connected = false;
const seen = {}, unread = {}, drafts = {}, knownPeers = {};
const pendingActions = new Set();
let uploading = false;
const statusNames = {preparing:'Đang chuẩn bị',offered:'Chờ chấp nhận',pending:'Lời mời nhận file',sending:'Đang gửi',receiving:'Đang nhận',waiting_result:'Đang xác minh',completed:'Hoàn tất',failed:'Thất bại',rejected:'Đã từ chối'};
function notice(text='') { el('notice').textContent=text; el('notice').hidden=!text; }
async function api(path, data) {
  const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json','X-Chat-Token':token},body:JSON.stringify(data)});
  const result=await response.json(); if(!response.ok)throw new Error(result.error || 'Yêu cầu thất bại'); return result;
}
function render() {
  const peer = knownPeers[currentId];
  const online = state.peers.some(p=>p.peer_id===currentId);
  el('messageInput').disabled = !online || !connected;
  el('fileInput').disabled = !online || !connected || uploading;
  document.querySelector('.send-button').disabled = !online || !connected || busy;
  el('peerName').textContent=peer?.name || 'Chọn người dùng';
  el('peerDetails').textContent=peer ? `${!online?'Offline':state.connections.includes(currentId)?'Đã kết nối':'Online'} • ${peer.ip}:${peer.tcp_port}` : 'Đang tìm người dùng trong LAN…';
  el('peerAvatar').src=avatar(currentId); el('peerAvatar').alt=peer?.name || '';
  document.querySelector('.chat-panel').setAttribute('aria-label',peer?`Hội thoại với ${peer.name}`:'Hội thoại');
  const peerHtml=Object.values(knownPeers).map(p=>`<button type="button" class="peer-row ${p.peer_id===currentId?'selected':''}" data-id="${escapeText(p.peer_id)}"><span class="peer-avatar avatar-bubble"><img src="${avatar(p.peer_id)}" alt=""/><i class="online-indicator" style="background:${state.peers.some(x=>x.peer_id===p.peer_id)?'#10cf6b':'#aaa'}"></i></span><span class="peer-copy"><b>${escapeText(p.name)}</b><small>${escapeText(p.ip)}:${p.tcp_port}${state.peers.some(x=>x.peer_id===p.peer_id)?'':' · Offline'}</small></span>${unread[p.peer_id]?`<span class="unread-badge">${unread[p.peer_id]}</span>`:''}</button>`).join('') || '<p class="no-peers">Chưa có người dùng online. Mở thêm một instance để chat.</p>';
  if(peerHtml!==lastPeers){el('peerList').innerHTML=peerHtml;lastPeers=peerHtml;}
  const items=[...(state.messages[currentId]||[]).map(m=>({...m,type:'text'})),...state.transfers.filter(t=>t.peer_id===currentId).map(t=>({...t,type:'file',from:t.direction==='incoming'?'peer':'me',time:t.created_at||0}))].sort((a,b)=>a.time-b.time);
  const html=items.map(m=>{
    let body;
    if(m.type==='text')body=`<div class="bubble">${escapeText(m.text)}</div>`;
    else {
      const pct=Math.max(0,Math.min(100,Number(m.progress)||0));
      let actions='';
      if(m.direction==='incoming' && m.status==='pending') actions=`<button data-action="accept" data-id="${escapeText(m.transfer_id)}">Nhận file</button><button data-action="reject" data-id="${escapeText(m.transfer_id)}">Từ chối</button>`;
      if(m.direction==='incoming' && m.status==='completed') actions=`<a href="/api/download?id=${encodeURIComponent(m.transfer_id)}" download="${escapeText(m.filename)}">Tải về máy</a>`;
      if(!['completed','failed','rejected'].includes(m.status) && m.status!=='pending') actions+=`<button data-action="cancel" data-id="${escapeText(m.transfer_id)}">Hủy truyền</button>`;
      if(m.status==='completed' && m.previewable) actions+=`<a href="/api/preview?id=${encodeURIComponent(m.transfer_id)}" target="_blank" rel="noopener">Xem ảnh</a>`;
      if(pendingActions.has(m.transfer_id)) actions='<small>Đang xử lý…</small>';
      body=`<div class="file-card"><img class="file-icon" src="./assets/file.svg" alt=""/><div class="file-details"><strong>${escapeText(m.filename)}</strong><small>${(m.size/1024).toFixed(1)} KiB · ${escapeText(statusNames[m.status]||m.status)}</small><small>${escapeText(m.reason||'')}</small><div class="file-actions">${actions}</div></div><div class="transfer-progress"><strong>${pct.toFixed(0)}%</strong><div class="progress-track"><span class="progress-fill" style="--progress:${pct}%"></span></div></div></div>`;
    }
    if(m.type==='file' && m.status==='completed' && m.previewable) body=`<a class="image-preview" href="/api/preview?id=${encodeURIComponent(m.transfer_id)}" target="_blank" rel="noopener"><img loading="lazy" src="/api/preview?id=${encodeURIComponent(m.transfer_id)}" alt="${escapeText(m.filename)}" /></a>`+body;
    const incoming=m.from==='peer', photo=`<span class="message-avatar-wrap avatar-bubble"><img class="message-avatar" src="${avatar(incoming?currentId:state.me?.peer_id)}" alt=""/></span>`;
    return `<article class="message message--${incoming?'incoming':'outgoing'}">${incoming?photo:''}<div class="message-content">${body}<time>${m.time?new Date(m.time*1000).toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'}):''}</time></div>${incoming?'':photo}</article>`;
  }).join('') || '<p class="no-peers">Tin nhắn và file sẽ xuất hiện ở đây.</p>';
  if(html!==lastMessages){const list=el('messageList'),bottom=list.scrollHeight-list.scrollTop-list.clientHeight<80;list.innerHTML=html;if(bottom || !lastMessages)list.scrollTop=list.scrollHeight;lastMessages=html;}
}
el('peerList').addEventListener('click',e=>{
  const row=e.target.closest('[data-id]');if(!row)return;
  if(currentId)drafts[currentId]=el('messageInput').value;
  currentId=row.dataset.id; unread[currentId]=0;lastMessages='';
  el('messageInput').value=drafts[currentId]||'';render();el('messageInput').focus();
});
el('composer').addEventListener('submit',async e=>{
  e.preventDefault();const input=el('messageInput'),text=input.value.trim(),peer=currentId;if(!text||!peer||busy)return;
  busy=true;render();
  try{await api('/api/send',{peer_id:peer,text});if(currentId===peer && input.value.trim()===text)input.value='';drafts[peer]='';notice();}
  catch(error){notice(error.message);}finally{busy=false;render();}
});
el('fileInput').addEventListener('change',async e=>{
  const file=e.target.files?.[0],peer=currentId;e.target.value='';if(!file||!peer)return;
  if(file.size>16*1024*1024){notice('Bản web hỗ trợ file tối đa 16 MiB.');return;}
  uploading=true;notice('Đang chuẩn bị file gửi…');render();
  try {
    const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('Không đọc được file'));reader.readAsDataURL(file);});
    await api('/api/upload',{peer_id:peer,filename:file.name,content});notice();
  }catch(error){notice(error.message);}finally{uploading=false;render();}
});
el('messageList').addEventListener('click',async e=>{
  const button=e.target.closest('[data-action]');if(!button)return;
  const id=button.dataset.id, action=button.dataset.action;
  if(pendingActions.has(id))return;pendingActions.add(id);render();
  try{await api(`/api/${action}`,{transfer_id:id});notice();}
  catch(error){notice(error.message);}finally{pendingActions.delete(id);render();}
});
el('refreshPeers').addEventListener('click',async()=>{try{await api('/api/refresh',{});notice();}catch(error){notice(error.message);}});
async function poll(){
  try{
    const response=await fetch('/api/state');if(!response.ok)throw new Error('Không kết nối được server');
    const next=await response.json();token=next.token;
    for(const [id,list] of Object.entries(next.messages)){
      const count=list.filter(m=>m.from==='peer').length;
      if(id!==currentId)unread[id]=(unread[id]||0)+Math.max(0,count-(seen[id]||0));seen[id]=count;
    }
    state=next;connected=true;
    for(const peer of state.peers) knownPeers[peer.peer_id]=peer;
    if(!currentId){
      if(currentId)drafts[currentId]=el('messageInput').value;
      currentId=state.peers[0]?.peer_id||null;el('messageInput').value=drafts[currentId]||'';lastMessages='';
    }
    if(currentId)unread[currentId]=0;
    document.querySelector('.my-profile h2').textContent=state.me.name;
    document.querySelector('.my-profile p').textContent=`${state.me.ip}:${state.me.port}`;
    document.querySelector('.profile-avatar').src=avatar(state.me.peer_id);
    el('serverStatus').textContent='Đã kết nối';render();
  }catch(error){connected=false;el('serverStatus').textContent='Mất kết nối server';render();notice('Không kết nối được Python. Chạy web_app.py và mở đúng cổng web.');}
  finally{setTimeout(poll,600);}
}
render();poll();
