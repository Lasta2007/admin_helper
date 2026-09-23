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

// WORK PC модуль с использованием DataTables и SearchPanes
let allWorkPcData=[];
let workPcHeaders=[];
let workPcTable=null;

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
  const tableElement=document.getElementById('workPcTable');
  
  // Всегда создаем правильную структуру таблицы перед инициализацией DataTables
  $('#workPcTable').empty();
  
  // Создаем заголовки таблицы даже если данных нет
  const thead=$('#workPcTable thead');
  thead.empty();
  const headerRow=$('<tr></tr>');
  
  if(headers && headers.length>0){
    headers.forEach(h=>{
      headerRow.append($('<th></th>').text(h));
    });
  } else {
    headerRow.append($('<th></th>').text('Нет данных'));
  }
  thead.append(headerRow);
  
  // Создаем тело таблицы
  const tbody=$('#workPcTable tbody');
  tbody.empty();
  
  // Преобразуем данные в формат для DataTables (массив массивов)
  const tableData=[];
  if(data && data.length>0 && headers && headers.length>0){
    data.forEach(pc=>{
      const row=[];
      headers.forEach(h=>{
        const cellValue=pc[h] !== undefined ? pc[h] : '-';
        row.push(cellValue);
      });
      tableData.push(row);
    });
  }
  
  // Уничтожаем предыдущий экземпляр DataTables если он существует
  if(workPcTable){
    workPcTable.destroy();
    workPcTable=null;
  }
  
  // Инициализируем DataTables с columnControl и searchList
  workPcTable=$('#workPcTable').DataTable({
    data: tableData,
    columnControl: [['searchList']],
    order: [[0, 'desc']], // Сортировка по первому столбцу (дата) по убыванию
    columns: headers && headers.length>0 ? headers.map(h=>({
      title: h,
      searchable: true,
      orderable: true // Разрешаем сортировку по клику на заголовки
    })) : [{title: 'Нет данных', searchable: false, orderable: false}],
    language: {
      search: 'Поиск:',
      lengthMenu: 'Показать _MENU_ записей',
      info: 'Показано с _START_ по _END_ из _TOTAL_ записей',
      infoEmpty: 'Нет записей',
      infoFiltered: '(отфильтровано из _MAX_ записей)',
      zeroRecords: 'Записи отсутствуют',
      paginate: {
        first: 'Первая',
        last: 'Последняя',
        next: 'Следующая',
        previous: 'Предыдущая'
      }
    },
    paging: false,
    pageLength: -1,
    responsive: true,
    autoWidth: false,
    scrollX: true,
    createdRow: function(row, data, dataIndex){
      $(row).css('font-size', '12px');
    },
    drawCallback: function(settings){
      $('#workPcTable thead th').css({'font-size': '12px', 'white-space': 'normal'});
      $('#workPcTable tbody td').css({'font-size': '12px', 'white-space': 'normal'});
    }
  });
}

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

// ALD Pro модуль
let aldProSettings = {
  url: '',
  login: '',
  password: '',
  cookies: ''
};
let selectedOuDn = null;

// Загрузка настроек ALD Pro при старте
async function loadAldProSettings() {
  try {
    const res = await fetch('/api/aldpro/settings');
    if (res.ok) {
      const data = await res.json();
      aldProSettings = data;
      document.getElementById('aldProUrlInput').value = aldProSettings.url || '';
      document.getElementById('aldProLoginInput').value = aldProSettings.login || '';
      document.getElementById('aldProPasswordInput').value = aldProSettings.password || '';
    }
  } catch (e) {
    console.error('Ошибка загрузки настроек ALD Pro:', e);
  }
}

// Сохранение настроек ALD Pro
document.getElementById('saveAldProSettingsBtn').onclick = async () => {
  const settings = {
    url: document.getElementById('aldProUrlInput').value.trim(),
    login: document.getElementById('aldProLoginInput').value.trim(),
    password: document.getElementById('aldProPasswordInput').value
  };
  
  try {
    const res = await fetch('/api/aldpro/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(settings)
    });
    
    if (res.ok) {
      alert('Настройки ALD Pro сохранены');
      aldProSettings = settings;
    } else {
      const error = await res.json();
      alert('Ошибка сохранения: ' + (error.detail || 'Неизвестная ошибка'));
    }
  } catch (e) {
    console.error('Ошибка сохранения настроек ALD Pro:', e);
    alert('Ошибка при сохранении настроек');
  }
};

