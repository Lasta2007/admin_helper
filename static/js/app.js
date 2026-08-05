const API='/api/networks';
let selected=null;
let editNetworkId=null;
let pingIntervalTimer=null;
let currentNetworkId=null;
let isPinging=false;
let allHosts=[];
let globalSearchTerm='';

// Получаем элементы модального окна
const networkModal=document.getElementById('networkModal');
const cidrInput=document.getElementById('cidrInput');
const descInput=document.getElementById('descInput');
const addBtn=document.getElementById('addBtn');
const saveNetworkBtn=document.getElementById('saveNetworkBtn');
const cancelNetworkBtn=document.getElementById('cancelNetworkBtn');

// Навигация
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('ipamNav').onclick=()=>showIPAM();
  document.getElementById('workPcNav').onclick=()=>showWorkPc();
  document.getElementById('settingsNav').onclick=()=>showSettings();
  document.getElementById('ipamNav').classList.add('active');
});

document.getElementById('backBtn').onclick=(e)=>{
 e.preventDefault();
 showIPAM();
};

document.getElementById('workPcBackBtn').onclick=(e)=>{
 e.preventDefault();
 showIPAM();
};

document.getElementById('settingsBackBtn').onclick=(e)=>{
 e.preventDefault();
 showIPAM();
};

async function load(){
 const res=await fetch(API);
 const data=await res.json();
 const tb=document.getElementById('networks');
 tb.innerHTML='';
 data.forEach(n=>{
   const tr=document.createElement('tr');
   tr.innerHTML=`<td>${n.cidr}</td><td>${n.description||''}</td>
     <td class="actions-cell">
       <button class="action-btn edit-btn" data-id="${n.id}" data-cidr="${n.cidr}" data-desc="${n.description||''}">✏️</button>
       <button class="action-btn delete-btn" data-id="${n.id}">🗑️</button>
     </td>`;
   tr.onclick=(e)=>{
     if(!e.target.classList.contains('action-btn')){
       selected=n;
       document.querySelectorAll('#networks tr').forEach(r=>r.classList.remove('selected'));
       tr.classList.add('selected');
     }
   };
   tr.ondblclick=(e)=>{
     if(!e.target.classList.contains('action-btn')){
       openNetwork(n);
     }
   };
   tb.appendChild(tr);
 });
 
 document.querySelectorAll('.edit-btn').forEach(btn=>{
   btn.onclick=(e)=>{
     e.stopPropagation();
     const id=parseInt(e.target.dataset.id);
     const cidr=e.target.dataset.cidr;
     const desc=e.target.dataset.desc;
     editNetworkId=id;
     openNetworkModal('Редактировать подсеть',cidr,desc);
   };
 });
 
 document.querySelectorAll('.delete-btn').forEach(btn=>{
   btn.onclick=async(e)=>{
     e.stopPropagation();
     const id=parseInt(e.target.dataset.id);
     const row=e.target.closest('tr');
     const cidr=row.querySelector('td:first-child').textContent;
     if(confirm(`Вы уверены, что хотите удалить подсеть ${cidr}?`)){
       await fetch(API+'/'+id,{method:'DELETE'});
       load();
     }
   };
 });
}

function showIPAM(){
 document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
 document.getElementById('ipamNav').classList.add('active');
 document.getElementById("netView").classList.remove("hidden");
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("settingsView").classList.add("hidden");
 document.getElementById("workPcView").classList.add("hidden");
 if(pingIntervalTimer){
   clearInterval(pingIntervalTimer);
   pingIntervalTimer=null;
 }
}

function showWorkPc(){
 document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
 document.getElementById('workPcNav').classList.add('active');
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("settingsView").classList.add("hidden");
 document.getElementById("workPcView").classList.remove("hidden");
 loadWorkPcData();
 if(pingIntervalTimer){
   clearInterval(pingIntervalTimer);
   pingIntervalTimer=null;
 }
}

