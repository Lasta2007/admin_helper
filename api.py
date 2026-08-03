from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import ipaddress
import subprocess
import asyncio
from datetime import datetime

from database import (
    get_networks,
    get_network,
    add_network,
    update_network,
    delete_network,
    get_hosts,
    save_host,
    get_all_settings,
    set_setting,
    update_online,
)

router = APIRouter(prefix="/api", tags=["api"])


class NetworkIn(BaseModel):
    cidr: str
    description: str = ""


class HostUpdate(BaseModel):
    network_id: int
    hostname: str = ""
    comment: str = ""
    online: int = 0


class SettingsUpdate(BaseModel):
    ping_interval: int


def validate_cidr(cidr: str):
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная подсеть: {e}")


@router.get("/networks")
def api_get_networks():
    return get_networks()


@router.post("/networks")
def api_add_network(data: NetworkIn):
    validate_cidr(data.cidr)
    try:
        new_id = add_network(data.cidr, data.description)
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "id": new_id,
        "cidr": data.cidr,
        "description": data.description,
    }


@router.put("/networks/{network_id}")
def api_update_network(network_id: int, data: NetworkIn):
    validate_cidr(data.cidr)

    if get_network(network_id) is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    update_network(network_id, data.cidr, data.description)
    return {"status": "ok"}


@router.delete("/networks/{network_id}")
def api_delete_network(network_id: int):
    if get_network(network_id) is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    delete_network(network_id)
    return {"status": "ok"}


@router.get("/networks/{network_id}/hosts")
def api_get_hosts(network_id: int):
    network = get_network(network_id)
    if network is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    net = ipaddress.ip_network(network["cidr"], strict=False)
    stored = {h["ip"]: h for h in get_hosts(network_id)}

    result = []
    for ip in net.hosts():
        ip_str = str(ip)
        if ip_str in stored:
            result.append(stored[ip_str])
        else:
            result.append({
                "id": None,
                "network_id": network_id,
                "ip": ip_str,
                "hostname": "",
                "comment": "",
                "online": 0,
            })
    return result


@router.put("/hosts/{ip}")
def api_update_host(ip: str, data: HostUpdate):
    save_host(
        network_id=data.network_id,
        ip=ip,
        hostname=data.hostname,
        comment=data.comment,
        online=data.online,
    )
    return {"status": "ok"}


# ----------------------------------------------------
# Settings API
# ----------------------------------------------------

@router.get("/settings")
def api_get_settings():
    return get_all_settings()


@router.put("/settings")
def api_update_settings(data: SettingsUpdate):
    set_setting("ping_interval", str(data.ping_interval))
    return {"status": "ok"}


# ----------------------------------------------------
# Ping functionality
# ----------------------------------------------------

async def ping_host(ip: str) -> bool:
    """
    Выполняет ping указанного хоста.
    Возвращает True если хост доступен, иначе False.
    """
    try:
        # Используем subprocess для выполнения ping команды
        # -c 1: отправить 1 пакет
        # -w 1: таймаут 1 секунда (Linux)
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-w", "1", ip,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        await process.wait()
        return process.returncode == 0
    except Exception:
        return False


@router.post("/networks/{network_id}/ping")
async def api_ping_network(network_id: int):
    """
    Выполняет ping всех хостов в указанной подсети
    и обновляет их статус доступности.
    """
    network = get_network(network_id)
    if network is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    hosts = get_hosts(network_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for host in hosts:
        is_online = await ping_host(host["ip"])
        update_online(network_id, host["ip"], 1 if is_online else 0, now)

    return {"status": "ok", "pinged": len(hosts)}