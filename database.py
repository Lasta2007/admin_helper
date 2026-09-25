import logging
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "admin_helper.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

logger = logging.getLogger('admin_helper')


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(cursor, table_name: str) -> bool:
    """Проверяет существование таблицы в БД."""
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def _hosts_unique_exists(cursor) -> bool:
    """Проверяет наличие UNIQUE-ограничения (network_id, ip) в таблице hosts."""
    for row in cursor.execute("PRAGMA index_list(hosts)").fetchall():
        idx_name = row['name']
        cols = [r['name'] for r in cursor.execute(f"PRAGMA index_info('{idx_name}')").fetchall()]
        if cols == ['network_id', 'ip']:
            return True
    return False


def migrate_db():
    """
    Миграция структуры базы данных.
    Добавляет новые колонки и таблицы при необходимости.
    Вызывается при каждом запуске приложения.
    Идемпотентна: безопасна для повторных вызовов.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Создаем таблицу networks если не существует
    if not _table_exists(cursor, 'networks'):
        cursor.execute("""
        CREATE TABLE networks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidr TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT ''
        )
        """)
        logger.info("[migrate_db] Таблица 'networks' создана")

    # Создаем таблицу hosts если не существует
    if not _table_exists(cursor, 'hosts'):
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
    else:
        # Проверяем наличие новых колонок в существующей таблице hosts
        columns = cursor.execute("PRAGMA table_info(hosts)").fetchall()
        column_names = [c['name'] for c in columns]

        if 'scanned_hostname' not in column_names:
            cursor.execute("ALTER TABLE hosts ADD COLUMN scanned_hostname TEXT DEFAULT ''")
            logger.info("[migrate_db] Добавлена колонка 'scanned_hostname' в таблицу 'hosts'")

        if 'open_ports' not in column_names:
            cursor.execute("ALTER TABLE hosts ADD COLUMN open_ports TEXT DEFAULT ''")
            logger.info("[migrate_db] Добавлена колонка 'open_ports' в таблицу 'hosts'")

        # Миграция старых БД: добавляем ограничение уникальности (network_id, ip),
        # если оно отсутствует (старая схема его не содержала)
        if not _hosts_unique_exists(cursor):
            cursor.executescript("""
                DELETE FROM hosts WHERE id NOT IN (
                    SELECT MIN(id) FROM hosts GROUP BY network_id, ip
                );
                CREATE TABLE hosts_new(
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
                );
                INSERT INTO hosts_new(id, network_id, ip, hostname, scanned_hostname,
                                      comment, online, mac, last_ping, open_ports)
                SELECT id, network_id, ip, hostname,
                       COALESCE(scanned_hostname, ''),
                       comment, online, mac, last_ping,
                       COALESCE(open_ports, '')
                FROM hosts;
                DROP TABLE hosts;
                ALTER TABLE hosts_new RENAME TO hosts;
            """)
            logger.info("[migrate_db] Добавлено UNIQUE-ограничение (network_id, ip) в таблицу 'hosts'")

    # Создаем таблицу settings если не существует
    if not _table_exists(cursor, 'settings'):
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

    # Создаем таблицу work_pc если не существует (модуль WORK PC)
    if not _table_exists(cursor, 'work_pc'):
        cursor.execute("""
        CREATE TABLE work_pc(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT DEFAULT '',
            os_version TEXT DEFAULT '',
            kernel_version TEXT DEFAULT '',
            computer_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            ip_type TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            mac_address TEXT DEFAULT '',
            motherboard TEXT DEFAULT '',
            hdd_free TEXT DEFAULT '',
            swap TEXT DEFAULT '',
            cpu TEXT DEFAULT '',
            disk_type TEXT DEFAULT '',
            r7_version TEXT DEFAULT '',
            kav_version TEXT DEFAULT '',
            csp_version TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
        """)
        logger.info("[migrate_db] Таблица 'work_pc' создана")

    # Создаем таблицу y360_sync_map если не существует (модуль Яндекс 360:
    # соответствие OU ALD Pro -> департамент Яндекс 360)
    if not _table_exists(cursor, 'y360_sync_map'):
        cursor.execute("""
        CREATE TABLE y360_sync_map(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
        logger.info("[migrate_db] Таблица 'y360_sync_map' создана")

    # Создаем таблицу y360_user_map если не существует (модуль Яндекс 360:
    # сопоставление пользователей ALD Pro и сотрудников Яндекс 360)
    if not _table_exists(cursor, 'y360_user_map'):
        cursor.execute("""
        CREATE TABLE y360_user_map(
            login TEXT PRIMARY KEY,
            email TEXT DEFAULT '',
            ou_dn TEXT DEFAULT '',
            dept_id TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
        """)
        logger.info("[migrate_db] Таблица 'y360_user_map' создана")

    # Единое хранилище настроек модулей (JSON-документы). Настройки всех
    # модулей (Яндекс 360, синхронизация, ALD Pro) лежат здесь, а не в
    # коде, поэтому не перезаписываются при слиянии веток git.
    if not _table_exists(cursor, 'module_settings'):
        cursor.execute("""
        CREATE TABLE module_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT DEFAULT ''
        )
        """)
        logger.info("[migrate_db] Таблица 'module_settings' создана")

    conn.commit()
    conn.close()
    logger.info("[migrate_db] Миграция базы данных завершена")


def init_db():
    """
    Устаревшая функция, оставлена для совместимости.
    Теперь используется migrate_db().
    """
    migrate_db()


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


# ----------------------------------------------------
# Module settings (JSON-документы настроек модулей)
# ----------------------------------------------------
# Хранятся в отдельной таблице module_settings: каждое значение — JSON.
# Настройки живут в БД (admin_helper.db), а не в коде/файлах репозитория,
# поэтому не перезаписываются при слиянии веток git и не попадают в diff.

def get_module_settings(key: str, default=None):
    """Прочитать настройки модуля (JSON-документ) из таблицы module_settings."""
    conn = get_connection()
    try:
        if not _table_exists(conn.cursor(), 'module_settings'):
            return default if default is not None else {}
        row = conn.execute(
            "SELECT value FROM module_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return default if default is not None else {}

    import json
    try:
        data = json.loads(row["value"])
        return data if isinstance(data, dict) else (default or {})
    except (ValueError, TypeError):
        logger.warning(f"[get_module_settings] Неверный JSON в настройках '{key}'")
        return default if default is not None else {}


def set_module_settings(key: str, data: dict):
    """Сохранить настройки модуля (JSON-документ) в таблицу module_settings."""
    import json
    conn = get_connection()
    try:
        # Таблица создаётся в migrate_db(); на случай прямого вызова до
        # миграции гарантируем её наличие здесь.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS module_settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO module_settings(key, value, updated_at)
            VALUES(?, ?, datetime('now'))
        """, (key, json.dumps(data, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()