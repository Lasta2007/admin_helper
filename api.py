import asyncio
import subprocess
import socket
import re
import ipaddress
import logging
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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('admin_helper')

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
    logger.info(f"[get_hostname] Запрос hostname для IP: {ip}")
    try:
        hostname, _, _ = await asyncio.get_event_loop().run_in_executor(
            None, socket.gethostbyaddr, ip
        )
        # Возвращаем только короткое имя (до первой точки)
        short_hostname = hostname.split('.')[0]
        logger.info(f"[get_hostname] Для IP {ip} получен hostname: {short_hostname} (полный: {hostname})")
        return short_hostname
    except (socket.herror, socket.gaierror, Exception) as e:
        # Если reverse DNS не удался, возвращаем пустую строку
        logger.warning(f"[get_hostname] Не удалось получить hostname для IP {ip}: {type(e).__name__}: {e}")
        return ""


async def get_mac_address(ip: str) -> str:
    """Получение MAC адреса через ARP таблицу."""
    logger.info(f"[get_mac_address] Запрос MAC адреса для IP: {ip}")
    
    # Сначала делаем ping чтобы устройство появилось в ARP таблице
    try:
        logger.debug(f"[get_mac_address] Выполнение ping для обновления ARP таблицы: {ip}")
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "2", ip,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            logger.debug(f"[get_mac_address] Ping успешен, STDOUT: {stdout.decode()[:200]}")
        else:
            logger.warning(f"[get_mac_address] Ping вернул код {process.returncode}, STDERR: {stderr.decode()[:200]}")
        # Увеличенная задержка чтобы ARP таблица обновилась
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning(f"[get_mac_address] Ошибка при выполнении ping для {ip}: {e}")
    
    try:
        # Попытка через команду ip neigh (более современная)
        logger.debug(f"[get_mac_address] Попытка получения MAC через 'ip neigh': {ip}")
        result = await asyncio.create_subprocess_exec(
            "ip", "neigh", "show", ip,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        output = stdout.decode().strip()
        err_output = stderr.decode().strip()
        logger.debug(f"[get_mac_address] 'ip neigh' STDOUT: '{output}'")
        if err_output:
            logger.debug(f"[get_mac_address] 'ip neigh' STDERR: '{err_output}'")
        
        # Парсинг вывода: "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        match = re.search(r'lladdr\s+([0-9a-fA-F:]{17})', output)
        if match:
            mac = match.group(1).lower()
            logger.info(f"[get_mac_address] Для IP {ip} получен MAC через 'ip neigh': {mac}")
            return mac
        
        # Если lladdr не найден, проверяем статус записи
        if "REACHABLE" in output or "STALE" in output or "DELAY" in output:
            logger.debug(f"[get_mac_address] Запись в ARP таблице найдена, но MAC не указан в стандартном формате")
            # Пробуем найти MAC в другом формате
            mac_match = re.search(r'([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})', output)
            if mac_match:
                mac = mac_match.group(1).lower()
                logger.info(f"[get_mac_address] Для IP {ip} получен MAC через альтернативный парсинг 'ip neigh': {mac}")
                return mac
        
        # Попытка через чтение /proc/net/arp (прямой доступ к ARP таблице ядра)
        logger.debug(f"[get_mac_address] Попытка получения MAC через /proc/net/arp: {ip}")
        try:
            with open('/proc/net/arp', 'r') as f:
                arp_content = f.read()
                logger.debug(f"[get_mac_address] Содержимое /proc/net/arp:\n{arp_content[:500]}")
                lines = arp_content.strip().split('\n')[1:]  # Пропускаем заголовок
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == ip:
                        mac_candidate = parts[3]
                        if mac_candidate != "00:00:00:00:00:00":
                            mac = mac_candidate.lower()
                            logger.info(f"[get_mac_address] Для IP {ip} получен MAC через /proc/net/arp: {mac}")
                            return mac
                        else:
                            logger.debug(f"[get_mac_address] Запись для {ip} найдена, но MAC равен 00:00:00:00:00:00")
        except FileNotFoundError:
            logger.debug("[get_mac_address] Файл /proc/net/arp не найден")
        except Exception as e:
            logger.warning(f"[get_mac_address] Ошибка при чтении /proc/net/arp: {e}")
        
        # Попытка через команду arp (классическая) - только если команда доступна
        try:
            logger.debug(f"[get_mac_address] Попытка получения MAC через 'arp -n': {ip}")
            result = await asyncio.create_subprocess_exec(
                "arp", "-n", ip,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            output = stdout.decode().strip()
            err_output = stderr.decode().strip()
            logger.debug(f"[get_mac_address] 'arp -n' STDOUT: '{output}'")
            if err_output:
                logger.debug(f"[get_mac_address] 'arp -n' STDERR: '{err_output}'")
            
            # Парсинг вывода arp: "192.168.1.1  0x1  ether  aa:bb:cc:dd:ee:ff  C  eth0"
            lines = output.split('\n')
            for line in lines:
                if ip in line:
                    parts = line.split()
                    for part in parts:
                        if re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', part):
                            mac = part.lower()
                            logger.info(f"[get_mac_address] Для IP {ip} получен MAC через 'arp': {mac}")
                            return mac
        except FileNotFoundError:
            logger.debug("[get_mac_address] Команда 'arp' не найдена в системе")
        except Exception as e:
            logger.warning(f"[get_mac_address] Ошибка при выполнении команды 'arp': {e}")
        
        logger.warning(f"[get_mac_address] Не удалось получить MAC адрес для IP {ip}: запись не найдена в ARP таблице")
    except Exception as e:
        logger.error(f"[get_mac_address] Ошибка при получении MAC адреса для IP {ip}: {type(e).__name__}: {e}")
    
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
    logger.info(f"[ping_host] Начало пинга для IP: {ip}, таймаут: {timeout}с")
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
        
        logger.info(f"[ping_host] Результат пинга для {ip}: {'ONLINE' if is_online else 'OFFLINE'}")
        
        hostname = ""
        mac = ""
        
        if is_online:
            # Получаем hostname через reverse DNS lookup с использованием socket
            logger.info(f"[ping_host] Хост {ip} доступен, получение hostname...")
            try:
                hostname = await get_hostname(ip)
                if hostname:
                    logger.info(f"[ping_host] Для {ip} получен hostname: {hostname}")
                else:
                    logger.warning(f"[ping_host] Не удалось получить hostname для {ip}")
            except Exception as e:
                logger.error(f"[ping_host] Ошибка при получении hostname для {ip}: {type(e).__name__}: {e}")
            
            # Получаем MAC адрес через arp или ip neigh
            logger.info(f"[ping_host] Хост {ip} доступен, получение MAC адреса...")
            try:
                mac = await get_mac_address(ip)
                if mac:
                    logger.info(f"[ping_host] Для {ip} получен MAC адрес: {mac}")
                else:
                    logger.warning(f"[ping_host] Не удалось получить MAC адрес для {ip}")
            except Exception as e:
                logger.error(f"[ping_host] Ошибка при получении MAC адреса для {ip}: {type(e).__name__}: {e}")
        else:
            logger.warning(f"[ping_host] Хост {ip} недоступен, получение hostname/mac пропущено")
        
        logger.info(f"[ping_host] Завершение пинга для {ip}: online={is_online}, hostname='{hostname}', mac='{mac}'")
        return is_online, hostname, mac
    except Exception as e:
        logger.error(f"[ping_host] Критическая ошибка при пинге {ip}: {type(e).__name__}: {e}")
        return False, "", ""


async def ping_all_hosts_parallel(hosts: list, network_id: int, timeout: int = 3):
    """
    Выполняет параллельный ping всех хостов в списке.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[ping_all_hosts_parallel] Начало параллельного пинга {len(hosts)} хостов для подсети ID={network_id}")
    
    async def ping_and_update(host):
        ip = host["ip"]
        logger.info(f"[ping_and_update] Обработка хоста {ip}...")
        is_online, hostname, mac = await ping_host(ip, timeout)
        # Обновляем только если получили hostname или mac, иначе сохраняем старые значения
        update_hostname = hostname if hostname else host.get("hostname", "")
        update_mac = mac if mac else host.get("mac", "")
        logger.info(f"[ping_and_update] Обновление БД для {ip}: online={is_online}, hostname='{update_hostname}', mac='{update_mac}'")
        update_online(network_id, host["ip"], 1 if is_online else 0, now, update_hostname, update_mac)
        return host["ip"], is_online
    
    tasks = [ping_and_update(host) for host in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = sum(1 for r in results if isinstance(r, tuple) and len(r) == 2 and r[1])
    logger.info(f"[ping_all_hosts_parallel] Завершение пинга: всего={len(hosts)}, успешно={success_count}")
    return results


@router.post("/networks/{network_id}/ping")
async def api_ping_network(network_id: int):
    """
    Выполняет параллельный ping всех хостов в указанной подсети
    и обновляет их статус доступности, hostname и mac.
    Таймаут берется из настроек.
    """
    logger.info(f"[api_ping_network] Запрос на пинг подсети ID={network_id}")
    network = get_network(network_id)
    if network is None:
        logger.error(f"[api_ping_network] Подсеть ID={network_id} не найдена")
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    hosts = get_hosts(network_id)
    timeout = int(get_setting("ping_timeout", "3"))
    
    logger.info(f"[api_ping_network] Пинг {len(hosts)} хостов в подсети {network['cidr']} с таймаутом {timeout}с")
    await ping_all_hosts_parallel(hosts, network_id, timeout)

    logger.info(f"[api_ping_network] Пинг подсети ID={network_id} завершен")
    return {"status": "ok", "pinged": len(hosts)}


@router.post("/hosts/{ip}/ping")
async def api_ping_single_host(ip: str, network_id: int = Query(...)):
    """
    Выполняет ping конкретного хоста и обновляет его статус, hostname и mac.
    Таймаут берется из настроек.
    network_id передается как query-параметр
    """
    logger.info(f"[api_ping_single_host] Запрос на пинг хоста {ip} (подсеть ID={network_id})")
    network = get_network(network_id)
    if network is None:
        logger.error(f"[api_ping_single_host] Подсеть ID={network_id} не найдена")
        raise HTTPException(status_code=404, detail="Подсеть не найдена")

    timeout = int(get_setting("ping_timeout", "3"))
    logger.info(f"[api_ping_single_host] Пинг хоста {ip} с таймаутом {timeout}с")
    is_online, hostname, mac = await ping_host(ip, timeout)
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Получаем текущие значения hostname и mac, если не получили новые
    current_host = get_host(network_id, ip)
    update_hostname = hostname if hostname else (current_host["hostname"] if current_host else "")
    update_mac = mac if mac else (current_host["mac"] if current_host else "")
    
    logger.info(f"[api_ping_single_host] Результат для {ip}: online={is_online}, hostname='{update_hostname}', mac='{update_mac}'")
    
    # Если хост существует - обновляем, иначе создаем новую запись
    if current_host:
        logger.info(f"[api_ping_single_host] Обновление существующей записи для {ip}")
        update_online(network_id, ip, 1 if is_online else 0, now, update_hostname, update_mac)
    else:
        logger.info(f"[api_ping_single_host] Создание новой записи для {ip}")
        save_host(
            network_id=network_id,
            ip=ip,
            hostname=update_hostname,
            comment="",
            online=1 if is_online else 0,
            mac=update_mac
        )
    
    logger.info(f"[api_ping_single_host] Завершение обработки хоста {ip}")
    return {
        "status": "ok",
        "ip": ip,
        "online": is_online,
        "hostname": update_hostname,
        "mac": update_mac
    }