function showSettings(){
 document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
 document.getElementById('settingsNav').classList.add('active');
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("workPcView").classList.add("hidden");
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
 document.getElementById('pingTimeoutInput').value=data.ping_timeout||3;
 document.getElementById('portScanEnabledCheckbox').checked=(data.port_scan_enabled==='1');
 document.getElementById('portScanIntervalInput').value=data.port_scan_interval||1440;
 
 // Загружаем настройки WORK PC
 try{
   const workPcRes=await fetch('/api/work_pc/settings');
   const workPcData=await workPcRes.json();
   if(workPcData.status==='ok'){
     document.getElementById('workPcLogPathInput').value=workPcData.settings.log_path||'';
     document.getElementById('workPcUpdateIntervalInput').value=workPcData.settings.update_interval||60;
   }
 }catch(e){
   console.error('Ошибка загрузки настроек WORK PC:', e);
 }
}

document.getElementById('saveSettingsBtn').onclick=async()=>{
 const interval=document.getElementById('pingIntervalInput').value;
 const timeout=document.getElementById('pingTimeoutInput').value;
 const portScanEnabled=document.getElementById('portScanEnabledCheckbox').checked?'1':'0';
 const portScanInterval=document.getElementById('portScanIntervalInput').value;
 await fetch('/api/settings',{
   method:'PUT',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({
     ping_interval:parseInt(interval),
     ping_timeout:parseInt(timeout),
     port_scan_enabled:portScanEnabled,
     port_scan_interval:parseInt(portScanInterval)
   })
 });
 alert('Настройки IPAM сохранены');
};

// WORK PC настройки - установка пути к log.txt
document.getElementById('saveWorkPcLogPathBtn').onclick=async()=>{
 const logPath=document.getElementById('workPcLogPathInput').value.trim();
 if(!logPath){
   alert('Укажите путь к файлу log.txt');
   return;
 }
 try{
   const res=await fetch('/api/work_pc/settings/log_path',{
     method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({log_path: logPath})
   });
   const result=await res.json();
   if(res.ok){
     alert('Путь к файлу установлен: '+logPath);
   }else{
     alert('Ошибка: '+(result.detail||'Не удалось установить путь'));
   }
 }catch(e){
   console.error('Ошибка при установке пути:', e);
   alert('Ошибка при установке пути к файлу');
 }
};

// WORK PC настройки - сохранение периода обновления
document.getElementById('saveWorkPcSettingsBtn').onclick=async()=>{
 const updateInterval=document.getElementById('workPcUpdateIntervalInput').value;
 try{
   const res=await fetch('/api/work_pc/settings/update_interval',{
     method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({update_interval: parseInt(updateInterval)})
   });
   const result=await res.json();
   if(res.ok){
     alert('Настройки WORK PC сохранены');
   }else{
     alert('Ошибка: '+(result.detail||'Не удалось сохранить настройки'));
   }
 }catch(e){
   console.error('Ошибка при сохранении настроек WORK PC:', e);
   alert('Ошибка при сохранении настроек WORK PC');
 }
};

async function openNetwork(network){
 currentNetworkId=network.id;
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.remove("hidden");
 document.getElementById("settingsView").classList.add("hidden");
 document.getElementById("networkTitle").textContent="Подсеть "+network.cidr;

 const resp=await fetch("/api/networks/"+network.id+"/hosts");
 allHosts=await resp.json();

 renderHosts(allHosts);

 setupPingInterval(currentNetworkId);
}

