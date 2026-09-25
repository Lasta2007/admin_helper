# -*- coding: utf-8 -*-
"""
Модуль синхронизации ALD Pro -> Яндекс 360 (выгрузка пользователей).

Загружает в Яндекс 360 структуру подразделений, начиная с заданного OU
ALD Pro (корень задаётся в настройках), и пользователей из этого subtree.

Правила синхронизации:
  * Головной (корневой) OU ALD Pro из синхронизации ИСКЛЮЧЁН — он не
    создаётся в Яндекс 360; выгружается только его поддерево (дочерние OU).
    Сотрудники, привязанные непосредственно к головному OU, определяются в
    корневые подразделения Яндекс 360 (без departmentId).
  * OU ALD Pro создаются в Яндекс 360 как департаменты
    (POST /v1/directory/organizations/{org_id}/departments), иерархия
    сохраняется через parentDepartmentId. Соответствие OU <-> департамент
    хранится в локальной БД (таблица y360_sync_map) и восстанавливается по
    примечанию департамента "ald_pro_dn=<dn>".
  * Считается, что электронная почта у пользователей ALD Pro есть по
    умолчанию: адрес берётся из атрибута mail, а если API ALD Pro его не
    вернул — формируется из логина (login@домен из настроек).
  * Существующие сотрудники Яндекс 360 проверяются на наличие в ALD Pro
    (по логину и по e-mail):
      - если сотрудника нет в ALD Pro — он блокируется (blocked=true);
      - принадлежность к подразделению сверяется по departmentId: при
        переносе пользователя между OU в ALD Pro пользователь переносится
        в соответствующий департамент Яндекс 360
        (PATCH /v1/directory/organizations/{org_id}/users/{login}).
  * Новые пользователи создаются через UserService_Create (POST
    /v1/directory/organizations/{org_id}/users) с логином ALD Pro и
    паролем-заглушкой; приглашение на e-mail не отправляется, чтобы не
    рассылать письма при выгрузке. Если создание через API недоступно
    (HTTP 405/403 — устаревший хост или отсутствие прав directory:write_*),
    пользователь попадает в план ручного создания.

Интервал автоматической синхронизации задаётся в настройках модуля
(поле sync_interval_minutes) и обрабатывается фоновой задачей в main.py.
"""

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import ald_pro
import yandex360
from database import get_setting, set_setting, DB_PATH

logger = logging.getLogger('admin_helper')

SETTINGS_KEY = 'y360_sync_settings'

DEFAULT_SETTINGS = {
    # Корневой OU ALD Pro, начиная с которого выгружается структура
    'root_ou_dn': '',
    # Интервал синхронизации данных между ALD Pro и Яндекс 360 (минуты)
    'sync_interval_minutes': 60,
    # Домен корпоративной почты Яндекс 360 (для новых учетных записей)
    'email_domain': '',
    # Блокировать в Яндекс 360 сотрудников, отсутствующих в ALD Pro
    'block_missing_users': True,
    # ID родительского департамента Яндекс 360 для корневого OU ALD Pro
    # (пусто — корневой OU создаётся на верхнем уровне)
    'parent_department_id': '',
    # DN технических/служебных OU, поддеревья которых НЕ выгружаются вовсе
    # (например контейнеры пользователей и компьютеров домена), один на
    # строку. Совпадение — по окончанию DN без учёта регистра.
    'exclude_ou_dns': ('cn=users,cn=accounts\n'
                       'cn=computers,cn=accounts\n'
                       'cn=builtin,cn=accounts'),
}

# Страницы выдачи Directory API (максимум по документации — 5000)
PAGE_LIMIT = 500
# Ограничение параллельности запросов к API Яндекс 360
API_CONCURRENCY = 4
# Максимальная глубина обхода дерева OU ALD Pro (защита от зацикливания)
MAX_OU_DEPTH = 25
# Сколько дочерних OU обходится параллельно на одном уровне дерева
OU_LEVEL_BATCH = 8


def _norm_dn(dn: str) -> str:
    """DN для сравнения: без пробелов вокруг запятых, нижний регистр."""
    return ','.join(p.strip().lower() for p in (dn or '').split(',') if p.strip())


def _is_excluded_dn(dn: str, excluded: List[str]) -> bool:
    """True, если dn сам является исключённым или лежит внутри исключённого."""
    ndn = _norm_dn(dn)
    if not ndn:
        return False
    for ex in excluded:
        nex = _norm_dn(ex)
        if not nex:
            continue
        if ndn == nex or ndn.endswith(',' + nex):
            return True
    return False


def get_excluded_ou_dns(settings: Optional[Dict[str, Any]] = None) -> List[str]:
    """Список DN исключаемых OU из настроек (по строке на каждый DN)."""
    settings = settings or get_sync_settings()
    raw = settings.get('exclude_ou_dns') or ''
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r'[\n;]+', str(raw))
    return [i.strip() for i in items if i and i.strip()]


def get_sync_settings() -> Dict[str, Any]:
    """Получить настройки синхронизации (объединённые с дефолтом).

    Хранятся в БД: таблица module_settings (ключ 'y360_sync_settings'),
    устаревшее расположение в таблице settings читается для совместимости.
    """
    result = dict(DEFAULT_SETTINGS)
    try:
        from database import get_module_settings
        stored = get_module_settings(SETTINGS_KEY)
        if not stored:  # совместимость со старым расположением (таблица settings)
            legacy = get_setting(SETTINGS_KEY)
            stored = json.loads(legacy) if legacy else {}
        if isinstance(stored, dict):
            result.update({k: v for k, v in stored.items()
                           if k in DEFAULT_SETTINGS})
    except Exception as e:
        logger.error(f"Ошибка чтения настроек синхронизации Яндекс 360: {e}")
    try:
        result['sync_interval_minutes'] = max(
            1, int(result.get('sync_interval_minutes') or 60))
    except (TypeError, ValueError):
        result['sync_interval_minutes'] = 60
    return result


def save_sync_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Сохранить настройки синхронизации в БД (таблицу module_settings)."""
    from database import set_module_settings
    current = get_sync_settings()
    for key in DEFAULT_SETTINGS:
        if key in settings and settings[key] is not None:
            current[key] = settings[key]
    set_module_settings(SETTINGS_KEY, current)
    logger.info("Настройки синхронизации Яндекс 360 сохранены в БД: root_ou=%s, "
                "interval=%s мин", current['root_ou_dn'],
                current['sync_interval_minutes'])
    return current


# ---------------------------------------------------------------------------
# Хранилище соответствий OU <-> департамент / пользователь <-> сотрудник
# (в таблицах y360_sync_map и y360_user_map, см. schema.sql).
# Работаем через database.get_connection(): в модуле database НЕТ функции
# get_db(), поэтому используем правильное имя (ошибка прошлой версии:
# "cannot import name 'get_db' from 'database'").
# ---------------------------------------------------------------------------

def _db():
    """Вернуть новое sqlite-соединение с основной БД (row_factory=Row)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _map_get(key: str) -> Optional[str]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM y360_sync_map WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None
    finally:
        conn.close()


