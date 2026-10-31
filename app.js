// UI MOCK ONLY. Replace this state with the existing P2P LAN discovery / messaging layer.
// Do not interpret the addresses or transfer percentages as live network data.
const avatar = name => `./assets/${name}.png`;
const peers = [
  { id:'huy', name:'Huy', ip:'192.168.1.15', avatar:'Chiikawa', online:true, unread:0 },
  { id:'nguyen', name:'Nguyên', ip:'192.168.1.22', avatar:'Usagi', online:true, unread:2 },
  { id:'linh', name:'Linh', ip:'192.168.1.27', avatar:'Furuhonya', online:true, unread:0 },
  { id:'minh', name:'Minh', ip:'192.168.1.33', avatar:'Kurimanju', online:true, unread:0 },
  { id:'khoa', name:'Khoa', ip:'192.168.1.41', avatar:'Hachiware', online:true, unread:0 }
];
const messages = {
  huy:[
    { from:'peer', type:'text', text:'Hii', time:'10:24' },
    { from:'me', type:'text', text:'hii', time:'10:24' },
    { from:'peer', type:'text', text:'gửi file nè', time:'10:25' },
    { from:'peer', type:'file', filename:'report.pdf', size:'24.8 MB', progress:68, time:'10:25' },
    { from:'me', type:'text', text:'okeee', time:'10:25' },
    { from:'me', type:'file', filename:'image.png', size:'12.4 MB', progress:36, time:'10:26' }
  ]
};
let currentId = 'huy';
const el = id => document.getElementById(id);
const escapeText = s => String(s).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const nowTime = () => new Date().toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});
function peerList() {
  const onlinePeers=peers.filter(p=>p.online); // no peers == no fake placeholder user cards
  el('peerList').innerHTML = onlinePeers.map(p=>`<button type="button" class="peer-row ${p.id===currentId?'selected':''}" data-id="${p.id}" ${p.id===currentId?'aria-current="true"':''}>
    <span class="peer-avatar avatar-bubble"><img src="${avatar(p.avatar)}" alt=""/><i class="online-indicator"></i></span>
    <span class="peer-copy"><b>${escapeText(p.name)}</b><small>${escapeText(p.ip)}</small></span>
    ${p.unread>0?`<span class="unread-badge">${p.unread}</span>`:''}</button>`).join('') || '<p class="no-peers">Không tìm thấy người dùng đang online trong mạng LAN.</p>';
  el('peerList').querySelectorAll('.peer-row').forEach(button=>button.addEventListener('click',()=>switchPeer(button.dataset.id)));
}
function messageHtml(msg,peer){
 const incoming=msg.from==='peer';
 const progress=Math.max(0,Math.min(Number(msg.progress)||0,100));
 const item=msg.type==='file'
 ? `<div class="file-card ${progress<100?'file-card--transferring':''}"><img class="file-icon" src="./assets/file.svg" alt="File"/><div class="file-details"><strong title="${escapeText(msg.filename)}">${escapeText(msg.filename)}</strong><small>${escapeText(msg.size)}</small></div>${progress<100?`<div class="transfer-progress"><strong>${progress}%</strong><div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><div class="progress-fill" style="--progress:${progress}%"></div></div></div>`:''}</div>`
 : `<div class="bubble">${escapeText(msg.text)}</div>`;
 return `<article class="message ${incoming?'message--incoming':'message--outgoing'}">
  ${incoming?`<span class="message-avatar-wrap avatar-bubble"><img class="message-avatar" src="${avatar(peer.avatar)}" alt=""/></span>`:''}
  <div class="message-content">${item}<time>${escapeText(msg.time)}</time></div>
  ${!incoming?`<span class="message-avatar-wrap avatar-bubble"><img class="message-avatar" src="${avatar('Usagi')}" alt=""/></span>`:''}
 </article>`;
}
function render(){
 const peer=peers.find(p=>p.id===currentId); if(!peer)return;
 el('peerAvatar').src=avatar(peer.avatar);el('peerAvatar').alt=`Avatar của ${peer.name}`;el('peerName').textContent=peer.name;el('peerDetails').innerHTML=`<span>Online</span><b aria-hidden="true">•</b> ${escapeText(peer.ip)}`;
 el('messageList').innerHTML=(messages[currentId]||[]).map(m=>messageHtml(m,peer)).join('');
 peerList(); el('messageList').scrollTop=el('messageList').scrollHeight;
}
function switchPeer(nextId){
 if(nextId===currentId)return;
 const panel=document.querySelector('.chat-panel');
 panel.classList.add('is-switching');
 window.setTimeout(()=>{
  currentId=nextId;
  const peer=peers.find(item=>item.id===currentId);
  if(peer)peer.unread=0;
  render();
  requestAnimationFrame(()=>panel.classList.remove('is-switching'));
 },150);
}
el('composer').addEventListener('submit',ev=>{
 ev.preventDefault();const input=el('messageInput'), text=input.value.trim();if(!text)return;
 (messages[currentId]??=[]).push({from:'me',type:'text',text,time:nowTime()});input.value='';render();
 // TODO: wire to existing P2P API/IPC sendText(peerId, text); this preview does NOT transmit.
});
el('fileInput').addEventListener('change',ev=>{
 const file=ev.target.files?.[0];if(!file)return;
 const size=(file.size/(1024*1024)).toFixed(1)+' MB';
 (messages[currentId]??=[]).push({from:'me',type:'file',filename:file.name,size,progress:0,time:nowTime()});
 ev.target.value='';render();
 // TODO: connect existing P2P sendFile API and its real progress events. Never fake % in production.
});
el('refreshPeers')?.addEventListener('click',()=>{
 const button=el('refreshPeers'), list=el('peerList');
 if(button.classList.contains('is-refreshing'))return;
 button.classList.add('is-refreshing');
 button.disabled=true;
 list.classList.add('is-refreshing');
 list.setAttribute('aria-busy','true');
 window.setTimeout(()=>{
  peerList();
  list.classList.remove('is-refreshing');
  list.removeAttribute('aria-busy');
  button.classList.remove('is-refreshing');
  button.disabled=false;
 },720);
});
render();