function renderHosts(hosts){
 const tbody=document.getElementById("hosts");
 tbody.innerHTML="";
 
 const filterValue=document.getElementById('hostFilter').value;
 const searchTerm=globalSearchTerm.toLowerCase();
 
 hosts.forEach(host=>{
   if(filterValue==='online' && !host.online) return;
   if(filterValue==='offline' && host.online) return;
   
   // Глобальный поиск по всем полям (IP, hostname ручной, scanned_hostname, comment, mac, ports)
   const searchStr=`${host.ip} ${host.hostname||''} ${host.scanned_hostname||''} ${host.comment||''} ${host.mac||''} ${host.open_ports||''}`.toLowerCase();
   if(searchTerm && !searchStr.includes(searchTerm)) return;
   
   const tr=document.createElement("tr");
   const statusClass=host.online?'status-online':'status-offline';
   const lastPing=host.last_ping ? new Date(host.last_ping).toLocaleString() : 'Никогда';
   const macDisplay=host.mac||'-';
   const portsDisplay=host.open_ports||'-';
   tr.innerHTML=`<td><span class="status-dot ${statusClass}" title="Последняя проверка: ${lastPing}"></span></td>
   <td>${host.scanned_hostname ? `<div>${host.ip}<div class="scanned-hostname">${host.scanned_hostname}</div></div>` : host.ip}</td>
   <td><button class="ping-btn" data-ip="${host.ip}" data-network="${currentNetworkId}">Ping</button></td>
   <td><input class="inlineHostname" value="${host.hostname||''}"></td>
   <td class="ports-cell">${portsDisplay}</td>
   <td><input class="inlineMac" value="${macDisplay}"></td>
   <td><input class="inlineComment" value="${host.comment||''}"></td>`;

   async function save(){
      await fetch("/api/hosts/"+encodeURIComponent(host.ip),{
        method:"PUT",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          network_id:currentNetworkId,
          hostname:tr.querySelector(".inlineHostname").value,
          comment:tr.querySelector(".inlineComment").value,
          online:host.online||0,
          mac:tr.querySelector(".inlineMac").value
        })
      });
   }

   tr.querySelector(".inlineHostname").onchange=save;
   tr.querySelector(".inlineComment").onchange=save;
   tr.querySelector(".inlineMac").onchange=save;
   
   tr.querySelector(".ping-btn").onclick=async(e)=>{
     const ip=e.target.dataset.ip;
     const netId=parseInt(e.target.dataset.network);
     await pingSingleHost(netId, ip, tr);
   };

   tbody.appendChild(tr);
 });
}

document.getElementById('hostFilter').onchange=()=>{
  renderHosts(allHosts);
};

// Глобальный поиск
document.getElementById('globalSearch').oninput=(e)=>{
  globalSearchTerm=e.target.value;
  renderHosts(allHosts);
};

async function pingSingleHost(networkId, ip, rowElement){
  if(isPinging)return;
  isPinging=true;
  
  const btn=rowElement.querySelector('.ping-btn');
  const originalText=btn.textContent;
  btn.textContent='...';
  btn.disabled=true;
  
  try{
    const resp=await fetch("/api/hosts/"+encodeURIComponent(ip)+"/ping?network_id="+networkId,{method:"POST"});
    const result=await resp.json();
    
    if(result.status==='ok'){
      const statusDot=rowElement.querySelector('.status-dot');
      statusDot.className='status-dot '+(result.online?'status-online':'status-offline');
      statusDot.title='Последняя проверка: '+new Date().toLocaleString();
      rowElement.querySelector('.inlineHostname').value=result.manual_hostname||'';
      rowElement.querySelector('.inlineMac').value=result.mac||'';
      
      const hostIndex=allHosts.findIndex(h=>h.ip===ip);
      if(hostIndex!==-1){
        allHosts[hostIndex].online=result.online?1:0;
        allHosts[hostIndex].hostname=result.manual_hostname||'';
        allHosts[hostIndex].scanned_hostname=result.scanned_hostname||'';
        allHosts[hostIndex].mac=result.mac||'';
        allHosts[hostIndex].open_ports=result.open_ports||'';
        allHosts[hostIndex].last_ping=new Date().toLocaleString();
      }
      
      // Обновляем ячейку с портами
      const portsCell=rowElement.querySelector('.ports-cell');
      if(portsCell){
        portsCell.textContent=result.open_ports||'-';
      }
    }
  }catch(e){
    console.error("Error pinging host",ip,e);
  }finally{
    btn.textContent=originalText;
    btn.disabled=false;
    isPinging=false;
  }
}

