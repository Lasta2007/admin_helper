import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "admin_helper.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_db():
    """
    Миграция структуры базы данных.
    Добавляет новые колонки и таблицы при необходимости.
    Вызывается при каждом запуске приложения.
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    # Получаем список существующих таблиц
    existing_tables = cursor.execute("""
        SELECT name FROM sqlite_master WHERE type='table'
    """).fetchall()
    table_names = [t['name'] for t in existing_tables]
    
    # Создаем таблицу networks если не существует
    if 'networks' not in table_names:
        cursor.execute("""
        CREATE TABLE networks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidr TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT ''
        )
        """)
        logger.info("[migrate_db] Таблица 'networks' создана")
    
    # Создаем таблицу hosts если не существует
    if 'hosts' not in table_names:
        cursor.execute("""
        CREATE TABLE hosts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            network_id INTEGER NOT NULL,
            ip TEXT NOT NULL,
            hostname TEXT DEFAULT '',
            scanned_hostname TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            online INTEGER DEFAULT 0,
            mac TEXT DEFAULT '',
            last_ping TEXT,
            open_ports TEXT DEFAULT '',
            UNIQUE(network_id, ip),
            FOREIGN KEY(network_id) REFERENCES networks(id)
        )
        """)
        logger.info("[migrate_db] Таблица 'hosts' создана")
    
    # Проверяем наличие колонки scanned_hostname в таблице hosts
    if 'hosts' in table_names:
        columns = cursor.execute("PRAGMA table_info(hosts)").fetchall()
        column_names = [c['name'] for c in columns]
        
        if 'scanned_hostname' not in column_names:
            cursor.execute("ALTER TABLE hosts ADD COLUMN scanned_hostname TEXT DEFAULT ''")
            logger.info("[migrate_db] Добавлена колонка 'scanned_hostname' в таблицу 'hosts'")
        
        if 'open_ports' not in column_names:
            cursor.execute("ALTER TABLE hosts ADD COLUMN open_ports TEXT DEFAULT ''")
            logger.info("[migrate_db] Добавлена колонка 'open_ports' в таблицу 'hosts'")
    
    # Создаем таблицу settings если не существует
    if 'settings' not in table_names:
        cursor.execute("""
        CREATE TABLE settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        logger.info("[migrate_db] Таблица 'settings' создана")
    
    # Вставляем настройки по умолчанию если их нет
    cursor.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('ping_interval', '60')")
    cursor.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('ping_timeout', '3')")
    cursor.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('port_scan_enabled', '0')")
    cursor.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('port_scan_interval', '1440')")
    
    conn.commit()
    conn.close()
    logger.info("[migrate_db] Миграция базы данных завершена")
    
    # Инициализируем таблицу work_pc
    init_work_pc_table_from_db()


# Импортируем logger после определения функций чтобы избежать циклического импорта
import logging
logger = logging.getLogger('admin_helper')


def init_db():
    """
    Устаревшая функция, оставлена для совместимости.
    Теперь используется migrate_db().
    """
    migrate_db()


def init_work_pc_table_from_db():
    """
    Инициализирует таблицу work_pc при миграции БД.
    Вызывается из migrate_db после создания основных таблиц.
    """
    from work_pc import init_work_pc_table as init_work_pc
    init_work_pc()


# ----------------------------------------------------
# Networks
# ----------------------------------------------------

def get_networks():
    conn = get_connection()

    rows = conn.execute("""
        SELECT *
        FROM networks
        ORDER BY cidr
    """).fetchall()

    conn.close()

    return [dict(r) for r in rows]


def get_network(network_id: int):
    conn = get_connection()

    row = conn.execute("""
        SELECT *
        FROM networks
        WHERE id=?
    """, (network_id,)).fetchone()

    conn.close()

    if row is None:
        return None

    return dict(row)


