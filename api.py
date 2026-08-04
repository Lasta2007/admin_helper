import asyncio
import subprocess
import socket
import re
import ipaddress
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from functools import lru_cache

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
    migrate_db,
    save_host_with_ports,
    update_online_with_ports,
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('admin_helper')

router = APIRouter(prefix="/api", tags=["api"])

# Семафор для ограничения количества одновременных ping-запросов
# Предотвращает перегрузку сети при сканировании больших подсетей
PING_SEMAPHORE = asyncio.Semaphore(20)

# Кэш для DNS/NetBIOS запросов (TTL 5 минут)
hostname_cache = {}
hostname_cache_time = {}
HOSTNAME_CACHE_TTL = 300  # секунд

# Кэш для сканирования портов (TTL 1 час)
port_scan_cache = {}
port_scan_cache_time = {}
PORT_SCAN_CACHE_TTL = 3600  # секунд

# Известные порты для сканирования
KNOWN_PORTS = {
    21: 'FTP',
    22: 'SSH',
    53: 'DNS',
    80: 'HTTP',
    443: 'HTTPS',
    3306: 'MySQL',
    3389: 'RDP',
    5432: 'PostgreSQL',
    6379: 'Redis',
    8080: 'HTTP-Alt',
    27017: 'MongoDB',
}


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
    port_scan_enabled: int = 0
    port_scan_interval: int = 1440


def validate_cidr(cidr: str):
    import ipaddress
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная подсеть: {e}")


async def get_netbios_name(ip: str, timeout: float = 2.0) -> str:
    """Получение NetBIOS имени через UDP запрос на порт 137."""
    logger.info(f"[get_netbios_name] Запрос NetBIOS имени для IP: {ip}")
    try:
        # NetBIOS Node Status Request packet
        # Формат запроса согласно RFC 1002
        netbios_packet = bytes([
            0x82, 0x54,  # Transaction ID (случайное)
            0x00, 0x00,  # Flags: Standard query
            0x00, 0x01,  # Questions: 1
            0x00, 0x00,  # Answer RRs: 0
            0x00, 0x00,  # Authority RRs: 0
            0x00, 0x00,  # Additional RRs: 0
            0x20,        # Length of name (32 символа encoded)
            # Encoded name: "* " (wildcard) padded to 16 chars then encoded
            # Символ '*' = 0x41, пробел ' ' = 0x40 в NetBIOS encoding
            # Для Node Status Request используем wildcard имя
            0x43, 0x4b, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
            0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
            0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
            0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41,
            0x00,        # Null terminator for name
            0x00, 0x21,  # Type: NBSTAT (33) - Node Status Request
            0x00, 0x01,  # Class: IN
        ])
        
        loop = asyncio.get_event_loop()
        
        def send_netbios_query():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            try:
                sock.sendto(netbios_packet, (ip, 137))
                data, _ = sock.recvfrom(2048)
                return data
            finally:
                sock.close()
        
        response = await loop.run_in_executor(None, send_netbios_query)
        
        if len(response) < 57:
            logger.warning(f"[get_netbios_name] Слишком короткий ответ от {ip} (длина: {len(response)})")
            return ""
        
        # Парсим ответ: количество имен в байте 56
        name_count = response[56]
        logger.debug(f"[get_netbios_name] Получено имен от {ip}: {name_count}")
        
        if name_count == 0 or name_count > 30:
            logger.warning(f"[get_netbios_name] Неверное количество имен: {name_count}")
            return ""
        
        offset = 57
        netbios_name = ""
        
        for i in range(name_count):
            if offset + 18 > len(response):
                break
            
            # Имя занимает 15 байт, 16-й байт - тип
            raw_name = response[offset:offset+15]
            name_type = response[offset+15]
            flags_byte = response[offset+16] if offset+16 < len(response) else 0
            
            # Декодируем имя (пробелы в конце обрезаем)
            try:
                decoded_name = raw_name.decode('ascii', errors='ignore').rstrip()
            except Exception:
                decoded_name = ""
            
            logger.debug(f"[get_netbios_name] Найдено имя: '{decoded_name}', тип: 0x{name_type:02X}, флаги: 0x{flags_byte:02X}")
            
            # Тип 0x00 - это уникальное имя узла (Workstation/Server name) - приоритет
            # Тип 0x20 - Server Service (также содержит имя компьютера)
            # Сначала ищем тип 0x00, если не нашли - берем 0x20
            if name_type == 0x00 and decoded_name:
                netbios_name = decoded_name
                logger.info(f"[get_netbios_name] Для IP {ip} получено NetBIOS имя: {netbios_name} (тип: 0x{name_type:02X})")
                return netbios_name
            elif name_type == 0x20 and decoded_name and not netbios_name:
                netbios_name = decoded_name
                logger.debug(f"[get_netbios_name] Найдено резервное имя (тип 0x20): {netbios_name}")
            
            offset += 18
        
        if netbios_name:
            logger.info(f"[get_netbios_name] Для IP {ip} получено NetBIOS имя (тип 0x20): {netbios_name}")
            return netbios_name
        
        logger.warning(f"[get_netbios_name] Не удалось найти NetBIOS имя в ответе от {ip}")
        return ""
        
    except socket.timeout:
        logger.debug(f"[get_netbios_name] Таймаут при запросе NetBIOS для {ip}")
        return ""
    except Exception as e:
        logger.warning(f"[get_netbios_name] Ошибка при получении NetBIOS имени для {ip}: {type(e).__name__}: {e}")
        return ""