async function pingNetwork(networkId){
  await fetch("/api/networks/"+networkId+"/ping",{method:"POST"});
  if(currentNetworkId===networkId){
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
   const interval=(parseInt(data.ping_interval)||60)*60*1000;
   
   pingIntervalTimer=setInterval(async ()=>{
     const res=await fetch(API);
     const networks=await res.json();
     for(const net of networks){
       try{
         await fetch("/api/networks/"+net.id+"/ping",{method:"POST"});
       }catch(e){
         console.error("Error pinging network "+net.id,e);
       }
     }
     if(currentNetworkId){
       const resp=await fetch("/api/networks/"+currentNetworkId+"/hosts");
       allHosts=await resp.json();
       renderHosts(allHosts);
     }
   },interval);
 });
}

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

// WORK PC модуль
let allWorkPcData=[];
let workPcHeaders=[];

async function loadWorkPcData(){
  try{
    const res=await fetch('/api/work_pc');
    if(!res.ok){
      console.error('Ошибка загрузки данных WORK PC:', res.status);
      return;
    }
    const result=await res.json();
    allWorkPcData=result.data||[];
    workPcHeaders=result.headers||[];
    renderWorkPcTable(allWorkPcData, workPcHeaders);
  }catch(e){
    console.error('Ошибка при загрузке WORK PC:', e);
  }
}

function renderWorkPcTable(data, headers){
  const tbody=document.getElementById('workPcTable');
  const theadRow=document.getElementById('workPcHeadersRow');
  tbody.innerHTML='';
  
  // Если есть заголовки из файла, обновляем шапку таблицы с фильтрами
  if(headers && headers.length > 0){
    theadRow.innerHTML='';
    headers.forEach((h, index)=>{
      const th=document.createElement('th');
      th.className='work-pc-header-cell';
      th.style.whiteSpace='nowrap';
      th.style.padding='8px';
      th.style.borderBottom='2px solid #ddd';
      th.style.position='relative';
      
      // Контейнер для содержимого заголовка
      const headerContent=document.createElement('div');
      headerContent.style.display='flex';
      headerContent.style.alignItems='center';
      headerContent.style.justifyContent='space-between';
      headerContent.style.gap='4px';
      
      const headerText=document.createElement('span');
      headerText.textContent=h;
      headerText.style.fontWeight='bold';
      headerText.style.flex='1';
      headerText.style.overflow='hidden';
      headerText.style.textOverflow='ellipsis';
      
      // Значок фильтра
      const filterIcon=document.createElement('span');
      filterIcon.textContent='🔽';
      filterIcon.className='filter-icon';
      filterIcon.style.cursor='pointer';
      filterIcon.style.fontSize='12px';
      filterIcon.style.padding='2px 4px';
      filterIcon.style.borderRadius='4px';
      filterIcon.style.background='#e0e0e0';
      filterIcon.title='Фильтр';
      filterIcon.dataset.columnIndex=index;
      
      headerContent.appendChild(headerText);
      headerContent.appendChild(filterIcon);
      th.appendChild(headerContent);
      
      // Выпадающий список (скрыт по умолчанию)
      const filterSelect=document.createElement('select');
      filterSelect.className='column-filter';
      filterSelect.dataset.columnIndex=index;
      filterSelect.style.display='none';
      filterSelect.style.position='absolute';
      filterSelect.style.top='100%';
      filterSelect.style.left='0';
      filterSelect.style.zIndex='1000';
      filterSelect.style.padding='4px';
      filterSelect.style.fontSize='12px';
      filterSelect.style.border='1px solid #ccc';
      filterSelect.style.borderRadius='4px';
      filterSelect.style.background='#fff';
      filterSelect.style.minWidth='150px';
      filterSelect.style.maxHeight='200px';
      filterSelect.style.overflowY='auto';
      filterSelect.style.boxShadow='0 2px 8px rgba(0,0,0,0.2)';
      
      // Добавляем опцию "Все"
      const allOption=document.createElement('option');
      allOption.value='';
      allOption.textContent='Все';
      filterSelect.appendChild(allOption);
      
      // Собираем уникальные значения для этой колонки
      const uniqueValues=new Set();
      data.forEach(pc=>{
        const values=Object.values(pc);
        if(index < values.length && values[index]){
          uniqueValues.add(values[index]);
        }
      });
      
      // Сортируем уникальные значения и добавляем в select
      Array.from(uniqueValues).sort().forEach(val=>{
        const option=document.createElement('option');
        option.value=val;
        option.textContent=val;
        filterSelect.appendChild(option);
      });
      
      filterSelect.onchange=()=>{
        filterWorkPcTable();
      };
      
      // Показываем/скрываем фильтр по клику на значок
      filterIcon.onclick=(e)=>{
        e.stopPropagation();
        const currentIndex=filterIcon.dataset.columnIndex;
        
        // Проверяем, открыт ли уже этот фильтр
        const isCurrentlyVisible=filterSelect.style.display==='block';
        
        // Скрываем все фильтры
        document.querySelectorAll('.column-filter').forEach(s=>{
          s.style.display='none';
        });
        document.querySelectorAll('.filter-icon').forEach(icon=>{
          icon.style.background='#e0e0e0';
          icon.style.color='inherit';
        });
        
        // Если фильтр был закрыт, открываем его
        if(!isCurrentlyVisible){
          filterSelect.style.display='block';
          filterIcon.style.background='#1976d2';
          filterIcon.style.color='#fff';
        }
      };
      
      th.appendChild(filterSelect);
      theadRow.appendChild(th);
    });
  }
  
  // Закрываем фильтры при клике вне таблицы или вне текущего заголовка
  document.addEventListener('click', (e)=>{
    if(!e.target.closest('.filter-icon') && !e.target.closest('.column-filter')){
      document.querySelectorAll('.column-filter').forEach(s=>s.style.display='none');
      document.querySelectorAll('.filter-icon').forEach(icon=>{
        icon.style.background='#e0e0e0';
        icon.style.color='inherit';
      });
    }
  });
  
  filterWorkPcTable(data);
}

