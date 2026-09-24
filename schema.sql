-- Полная схема базы данных Admin Helper.
-- Фактическое создание/миграция структуры выполняется в database.migrate_db(),
-- этот файл служит справочным описанием актуальной схемы.

CREATE TABLE IF NOT EXISTS networks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cidr TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hosts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id INTEGER NOT NULL,
    ip TEXT NOT NULL,
    hostname TEXT DEFAULT '',            -- поле для ручного заполнения
    scanned_hostname TEXT DEFAULT '',    -- имя, полученное при сканировании (DNS/NetBIOS)
    comment TEXT DEFAULT '',
    online INTEGER DEFAULT 0,
    mac TEXT DEFAULT '',
    last_ping TEXT,
    open_ports TEXT DEFAULT '',
    UNIQUE(network_id, ip),
    FOREIGN KEY(network_id) REFERENCES networks(id)
);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_pc(
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
);

-- Модуль Яндекс 360: соответствие OU ALD Pro -> департамент Яндекс 360
-- (ключ вида 'dep:<dn>')
CREATE TABLE IF NOT EXISTS y360_sync_map(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Модуль Яндекс 360: сопоставление пользователей ALD Pro и сотрудников 360
CREATE TABLE IF NOT EXISTS y360_user_map(
    login TEXT PRIMARY KEY,
    email TEXT DEFAULT '',
    ou_dn TEXT DEFAULT '',
    dept_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
