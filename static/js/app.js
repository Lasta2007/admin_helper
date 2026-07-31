
const API='/api/networks';
let selected=null;

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

async function openNetwork(network){
 document.getElementById("netView").classList.add("hidden");
 document.getElementById("hostView").classList.remove("hidden");
 document.getElementById("networkTitle").textContent="Подсеть "+network.cidr;

 const resp=await fetch("/api/networks/"+network.id+"/hosts");
 const hosts=await resp.json();

 const tbody=document.getElementById("hosts");
 tbody.innerHTML="";

 hosts.forEach(host=>{
   const tr=document.createElement("tr");
   tr.innerHTML=`<td>${host.ip}</td>
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
}

document.getElementById("backBtn").onclick=(e)=>{
 e.preventDefault();
 document.getElementById("hostView").classList.add("hidden");
 document.getElementById("netView").classList.remove("hidden");
};

load();



let editNetworkId=null;
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


// ===== PATCH =====
window.addEventListener("DOMContentLoaded", () => {
    const add=document.getElementById("addBtn");
    const edit=document.getElementById("editBtn");
    const save=document.getElementById("saveNetworkBtn");
    const cancel=document.getElementById("cancelNetworkBtn");

    if(add){
        add.onclick=()=>openNetworkModal("Добавить подсеть");
    }

    if(edit){
        edit.onclick=()=>{
            if(!selected){
                alert("Выберите подсеть");
                return;
            }
            editNetworkId=selected.id;
            openNetworkModal(
                "Редактировать подсеть",
                selected.cidr,
                selected.description||""
            );
        };
    }

    if(save){
        save.onclick=async()=>{
            const body={
                cidr:cidrInput.value.trim(),
                description:descInput.value.trim()
            };

            const url=editNetworkId?API+"/"+editNetworkId:API;
            const method=editNetworkId?"PUT":"POST";

            const r=await fetch(url,{
                method,
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify(body)
            });

            if(r.ok){
                closeNetworkModal();
                await load();
            }else{
                alert(await r.text());
            }
        };
    }

    if(cancel){
        cancel.onclick=closeNetworkModal;
    }
});
