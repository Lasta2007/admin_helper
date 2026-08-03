import asyncio
import subprocess
import socket
import re
import ipaddress
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import (
    get_networks,
    get_network,
    add_network,
    update_network,
    delete_network,
    get_hosts,
    get_host,
    save_host,
    get_all_settings,
    get_setting,
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
    mac: str = ""


class SettingsUpdate(BaseModel):
    ping_interval: int
    ping_timeout: int = 3


def validate_cidr(cidr: str):
    import ipaddress
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная подсеть: {e}")


async def get_hostname(ip: str) -> str:
    """Получение hostname через reverse DNS lookup."""
    try:
        hostname, _, _ = await asyncio.get_event_loop().run_in_executor(
            None, socket.gethostbyaddr, ip
        )
        # Возвращаем только короткое имя (до первой точки)
        return hostname.split('.')[0]
    except (socket.herror, socket.gaierror, Exception):
        # Если reverse DNS не удался, возвращаем пустую строку
        return ""


async def get_mac_address(ip: str) -> str:
    """Получение MAC адреса через ARP таблицу."""
    # Сначала делаем ping чтобы устройство появилось в ARP таблице
    try:
        await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Небольшая задержка чтобы ARP таблица обновилась
        await asyncio.sleep(0.5)
    except Exception:
        pass
    
    try:
        # Попытка через команду ip neigh (более современная)
        result = await asyncio.create_subprocess_exec(
            "ip", "neigh", "show", ip,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, _ = await result.communicate()
        output = stdout.decode().strip()
        
        # Парсинг вывода: "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        match = re.search(r'lladdr\s+([0-9a-fA-F:]{17})', output)
        if match:
            return match.group(1).lower()
            
        # Попытка через команду arp (классическая)
        result = await asyncio.create_subprocess_exec(
            "arp", "-n", ip,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, _ = await result.communicate()
        output = stdout.decode().strip()
        
        # Парсинг вывода arp: "192.168.1.1  0x1  ether  aa:bb:cc:dd:ee:ff  C  eth0"
        lines = output.split('\n')
        for line in lines:
            if ip in line:
                parts = line.split()
                for part in parts:
                    if re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', part):
                        return part.lower()
    except Exception:
        pass
    
    return ""


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
        mac=data.mac,
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
    set_setting("ping_timeout", str(data.ping_timeout))
    return {"status": "ok"}


# ----------------------------------------------------
# Ping functionality
# ----------------------------------------------------

async def ping_host(ip: str, timeout: int = 3) -> tuple[bool, str, str]:
    """
    Выполняет ping указанного хоста и получает hostname/mac.
    Возвращает кортеж (is_online, hostname, mac).
    timeout - таймаут в секундах (по умолчанию 3)
    """
    try:
        # Используем subprocess для выполнения ping команды
        # -c 1: отправить 1 пакет
        # -W timeout: таймаут в секундах (Linux)
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout), ip,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        is_online = process.returncode == 0
        
        hostname = ""
        mac = ""
        
        if is_online:
            # Получаем hostname через reverse DNS lookup с использованием socket
            try:
                hostname = await get_hostname(ip)
            except Exception:
                pass
            
            # Получаем MAC адрес через arp или ip neigh
            try:
                mac = await get_mac_address(ip)
            except Exception:
                pass
        
        return is_online, hostname, mac
    except Exception:
        return False, "", ""


async def ping_all_hosts_parallel(hosts: list, network_id: int, timeout: int = 3):
    """
    Выполняет параллельный ping всех хостов в списке.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    async def ping_and_update(host):
        is_online, hostname, mac = await ping_host(host["ip"], timeout)
        # Обновляем только если получили hostname или mac, иначе сохраняем старые значения
        update_hostname = hostname if hostname else host.get("hostname", "")
        update_mac = mac if mac else host.get("mac", "")
        update_online(network_id, host["ip"], 1 if is_online else 0, now, update_hostname, update_mac)
        return host["ip"], is_online
    
    tasks = [ping_and_update(host) for host in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results


@router.post("/networks/{network_id}/ping")
async def api_ping_network(network_id: int):
    """
    Выполняет параллельный ping всех хостов в указанной подсети
    и обновляет их статус доступности, hostname и mac.
    Таймаут берется из настроек.
    """
    network = get_network(network_id)
    if network is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    hosts = get_hosts(network_id)
    timeout = int(get_setting("ping_timeout", "3"))
    
    await ping_all_hosts_parallel(hosts, network_id, timeout)

    return {"status": "ok", "pinged": len(hosts)}


@router.post("/hosts/{ip}/ping")
async def api_ping_single_host(ip: str, network_id: int = Query(...)):
    """
    Выполняет ping конкретного хоста и обновляет его статус, hostname и mac.
    Таймаут берется из настроек.
    network_id передается как query-параметр
    """
    network = get_network(network_id)
    if network is None:
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    timeout = int(get_setting("ping_timeout", "3"))
    is_online, hostname, mac = await ping_host(ip, timeout)
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Получаем текущие значения hostname и mac, если не получили новые
    current_host = get_host(network_id, ip)
    update_hostname = hostname if hostname else (current_host["hostname"] if current_host else "")
    update_mac = mac if mac else (current_host["mac"] if current_host else "")
    
    # Если хост существует - обновляем, иначе создаем новую запись
    if current_host:
        update_online(network_id, ip, 1 if is_online else 0, now, update_hostname, update_mac)
    else:
        save_host(
            network_id=network_id,
            ip=ip,
            hostname=update_hostname,
            comment="",
            online=1 if is_online else 0,
            mac=update_mac
        )
    
    return {
        "status": "ok",
        "ip": ip,
        "online": is_online,
        "hostname": update_hostname,
        "mac": update_mac
    }