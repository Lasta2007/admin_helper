
const API='/api/networks';
let selected=null;
let editNetworkId=null;
let pingIntervalTimer=null;
let currentNetworkId=null;

// Получаем элементы модального окна
const networkModal=document.getElementById('networkModal');
const cidrInput=document.getElementById('cidrInput');
const descInput=document.getElementById('descInput');
const addBtn=document.getElementById('addBtn');
const editBtn=document.getElementById('editBtn');
const saveNetworkBtn=document.getElementById('saveNetworkBtn');
const cancelNetworkBtn=document.getElementById('cancelNetworkBtn');

// Навигация
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.nav-item').forEach(item=>{
    item.addEventListener('click',function(){
      document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
      this.classList.add('active');
    });
  });

  document.getElementById('settingsNav').onclick=()=>showSettings();
});

document.getElementById('backBtn').onclick=(e)=>{
 e.preventDefault();
 showNetworks();
};

async function load(){
 const res=await fetch(API);
 const data=await res.json();
 const tb=document.getElementById('networks');
 tb.innerHTML='';
 data.forEach(n=>{
   const tr=document.createElement('tr');
   tr.innerHTML=`<td>${n.cidr}</td><td>${n.description}</td>`;
   tr.onclick=()=>selected=n;
   tr.ondblclick=()=>openNetwork(n);
   tb.appendChild(tr);
 });
}

function showNetworks(){
 document.getElementById("netView").classList.remove("hidden");
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("settingsView").classList.add("hidden");
 if(pingIntervalTimer){
   clearInterval(pingIntervalTimer);
   pingIntervalTimer=null;
 }
}

function showSettings(){
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("settingsView").classList.remove("hidden");
 loadSettings();
 if(pingIntervalTimer){
   clearInterval(pingIntervalTimer);
   pingIntervalTimer=null;
 }
}

async function loadSettings(){
 const res=await fetch('/api/settings');
 const data=await res.json();
 document.getElementById('pingIntervalInput').value=data.ping_interval||60;
}

document.getElementById('saveSettingsBtn').onclick=async()=>{
 const interval=document.getElementById('pingIntervalInput').value;
 await fetch('/api/settings',{
   method:'PUT',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({ping_interval:parseInt(interval)})
 });
 alert('Настройки сохранены');
 setupPingInterval();
};

async function openNetwork(network){
 currentNetworkId=network.id;
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.remove("hidden");
 document.getElementById("settingsView").classList.add("hidden");
 document.getElementById("networkTitle").textContent="Подсеть "+network.cidr;

 const resp=await fetch("/api/networks/"+network.id+"/hosts");
 const hosts=await resp.json();

 const tbody=document.getElementById("hosts");
 tbody.innerHTML="";

 hosts.forEach(host=>{
   const tr=document.createElement("tr");
   const statusClass=host.online?'status-online':'status-offline';
   tr.innerHTML=`<td><span class="status-dot ${statusClass}"></span></td>
   <td>${host.ip}</td>
   <td><input class="inlineHostname" value="${host.hostname||''}"></td>
   <td><input class="inlineComment" value="${host.comment||''}"></td>`;

   async function save(){
      await fetch("/api/hosts/"+encodeURIComponent(host.ip),{
        method:"PUT",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          network_id:network.id,
          hostname:tr.querySelector(".inlineHostname").value,
          comment:tr.querySelector(".inlineComment").value,
          online:host.online||0
        })
      });
   }

   tr.querySelector(".inlineHostname").onchange=save;
   tr.querySelector(".inlineComment").onchange=save;

   tbody.appendChild(tr);
 });

 // Запускаем периодический ping для этой подсети
 setupPingInterval(currentNetworkId);
}

async function pingNetwork(networkId){
 await fetch("/api/networks/"+networkId+"/ping",{method:"POST"});
 // Обновляем отображение после пинга
 if(currentNetworkId){
   const network={id:currentNetworkId};
   openNetwork(network);
 }
}

function getCurrentNetwork(){
 return selected;
}

async function setupPingInterval(networkId){
 if(pingIntervalTimer){
   clearInterval(pingIntervalTimer);
 }
 
 if(!networkId)return;
 
 fetch('/api/settings').then(res=>res.json()).then(data=>{
   const interval=(parseInt(data.ping_interval)||60)*1000;
   
   pingIntervalTimer=setInterval(()=>{
     pingNetwork(networkId);
   },interval);
 });
}

document.getElementById('pingBtn').onclick=async()=>{
 if(currentNetworkId){
   await pingNetwork(currentNetworkId);
 }
};

load();

function openNetworkModal(title,cidr='',desc=''){
 document.getElementById('modalTitle').textContent=title;
 cidrInput.value=cidr;
 descInput.value=desc;
 networkModal.classList.remove('hidden');
}
function closeNetworkModal(){
 networkModal.classList.add('hidden');
 editNetworkId=null;
}
addBtn.onclick=()=>openNetworkModal('Добавить подсеть');

editBtn.onclick=()=>{
 if(!selected){alert('Выберите подсеть');return;}
 editNetworkId=selected.id;
 openNetworkModal('Редактировать подсеть',selected.cidr,selected.description||'');
};

cancelNetworkBtn.onclick=closeNetworkModal;

saveNetworkBtn.onclick=async()=>{
 const body={
   cidr:cidrInput.value.trim(),
   description:descInput.value.trim()
 };
 const url=editNetworkId?API+'/'+editNetworkId:API;
 const method=editNetworkId?'PUT':'POST';
 const r=await fetch(url,{
   method,
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify(body)
 });
 if(r.ok){
    closeNetworkModal();
    load();
 }else{
    alert('Ошибка сохранения');
 }
};

