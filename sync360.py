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
  * Новые пользователи создаются через POST /v1/directory/.../users с
    логином ALD Pro и паролем-заглушкой; приглашение на e-mail не
    отправляется (sendEmail=false), чтобы не рассылать письма при выгрузке.

Интервал автоматической синхронизации задаётся в настройках модуля
(поле sync_interval_minutes) и обрабатывается фоновой задачей в main.py.
"""

import asyncio
import json
import logging
import re
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
        deps, emps = await asyncio.gather(
            _paginate(client, f'/v1/directory/organizations/{org_id}/departments', org_id),
            _paginate(client, f'/v1/directory/organizations/{org_id}/users', org_id),
        )

    departments = [{'id': str(d.get('id')), 'name': d.get('name') or '',
                    'parent': str(d.get('parentDepartmentId') or ''),
                    'note': d.get('note') or ''} for d in deps]
    employees = [{'id': str(e.get('id')), 'login': str(e.get('login') or ''),
                  'email': _norm_email(e.get('email')),
                  'alt_emails': [_norm_email(a) for a in (e.get('altemails') or [])],
                  'department': str(e.get('department') or ''),
                  'note': e.get('note') or '',
                  'blocked': bool(e.get('blocked'))} for e in emps]
    return {'departments': departments, 'employees': employees}


# ---------------------------------------------------------------------------
# Синхронизация подразделений
# ---------------------------------------------------------------------------

NOTE_DN_RE = re.compile(r'ald_pro_dn=([^;\s]+)')


def _dept_note(department: dict) -> str:
    return department.get('note') or ''


async def sync_departments(ous: List[dict], y360_state: dict,
                           client, org_id: str, report: dict,
                           settings_parent_dept: str = '',
                           root_dn: str = ''):
    """Создать/сопоставить департаменты Яндекс 360 для OU ALD Pro.

    Головной (корневой) OU ALD Pro `root_dn` из синхронизации исключается:
    подразделение с таким именем в Яндекс 360 не создаётся — его дочерние OU
    становятся корневыми департаментами (при наличии parent_department_id из
    настроек вешаются на него).
    """
    deps = y360_state['departments']
    by_note_dn = {}
    for d in deps:
        m = NOTE_DN_RE.search(_dept_note(d))
        if m:
            by_note_dn[m.group(1).lower()] = d['id']

    sem = asyncio.Semaphore(API_CONCURRENCY)
    dep_url = f'/v1/directory/organizations/{org_id}/departments'
    root_dn_lower = (root_dn or '').strip().lower()

    for ou in ous:  # список отсортирован: родители идут раньше детей
        dn = ou['dn']
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
            dept_id = None  # департамент был удалён вручную — пересоздаём
        if not dept_id:
            dept_id = by_note_dn.get(dn.lower())
        if dept_id:
            _map_set(map_key, dept_id)
            report['departments']['matched'] += 1
            continue
        parent_id = _map_get('dep:' + ou['parent']) if ou['parent'] else None
        if ou['parent'] and parent_id is None:
            # родитель — головной OU (исключён из синхронизации) или его
            # департамент ещё не сопоставлен: такой департамент становится
            # корневым (вешается на parent_department_id из настроек)
            parent_id = str(settings_parent_dept or '').strip() or None
        elif not ou['parent']:
            # непосредственные дети головного OU — корневые департаменты
            parent_id = str(settings_parent_dept or '').strip() or None
        payload = {'name': ou['name'][:150] or dn,
                   'note': 'ald_pro_dn=%s' % dn}
        if parent_id:
            payload['parentDepartmentId'] = int(parent_id)
        async with sem:
            resp = await client.post(dep_url, params={'org_id': org_id}, json=payload)
        if resp.status_code not in (200, 201):
            report['errors'].append(
                "Не удалось создать подразделение '%s' в Яндекс 360 "
                "(HTTP %s): %s" % (dn, resp.status_code, resp.text[:150]))
            continue
        created = resp.json() or {}
        dept_id = str(created.get('id') or '')
        if dept_id:
            _map_set(map_key, dept_id)
            deps.append({'id': dept_id, 'name': ou['name'],
                         'parent': parent_id or '', 'note': payload['note']})
            report['departments']['created'] += 1


# ---------------------------------------------------------------------------
# Синхронизация пользователей
# ---------------------------------------------------------------------------

async def sync_users(users: Dict[str, dict], y360_state: dict,
                     client, org_id: str, settings: dict, report: dict):
    """
    Создать/обновить сотрудников Яндекс 360 по данным ALD Pro и заблокировать
    тех, кого больше нет в ALD Pro.
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
        """
        if not dn:
            return ''
        return _map_get('dep:' + dn)

    async def patch_employee(login: str, payload: dict) -> Optional[str]:
        async with sem:
            resp = await client.patch(f"{users_url}/{login}",
                                      params={'org_id': org_id}, json=payload)
        if resp.status_code != 200:
            return "HTTP %s: %s" % (resp.status_code, resp.text[:150])
        return None

    processed_logins = set()

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
            # Новый сотрудник: создаём учетную запись в Яндекс 360
            email = u['email']
            if domain and not email.endswith('@' + domain.lower()):
                email = f"{login}@{domain}"
            payload = {
                'firstName': u['firstName'] or login,
                'lastName': u['lastName'] or '-',
                'email': email,
                'login': login,
                'password': 'AldPr$ync%d' % (int(time.time()) % 100000),
                'note': 'ald_pro_uid=%s' % login,
                'sendEmail': False,
            }
            if target_dept:
                payload['departmentId'] = int(target_dept)
            if u['displayName'] and u['displayName'] != login:
                payload['commonName'] = u['displayName']
            async with sem:
                resp = await client.post(users_url, params={'org_id': org_id},
                                         json=payload)
            if resp.status_code not in (200, 201):
                report['errors'].append(
                    "Не удалось создать пользователя %s в Яндекс 360 "
                    "(HTTP %s): %s" % (login, resp.status_code, resp.text[:150]))
                continue
            created = resp.json() or {}
            new_id = str(created.get('id') or '')
            if new_id:
                employees.append({'id': new_id, 'login': login,
                                  'email': _norm_email(created.get('email')),
                                  'alt_emails': [], 'department': target_dept,
                                  'note': payload['note'],
                                  'blocked': False})
                emp_by_login[login.lower()] = employees[-1]
            _user_map_set(login, email, u['ou_dn'], target_dept)
            report['users']['created'] += 1
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
       исключается из создания в Яндекс 360).
    3. Синхронизирует подразделения, затем пользователей.
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
        'departments': {'created': 0, 'matched': 0, 'skipped_root': 0},
        'users': {'created': 0, 'moved': 0, 'updated': 0, 'blocked': 0,
                  'unchanged': 0, 'skipped': 0},
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

        # 3. Запись изменений в Яндекс 360
        base = yandex360.api_base_url()
        headers = {'Authorization': f'OAuth {token}',
                   'Accept': 'application/json'}
        async with yandex360.make_async_client(base_url=base, headers=headers) as client:
            await sync_departments(state['ous'], y360_state, client, org_id,
                                   report,
                                   settings.get('parent_department_id', ''),
                                   root_dn=root_dn)
            await sync_users(state['users'], y360_state, client, org_id,
                             settings, report)

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
            "подразделения: создано=%s сопоставлено=%s "
            "исключено(головной OU)=%s, "
            "пользователи: создано=%s обновлено=%s перенесено=%s "
            "заблокировано=%s без изменений=%s пропущено=%s, "
            "ошибок=%s%s",
            trigger,
            report.get('ald_ous'), report.get('ald_users'),
            dep.get('created', 0), dep.get('matched', 0),
            dep.get('skipped_root', 0),
            usr.get('created', 0), usr.get('updated', 0),
            usr.get('moved', 0), usr.get('blocked', 0),
            usr.get('unchanged', 0), usr.get('skipped', 0),
            len(report.get('errors') or []),
            (' | первые ошибки: ' + '; '.join((report.get('errors') or [])[:2]))
            if report.get('errors') else '',
        )
        set_setting('y360_last_sync_ts', str(int(time.time())))
    return result


# ---------------------------------------------------------------------------
# Предпросмотр синхронизации (dry-run): сверка без записи в Яндекс 360
# ---------------------------------------------------------------------------

async def build_preview() -> Dict[str, Any]:
    """
    Собрать отчёт о планируемых изменениях БЕЗ записи в Яндекс 360.

    Показывает: подразделения ALD Pro, которые будут созданы (головной OU
    исключается); пользователей, которые будут созданы; переносы между
    подразделениями; сотрудников Яндекс 360, отсутствующих в ALD Pro
    (кандидатов на блокировку).
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

    # Соответствия департаментов (по кэшу или примечанию ald_pro_dn=)
    dept_by_note = {}
    for d in y360_state['departments']:
        m = NOTE_DN_RE.search(d.get('note') or '')
        if m:
            dept_by_note[m.group(1).lower()] = d['id']

    def dept_for_ou(dn: str) -> Optional[str]:
        if dn and dn.lower() == root_dn_lower:
            return ''  # головной OU не создаётся — пользователи вне департамента
        return _map_get('dep:' + dn) or dept_by_note.get((dn or '').lower())

    new_departments = [
        {'dn': ou['dn'], 'name': ou['name'], 'parent': ou['parent']}
        for ou in state['ous']
        if ou['dn'].lower() != root_dn_lower and not dept_for_ou(ou['dn'])
    ]

    emp_by_login = {e['login'].lower(): e for e in y360_state['employees'] if e['login']}
    emp_by_email = {}
    for e in y360_state['employees']:
        for mail in ([e['email']] + e['alt_emails']):
            if mail:
                emp_by_email.setdefault(mail, e)

    ald_logins = set()
    users_to_create, users_to_move = [], []
    for key in sorted(state['users']):
        u = state['users'][key]
        ald_logins.add(key)
        target_dept = dept_for_ou(u['ou_dn'])
        emp = emp_by_login.get(key) or emp_by_email.get(u['email'])
        if emp is None:
            users_to_create.append({
                'login': u['login'], 'email': u['email'],
                'displayName': u['displayName'], 'ou_dn': u['ou_dn'],
                'department': target_dept or '(будет создан)',
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
    }