def _clean_hostname_cache():
    """Очистка устаревших записей кэша hostname."""
    import time
    current_time = time.time()
    expired_ips = [
        ip for ip, cache_time in hostname_cache_time.items()
        if current_time - cache_time > HOSTNAME_CACHE_TTL
    ]
    for ip in expired_ips:
        hostname_cache.pop(ip, None)
        hostname_cache_time.pop(ip, None)


async def get_hostname(ip: str) -> str:
    """Получение hostname через reverse DNS lookup или NetBIOS с кэшированием."""
    import time
    
    # Проверяем кэш
    current_time = time.time()
    if ip in hostname_cache and (current_time - hostname_cache_time.get(ip, 0)) < HOSTNAME_CACHE_TTL:
        logger.debug(f"[get_hostname] Кэш хит для IP: {ip}")
        return hostname_cache[ip]
    
    logger.info(f"[get_hostname] Запрос hostname для IP: {ip}")
    
    # Попытка 1: Reverse DNS lookup
    try:
        hostname, _, _ = await asyncio.get_event_loop().run_in_executor(
            None, socket.gethostbyaddr, ip
        )
        # Возвращаем только короткое имя (до первой точки)
        short_hostname = hostname.split('.')[0]
        logger.info(f"[get_hostname] Для IP {ip} получен hostname через DNS: {short_hostname} (полный: {hostname})")
        
        # Сохраняем в кэш
        hostname_cache[ip] = short_hostname
        hostname_cache_time[ip] = current_time
        return short_hostname
    except (socket.herror, socket.gaierror) as e:
        logger.debug(f"[get_hostname] Reverse DNS не удался для {ip}: {type(e).__name__}: {e}")
    except Exception as e:
        logger.warning(f"[get_hostname] Ошибка reverse DNS для {ip}: {type(e).__name__}: {e}")
    
    # Попытка 2: NetBIOS query (для Windows машин в локальной сети)
    logger.info(f"[get_hostname] Попытка получения NetBIOS имени для {ip}")
    netbios_name = await get_netbios_name(ip, timeout=2.0)
    if netbios_name:
        logger.info(f"[get_hostname] Для IP {ip} получено NetBIOS имя: {netbios_name}")
        
        # Сохраняем в кэш
        hostname_cache[ip] = netbios_name
        hostname_cache_time[ip] = current_time
        return netbios_name
    
    logger.warning(f"[get_hostname] Не удалось получить hostname/NetBIOS для IP {ip}")
    
    # Сохраняем пустой результат в кэш чтобы избежать повторных запросов
    hostname_cache[ip] = ""
    hostname_cache_time[ip] = current_time
    return ""