def add_network(cidr: str, description: str):
    conn = get_connection()

    cursor = conn.execute("""
        INSERT INTO networks(cidr, description)
        VALUES(?, ?)
    """, (cidr, description))

    network_id = cursor.lastrowid
    
    # Создаем записи для всех хостов в подсети
    import ipaddress
    net = ipaddress.ip_network(cidr, strict=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for ip in net.hosts():
        conn.execute("""
            INSERT OR IGNORE INTO hosts(network_id, ip, hostname, comment, online, mac, last_ping)
            VALUES(?, ?, '', '', 0, '', ?)
        """, (network_id, str(ip), now))

    conn.commit()

    conn.close()

    return network_id


def update_network(network_id: int, cidr: str, description: str):
    conn = get_connection()
    
    # Получаем текущий CIDR для сравнения
    current_network = get_network(network_id)
    old_cidr = current_network["cidr"] if current_network else None
    
    conn.execute("""
        UPDATE networks
        SET cidr=?,
            description=?
        WHERE id=?
    """, (
        cidr,
        description,
        network_id
    ))
    
    # Если CIDR изменился, обновляем хосты
    if old_cidr and old_cidr != cidr:
        import ipaddress
        
        # Удаляем хосты, которые больше не входят в новую подсеть
        old_net = ipaddress.ip_network(old_cidr, strict=False)
        new_net = ipaddress.ip_network(cidr, strict=False)
        
        old_ips = set(str(ip) for ip in old_net.hosts())
        new_ips = set(str(ip) for ip in new_net.hosts())
        
        # Удаляем хосты, которые были в старой подсети, но нет в новой
        ips_to_remove = old_ips - new_ips
        for ip in ips_to_remove:
            conn.execute("""
                DELETE FROM hosts
                WHERE network_id=? AND ip=?
            """, (network_id, ip))
        
        # Добавляем хосты, которые есть в новой подсети, но не было в старой
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ips_to_add = new_ips - old_ips
        for ip in ips_to_add:
            conn.execute("""
                INSERT OR IGNORE INTO hosts(network_id, ip, hostname, comment, online, mac, last_ping)
                VALUES(?, ?, '', '', 0, '', ?)
            """, (network_id, ip, now))

    conn.commit()
    conn.close()


def delete_network(network_id: int):
    conn = get_connection()

    conn.execute("""
        DELETE FROM hosts
        WHERE network_id=?
    """, (network_id,))

    conn.execute("""
        DELETE FROM networks
        WHERE id=?
    """, (network_id,))

    conn.commit()
    conn.close()


# ----------------------------------------------------
# Hosts
# ----------------------------------------------------

def get_hosts(network_id: int):
    conn = get_connection()

    rows = conn.execute("""
        SELECT *
        FROM hosts
        WHERE network_id=?
        ORDER BY ip
    """, (network_id,)).fetchall()

    conn.close()

    return [dict(r) for r in rows]


def get_host(network_id: int, ip: str):
    conn = get_connection()

    row = conn.execute("""
        SELECT *
        FROM hosts
        WHERE network_id=?
          AND ip=?
    """, (
        network_id,
        ip
    )).fetchone()

    conn.close()

    if row is None:
        return None

    return dict(row)


def save_host(network_id: int,
              ip: str,
              hostname: str,
              comment: str,
              online: int = 0,
              mac: str = '',
              last_ping: str = None):
    """
    Создает запись, если её нет,
    либо обновляет существующую.
    """

    conn = get_connection()

    row = conn.execute("""
        SELECT id
        FROM hosts
        WHERE network_id=?
          AND ip=?
    """, (
        network_id,
        ip
    )).fetchone()

    if row is None:

        conn.execute("""
            INSERT INTO hosts(
                network_id,
                ip,
                hostname,
                comment,
                online,
                mac,
                last_ping
            )
            VALUES(?,?,?,?,?,?,?)
        """, (
            network_id,
            ip,
            hostname,
            comment,
            online,
            mac,
            last_ping
        ))

    else:

        conn.execute("""
            UPDATE hosts
            SET hostname=?,
                comment=?,
                online=?,
                mac=?,
                last_ping=?
            WHERE id=?
        """, (
            hostname,
            comment,
            online,
            mac,
            last_ping,
            row["id"]
        ))

    conn.commit()
    conn.close()


def update_online(network_id: int,
                  ip: str,
                  online: int,
                  last_ping: str,
                  hostname: str = '',
                  mac: str = ''):
    conn = get_connection()

    conn.execute("""
        UPDATE hosts
        SET online=?,
            last_ping=?,
            hostname=?,
            mac=?
        WHERE network_id=?
          AND ip=?
    """, (
        online,
        last_ping,
        hostname,
        mac,
        network_id,
        ip
    ))

    conn.commit()
    conn.close()


def save_host_with_ports(network_id: int,
              ip: str,
              hostname: str,
              comment: str,
              online: int = 0,
              mac: str = '',
              last_ping: str = None,
              open_ports: str = '',
              scanned_hostname: str = ''):
    """
    Создает запись, если её нет,
    либо обновляет существующую (с поддержкой open_ports и scanned_hostname).
    """

    conn = get_connection()

    row = conn.execute("""
        SELECT id
        FROM hosts
        WHERE network_id=?
          AND ip=?
    """, (
        network_id,
        ip
    )).fetchone()

    if row is None:

        conn.execute("""
            INSERT INTO hosts(
                network_id,
                ip,
                hostname,
                comment,
                online,
                mac,
                last_ping,
                open_ports,
                scanned_hostname
            )
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            network_id,
            ip,
            hostname,
            comment,
            online,
            mac,
            last_ping,
            open_ports,
            scanned_hostname
        ))

    else:

        conn.execute("""
            UPDATE hosts
            SET hostname=?,
                comment=?,
                online=?,
                mac=?,
                last_ping=?,
                open_ports=?,
                scanned_hostname=?
            WHERE id=?
        """, (
            hostname,
            comment,
            online,
            mac,
            last_ping,
            open_ports,
            scanned_hostname,
            row["id"]
        ))

    conn.commit()
    conn.close()


def update_online_with_ports(network_id: int,
                  ip: str,
                  online: int,
                  last_ping: str,
                  hostname: str = '',
                  mac: str = '',
                  open_ports: str = '',
                  scanned_hostname: str = ''):
    conn = get_connection()

    conn.execute("""
        UPDATE hosts
        SET online=?,
            last_ping=?,
            hostname=?,
            mac=?,
            open_ports=?,
            scanned_hostname=?
        WHERE network_id=?
          AND ip=?
    """, (
        online,
        last_ping,
        hostname,
        mac,
        open_ports,
        scanned_hostname,
        network_id,
        ip
    ))

    conn.commit()
    conn.close()


# ----------------------------------------------------
# Settings
# ----------------------------------------------------

def get_setting(key: str, default: str = None):
    conn = get_connection()

    row = conn.execute("""
        SELECT value
        FROM settings
        WHERE key=?
    """, (key,)).fetchone()

    conn.close()

    if row is None:
        return default

    return row["value"]


def set_setting(key: str, value: str):
    conn = get_connection()

    conn.execute("""
        INSERT OR REPLACE INTO settings(key, value)
        VALUES(?, ?)
    """, (key, value))

    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_connection()

    rows = conn.execute("""
        SELECT key, value
        FROM settings
        ORDER BY key
    """).fetchall()

    conn.close()

    return {row["key"]: row["value"] for row in rows}