// Проверка подключения к ALD Pro
document.getElementById('testAldProConnectionBtn').onclick = async () => {
  const settings = {
    url: document.getElementById('aldProUrlInput').value.trim(),
    login: document.getElementById('aldProLoginInput').value.trim(),
    password: document.getElementById('aldProPasswordInput').value
  };
  
  try {
    const res = await fetch('/api/aldpro/test-connection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(settings)
    });
    
    const result = await res.json();
    if (res.ok && result.success) {
      alert('Подключение успешно! Сессия установлена.');
      aldProSettings = {...settings, cookies: result.cookies};
    } else {
      alert('Ошибка подключения: ' + (result.detail || 'Неизвестная ошибка'));
    }
  } catch (e) {
    console.error('Ошибка проверки подключения:', e);
    alert('Ошибка при проверке подключения');
  }
};

// Показать страницу ALD Pro
function showAldPro() {
  document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
  document.getElementById('aldProNav').classList.add('active');
  document.getElementById('netView').classList.add('hidden');
  document.getElementById('hostView').classList.add('hidden');
  document.getElementById('settingsView').classList.add('hidden');
  document.getElementById('workPcView').classList.add('hidden');
  document.getElementById('aldProView').classList.remove('hidden');
  
  loadAldProOrganizationalUnits();
}

document.getElementById('aldProNav').onclick = () => showAldPro();

document.getElementById('aldProBackBtn').onclick = (e) => {
  e.preventDefault();
  showIPAM();
};

document.getElementById('refreshAldProBtn').onclick = () => {
  loadAldProOrganizationalUnits();
};

// Загрузка организационных подразделений ALD Pro
async function loadAldProOrganizationalUnits() {
  const treeContainer = document.getElementById('aldProTree');
  const usersContainer = document.getElementById('aldProUsers');
  
  treeContainer.innerHTML = '<div class="ald-pro-loading">Загрузка данных ALD Pro...</div>';
  usersContainer.innerHTML = '';
  
  try {
    const res = await fetch('/api/aldpro/organizational-units');
    if (!res.ok) {
      const error = await res.json();
      treeContainer.innerHTML = '<div class="ald-pro-error">Ошибка: ' + (error.detail || 'Не удалось загрузить подразделения') + '</div>';
      return;
    }
    
    const result = await res.json();
    if (result.success && result.data) {
      renderAldProTree(result.data, treeContainer);
    } else {
      treeContainer.innerHTML = '<div class="ald-pro-error">Ошибка получения данных</div>';
    }
  } catch (e) {
    console.error('Ошибка загрузки подразделений ALD Pro:', e);
    treeContainer.innerHTML = '<div class="ald-pro-error">Ошибка сети: ' + e.message + '</div>';
  }
}

// Рендеринг дерева подразделений
function renderAldProTree(units, container) {
  if (!units || units.length === 0) {
    container.innerHTML = '<div class="ald-pro-loading">Подразделения не найдены</div>';
    return;
  }
  
  let html = '<h3>Организационные подразделения</h3><ul>';
  html += renderAldProTreeRecursive(units);
  html += '</ul>';
  container.innerHTML = html;

  // Раскрываем первый уровень дерева, чтобы дочерние подразделения
  // были видны сразу (иначе отображается только корневая запись)
  container.querySelectorAll(':scope > ul > li > ul').forEach(ul => {
    ul.style.display = '';
    const arrow = ul.parentElement.querySelector('.ald-pro-arrow');
    if (arrow) arrow.classList.add('expanded');
  });

  // Обработчики кликов по подразделениям
  container.querySelectorAll('.ald-pro-item').forEach(item => {
    item.onclick = (e) => {
      e.stopPropagation();
      container.querySelectorAll('.ald-pro-item').forEach(i => i.classList.remove('selected'));
      item.classList.add('selected');
      selectedOuDn = item.dataset.dn;
      loadAldProUsers(selectedOuDn);
    };
  });

  // Клик по стрелке — свернуть/развернуть ветку
  container.querySelectorAll('.ald-pro-arrow').forEach(arrow => {
    arrow.onclick = (e) => {
      e.stopPropagation();
      const li = arrow.closest('li');
      const sub = li ? li.querySelector(':scope > ul') : null;
      if (!sub) return;
      const hidden = sub.style.display === 'none';
      sub.style.display = hidden ? '' : 'none';
      arrow.classList.toggle('expanded', hidden);
    };
  });
}

