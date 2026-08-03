import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "admin_helper.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Создает таблицы при первом запуске.
    """
    conn = get_connection()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS networks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cidr TEXT UNIQUE NOT NULL,
        description TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS hosts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        network_id INTEGER NOT NULL,
        ip TEXT NOT NULL,
        hostname TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        online INTEGER DEFAULT 0,
        mac TEXT DEFAULT '',
        last_ping TEXT,
        UNIQUE(network_id, ip),
        FOREIGN KEY(network_id) REFERENCES networks(id)
    );

    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO settings(key, value) VALUES('ping_interval', '60');
    INSERT OR IGNORE INTO settings(key, value) VALUES('ping_timeout', '3');
    """)

    conn.commit()
    conn.close()


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

    conn.commit()

    network_id = cursor.lastrowid

    conn.close()

    return network_id


def update_network(network_id: int, cidr: str, description: str):
    conn = get_connection()

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
              mac: str = ''):
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
                mac
            )
            VALUES(?,?,?,?,?,?)
        """, (
            network_id,
            ip,
            hostname,
            comment,
            online,
            mac
        ))

    else:

        conn.execute("""
            UPDATE hosts
            SET hostname=?,
                comment=?,
                online=?,
                mac=?
            WHERE id=?
        """, (
            hostname,
            comment,
            online,
            mac,
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