def _map_set(key: str, value: str):
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO y360_sync_map (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))
    finally:
        conn.close()


def _map_delete(key: str):
    """Удалить соответствие из кэша (например, при исключении OU из синхронизации)."""
    conn = _db()
    try:
        with conn:
            conn.execute("DELETE FROM y360_sync_map WHERE key = ?", (key,))
    finally:
        conn.close()


def _user_map_get(login: str) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM y360_user_map WHERE login = ?",
            (login.lower(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _user_map_set(login: str, email: str, ou_dn: str, dept_id: str):
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO y360_user_map (login, email, ou_dn, dept_id, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(login) DO UPDATE SET email = excluded.email, "
                "ou_dn = excluded.ou_dn, dept_id = excluded.dept_id, "
                "updated_at = excluded.updated_at",
                (login.lower(), email, ou_dn, dept_id))
    finally:
        conn.close()


def reset_sync_map():
    """Сбросить кэш соответствий (например, после смены организации)."""
    conn = _db()
    try:
        with conn:
            conn.execute("DELETE FROM y360_sync_map")
            conn.execute("DELETE FROM y360_user_map")
    finally:
        conn.close()


def _managed_logins() -> set:
    """Логины пользователей, сопоставленных этим сервисом (y360_user_map)."""
    conn = _db()
    try:
        return {row['login'].lower() for row in
                conn.execute("SELECT login FROM y360_user_map")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Утилиты нормализации данных
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _norm_email(value) -> str:
    """Первый e-mail из значения атрибута, в нижнем регистре."""
    if not value:
        return ""
    if isinstance(value, list):
        value = ",".join(str(v) for v in value)
    m = EMAIL_RE.search(str(value))
    return m.group(0).lower() if m else ""


def _first(d: dict, *keys, default=None):
    """Первое непустое значение среди ключей словаря (без учёта регистра)."""
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = lowered.get(key.lower())
        if isinstance(v, list):
            v = v[0] if v else None
        if v not in (None, "", []):
            return v
    return default


def _split_common_name(cn: str) -> (str, str):
    """Разбить ФИО/имя ALD Pro на (firstName, lastName)."""
    parts = [p for p in (cn or "").split() if p]
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def _flatten_ou_tree(nodes: List[dict]) -> List[dict]:
    """Обойти дерево OU ALD Pro в список {dn, parent, name} (родители раньше)."""
    flat = []

    def walk(items, parent_dn):
        for item in items or []:
            dn = (_first(item, 'organizationunitlistitem_dn',
                         'organizationunit_dn', 'dn', default='') or '')
            if not dn or any(f['dn'] == dn for f in flat):
                continue
            name = (_first(item, 'organizationunitlistitem_display_name',
                           'organizationunit_display_name',
                           'organizationunitlistitem_ou',
                           'organizationunit_ou', default='').strip()
                    or dn.split(',')[0].replace('OU=', '', 1).replace('ou=', '', 1))
            flat.append({'dn': dn, 'parent': parent_dn, 'name': name})
            walk(item.get('children'), dn)

    walk(nodes, '')
    return flat


# ---------------------------------------------------------------------------
# Чтение данных ALD Pro
# ---------------------------------------------------------------------------

def _parse_user_list(result: Any) -> List[dict]:
    """Достать список пользователей из ответа эндпоинта users-list ALD Pro."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get('data')
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ('userlistitems', 'users', 'items', 'content'):
                if isinstance(data.get(key), list):
                    return data[key]
    return []


def _collect_raw_users(items: List[dict]) -> List[dict]:
    """Нормализовать записи пользователей ALD Pro в плоские словари."""
    out: List[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = raw.get('userlistitem') if isinstance(raw.get('userlistitem'),
                                                     dict) else raw
        out.append(item)
    return out


def _ou_name_from_dn(dn: str) -> str:
    """Имя подразделения из DN (первый компонент RDN)."""
    first = (dn or '').split(',')[0]
    for prefix in ('OU=', 'ou=', 'CN=', 'cn='):
        if first.startswith(prefix):
            first = first[len(prefix):]
            break
    return first or (dn or '')


async def fetch_ald_state(root_dn: str,
                          email_domain: str = '',
                          exclude_dns: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Собрать состояние ALD Pro: структуру подразделений subtree root_dn и
    его пользователей.

    Особенности сбора:
      * Дерево обходится рекурсивно напрямую через API дочерних OU
        (GET /api/ds/organizational-units/{dn}/organizational-units) для
        КАЖДОГО узла — флаг organizationunitlistitem_is_leaf у ALD Pro
        ненадёжен, из-за чего часть подразделений могла не попадать в
        выборку.
      * Головной OU (root_dn) возвращается в списке ous с parent='' —
        сам модуль синхронизации исключает его из создания в Яндекс 360.
      * E-mail считается наличием по умолчанию: берётся из атрибута mail,
        а если API его не вернул — формируется как login@домен (домен из
        настроек синхронизации либо из userPrincipalName).

    Возвращает ous — плоский список подразделений subtree (родители раньше
    детей, включая корень); users — словарь нормализованных профилей по
    логину; generated_emails — число пользователей, которым адрес создан
    автоматически; fallback_emails — число адресов, взятых из UPN.
    """
    import asyncio

    ous: List[dict] = []
    users: Dict[str, dict] = {}
    domain = (email_domain or '').strip().lstrip('@').lower()
    excluded = [d for d in (exclude_dns or []) if d.strip()]

    def add_ou(dn: str, name: str, parent: str):
        dn = unquote(str(dn or ''))
        if not dn or any(o['dn'].lower() == dn.lower() for o in ous):
            return False
        ous.append({'dn': dn, 'name': (name or '').strip()
                    or _ou_name_from_dn(dn), 'parent': parent or ''})
        return True

    async def walk_children(client, parent_dn: str, depth: int = 0):
        """Рекурсивно обойти дочерние OU родителя (subtree целиком)."""
        if depth > MAX_OU_DEPTH:
            logger.warning("Достигнута максимальная глубина дерева OU (%s), "
                           "обход ниже '%s' остановлен", MAX_OU_DEPTH,
                           parent_dn)
            return
        children = await ald_pro.fetch_child_units(parent_dn, client=client)
        known = {o['dn'].lower(): o for o in ous}
        added = []
        for ch in children:
            dn = unquote(str(ch.get('dn') or ''))
            if not dn or dn.lower() in known:
                continue
            # технические/служебные контейнеры не выгружаем ВОВСЕ — вместе
            # со всем поддеревом (в них обычно лежат встроенные учетки и
            # компьютеры, а не сотрудники организации)
            if _is_excluded_dn(dn, excluded):
                logger.info("OU '%s' исключён из синхронизации настройкой "
                            "exclude_ou_dns — поддерево не обходится", dn)
                continue
            known[dn.lower()] = {'dn': dn}
            add_ou(dn, ch.get('name'), parent_dn)
            added.append(dn)
        # обходим следующий уровень рекурсии параллельно пачками
        for i in range(0, len(added), OU_LEVEL_BATCH):
            batch = added[i:i + OU_LEVEL_BATCH]
            await asyncio.gather(*(walk_children(client, dn, depth + 1)
                                   for dn in batch))

    async def load_users(client, dn: str):
        ures = await ald_pro.get_organizational_unit_users(dn, client=client)
        for raw in _collect_raw_users(_parse_user_list(ures)):
            login = (_first(raw, 'userlistitem_login', 'login',
                            'sAMAccountName', 'samaccountname', 'uid',
                            default='') or '')
            login = str(login).strip()
            if not login:
                continue
            key = login.lower()
            if key in users:
                continue  # профиль уже собран (пользователь привязан к
                          # первой встреченной OU — как в AD)
            upn = str(_first(raw, 'userPrincipalName', 'userprincipalname',
                             'userlistitem_user_principal_name',
                             default='') or '').strip()
            email = _norm_email(_first(raw, 'userlistitem_mail', 'mail',
                                       'email', 'proxyAddresses',
                                       'proxyaddresses'))
            source = 'mail'
            if not email and '@' in upn:
                # почта по умолчанию: берём из userPrincipalName
                email = _norm_email(upn)
                source = 'upn'
            if not email:
                # почты в профиле нет — считаем, что она есть по умолчанию,
                # и формируем адрес из логина
                email = f"{key}@{domain}" if domain else f"{key}@mail.local"
                source = 'generated'
            first_name = str(_first(raw, 'userlistitem_first_name',
                                    'givenName', 'givenname', default='') or '')
            last_name = str(_first(raw, 'userlistitem_last_name', 'sn',
                                   default='') or '')
            cn = str(_first(raw, 'userlistitem_common_name', 'cn',
                            'display_name', 'displayName', default='') or login)
            if not first_name and not last_name:
                first_name, last_name = _split_common_name(cn)
            users[key] = {
                'login': login,
                'email': email,
                'email_source': source,
                'firstName': first_name,
                'lastName': last_name,
                'displayName': cn,
                'position': str(_first(raw, 'userlistitem_title', 'title',
                                       default='') or ''),
                'phone': str(_first(raw, 'userlistitem_telephone',
                                    'telephoneNumber', 'telephonenumber',
                                    default='') or ''),
                'ou_dn': unquote(dn),
            }

    root_dn_decoded = unquote(root_dn)
    # 1. Структура подразделений: обход subtree напрямую по API детей,
    #    чтобы не терять узлы с некорректным is_leaf.
    add_ou(root_dn_decoded, _ou_name_from_dn(root_dn_decoded), '')
    client = await ald_pro.get_shared_client()
    if client is None:
        raise RuntimeError("ALD Pro не настроен или недоступен")
    try:
        await walk_children(client, root_dn_decoded)

        # 2. Пользователи всех подразделений subtree (включая головной OU —
        #    они попадут в корневые подразделения Яндекс 360)
        for ou in list(ous):
            try:
                await load_users(client, ou['dn'])
            except Exception as e:
                logger.warning("Не удалось получить пользователей OU '%s': %s",
                               ou['dn'], e)
    finally:
        await ald_pro.close_shared_client()

    generated = sum(1 for u in users.values()
                    if u['email_source'] == 'generated')
    from_upn = sum(1 for u in users.values() if u['email_source'] == 'upn')
    logger.info("ALD Pro subtree '%s': подразделений=%s, пользователей=%s "
                "(почта из mail=%s, из UPN=%s, создана по умолчанию=%s)",
                root_dn_decoded, len(ous), len(users),
                len(users) - generated - from_upn, from_upn, generated)
    return {'ous': ous, 'users': users,
            'generated_emails': generated, 'fallback_emails': from_upn}


# ---------------------------------------------------------------------------
# Чтение данных Яндекс 360
# ---------------------------------------------------------------------------

async def _paginate(client, url: str, org_id: str) -> List[dict]:
    """Выбрать все страницы списка Directory API (limit/offset)."""
    items: List[dict] = []
    offset = 0
    while True:
        resp = await client.get(url, params={'org_id': org_id,
                                             'limit': PAGE_LIMIT,
                                             'offset': offset})
        if resp.status_code != 200:
            raise RuntimeError(
                "Яндекс 360 вернул HTTP %s при GET %s: %s"
                % (resp.status_code, url, resp.text[:200]))
        body = resp.json() or {}
        page = body.get('items') or []
        items.extend(page)
        total = int(body.get('total') or len(items))
        offset += PAGE_LIMIT
        if offset >= total or not page:
            break
        await asyncio.sleep(0.1)  # щадим rate limit API
    return items


async def fetch_y360_departments(client, org_id: str) -> List[dict]:
    deps = await _paginate(client, f'/v1/directory/organizations/{org_id}/departments', org_id)
    return [{'id': str(d.get('id')), 'name': d.get('name') or '',
             'parent': str(d.get('parentDepartmentId') or ''),
             'note': d.get('note') or ''} for d in deps]


async def fetch_y360_users(client, org_id: str) -> List[dict]:
    emps = await _paginate(client, f'/v1/directory/organizations/{org_id}/users', org_id)
    return [{'id': str(e.get('id')), 'login': str(e.get('login') or ''),
             'email': _norm_email(e.get('email')),
             'alt_emails': [_norm_email(a) for a in (e.get('altemails') or [])],
             'department': str(e.get('department') or ''),
             'note': e.get('note') or '',
             'blocked': bool(e.get('blocked'))} for e in emps]


async def fetch_y360_state() -> Dict[str, Any]:
    """Получить текущие подразделения и сотрудников организации Яндекс 360."""
    settings = yandex360.get_settings()
    org_id = str(settings.get('org_id') or '').strip()
    token = (settings.get('oauth_token') or '').strip()
    if not org_id or not token:
        raise RuntimeError("Яндекс 360 не настроен: укажите org_id и OAuth-токен")

    base = yandex360.api_base_url()
    headers = {'Authorization': f'OAuth {token}', 'Accept': 'application/json'}
    async with yandex360.make_async_client(base_url=base, headers=headers) as client:
        departments, employees = await asyncio.gather(
            fetch_y360_departments(client, org_id),
            fetch_y360_users(client, org_id))
    return {'departments': departments, 'employees': employees}


# ---------------------------------------------------------------------------
# Синхронизация подразделений
# ---------------------------------------------------------------------------

NOTE_DN_RE = re.compile(r'ald_pro_dn=([^;\s]+)')


def _dept_note(department: dict) -> str:
    return department.get('note') or ''


def _norm_dept_name(name: str) -> str:
    """Нормализованное название подразделения для сопоставления (без учёта
    регистра, лишних пробелов и «ёлочек»)."""
    return re.sub(r'\s+', ' ', (name or '').replace('\u00ab', '')
                  .replace('\u00bb', '')).strip().lower()


async def sync_departments(ous: List[dict], y360_state: dict,
                           client, org_id: str, report: dict,
                           settings_parent_dept: str = '',
                           root_dn: str = '', dry_run: bool = False,
                           write_token: Optional[str] = None):
    """Сопоставить департаменты Яндекс 360 с OU ALD Pro и создать недостающие.

    Особенности:
    - Названия OU ALD Pro хранятся в виде полных DN
      ('ou=Отдел,ou=Родитель,...'). Для Яндекс 360 используется ТОЛЬКО
      короткое название из первого RDN ('Отдел'), а не полный DN.
    - Головной (корневой) OU ALD Pro `root_dn` из синхронизации исключается:
      подразделение с таким именем в Яндекс 360 не создаётся — его дочерние OU
      становятся корневыми департаментами (при наличии parent_department_id из
      настроек вешаются на него).
    - Недостающие подразделения СОЗДАЮТСЯ через DepartmentService_Create
      (POST /v1/directory/organizations/{org_id}/departments), иерархия
      сохраняется через parentDepartmentId. Созданные подразделения сразу
      добавляются в y360_state, чтобы дети и пользователи обрабатывались в
      этом же проходе. Примечание "ald_pro_dn=<dn>" пишется сразу при
      создании (и дорабатывается PATCH-ем для найденных по имени).
    - dry_run=True: записи в API не выполняются (ни POST, ни PATCH),
      недостающие подразделения попадают только в план создания
      report['departments']['to_create'].
    - Если создание невозможно (HTTP 405 — метод недоступен на текущем
      хосте/токене без права directory:write_departments), подразделение
      попадает в план создания report['departments']['to_create'] — после
      ручного заведения в панели администратора следующая синхронизация
      сопоставит его автоматически.
    """
    deps = y360_state['departments']
    by_note_dn = {}           # dn из примечания ald_pro_dn= -> id
    by_parent_name = {}       # (parent_id, имя) -> id
    by_name = {}              # имя -> [id]
    for d in deps:
        m = NOTE_DN_RE.search(_dept_note(d))
        if m:
            by_note_dn[m.group(1).lower()] = d['id']
        key = (d['parent'], _norm_dept_name(d['name']))
        by_parent_name.setdefault(key, d['id'])
        if key[0] == '':
            by_parent_name.setdefault(('', _norm_dept_name(d['name'])), d['id'])
        by_name.setdefault(_norm_dept_name(d['name']), []).append(d['id'])

    sem = asyncio.Semaphore(API_CONCURRENCY)
    dep_url = f'/v1/directory/organizations/{org_id}/departments'
    root_dn_lower = (root_dn or '').strip().lower()
    default_parent = str(settings_parent_dept or '').strip()
    create_failed_perm = False  # POST /departments недоступен (405/403)

    async def try_patch_note(dept_id: str, dn: str) -> None:
        """Пометить найденный департамент примечанием с DN ALD Pro."""
        if dry_run:
            return
        try:
            async with sem:
                await client.patch(f"{dep_url}/{dept_id}",
                                   params={'org_id': org_id},
                                   json={'note': 'ald_pro_dn=%s' % dn})
        except Exception:
            pass  # примечание — лишь ускорение сопоставления, не ошибка

    for ou in ous:  # список отсортирован: родители идут раньше детей
        dn = ou['dn']
        name = _ou_name_from_dn(dn) or ou.get('name') or dn  # короткое имя!
        map_key = 'dep:' + dn
        if root_dn_lower and dn.lower() == root_dn_lower:
            # головной подразделение ALD Pro НЕ создаётся в Яндекс 360;
            # сбрасываем прежнее соответствие, если оно было сохранено
            _map_delete(map_key)
            report['departments']['skipped_root'] = \
                report['departments'].get('skipped_root', 0) + 1
            logger.info("Головной OU '%s' исключён из синхронизации "
                        "(не создаётся в Яндекс 360)", dn)
            continue

        dept_id = _map_get(map_key)
        if dept_id and not any(d['id'] == dept_id for d in deps):
            dept_id = None  # департамент был удалён вручную — ищем заново
        matched_via_note = False
        if not dept_id:
            dept_id = by_note_dn.get(dn.lower())
            matched_via_note = bool(dept_id)
        if not dept_id:
            # поиск по КОРОТКОМУ имени с учётом родителя (после того, как
            # родительское OU уже сопоставлено в этом же проходе)
            parent_id = (_map_get('dep:' + ou['parent'])
                         if ou['parent'] else None)
            candidates = []
            if ou['parent']:
                if parent_id:
                    candidates.append((parent_id, _norm_dept_name(name)))
            else:
                # непосредственные дети головного OU: корневые департаменты
                # или департаменты внутри parent_department_id из настроек
                candidates.append(('', _norm_dept_name(name)))
                if default_parent:
                    candidates.append((default_parent, _norm_dept_name(name)))
            for c in candidates:
                dept_id = by_parent_name.get(c)
                if dept_id:
                    break
            if not dept_id:
                ids = by_name.get(_norm_dept_name(name)) or []
                if len(ids) == 1:  # имя уникально в организации
                    dept_id = ids[0]
        if dept_id:
            _map_set(map_key, dept_id)
            report['departments']['matched'] += 1
            if dry_run:
                by_note_dn.setdefault(dn.lower(), dept_id)
            elif not matched_via_note:
                # сохраняем dn в примечании — следующие прогоны сопоставляются
                # однозначно, даже при одинаковых именах в разных ветках
                await try_patch_note(dept_id, dn)
                by_note_dn[dn.lower()] = dept_id
            continue

        # Подразделение в Яндекс 360 отсутствует — создаём через
        # DepartmentService_Create. Родительский департамент:
        parent_id = _map_get('dep:' + ou['parent']) if ou['parent'] else None
        if (not ou['parent'] or ou['parent'].lower() == root_dn_lower
                or parent_id is None):
            parent_id = default_parent or None

        if not create_failed_perm and not dry_run:
            async with sem:
                res = await yandex360.create_department(
                    client, org_id, name=name,
                    parent_department_id=parent_id,
                    note='ald_pro_dn=%s' % dn,
                    token=write_token)
            if res.get('success'):
                new_id = res.get('id') or ''
                if new_id:
                    _map_set(map_key, new_id)
                    deps.append({'id': new_id, 'name': name,
                                 'parent': str(parent_id or ''),
                                 'note': 'ald_pro_dn=%s' % dn})
                    by_note_dn[dn.lower()] = new_id
                    by_parent_name[(str(parent_id or ''),
                                    _norm_dept_name(name))] = new_id
                    by_name.setdefault(_norm_dept_name(name), []).append(new_id)
                    report['departments']['created'] += 1
                    logger.info("Создано подразделение '%s' (id=%s, "
                                "родитель=%s) в Яндекс 360", name, new_id,
                                parent_id or '—')
                else:
                    # создан, но id в ответе не пришёл — обновим состояние
                    report['departments']['created'] += 1
                    report['errors'].append(
                        "Подразделение '%s' создано, но id не получен — "
                        "сопоставление выполнится на следующем проходе"
                        % name)
                continue
            status = res.get('status')
            detail = res.get('detail') or ''
            if status in (405, 403):
                # Метод/права недоступны — дальнейшие POST бессмысленны,
                # складываем всё в план ручного создания
                create_failed_perm = True
                report['errors'].append(
                    "DepartmentService_Create вернул HTTP %s: %s. "
                    "Метод создания подразделений доступен только при "
                    "авторизации токеном корпоративного приложения с правом "
                    "directory:write_departments (обычный OAuth-токен «личного» "
                    "приложения возвращает 405 MethodNotAllowedError). "
                    "Укажите токен с правами записи в поле «Токен для записи» "
                    "(на странице настроек Яндекс 360) и повторите синхронизацию. "
                    "Оставшиеся подразделения помещены в план создания."
                    % (status, detail[:120]))
            else:
                report['errors'].append(
                    "Не удалось создать подразделение '%s' в Яндекс 360 "
                    "(HTTP %s): %s" % (name, status, detail[:150]))

        report['departments']['to_create'].append({
            'name': name[:150] or dn,
            'dn': dn,
            'parent_dn': ou['parent'],
            'parent_department_id': parent_id or '',
        })
        if not dry_run:
            report['departments']['create_blocked'] = \
                report['departments'].get('create_blocked', 0) + 1
            logger.warning("Подразделение '%s' (OU '%s') не создано через "
                           "API — помещено в план создания.", name, dn)


def _apply_created_departments(y360_state: dict, to_create: List[dict]):
    """Добавить подразделения из плана создания в состояние (для того, чтобы
    sync_users корректно обрабатывал пользователей несозданных OU)."""
    for item in to_create:
        y360_state['departments'].append({
            'id': '', 'name': item['name'],
            'parent': item['parent_department_id'],
            'note': 'ald_pro_dn=%s' % item['dn']})


# ---------------------------------------------------------------------------
# Синхронизация пользователей
# ---------------------------------------------------------------------------

async def sync_users(users: Dict[str, dict], y360_state: dict,
                     client, org_id: str, settings: dict, report: dict,
                     write_token: Optional[str] = None):
    """
    Обновить сотрудников Яндекс 360 по данным ALD Pro и заблокировать
    тех, кого больше нет в ALD Pro.

    Создание НОВЫХ сотрудников выполняется через UserService_Create
    (POST /v1/directory/organizations/{org_id}/users) с логином ALD Pro,
    именем из ALD Pro и назначением подразделения сразу при создании
    (departmentId). Пароль задаётся заглушкой, письмо-приглашение не
    рассылается (в текущей версии API флаг sendEmail не поддерживается).
    Если создание невозможно (HTTP 405 — устаревший хост или отсутствие
    права directory:write_users), пользователь заносится в план создания
    report['users']['to_create'] — после заведения учётки следующая
    синхронизация найдёт её по логину/email и применит подразделение.
    Существующие сотрудники проверяются на наличие в ALD Pro и на
    принадлежность к подразделениям (перенос выполняется PATCH-запросами).
    """
    employees = y360_state['employees']
    departments = y360_state['departments']
    known_dept_ids = {d['id'] for d in departments}
    emp_by_login = {e['login'].lower(): e for e in employees if e['login']}
    emp_by_email = {}
    for e in employees:
        for mail in ([e['email']] + e['alt_emails']) if e['email'] else e['alt_emails']:
            if mail:
                emp_by_email.setdefault(mail, e)

    domain = (settings.get('email_domain') or '').strip()
    sem = asyncio.Semaphore(API_CONCURRENCY)
    users_url = f'/v1/directory/organizations/{org_id}/users'

    def dept_for_ou(dn: str) -> Optional[str]:
        """Департамент Яндекс 360 для OU ALD Pro.

        Для головного OU (и для неизвестных OU) возвращается '' — пустая
        строка означает «в корневом подразделении» (departmentId не задаётся).
        None — соответствие ещё не создано, пользователя обрабатывать рано.
        Подразделения из плана создания имеют id == '' и трактуются так же,
        как корневые (пользователь попадёт в план создания без должности).
        """
        if not dn:
            return ''
        mapped = _map_get('dep:' + dn)
        if mapped is not None:
            return mapped
        # сопоставление по примечанию ald_pro_dn= / план создания
        for d in departments:
            if dn.lower() in (d.get('note') or '').lower():
                return d['id'] or ''
        return None

    async def patch_employee(login: str, payload: dict) -> Optional[str]:
        async with sem:
            resp = await client.patch(f"{users_url}/{login}",
                                      params={'org_id': org_id}, json=payload)
        if resp.status_code != 200:
            return "HTTP %s: %s" % (resp.status_code, resp.text[:150])
        return None

    async def create_employee(payload: dict) -> Dict[str, Any]:
        """Создать сотрудника (UserService_Create)."""
        headers = None
        if write_token:
            headers = {'Authorization': f'OAuth {write_token}'}
        async with sem:
            resp = await client.post(users_url, params={'org_id': org_id},
                                     json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            return {'success': False, 'status': resp.status_code,
                    'detail': resp.text[:300]}
        body = resp.json() or {}
        emp = body.get('employee') or body.get('user') or body
        return {'success': True,
                'login': str(emp.get('login') or payload.get('login') or ''),
                'id': str(emp.get('id') or '')}

    processed_logins = set()
    create_api_blocked = False  # POST /users недоступен (405/403)

    for key in sorted(users):
        u = users[key]
        login = u['login']
        processed_logins.add(login.lower())
        target_dept = dept_for_ou(u['ou_dn'])
        if target_dept is None:
            report['errors'].append(
                "Подразделение '%s' не сопоставлено с Яндекс 360 — "
                "пользователь %s пропущен" % (u['ou_dn'], login))
            report['users']['skipped'] += 1
            continue

        emp = emp_by_login.get(login.lower()) or emp_by_email.get(u['email'])
        mapped = _user_map_get(login.lower())

        if emp is None:
            email = u['email']
            if domain and not email.endswith('@' + domain.lower()):
                email = f"{login}@{domain}"
            dept_name = ''
            if target_dept:
                dept_name = next((d['name'] for d in departments
                                  if d['id'] == target_dept), '')

            if not create_api_blocked:
                payload = {
                    'login': login,
                    'email': email,
                    'firstName': u['firstName'] or login,
                    'lastName': u['lastName'] or '-',
                    'blocked': False,
                    # пароль-заглушка: вход по нему не предполагается,
                    # пользователь сменит пароль при первом входе
                    'password': secrets.token_urlsafe(16),
                }
                if u.get('displayName'):
                    payload['displayName'] = u['displayName']
                if target_dept:
                    try:
                        payload['departmentId'] = int(target_dept)
                    except (TypeError, ValueError):
                        pass
                res = await create_employee(payload)
                if res.get('success'):
                    new_emp = {'id': res.get('id') or '',
                               'login': res.get('login') or login,
                               'email': email, 'alt_emails': [],
                               'department': target_dept or '',
                               'note': 'ald_pro_uid=%s' % login,
                               'blocked': False}
                    employees.append(new_emp)
                    emp_by_login[new_emp['login'].lower()] = new_emp
                    emp_by_email.setdefault(email, new_emp)
                    report['users']['created'] += 1
                    _user_map_set(login, email, u['ou_dn'], target_dept)
                    logger.info("Создан пользователь Яндекс 360: %s (%s, "
                                "подразделение=%s)", login, email,
                                dept_name or '—')
                    continue
                status = res.get('status')
                detail = res.get('detail') or ''
                body_l = detail.lower()
                if status in (405, 403):
                    create_api_blocked = True
                    report['errors'].append(
                        "UserService_Create вернул HTTP %s: %s. Метод создания "
                        "сотрудников доступен только токену корпоративного "
                        "приложения с правом directory:write_users. Укажите "
                        "токен с правами записи в поле «Токен для записи» "
                        "(настройки Яндекс 360). Оставшиеся пользователи "
                        "помещены в план ручного создания."
                        % (status, detail[:120]))
                elif status == 409 or 'alreadyexist' in body_l \
                        or 'уже существ' in body_l:
                    # логин занят (например, почтовый ящик заведён ранее) —
                    # создаём учётку с альтернативным логином
                    alt_login = re.sub(r'[^a-z0-9._-]', '',
                                       email.lower().replace('@', '.'))
                    alt_payload = dict(payload)
                    alt_payload['login'] = alt_login
                    alt_payload['altemails'] = [email]
                    res2 = await create_employee(alt_payload)
                    if res2.get('success'):
                        new_emp = {'id': res2.get('id') or '',
                                   'login': alt_login, 'email': email,
                                   'alt_emails': [],
                                   'department': target_dept or '',
                                   'note': 'ald_pro_uid=%s' % login,
                                   'blocked': False}
                        employees.append(new_emp)
                        emp_by_login[alt_login] = new_emp
                        emp_by_login.setdefault(login.lower(), new_emp)
                        emp_by_email.setdefault(email, new_emp)
                        report['users']['created'] += 1
                        _user_map_set(login, email, u['ou_dn'], target_dept)
                        logger.info("Создан пользователь Яндекс 360 с "
                                    "альтернативным логином %s (%s)",
                                    alt_login, email)
                        continue
                    report['errors'].append(
                        "Не удалось создать пользователя %s (логин занят, "
                        "создание под %s не выполнено, HTTP %s): %s"
                        % (login, alt_login, res2.get('status'),
                           (res2.get('detail') or '')[:120]))
                else:
                    report['errors'].append(
                        "Не удалось создать пользователя %s в Яндекс 360 "
                        "(HTTP %s): %s" % (login, status, detail[:150]))

            # Фallback: план ручного создания
            report['users']['to_create'].append({
                'login': login,
                'email': email,
                'firstName': u['firstName'] or login,
                'lastName': u['lastName'] or '-',
                'displayName': u['displayName'] or '',
                'ou_dn': u['ou_dn'],
                'department_id': target_dept or '',
                'department_name': dept_name,
            })
            report['users']['create_blocked'] = \
                report['users'].get('create_blocked', 0) + 1
            continue

        # Сотрудник существует — сверяем принадлежность к подразделению и
        # актуальность учётки (наличие в ALD Pro проверяется ниже)
        changes_needed = []
        if emp['department'] != target_dept:
            if target_dept:
                changes_needed.append(('departmentId', int(target_dept)))
            elif emp['department'] and emp['department'] in known_dept_ids:
                # пользователь перенесён в головной OU ALD Pro (вне
                # синхронизируемого дерева подразделений) — снимаем с
                # должности в Яндекс 360
                changes_needed.append('unassignDepartment')
                changes_needed.append(('departmentId', 0))
        if emp['blocked']:
            changes_needed.append(('blocked', False))
            changes_needed.append(('unblockReason', 'restored'))

        if changes_needed:
            payload = {k: v for k, v in changes_needed}
            err = await patch_employee(emp['login'] or login, payload)
            if err and 'unassignDepartment' in payload:
                # некоторые версии API не принимают вымышленный ключ —
                # пробуем снять с должности только departmentId=0
                payload.pop('unassignDepartment', None)
                err = await patch_employee(emp['login'] or login, payload)
            if err:
                report['errors'].append(
                    "Не удалось обновить пользователя %s в Яндекс 360 (%s)"
                    % (login, err))
                continue
            moved = ('departmentId' in payload and emp['department']
                     != target_dept)
            if moved:
                report['users']['moved'] += 1
            else:
                report['users']['updated'] += 1
            emp['department'] = target_dept
            emp['blocked'] = False
        else:
            report['users']['unchanged'] += 1
        _user_map_set(login, emp['email'] or u['email'], u['ou_dn'], target_dept)

    # Проверка существующих сотрудников Яндекс 360 на наличие в ALD Pro.
    # Затрагиваются только учётки, созданные/сопоставленные этим сервисом
    # (по y360_user_map или примечанию ald_pro_uid=), чтобы не блокировать
    # произвольные учетные записи организации.
    if settings.get('block_missing_users'):
        managed_logins = _managed_logins()
        for emp in employees:
            login = emp['login'].lower()
            if not login or login in processed_logins:
                continue
            is_managed = login in managed_logins or 'ald_pro_uid=' in emp.get('note', '')
            if not is_managed:
                continue
            if emp['blocked']:
                continue
            err = await patch_employee(emp['login'], {
                'blocked': True,
                'blockReason': 'dismissed',
                'note': 'Заблокирован синхронизацией: отсутствует в ALD Pro',
            })
            if err:
                report['errors'].append(
                    "Не удалось заблокировать %s в Яндекс 360 (%s)"
                    % (emp['login'], err))
                continue
            report['users']['blocked'] += 1


# ---------------------------------------------------------------------------
# Основная процедура синхронизации
# ---------------------------------------------------------------------------

_last_result = {'finished_at': None, 'duration_sec': None, 'success': None,
                'report': None, 'error': None}


def get_last_sync_result() -> Dict[str, Any]:
    return dict(_last_result)


async def run_full_sync(trigger: str = 'manual') -> Dict[str, Any]:
    """
    Выполнить полную синхронизацию ALD Pro -> Яндекс 360.

    1. Читает настройки (корневой OU, интервал, домен почты).
    2. Собирает дерево OU и пользователей из ALD Pro (головной OU
       исключается; названия подразделений — короткие имена, не DN).
    3. Сопоставляет и создаёт подразделения (DepartmentService_Create),
       создаёт новых сотрудников (UserService_Create) и обновляет
       существующих (перенос между департаментами, разблокировка).
       Если создание объектов через API недоступно (HTTP 405/403), они
       попадают в планы создания report['departments']['to_create'] и
       report['users']['to_create'] для ручного заведения.
    4. Блокирует сотрудники Яндекс 360, удалённые из ALD Pro.
    """
    started = time.time()
    settings = get_sync_settings()
    root_dn = (settings.get('root_ou_dn') or '').strip()
    report = {
        'trigger': trigger,
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'root_ou': root_dn,
        'ald_ous': 0,
        'ald_users': 0,
        'ald_users_email_generated': 0,
        'ald_users_email_from_upn': 0,
        'departments': {'created': 0, 'matched': 0, 'skipped_root': 0,
                        'create_blocked': 0, 'to_create': []},
        'users': {'created': 0, 'moved': 0, 'updated': 0, 'blocked': 0,
                  'unchanged': 0, 'skipped': 0,
                  'create_blocked': 0, 'to_create': []},
        'errors': [],
    }
    result = {'success': False, 'report': report}

    if not root_dn:
        result['error'] = 'Не задан корневой OU ALD Pro в настройках синхронизации'
        _last_result.update({'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                             'duration_sec': round(time.time() - started, 1),
                             'success': False, 'report': report,
                             'error': result['error']})
        return result

    y360_settings = yandex360.get_settings()
    org_id = str(y360_settings.get('org_id') or '').strip()
    token = (y360_settings.get('oauth_token') or '').strip()
    if not org_id or not token:
        result['error'] = 'Яндекс 360 не настроен (org_id / OAuth-токен)'
        _last_result.update({'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                             'duration_sec': round(time.time() - started, 1),
                             'success': False, 'report': report,
                             'error': result['error']})
        return result

    try:
        # 1. Данные ALD Pro (поддерево головного OU; сам головной OU
        #    синхронизатором не создаётся)
        state = await fetch_ald_state(
            root_dn,
            email_domain=settings.get('email_domain', ''),
            exclude_dns=get_excluded_ou_dns(settings))
        report['ald_ous'] = len(state['ous'])
        report['ald_users'] = len(state['users'])
        report['ald_users_email_generated'] = state['generated_emails']
        report['ald_users_email_from_upn'] = state['fallback_emails']

        # 2. Текущее состояние Яндекс 360
        y360_state = await fetch_y360_state()

        # 3. Запись изменений в Яндекс 360: подразделения создаются через
        #    DepartmentService_Create, сотрудники — через UserService_Create
        base = yandex360.api_base_url()
        headers = {'Authorization': f'OAuth {token}',
                   'Accept': 'application/json'}
        # Отдельный токен для операций записи (создание подразделений и
        # сотрудников). Если не задан — используется основной oauth_token.
        write_token = (yandex360.get_write_token() or token).strip()
        async with yandex360.make_async_client(base_url=base, headers=headers) as client:
            await sync_departments(state['ous'], y360_state, client, org_id,
                                   report,
                                   settings.get('parent_department_id', ''),
                                   root_dn=root_dn, write_token=write_token)
            # Пользователи OU, подразделения которых отсутствуют в Яндекс 360
            # (план создания), не должны считаться «несопоставленными»:
            # добавляем их в состояние с пустым id.
            _apply_created_departments(y360_state,
                                       report['departments']['to_create'])
            await sync_users(state['users'], y360_state, client, org_id,
                             settings, report, write_token=write_token)

        result['success'] = not report['errors']
        result['error'] = ('; '.join(report['errors'][:5])) or None
    except Exception as e:
        logger.exception("Ошибка синхронизации с Яндекс 360")
        result['error'] = str(e)
    finally:
        duration = round(time.time() - started, 1)
        report['duration_sec'] = duration
        _last_result.update({
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'duration_sec': duration,
            'success': result['success'],
            'report': report,
            'error': result.get('error'),
        })
        dep = report.get('departments') or {}
        usr = report.get('users') or {}
        # ВАЖНО: количество плейсхолдеров %s строго совпадает с числом
        # аргументов (в прошлой версии здесь возникал
        # "TypeError: not all arguments converted during string formatting")
        logger.info(
            "Синхронизация Яндекс 360 завершена (trigger=%s): "
            "OU=%s, пользователей ALD Pro=%s, "
            "подразделения: сопоставлено=%s исключено(головной OU)=%s "
            "к созданию=%s, "
            "пользователи: обновлено=%s перенесено=%s "
            "заблокировано=%s без изменений=%s пропущено=%s "
            "к созданию=%s, "
            "ошибок=%s%s",
            trigger,
            report.get('ald_ous'), report.get('ald_users'),
            dep.get('matched', 0),
            dep.get('skipped_root', 0),
            len(dep.get('to_create') or []),
            usr.get('updated', 0),
            usr.get('moved', 0), usr.get('blocked', 0),
            usr.get('unchanged', 0), usr.get('skipped', 0),
            len(usr.get('to_create') or []),
            len(report.get('errors') or []),
            (' | первые ошибки: ' + '; '.join((report.get('errors') or [])[:2]))\
            if report.get('errors') else '',
        )
        set_setting('y360_last_sync_ts', str(int(time.time())))
    return result


# ---------------------------------------------------------------------------
# Предпросмотр синхронизации (dry-run): сверка без записи в Яндекс 360
# ---------------------------------------------------------------------------

async def build_preview() -> Dict[str, Any]:
    """
    Собрать отчёт о планируемых изменениях БЕЗ записей в Яндекс 360.

    Показывает: подразделения ALD Pro, которых ещё нет в Яндекс 360
    (головной OU исключается; имена — короткие названия, не DN) — они будут
    СОЗДАНЫ через DepartmentService_Create при запуске синхронизации;
    сотрудников ALD Pro, которых нет в Яндекс 360 — они будут созданы через
    UserService_Create; переносы существующих сотрудников между
    подразделениями; сотрудников Яндекс 360, отсутствующих в ALD Pro
    (кандидатов на блокировку).
    Реализована через запуск настоящей процедуры синхронизации подразделений
    в режиме dry_run (без POST/PATCH), поэтому план создания совпадает с
    реальным поведением (включая иерархию родительских департаментов).
    """
    settings = get_sync_settings()
    root_dn = (settings.get('root_ou_dn') or '').strip()
    if not root_dn:
        raise RuntimeError('Не задан корневой OU ALD Pro в настройках синхронизации')
    y360_settings = yandex360.get_settings()
    org_id = str(y360_settings.get('org_id') or '').strip()
    token = (y360_settings.get('oauth_token') or '').strip()
    if not org_id or not token:
        raise RuntimeError('Яндекс 360 не настроен (org_id / OAuth-токен)')

    state = await fetch_ald_state(
        root_dn,
        email_domain=settings.get('email_domain', ''),
        exclude_dns=get_excluded_ou_dns(settings))
    y360_state = await fetch_y360_state()

    root_dn_lower = root_dn.lower()
    default_parent = str(settings.get('parent_department_id') or '').strip()

    # План создания подразделений считаем той же процедурой, что и реальная
    # синхронизация, но в режиме dry_run (без POST/PATCH к API): сопоставления
    # по кэшу y360_sync_map, примечанию ald_pro_dn= и короткому имени с
    # учётом родителя. Временный report нужен только для to_create/errors.
    tmp_report = {'departments': {'created': 0, 'matched': 0,
                                  'skipped_root': 0, 'to_create': []},
                  'errors': []}
    await sync_departments(state['ous'], y360_state, None, org_id,
                           tmp_report, default_parent, root_dn=root_dn,
                           dry_run=True)
    new_departments = tmp_report['departments']['to_create']

    # Соответствия OU -> департамент: реально существующие департаменты
    # (id != '') из плана создания не учитываем — они ещё не заведены.
    dept_by_note = {}
    by_parent_name = {}
    by_name = {}
    ou_by_dn = {}
    for d in y360_state['departments']:
        m = NOTE_DN_RE.search(d.get('note') or '')
        if m:
            dept_by_note[m.group(1).lower()] = d['id']
        key = (d['parent'], _norm_dept_name(d['name']))
        by_parent_name.setdefault(key, d['id'])
        by_name.setdefault(_norm_dept_name(d['name']), []).append(d['id'])
    for ou in state['ous']:
        ou_by_dn[ou['dn'].lower()] = ou

    resolved: Dict[str, Optional[str]] = {}  # dn(lower) -> id департамента

    def resolve_dept(dn: str) -> Optional[str]:
        """Найти департамент Яндекс 360 для OU (рекурсивно по родителю)."""
        if not dn:
            return ''
        key = dn.lower()
        if key in resolved:
            return resolved[key]
        if key == root_dn_lower:
            resolved[key] = ''
            return ''
        resolved[key] = None  # защита от зацикливания
        cached = _map_get('dep:' + dn)
        if cached:
            resolved[key] = cached
            return cached
        by_note = dept_by_note.get(key)
        if by_note:
            resolved[key] = by_note
            return by_note
        ou = ou_by_dn.get(key)
        name = _ou_name_from_dn(dn) if ou else ''
        name = (name or (ou or {}).get('name') or '').strip()
        parent_dn = (ou or {}).get('parent') or ''
        result = None
        parent_id = resolve_dept(parent_dn) if parent_dn else ''
        candidates = []
        if parent_dn and parent_dn.lower() != root_dn_lower and parent_id:
            candidates.append((parent_id, _norm_dept_name(name)))
        if not parent_dn or parent_dn.lower() == root_dn_lower:
            candidates.append(('', _norm_dept_name(name)))
            if default_parent:
                candidates.append((default_parent, _norm_dept_name(name)))
        for c in candidates:
            if c in by_parent_name:
                result = by_parent_name[c]
                break
        if result is None:
            ids = by_name.get(_norm_dept_name(name)) or []
            if len(ids) == 1:
                result = ids[0]
        resolved[key] = result
        return result

    def dept_for_ou(dn: str) -> Optional[str]:
        if not dn or dn.lower() == root_dn_lower:
            return ''
        return resolve_dept(dn)

    emp_by_login = {e['login'].lower(): e for e in y360_state['employees'] if e['login']}
    emp_by_email = {}
    for e in y360_state['employees']:
        for mail in ([e['email']] + e['alt_emails']):
            if mail:
                emp_by_email.setdefault(mail, e)

    domain = (settings.get('email_domain') or '').strip()
    ald_logins = set()
    users_to_create, users_to_move = [], []
    for key in sorted(state['users']):
        u = state['users'][key]
        ald_logins.add(key)
        target_dept = dept_for_ou(u['ou_dn'])
        emp = emp_by_login.get(key) or emp_by_email.get(u['email'])
        email = u['email']
        if domain and not email.endswith('@' + domain.lower()):
            email = f"{u['login']}@{domain}"
        if emp is None:
            users_to_create.append({
                'login': u['login'], 'email': email,
                'displayName': u['displayName'], 'ou_dn': u['ou_dn'],
                'department': target_dept or '(подразделение будет создано)',
            })
        elif target_dept and emp['department'] != target_dept:
            users_to_move.append({
                'login': emp['login'], 'email': emp['email'] or u['email'],
                'from_department': emp['department'] or '-',
                'to_department': target_dept, 'ou_dn': u['ou_dn'],
            })

    # Существующие сотрудники Яндекс 360: проверка наличия в ALD Pro
    managed_logins = _managed_logins()
    users_missing_in_ald = []
    for e in y360_state['employees']:
        login = e['login'].lower()
        if not login or login in ald_logins:
            continue
        is_managed = login in managed_logins or 'ald_pro_uid=' in e.get('note', '')
        if not is_managed or e['blocked']:
            continue
        users_missing_in_ald.append({'login': e['login'], 'email': e['email'],
                                     'department': e['department']})

    return {
        'root_ou': root_dn,
        'ald_departments_total': len(new_departments),
        'ald_departments_tree': len(state['ous']),
        'ald_users': len(state['users']),
        'ald_users_with_email': len(state['users']),
        'ald_users_skipped_no_email': 0,
        'ald_users_email_generated': state['generated_emails'],
        'ald_users_email_from_upn': state['fallback_emails'],
        'y360_departments_total': len(y360_state['departments']),
        'y360_users_total': len(y360_state['employees']),
        'new_departments': new_departments[:200],
        'users_to_create': users_to_create[:500],
        'users_to_move': users_to_move[:500],
        'users_missing_in_ald': users_missing_in_ald[:500],
        'block_missing_users': bool(settings.get('block_missing_users')),
        'creation_via_api': (
            'Подразделения из списка «новые» будут созданы через '
            'DepartmentService_Create (POST /v1/directory/.../departments), '
            'сотрудники из списка «к созданию» — через UserService_Create '
            '(POST /v1/directory/.../users) с логином ALD Pro и паролем-'
            'заглушкой. Требуется право OAuth directory:write_departments / '
            'directory:write_users; если API вернёт 405/403, объекты попадут '
            'в план ручного создания в отчёте синхронизации.'),
    }