// Рекурсивная функция для рендеринга дерева
function renderAldProTreeRecursive(units, level = 0) {
  if (!units || units.length === 0) return '';

  let html = '';
  units.forEach(unit => {
    const displayName = unit.organizationunitlistitem_display_name || unit.organizationunitlistitem_ou || 'Без названия';
    const isLeaf = unit.organizationunitlistitem_is_leaf;
    const icon = isLeaf ? '📁' : '📂';
    const hasChildren = unit.children && unit.children.length > 0;
    const dn = unit.organizationunitlistitem_dn || '';

    html += `<li>`;
    html += `<div class="ald-pro-item" data-dn="${dn}" data-name="${displayName}">`;
    // Добавляем стрелку для элементов с детьми
    if (hasChildren) {
      html += `<span class="ald-pro-arrow">▶</span>`;
    } else {
      html += `<span class="ald-pro-arrow" style="visibility: hidden;">▶</span>`;
    }
    html += `<span class="ald-pro-icon">${icon}</span>`;
    html += `<span class="ald-pro-name">${displayName}</span>`;
    html += `</div>`;

    // Рекурсивно рендерим дочерние подразделения
    if (hasChildren) {
      html += '<ul style="display: none;">';
      html += renderAldProTreeRecursive(unit.children, level + 1);
      html += '</ul>';
    }

    html += `</li>`;
  });

  return html;
}

// Загрузка пользователей подразделения
async function loadAldProUsers(ouDn) {
  const usersContainer = document.getElementById('aldProUsers');
  
  if (!ouDn) {
    usersContainer.innerHTML = '<div class="ald-pro-loading">Выберите подразделение для просмотра пользователей</div>';
    return;
  }
  
  usersContainer.innerHTML = '<div class="ald-pro-loading">Загрузка пользователей...</div>';
  
  try {
    // Кодируем DN для URL
    const encodedDn = encodeURIComponent(ouDn);
    const res = await fetch(`/api/aldpro/organizational-units/${encodedDn}/users-list`);
    
    if (!res.ok) {
      const error = await res.json();
      usersContainer.innerHTML = '<div class="ald-pro-error">Ошибка: ' + (error.detail || 'Не удалось загрузить пользователей') + '</div>';
      return;
    }
    
    const result = await res.json();
    if (result.success && result.data) {
      renderAldProUsers(result.data, usersContainer);
    } else {
      usersContainer.innerHTML = '<div class="ald-pro-loading">Пользователи не найдены</div>';
    }
  } catch (e) {
    console.error('Ошибка загрузки пользователей ALD Pro:', e);
    usersContainer.innerHTML = '<div class="ald-pro-error">Ошибка сети: ' + e.message + '</div>';
  }
}

// Рендеринг таблицы пользователей
function renderAldProUsers(users, container) {
  if (!users || users.length === 0) {
    container.innerHTML = '<h3>Пользователи</h3><div class="ald-pro-loading">Пользователи не найдены</div>';
    return;
  }
  
  let html = '<h3>Пользователи (' + users.length + ')</h3>';
  html += '<table><thead><tr><th>Логин</th><th>ФИО</th><th>Должность</th><th>Статус</th></tr></thead><tbody>';
  
  users.forEach(user => {
    const login = user.userlistitem_login || '-';
    const fullName = user.userlistitem_common_name || '-';
    const title = user.userlistitem_title || '-';
    const locked = user.userlistitem_locked;
    const statusHtml = locked 
      ? '<span style="color:#f44336">🔒 Заблокирован</span>' 
      : '<span style="color:#4caf50">✓ Активен</span>';
    
    html += `<tr>
      <td>${login}</td>
      <td>${fullName}</td>
      <td>${title}</td>
      <td>${statusHtml}</td>
    </tr>`;
  });
  
  html += '</tbody></table>';
  container.innerHTML = html;
}

// Загружаем настройки ALD Pro при загрузке страницы
document.addEventListener('DOMContentLoaded', function() {
  loadAldProSettings();
});