function filterWorkPcTable(data=allWorkPcData){
  const tbody=document.getElementById('workPcTable');
  tbody.innerHTML='';
  
  // Получаем значения всех фильтров (теперь это select)
  const filters=[];
  document.querySelectorAll('.column-filter').forEach(select=>{
    filters.push(select.value);
  });
  
  // Глобальный поиск
  const globalSearchTerm=document.getElementById('workPcSearch').value.toLowerCase();
  
  let filteredData=data.filter(pc=>{
    const values=Object.values(pc);
    
    // Проверка по глобальному поиску
    if(globalSearchTerm){
      const searchStr=values.join(' ').toLowerCase();
      if(!searchStr.includes(globalSearchTerm)) return false;
    }
    
    // Проверка по фильтрам колонок
    for(let i=0; i<filters.length; i++){
      if(filters[i] && i<values.length){
        if(values[i] !== filters[i]) return false;
      }
    }
    
    return true;
  });
  
  // Сортировка по дате авторизации (первая колонка) - свежие записи в начале
  filteredData.sort((a, b)=>{
    const valuesA=Object.values(a);
    const valuesB=Object.values(b);
    if(valuesA.length > 0 && valuesB.length > 0){
      const dateA=new Date(valuesA[0]);
      const dateB=new Date(valuesB[0]);
      if(!isNaN(dateA) && !isNaN(dateB)){
        return dateB - dateA; // По убыванию (свежие сначала)
      }
    }
    return 0;
  });
  
  filteredData.forEach(pc=>{
    const tr=document.createElement('tr');
    const values=Object.values(pc);
    values.forEach(val=>{
      const td=document.createElement('td');
      td.textContent=val||'-';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

document.getElementById('workPcSearch').oninput=()=>{
  filterWorkPcTable(allWorkPcData);
};

document.getElementById('refreshWorkPcBtn').onclick=async()=>{
  try{
    const res=await fetch('/api/work_pc/refresh', {method:'POST'});
    const result=await res.json();
    if(result.status==='ok' || result.status==='warning'){
      alert(`Данные обновлены. Записей: ${result.records_count||0}`);
      loadWorkPcData();
    }else{
      alert('Ошибка обновления: '+ (result.message||'Неизвестная ошибка'));
    }
  }catch(e){
    console.error('Ошибка при обновлении WORK PC:', e);
    alert('Ошибка при обновлении данных');
  }
};