async def get_mac_address(ip: str) -> str:
    """Получение MAC адреса через ARP таблицу и SNMP."""
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
        
        # Попытка через SNMP (если предыдущие методы не сработали)
        logger.info(f"[get_mac_address] Локальные методы не сработали, попытка получения MAC через SNMP для {ip}")
        try:
            # Определяем подсеть IP адреса для поиска шлюза
            ip_obj = ipaddress.ip_address(ip)
            # Получаем все подсети из БД чтобы найти подходящую
            from database import get_networks
            networks = get_networks()
            gateway_ip = None
            
            for net in networks:
                try:
                    network = ipaddress.ip_network(net['cidr'], strict=False)
                    if ip_obj in network:
                        # Предполагаем что шлюз это первый адрес в подсети (обычно .1 или .254)
                        # В реальном сценарии лучше хранить шлюз явно в БД
                        # Здесь пытаемся найти шлюз перебором常见 вариантов
                        possible_gateways = [
                            str(network.network_address + 1),  # Первый хост
                            str(network.broadcast_address - 1),  # Последний хост (часто .254)
                        ]
                        for gw in possible_gateways:
                            # Проверяем доступен ли шлюз
                            gw_process = await asyncio.create_subprocess_exec(
                                "ping", "-c", "1", "-W", "1", gw,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE
                            )
                            gw_stdout, _ = await gw_process.communicate()
                            if gw_process.returncode == 0:
                                gateway_ip = gw
                                logger.info(f"[get_mac_address] Найден шлюз {gateway_ip} для подсети {net['cidr']}")
                                break
                        if gateway_ip:
                            break
                except Exception as e:
                    logger.debug(f"[get_mac_address] Ошибка при обработке подсети {net['cidr']}: {e}")
                    continue
            
            if gateway_ip:
                logger.info(f"[get_mac_address] Выполнение snmpwalk для шлюза {gateway_ip} и IP {ip}")
                # Используем snmpwalk для получения MAC через таблицу ipNetToMediaPhysAddress (1.3.6.1.2.1.4.22.1.2)
                snmp_process = await asyncio.create_subprocess_exec(
                    "snmpwalk", "-v2c", "-c", "public", gateway_ip, "1.3.6.1.2.1.4.22.1.2",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                snmp_stdout, snmp_stderr = await snmp_process.communicate()
                
                if snmp_process.returncode == 0:
                    snmp_output = snmp_stdout.decode()
                    logger.debug(f"[get_mac_address] SNMP walk результат: {snmp_output[:1000]}")
                    
                    # Ищем строку содержащую наш IP
                    # Формат может быть разным:
                    # 1) SNMPv2-SMI::mib-2.4.22.1.2.X.X.X.X = STRING: aa:bb:cc:dd:ee:ff
                    # 2) iso.3.6.1.2.1.4.22.1.2.X.X.X.X = Hex-STRING: 00 20 6B FD D0 EA
                    for line in snmp_output.split('\n'):
                        if ip in line:
                            logger.debug(f"[get_mac_address] Найдена строка SNMP для {ip}: {line}")
                            
                            # Попытка 1: MAC в формате aa:bb:cc:dd:ee:ff или aa-bb-cc-dd-ee-ff
                            match = re.search(r'=.*?([0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2}[:-][0-9a-fA-F]{2})', line)
                            if match:
                                mac = match.group(1).lower()
                                logger.info(f"[get_mac_address] Для IP {ip} получен MAC через SNMP (формат с разделителями): {mac}")
                                return mac
                            
                            # Попытка 2: MAC в формате Hex-STRING: 00 20 6B FD D0 EA
                            hex_match = re.search(r'Hex-STRING:\s*([0-9a-fA-F]{2}\s+[0-9a-fA-F]{2}\s+[0-9a-fA-F]{2}\s+[0-9a-fA-F]{2}\s+[0-9a-fA-F]{2}\s+[0-9a-fA-F]{2})', line, re.IGNORECASE)
                            if hex_match:
                                hex_mac = hex_match.group(1)
                                # Преобразуем "00 20 6B FD D0 EA" в "00:20:6b:fd:d0:ea"
                                mac = ':'.join(hex_mac.split()).lower()
                                logger.info(f"[get_mac_address] Для IP {ip} получен MAC через SNMP (Hex-STRING): {mac}")
                                return mac
                            
                            # Попытка 3: Универсальный поиск любой последовательности из 6 байт
                            # Ищем 6 групп по 2 шестнадцатеричных цифры разделенных пробелами или другими символами
                            universal_match = re.search(r'=.*?([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})\s+([0-9a-fA-F]{2})(?!\s*[0-9a-fA-F]{2})', line)
                            if universal_match:
                                mac_bytes = [universal_match.group(i).lower() for i in range(1, 7)]
                                mac = ':'.join(mac_bytes)
                                logger.info(f"[get_mac_address] Для IP {ip} получен MAC через SNMP (универсальный парсинг): {mac}")
                                return mac
                                
                    logger.warning(f"[get_mac_address] IP {ip} не найден в SNMP ответе от {gateway_ip} или не удалось распарсить MAC")
                    logger.debug(f"[get_mac_address] Полный SNMP вывод: {snmp_output[:2000]}")
                else:
                    snmp_error = snmp_stderr.decode()
                    logger.warning(f"[get_mac_address] SNMP walk вернул ошибку: {snmp_error[:200]}")
            else:
                logger.warning(f"[get_mac_address] Не удалось определить шлюз для IP {ip}")
                
        except FileNotFoundError:
            logger.debug("[get_mac_address] Команда 'snmpwalk' не найдена в системе")
        except Exception as e:
            logger.warning(f"[get_mac_address] Ошибка при выполнении SNMP запроса: {type(e).__name__}: {e}")
        
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
            host = stored[ip_str]
            # Объединяем scanned_hostname и hostname для отображения
            # scanned_hostname показывается под IP мелким шрифтом
            # hostname - это поле для ручного заполнения
            result.append({
                "id": host.get("id"),
                "network_id": host.get("network_id", network_id),
                "ip": ip_str,
                "hostname": host.get("hostname", ""),  # Ручное поле
                "scanned_hostname": host.get("scanned_hostname", ""),  # Сканированное поле
                "comment": host.get("comment", ""),
                "online": host.get("online", 0),
                "mac": host.get("mac", ""),
                "open_ports": host.get("open_ports", ""),
                "last_ping": host.get("last_ping"),
            })
        else:
            result.append({
                "id": None,
                "network_id": network_id,
                "ip": ip_str,
                "hostname": "",
                "scanned_hostname": "",
                "comment": "",
                "online": 0,
                "mac": "",
                "open_ports": "",
                "last_ping": None,
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
    # Обновляем настройки сканирования портов если они переданы
    if hasattr(data, 'port_scan_enabled') and data.port_scan_enabled is not None:
        set_setting("port_scan_enabled", str(data.port_scan_enabled))
    if hasattr(data, 'port_scan_interval') and data.port_scan_interval is not None:
        set_setting("port_scan_interval", str(data.port_scan_interval))
    return {"status": "ok"}


# ----------------------------------------------------
# Ping functionality
# ----------------------------------------------------

async def ping_host(ip: str, timeout: int = 3) -> tuple[bool, str, str, str]:
    """
    Выполняет ping указанного хоста и получает hostname/mac/open_ports.
    Возвращает кортеж (is_online, hostname, mac, open_ports).
    timeout - таймаут в секундах (по умолчанию 3)
    Использует семафор для ограничения параллелизма.
    """
    logger.info(f"[ping_host] Начало пинга для IP: {ip}, таймаут: {timeout}с")
    
    # Используем семафор для ограничения количества одновременных запросов
    async with PING_SEMAPHORE:
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
            open_ports = ""
            
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
                
                # Сканируем открытые порты если включено в настройках
                port_scan_enabled = get_setting("port_scan_enabled", "0") == "1"
                if port_scan_enabled:
                    logger.info(f"[ping_host] Хост {ip} доступен, сканирование портов...")
                    try:
                        open_ports = await scan_ports(ip)
                        if open_ports:
                            logger.info(f"[ping_host] Для {ip} найдены открытые порты: {open_ports}")
                        else:
                            logger.debug(f"[ping_host] Для {ip} открытых портов не найдено")
                    except Exception as e:
                        logger.error(f"[ping_host] Ошибка при сканировании портов для {ip}: {type(e).__name__}: {e}")
            else:
                logger.warning(f"[ping_host] Хост {ip} недоступен, получение hostname/mac/ports пропущено")
            
            logger.info(f"[ping_host] Завершение пинга для {ip}: online={is_online}, hostname='{hostname}', mac='{mac}', ports='{open_ports}'")
            return is_online, hostname, mac, open_ports
        except Exception as e:
            logger.error(f"[ping_host] Критическая ошибка при пинге {ip}: {type(e).__name__}: {e}")
            return False, "", "", ""


async def scan_ports(ip: str, timeout: float = 1.0) -> str:
    """
    Сканирует известные порты на хосте.
    Возвращает строку с перечнем открытых портов в формате: HTTP(80),SSH(22)
    """
    import time
    
    # Проверяем кэш
    current_time = time.time()
    if ip in port_scan_cache and (current_time - port_scan_cache_time.get(ip, 0)) < PORT_SCAN_CACHE_TTL:
        logger.debug(f"[scan_ports] Кэш хит для IP: {ip}")
        return port_scan_cache[ip]
    
    logger.info(f"[scan_ports] Начало сканирования портов для IP: {ip}")
    open_ports_list = []
    
    # Создаем задачи для проверки каждого порта
    async def check_port(port: int, service: str) -> Optional[Tuple[int, str]]:
        try:
            loop = asyncio.get_event_loop()
            
            def try_connect():
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, port))
                sock.close()
                return result == 0
            
            is_open = await loop.run_in_executor(None, try_connect)
            if is_open:
                return (port, service)
        except Exception as e:
            logger.debug(f"[scan_ports] Ошибка при проверке порта {port} для {ip}: {e}")
        return None
    
    # Проверка всех портов параллельно с ограничением
    port_semaphore = asyncio.Semaphore(50)  # Ограничение до 50 одновременных подключений
    
    async def limited_check_port(port: int, service: str):
        async with port_semaphore:
            return await check_port(port, service)
    
    tasks = [limited_check_port(port, service) for port, service in KNOWN_PORTS.items()]
    results = await asyncio.gather(*tasks)
    
    for result in results:
        if result:
            port, service = result
            open_ports_list.append(f"{service}({port})")
    
    # Формируем результирующую строку
    open_ports_str = ",".join(open_ports_list)
    
    # Сохраняем в кэш
    port_scan_cache[ip] = open_ports_str
    port_scan_cache_time[ip] = current_time
    
    logger.info(f"[scan_ports] Завершение сканирования для {ip}: найдено портов={len(open_ports_list)}")
    return open_ports_str


async def ping_all_hosts_parallel(hosts: list, network_id: int, timeout: int = 3):
    """
    Выполняет параллельный ping всех хостов в списке с ограничением параллелизма.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[ping_all_hosts_parallel] Начало параллельного пинга {len(hosts)} хостов для подсети ID={network_id}")
    
    # Очищаем кэш hostname перед началом сканирования
    _clean_hostname_cache()
    
    async def ping_and_update(host):
        ip = host["ip"]
        logger.debug(f"[ping_and_update] Обработка хоста {ip}...")
        is_online, hostname, mac, open_ports = await ping_host(ip, timeout)
        # Получаем текущие значения из БД для сохранения существующих hostname и mac
        current_host = get_host(network_id, ip)
        # scanned_hostname - это hostname полученный при сканировании (DNS/NetBIOS)
        # hostname - это поле для ручного заполнения пользователем
        update_scanned_hostname = hostname if hostname else (current_host["scanned_hostname"] if current_host and "scanned_hostname" in current_host else "")
        update_mac = mac if mac else (current_host["mac"] if current_host else "")
        update_open_ports = open_ports if open_ports else (current_host.get("open_ports", "") if current_host else "")
        # Сохраняем ручной hostname без изменений
        manual_hostname = current_host["hostname"] if current_host else ""
        logger.debug(f"[ping_and_update] Обновление БД для {ip}: online={is_online}, scanned_hostname='{update_scanned_hostname}', manual_hostname='{manual_hostname}', mac='{update_mac}', ports='{update_open_ports}'")
        # Если хост доступен - создаем/обновляем запись, если нет - только обновляем статус если запись существует
        if is_online:
            save_host_with_ports(
                network_id=network_id,
                ip=ip,
                hostname=manual_hostname,  # Сохраняем ручной hostname
                comment="",
                online=1,
                mac=update_mac,
                last_ping=now,
                open_ports=update_open_ports,
                scanned_hostname=update_scanned_hostname  # Сохраняем сканированный hostname
            )
        else:
            # Для недоступных хостов всегда создаем/обновляем запись, чтобы сохранить last_ping
            save_host_with_ports(
                network_id=network_id,
                ip=ip,
                hostname=manual_hostname,  # Сохраняем ручной hostname
                comment="",
                online=0,
                mac=update_mac,
                last_ping=now,
                open_ports=update_open_ports,
                scanned_hostname=update_scanned_hostname  # Сохраняем сканированный hostname
            )
        return host["ip"], is_online
    
    # Используем gather для параллельного выполнения с ограничением через семафор
    tasks = [ping_and_update(host) for host in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = sum(1 for r in results if isinstance(r, tuple) and len(r) == 2 and r[1])
    error_count = sum(1 for r in results if isinstance(r, Exception))
    logger.info(f"[ping_all_hosts_parallel] Завершение пинга: всего={len(hosts)}, успешно={success_count}, ошибок={error_count}")
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

    # Генерируем все хосты из подсети, а не только те, что есть в БД
    net = ipaddress.ip_network(network["cidr"], strict=False)
    hosts_to_ping = [{"ip": str(ip)} for ip in net.hosts()]
    
    timeout = int(get_setting("ping_timeout", "3"))
    
    logger.info(f"[api_ping_network] Пинг {len(hosts_to_ping)} хостов в подсети {network['cidr']} с таймаутом {timeout}с")
    await ping_all_hosts_parallel(hosts_to_ping, network_id, timeout)

    logger.info(f"[api_ping_network] Пинг подсети ID={network_id} завершен")
    return {"status": "ok", "pinged": len(hosts_to_ping)}


@router.post("/hosts/{ip}/ping")
async def api_ping_single_host(ip: str, network_id: int = Query(...)):
    """
    Выполняет ping конкретного хоста и обновляет его статус, scanned_hostname (DNS/NetBIOS) и mac.
    Поле hostname (ручное) не изменяется.
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
    is_online, scanned_hostname, mac, open_ports = await ping_host(ip, timeout)
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Получаем текущие значения hostname (ручное) и mac, если не получили новые
    current_host = get_host(network_id, ip)
    # scanned_hostname - это hostname полученный при сканировании (DNS/NetBIOS)
    update_scanned_hostname = scanned_hostname if scanned_hostname else (current_host["scanned_hostname"] if current_host and "scanned_hostname" in current_host else "")
    update_mac = mac if mac else (current_host["mac"] if current_host else "")
    update_open_ports = open_ports if open_ports else (current_host.get("open_ports", "") if current_host else "")
    # Сохраняем ручной hostname без изменений
    manual_hostname = current_host["hostname"] if current_host else ""
    
    logger.info(f"[api_ping_single_host] Результат для {ip}: online={is_online}, scanned_hostname='{update_scanned_hostname}', manual_hostname='{manual_hostname}', mac='{update_mac}', ports='{update_open_ports}'")
    
    # Если хост существует - обновляем, иначе создаем новую запись
    if current_host:
        logger.info(f"[api_ping_single_host] Обновление существующей записи для {ip}")
        update_online_with_ports(network_id, ip, 1 if is_online else 0, now, manual_hostname, update_mac, update_open_ports, update_scanned_hostname)
    else:
        logger.info(f"[api_ping_single_host] Создание новой записи для {ip}")
        save_host_with_ports(
            network_id=network_id,
            ip=ip,
            hostname=manual_hostname,  # Ручное поле hostname
            comment="",
            online=1 if is_online else 0,
            mac=update_mac,
            last_ping=now,
            open_ports=update_open_ports,
            scanned_hostname=update_scanned_hostname  # Сканированный hostname
        )
    
    logger.info(f"[api_ping_single_host] Завершение обработки хоста {ip}")
    return {
        "status": "ok",
        "ip": ip,
        "online": is_online,
        "scanned_hostname": update_scanned_hostname,
        "manual_hostname": manual_hostname,
        "mac": update_mac,
        "open_ports": update_open_ports
    }