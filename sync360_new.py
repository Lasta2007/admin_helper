# -*- coding: utf-8 -*-
"""
Новый модуль синхронизации ALD Pro -> Яндекс 360 (пишется с нуля).

ЭТАП 1 (текущая реализация): получение дерева подразделений организации
Яндекс 360 и пользователей, находящихся в этих подразделениях.

Источники данных (см. https://yandex.ru/dev/api360/doc/ru/):
  * DepartmentService_List
      GET https://api360.yandex.net/directory/v1/org/{orgId}/departments
          ?limit=N&page_index=M
    Ответ: {"total": int, "departments": [ {id, name, parentDepartmentId,
    ...} ]}. Пагинация — только параметром page_index (номер страницы),
    orgId передается ИСКЛЮЧИТЕЛЬНО в пути запроса.
  * UserService_List
      GET https://api360.yandex.net/directory/v1/org/{orgId}/users
          ?limit=N&page_index=M
    Ответ: {"total": int, "users": [ {id, login, email, department,
    departmentId, blocked, ...} ]}.

Построение дерева:
  * Подразделение считается КОРНЕВЫМ, если его parentDepartmentId равен 0
    либо идентификатору самой организации (в API 360 родителем верхнего
    уровня является организация). В визуальном дереве для корневых
    подразделений родитель (childID в терминологии пользователя) = 0.
  * Каждый узел дерева содержит id и parentId ("childID" — id родителя) —
    их видно в скобках рядом с названием в текстовом представлении:
        Название подразделения (id=12345, childID=0)
    где childID — идентификатор родительского подразделения
    (для корневых — 0).
  * Пользователи прикрепляются к подразделению по полю department /
    departmentId; пользователи без подразделением (или ссылающиеся на
    несуществующий id) попадают в специальный узел «Без подразделения».

Публичный API модуля:
  * get_settings() / save_settings()     — настройки интеграции (org_id, токены)
  * fetch_departments()                  — сырой список подразделений Я360
  * fetch_users()                        — сырой список сотрудников Я360
  * build_department_tree()              — дерево подразделений + пользователи
  * render_tree_text()                   — текстовое (ASCII) представление
  * get_y360_tree(only_roots)            — полный результат: JSON + текст
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yandex360
from database import get_module_settings, set_module_settings

logger = logging.getLogger('admin_helper')

SETTINGS_KEY = 'y360_api_settings'   # общие настройки интеграции (токен, org_id)
STATUS_KEY = 'y360_tree_status'      # результат последней операции построения дерева

# Максимальный размер страницы методов *_List (документация допускает до 1000,
# 500 — безопасное значение, проверенное на практике).
PAGE_LIMIT = 500

# Узел для сотрудников, не привязанных ни к одному подразделению из дерева.
ORPHANS_TITLE = 'Без подразделения'


# ---------------------------------------------------------------------------
# Настройки интеграции
# ---------------------------------------------------------------------------

def get_settings() -> Dict[str, Any]:
    """Настройки Яндекс 360 (org_id, oauth_token, хосты) — из БД через yandex360."""
    return yandex360.get_settings()


def save_settings(api_host: str = None, org_id: str = None,
                  oauth_token: str = None, oauth_token_write: str = None,
                  client_id: str = None) -> bool:
    """Сохранить настройки интеграции (частичное обновление).

    yandex360.save_settings требует обязательные аргументы — недостающие
    берём из текущих настроек.
    """
    cur = get_settings()
    return yandex360.save_settings(
        api_host=api_host if api_host is not None else cur.get('api_host', ''),
        org_id=org_id if org_id is not None else cur.get('org_id', ''),
        oauth_token=(oauth_token if oauth_token is not None
                     else cur.get('oauth_token', '')),
        client_id=client_id if client_id is not None else cur.get('client_id', ''),
        oauth_token_write=oauth_token_write,
    )


def _require_configured() -> str:
    settings = get_settings()
    org_id = str(settings.get('org_id') or '').strip()
    token = str(settings.get('oauth_token') or '').strip()
    if not org_id:
        raise RuntimeError('Не задан идентификатор организации (orgId) в '
                           'настройках интеграции Яндекс 360')
    if not token:
        raise RuntimeError('Не задан OAuth-токен в настройках интеграции '
                           'Яндекс 360 (нужно корпоративное приложение с '
                           'правами directory:read_departments, '
                           'directory:read_users)')
    return org_id


# ---------------------------------------------------------------------------
# низкоуровневый выбор страниц (UserService_List / DepartmentService_List)
# ---------------------------------------------------------------------------

async def _fetch_all(method: str, path: str, list_key: str) -> List[dict]:
    """Выбрать все страницы списка Directory API.

    Запрос строго по документации: orgId только в пути, query — limit и
    page_index (номер страницы). Все /directory/... запросы выполняет
    yandex360.request(), который принудительно использует правильный хост
    https://api360.yandex.net.
    """
    items: List[dict] = []
    page_index = 0
    while True:
        resp = await yandex360.request(method, path,
                                       params={'limit': PAGE_LIMIT,
                                               'page_index': page_index})
        if resp.status_code != 200:
            raise _http_error(resp, method)
        body = resp.json() or {}
        page = body.get(list_key)
        if not isinstance(page, list):
            # запасной вариант на случай отличий ответа
            for key in ('departments', 'users', 'items'):
                if isinstance(body.get(key), list):
                    page = body[key]
                    break
        page = page or []
        items.extend(page)
        total = int(body.get('total') or 0)
        if not page:
            break
        if total and len(items) >= total:
            break
        if not total and len(page) < PAGE_LIMIT:
            break
        page_index += 1
        await asyncio.sleep(0.05)  # щадим rate limit
    return items


def _http_error(resp, method: str) -> RuntimeError:
    """Сформировать понятную ошибку по ответу API."""
    status = resp.status_code
    url = str(resp.request.url)
    body = (resp.text or '')[:300]
    if status == 401:
        auth_hdr = resp.request.headers.get('authorization') or ''
        tok_mask = (auth_hdr[:14] + '...' + auth_hdr[-4:]
                    if len(auth_hdr) > 22 else auth_hdr or 'ОТСУТСТВУЕТ')
        hint = ('\nHTTP 401 «Не авторизован» при правильном хосте означает '
                'проблему с токеном.\nЗаголовок запроса: Authorization: %s\n'
                'Проверьте: 1) токен выдан корпоративному приложению Яндекс '
                '360, установленному в организации (не личному приложению '
                'Яндекс ID); 2) пользователь-владелец токена состоит в '
                'организации и имеет права администратора каталога; '
                '3) срок действия токена не истёк.' % tok_mask)
    elif status == 403:
        hint = ' HTTP 403: токен принят, но не хватает прав (directory:read_*).'
    elif status == 404:
        host = url.split('/')[2] if '://' in url else url
        hint = (' Проверьте orgId.' if host == 'api360.yandex.net' else
                ' Запрос ушёл не на api360.yandex.net — проверьте настройки.')
    else:
        hint = ''
    return RuntimeError('Яндекс 360 вернул HTTP %s при %s %s.%s Ответ: %s'
                        % (status, method, url, hint, body))


# ---------------------------------------------------------------------------
# Получение данных из Яндекс 360
# ---------------------------------------------------------------------------

async def fetch_departments(org_id: Optional[str] = None) -> List[dict]:
    """Сырой список подразделений (DepartmentService_List, все страницы)."""
    org_id = org_id or _require_configured()
    path = yandex360.org_path(org_id, 'departments')
    deps = await _fetch_all('GET', path, 'departments')
    logger.info('Яндекс 360: получено подразделений: %d', len(deps))
    return deps


async def fetch_users(org_id: Optional[str] = None) -> List[dict]:
    """Сырой список сотрудников (UserService_List, все страницы)."""
    org_id = org_id or _require_configured()
    path = yandex360.org_path(org_id, 'users')
    users = await _fetch_all('GET', path, 'users')
    logger.info('Яндекс 360: получено сотрудников: %d', len(users))
    return users


# ---------------------------------------------------------------------------
# Построение дерева подразделений
# ---------------------------------------------------------------------------

def _to_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _user_dept_id(user: dict) -> Optional[int]:
    """Определить id подразделения сотрудника по ответу UserService_List.

    В разных версиях API поле может называться departmentId (число) или
    department (строка с id или названием).
    """
    did = _to_int(user.get('departmentId'))
    if did is not None:
        return did
    dep = user.get('department')
    if isinstance(dep, dict):
        return _to_int(dep.get('id'))
    did = _to_int(dep)
    if did is not None:
        return did
    return None


def build_department_tree(departments: List[dict], users: List[dict],
                          org_id) -> Dict[str, Any]:
    """Построить дерево подразделений и распределить по нему пользователей.

    Возвращает dict:
      {
        'org_id': int,
        'roots': [ <узел>, ... ],
        'all_nodes': [ <узел>, ... ],       # плоский список, включая orphan-узел
        'node_by_id': {id: <узел>},
        'orphans_title': str,
      }
    Узел:
      {
        'id': int | None,        # None для служебного узла «Без подразделения»
        'name': str,
        'parentId': int,         # «childID»: id родителя; 0 для корневых
        'isRoot': bool,
        'isOrgRoot': bool,       # служебный ли это узел (не подразделение)
        'children': [ <узел>, ... ],
        'users': [ {'id','login','email','fullName','blocked'}, ... ],
        'raw': <исходный dict API>,
      }
    """
    org_id_int = _to_int(org_id)

    # --- нормализуем подразделения -----------------------------------------
    nodes_by_id: Dict[int, dict] = {}
    order: List[int] = []
    for d in departments:
        did = _to_int(d.get('id'))
        if did is None:
            continue
        node = {
            'id': did,
            'name': (d.get('name') or '(без названия)').strip(),
            'parentIdRaw': _to_int(d.get('parentDepartmentId')),
            'parentId': 0,
            'isRoot': False,
            'isOrgRoot': False,
            'children': [],
            'users': [],
            'raw': d,
        }
        nodes_by_id[did] = node
        order.append(did)

    # --- определяем корни ----------------------------------------------------
    # Корневое подразделение: parentDepartmentId отсутствует/0 или равен id
    # организации (родитель верхнего уровня в API 360 — сама организация).
    for did in order:
        node = nodes_by_id[did]
        pid = node['parentIdRaw']
        if pid is None or pid == 0 or (org_id_int is not None and pid == org_id_int) \
                or pid not in nodes_by_id:
            node['isRoot'] = True
            node['parentId'] = 0
        else:
            node['parentId'] = pid

    roots: List[dict] = []
    for did in order:
        node = nodes_by_id[did]
        if node['isRoot']:
            roots.append(node)
        else:
            nodes_by_id[node['parentId']]['children'].append(node)

    # защита от циклов/сирот при некорректных parentDepartmentId: узлы,
    # которые не попали ни в корни, ни в чьи-то дети, считаем корневыми.
    placed = set(order) & {r['id'] for r in roots}
    def _collect_ids(ns):
        for n in ns:
            yield n['id']
            yield from _collect_ids(n['children'])
    reachable = set(_collect_ids(roots))
    for did in order:
        if did not in reachable:
            node = nodes_by_id[did]
            node['isRoot'] = True
            node['parentId'] = 0
            roots.append(node)

    # --- служебный узел «Без подразделения» ----------------------------------
    orphans_node = {
        'id': None,
        'name': ORPHANS_TITLE,
        'parentId': 0,
        'isRoot': True,
        'isOrgRoot': True,
        'children': [],
        'users': [],
        'raw': None,
    }

    # --- распределяем пользователей ------------------------------------------
    matched = unmatched = 0
    for u in users:
        info = {
            'id': str(u.get('id') or ''),
            'login': str(u.get('login') or ''),
            'email': (u.get('email') or '').strip().lower(),
            'fullName': ' '.join(filter(None, [u.get('lastName'),
                                               u.get('firstName'),
                                               u.get('middleName')])),
            'position': u.get('position') or '',
            'blocked': bool(u.get('blocked')),
        }
        did = _user_dept_id(u)
        target = nodes_by_id.get(did) if did is not None else None
        if target is not None:
            target['users'].append(info)
            matched += 1
        else:
            orphans_node['users'].append(info)
            unmatched += 1

    all_nodes = [nodes_by_id[d] for d in order] + [orphans_node]

    # сортировка для стабильного вывода
    all_nodes_sorted = sorted(all_nodes, key=lambda n: (not n['isRoot'],
                                                         n['name'].lower()))
    roots_sorted = sorted(roots, key=lambda n: n['name'].lower())
    stack = [orphans_node] + roots_sorted
    while stack:
        n = stack.pop()
        n['children'].sort(key=lambda c: c['name'].lower())
        stack.extend(n['children'])

    result = {
        'org_id': org_id,
        'roots': roots_sorted,
        'orphan_node': orphans_node,
        'all_nodes': all_nodes,
        'node_by_id': nodes_by_id,
        'stats': {
            'departments': len(nodes_by_id),
            'root_departments': len(roots_sorted),
            'users_total': len(users),
            'users_matched': matched,
            'users_without_department': unmatched,
        },
    }
    return result


# ---------------------------------------------------------------------------
# Текстовое (ASCII) представление дерева
# Формат узла:  Название (id=<ID подразделения>, childID=<ID родителя>)
# Для корневых подразделений childID = 0.
# ---------------------------------------------------------------------------

def render_tree_text(tree: Dict[str, Any]) -> str:
    lines: List[str] = []
    org_id = tree.get('org_id')
    stats = tree.get('stats', {})
    lines.append('Яндекс 360: дерево организации orgId=%s '
                 '(подразделений=%d, корней=%d, пользователей=%d, '
                 'без подразделения=%d)'
                 % (org_id, stats.get('departments', 0),
                    stats.get('root_departments', 0),
                    stats.get('users_total', 0),
                    stats.get('users_without_department', 0)))
    lines.append('Формат: Название (id=ID подразделения, '
                 'childID=ID родительского подразделения; для корней childID=0)')

    def fmt(node: dict) -> str:
        label = node['name']
        users = len(node.get('users') or [])
        suffix = f' [сотрудников: {users}]' if users else ''
        if node.get('isOrgRoot') and node.get('id') is None:
            return f'{label}{suffix}'
        return f"{label} (id={node['id']}, childID={node['parentId']}){suffix}"

    def walk(node: dict, prefix: str, is_last: bool, is_root_call: bool):
        connector = '└─ ' if is_last else '├─ '
        if is_root_call:
            lines.append(fmt(node))
            new_prefix = ''
        else:
            lines.append(prefix + connector + fmt(node))
            new_prefix = prefix + ('   ' if is_last else '│  ')
        children = node.get('children') or []
        last = len(children) - 1
        for i, child in enumerate(children):
            walk(child, new_prefix, i == last, False)

    for root in tree['roots']:
        walk(root, '', False, True)
    orphan = tree.get('orphan_node')
    if orphan and orphan['users']:
        walk(orphan, '', True, True)
    return '\n'.join(lines)


def tree_to_json(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Сериализуемое представление дерева (без исходных raw-полей API)."""
    def node_to_dict(node: dict) -> dict:
        return {
            'id': node['id'],
            'name': node['name'],
            'parentId': node['parentId'],   # childID: 0 для корневых
            'isRoot': node['isRoot'],
            'isServiceNode': bool(node.get('isOrgRoot')),
            'userCount': len(node['users']),
            'users': node['users'],
            'children': [node_to_dict(c) for c in node['children']],
        }
    return {
        'org_id': tree['org_id'],
        'stats': tree['stats'],
        'tree': [node_to_dict(r) for r in tree['roots']],
        'withoutDepartment': node_to_dict(tree['orphan_node'])
        if tree['orphan_node']['users'] else None,
    }


# ---------------------------------------------------------------------------
# Публичная операция ЭТАПА 1
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ALD Pro: дерево OU и пользователи subtree базового OU
# ---------------------------------------------------------------------------
#
# Источник данных — API ALD Pro (см. 09_Документация_API_ALD_Pro):
#   * GET /api/ds/organizational-units/{dn}/organizational-units — дети OU;
#   * GET /api/ds/organizational-units/{dn}/users-list — пользователи OU.
#
# Идентификаторы в дереве (условные, стабильные между запусками):
#   * id      — порядковый номер подразделения в дереве (1, 2, 3 ...);
#               узел «Без подразделения» получает отрицательный id (-1).
#   * childID — id родительского узла; для корневого (базового) OU childID = 0.
# В текстовом выводе рядом с названием:  Название (id=N, childID=M).

ALD_SETTINGS_KEY = 'y360_sync_settings'   # общие настройки выгрузки (root_ou_dn и др.)
ALD_STATUS_KEY = 'aldpro_tree_status'     # результат последней операции по ALD Pro
ALD_ORPHANS_TITLE = 'Без подразделения'
ALD_MAX_DEPTH = 40                        # защита от некорректного цикла в каталоге


# Значения настроек выгрузки по умолчанию
ALD_DEFAULT_SETTINGS: Dict[str, Any] = {
    'root_ou_dn': '',
    'email_domain': '',
    'parent_department_id': '',
    'sync_interval_minutes': 60,
    'block_missing_users': True,
}


def get_ald_sync_settings() -> Dict[str, Any]:
    """Настройки выгрузки (базовый OU ALD Pro, домен почты и т.д.)."""
    result = dict(ALD_DEFAULT_SETTINGS)
    s = get_module_settings(ALD_SETTINGS_KEY)
    if isinstance(s, dict):
        result.update({k: v for k, v in s.items()
                       if k in ALD_DEFAULT_SETTINGS})
    try:
        result['sync_interval_minutes'] = max(
            1, int(result.get('sync_interval_minutes') or 60))
    except (TypeError, ValueError):
        result['sync_interval_minutes'] = 60
    return result


def save_ald_sync_settings(root_ou_dn: str = None, email_domain: str = None,
                           parent_department_id: str = None,
                           sync_interval_minutes: int = None,
                           block_missing_users: bool = None) -> Dict[str, Any]:
    """Частичное обновление настроек выгрузки (сохраняются в общую БД)."""
    cur = get_ald_sync_settings()
    new = {
        'root_ou_dn': (cur.get('root_ou_dn', '') if root_ou_dn is None
                       else root_ou_dn.strip()),
        'email_domain': (cur.get('email_domain', '') if email_domain is None
                         else email_domain.strip()),
        'parent_department_id': (cur.get('parent_department_id', '')
                                 if parent_department_id is None
                                 else str(parent_department_id).strip()),
        'sync_interval_minutes': (int(cur.get('sync_interval_minutes') or 60)
                                  if sync_interval_minutes is None
                                  else int(sync_interval_minutes)),
        'block_missing_users': (bool(cur.get('block_missing_users', True))
                                if block_missing_users is None
                                else bool(block_missing_users)),
    }
    set_module_settings(ALD_SETTINGS_KEY, new)
    return new


def _ald_first(d: dict, *keys, default=None):
    """Первое непустое значение среди ключей (без учёта регистра)."""
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = lowered.get(key.lower())
        if isinstance(v, list):
            v = v[0] if v else None
        if v not in (None, '', []):
            return v
    return default


def _ald_norm_email(value) -> str:
    import re
    if not value:
        return ''
    if isinstance(value, list):
        value = ','.join(str(v) for v in value)
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', str(value))
    return m.group(0).lower() if m else ''


def _ald_parse_user_items(result: Any) -> List[dict]:
    """Вытащить плоский список записей пользователей из ответа users-list."""
    items: List[Any] = []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        data = result.get('data')
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ('userlistitems', 'users', 'items', 'content'):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
    out: List[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        inner = raw.get('userlistitem')
        out.append(inner if isinstance(inner, dict) else raw)
    return out


async def fetch_ald_pro_tree(base_ou_dn: Optional[str] = None) -> Dict[str, Any]:
    """Получить из ALD Pro дерево OU subtree базового OU и пользователей OU.

    Обход детей выполняется рекурсивно (флаг is_leaf у ALD Pro ненадёжен),
    пользователи запрашиваются для каждого узла. Возвращает структуру:
      {
        'base_dn': <DN базового OU>,
        'roots': [ <узел>, ... ],           # обычно один корень = базовый OU
        'all_nodes': [ <узел>, ... ],       # плоский список (родители раньше)
        'stats': {...},
      }
    Узел:
      {
        'id': int,                          # условный id узла в дереве
        'childID': int,                     # id родителя; 0 для корневого OU
        'name': str, 'dn': str, 'parent_dn': str,
        'isRoot': bool, 'isServiceNode': bool,
        'children': [...], 'users': [{'login','email','displayName',...}],
      }
    """
    import asyncio
    from urllib.parse import unquote
    import ald_pro

    settings = get_ald_sync_settings()
    base_dn = (base_ou_dn if base_ou_dn is not None
               else settings.get('root_ou_dn', '')).strip()
    if not base_dn:
        raise RuntimeError('Не задан базовый OU ALD Pro (поле «Базовый OU» на '
                           'странице Яндекс 360 или в настройках интеграции)')
    base_dn = unquote(base_dn)

    client = await ald_pro._get_authenticated_client()
    if not client:
        raise RuntimeError('ALD Pro не настроен или недоступен — проверьте '
                           'подключение на странице «ALD Pro»')

    nodes_by_dn: Dict[str, dict] = {}
    next_id = 1

    def add_node(dn: str, name: str, parent_dn: str) -> dict:
        nonlocal next_id
        node = {
            'id': next_id,
            'childID': 0,
            'name': (name or '').strip() or dn.split(',')[0].lstrip('OUou='),
            'dn': dn,
            'parent_dn': parent_dn,
            'isRoot': not parent_dn,
            'isServiceNode': False,
            'children': [],
            'users': [],
        }
        next_id += 1
        nodes_by_dn[dn.lower()] = node
        return node

    async def walk(dn: str, name: str, parent_dn: str, depth: int) -> dict:
        node = add_node(dn, name, parent_dn)
        node['childID'] = 0 if not parent_dn else \
            (nodes_by_dn.get(parent_dn.lower(), {}).get('id') or 0)
        if depth >= ALD_MAX_DEPTH:
            logger.warning('ALD Pro: достигнута максимальная глубина дерева '
                           '(%d), обход ниже %s остановлен', ALD_MAX_DEPTH, dn)
            return node
        children = await ald_pro.fetch_child_units(dn, client=client)
        grandkids = await asyncio.gather(*(
            walk(unquote(str(ch.get('dn') or '')),
                 str(ch.get('name') or ''), dn, depth + 1)
            for ch in children if ch.get('dn')),
            return_exceptions=True)
        for gk in grandkids:
            if isinstance(gk, Exception):
                logger.warning('ALD Pro: ошибка обхода ветки: %s', gk)
            elif isinstance(gk, dict):
                node['children'].append(gk)
        node['children'].sort(key=lambda n: n['name'].lower())
        return node

    base_node = await walk(base_dn, '', '', 0)

    # --- пользователи каждого узла ------------------------------------------
    # (запрашиваем и для базового OU до отсечения его из дерева: его сотрудники
    #  попадут в служебный узел «Без подразделения»)
    seen_logins: set = set()

    async def load_users(node: dict):
        res = await ald_pro.get_organizational_unit_users(node['dn'],
                                                          client=client)
        for raw in _ald_parse_user_items(res):
            login = str(_ald_first(raw, 'userlistitem_login', 'login',
                                   'sAMAccountName', 'samaccountname', 'uid',
                                   default='') or '').strip()
            if not login or login.lower() in seen_logins:
                continue
            seen_logins.add(login.lower())
            upn = str(_ald_first(raw, 'userPrincipalName',
                                 'userprincipalname',
                                 'userlistitem_user_principal_name',
                                 default='') or '').strip()
            email = _ald_norm_email(_ald_first(raw, 'userlistitem_mail',
                                               'mail', 'email'))
            source = 'mail'
            if not email and '@' in upn:
                email, source = _ald_norm_email(upn), 'upn'
            # Почта не генерируется: если у пользователя нет реального
            # адреса (mail / userPrincipalName), email остаётся пустым.
            node['users'].append({
                'login': login,
                'email': email,
                'email_source': source,
                'displayName': str(_ald_first(raw, 'userlistitem_common_name',
                                              'cn', 'display_name',
                                              'displayName', default=login)),
                'firstName': str(_ald_first(raw, 'userlistitem_first_name',
                                            'givenName', default='') or ''),
                'lastName': str(_ald_first(raw, 'userlistitem_last_name',
                                           'sn', default='') or ''),
                'position': str(_ald_first(raw, 'userlistitem_title', 'title',
                                           default='') or ''),
                'ou_dn': node['dn'],
            })

    all_nodes = sorted(nodes_by_dn.values(), key=lambda n: n['id'])
    await asyncio.gather(*(load_users(n) for n in all_nodes),
                         return_exceptions=True)

    # --- отсечение базового OU из дерева -------------------------------------
    # Базовый OU НЕ включается в дерево: его непосредственные дочерние OU
    # становятся корневыми узлами (childID=0). Пользователи, привязанные
    # к самому базовому OU, попадают в служебный узел «Без подразделения».
    roots: List[dict] = list(base_node['children'])
    for r in roots:
        r['childID'] = 0
        r['isRoot'] = True
    nodes_by_dn.pop(base_dn.lower(), None)
    all_nodes = [n for n in all_nodes if n is not base_node]

    # --- служебный узел «Без подразделения» ----------------------------------
    orphan = {
        'id': -1,
        'childID': 0,
        'name': ALD_ORPHANS_TITLE,
        'dn': '',
        'parent_dn': '',
        'isRoot': True,
        'isServiceNode': True,
        'children': [],
        'users': list(base_node['users']),
    }

    roots.sort(key=lambda n: n['name'].lower())
    stats = {
        'base_dn': base_dn,
        'departments': len(all_nodes),
        'root_departments': len(roots),
        'users_total': sum(len(n['users']) for n in all_nodes)
                       + len(orphan['users']),
        'users_matched': sum(len(n['users']) for n in all_nodes),
        'users_without_department': len(orphan['users']),
    }
    return {
        'base_dn': base_dn,
        'roots': roots,
        'orphan_node': orphan,
        'all_nodes': all_nodes,
        'node_by_dn': nodes_by_dn,
        'stats': stats,
    }


def render_ald_tree_text(tree: Dict[str, Any]) -> str:
    """ASCII-представление дерева ALD Pro: Название (id=N, childID=M)."""
    lines: List[str] = []
    stats = tree.get('stats', {})
    lines.append('ALD Pro: дерево подразделений внутри базового OU "%s" '
                 '(сам базовый OU в дерево не включён; '
                 'подразделений=%d, пользователей=%d)'
                 % (stats.get('base_dn', ''), stats.get('departments', 0),
                    stats.get('users_total', 0)))
    lines.append('Формат: Название (id=ID узла в дереве, childID=ID родителя; '
                 'для корневых подразделений childID=0)')

    def fmt(node: dict) -> str:
        users = len(node.get('users') or [])
        suffix = f' [сотрудников: {users}]' if users else ''
        return f"{node['name']} (id={node['id']}, childID={node['childID']}){suffix}"

    def walk(node: dict, prefix: str, is_last: bool, is_root_call: bool):
        if is_root_call:
            lines.append(fmt(node))
            new_prefix = ''
        else:
            lines.append(prefix + ('└─ ' if is_last else '├─ ') + fmt(node))
            new_prefix = prefix + ('   ' if is_last else '│  ')
        children = node.get('children') or []
        last = len(children) - 1
        for i, child in enumerate(children):
            walk(child, new_prefix, i == last, False)

    for root in tree['roots']:
        walk(root, '', False, True)
    orphan = tree.get('orphan_node')
    if orphan and orphan['users']:
        walk(orphan, '', True, True)
    return '\n'.join(lines)


def ald_tree_to_json(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Машиночитаемое представление дерева ALD Pro (для отображения в UI)."""
    def node_to_dict(node: dict) -> dict:
        return {
            'id': node['id'],
            'childID': node['childID'],
            'name': node['name'],
            'dn': node['dn'],
            'isRoot': node['isRoot'],
            'isServiceNode': node['isServiceNode'],
            'userCount': len(node['users']),
            'users': node['users'],
            'children': [node_to_dict(c) for c in node['children']],
        }
    return {
        'source': 'ald_pro',
        'base_dn': tree['base_dn'],
        'stats': tree['stats'],
        'tree': [node_to_dict(r) for r in tree['roots']],
        'withoutDepartment': node_to_dict(tree['orphan_node'])
        if tree['orphan_node']['users'] else None,
    }


async def get_ald_pro_tree(base_ou_dn: Optional[str] = None) -> Dict[str, Any]:
    """Публичная операция: построить дерево ALD Pro и вернуть JSON + текст.

    base_ou_dn — базовый OU из запроса пользователя; если пустой — берётся
    сохранённая настройка root_ou_dn. Результат сохраняется в статус.
    """
    started = datetime.now().isoformat(timespec='seconds')
    try:
        tree = await fetch_ald_pro_tree(base_ou_dn)
        payload = ald_tree_to_json(tree)
        text = render_ald_tree_text(tree)
        status = {
            'success': True,
            'started': started,
            'finished': datetime.now().isoformat(timespec='seconds'),
            'base_dn': tree['base_dn'],
            'stats': tree['stats'],
            'error': None,
        }
        result = {'success': True, 'json': payload, 'text': text,
                  'stats': tree['stats']}
    except Exception as e:
        logger.exception('Ошибка построения дерева ALD Pro')
        status = {
            'success': False,
            'started': started,
            'finished': datetime.now().isoformat(timespec='seconds'),
            'base_dn': (base_ou_dn or '').strip() or None,
            'stats': None,
            'error': str(e),
        }
        result = {'success': False, 'error': str(e)}
    try:
        set_module_settings(ALD_STATUS_KEY, status)
    except Exception as e:
        logger.warning('Не удалось сохранить статус дерева ALD Pro: %s', e)
    return result


def get_ald_pro_last_status() -> Dict[str, Any]:
    status = get_module_settings(ALD_STATUS_KEY)
    return status if isinstance(status, dict) else {}


# ===========================================================================
# ЭТАП 2: синхронизация СТРУКТУРЫ подразделений ALD Pro -> Яндекс 360
# ===========================================================================
#
# Соответствие OU ALD Pro <-> подразделение Яндекс 360 хранится в таблице
# y360_sync_map (ключ 'dep:<dn>', значение — id подразделения 360).
#
# Алгоритм (используются ТОЛЬКО методы из документации API Яндекс 360,
# https://yandex.ru/dev/api360/doc/ru/):
#   1. Дерево OU ALD Pro (fetch_ald_pro_tree) и список подразделений 360
#      (DepartmentService_List) читаются полностью.
#   2. Обход дерева ALD Pro идёт строго от корней к листьям (BFS): родитель
#      создаётся раньше детей, поэтому parentDepartmentId всегда известен.
#   3. Для каждого OU:
#      * соответствия нет            -> DepartmentService_Create
#        (POST /directory/v1/org/{orgId}/departments); родителем корневых
#        OU считается организация (parentDepartmentId = orgId) либо отдел
#        из настройки parent_department_id;
#      * имя в 360 отличается        -> DepartmentService_Update (PATCH);
#      * переезд OU в ALD Pro        -> PATCH parentDepartmentId на новый
#        родитель 360;
#      * подразделения в 360, созданные синхронизацией для удалённых в
#        ALD Pro OU, НЕ удаляются автоматически (безопасно): они помечаются
#        как "stale" и выводятся в отчёте.
#   4. Повторное использование: если в 360 уже есть подразделение с таким же
#      именем внутри того же родителя и оно ни за кем не закреплено — оно
#      закрепляется за OU (первичная привязка), без создания дубля.
#
# Периодический фоновый запуск — run_departments_sync_loop().

DEPT_MAP_PREFIX = 'dep:'          # ключ y360_sync_map: 'dep:<dn>' -> id dept 360
DEPT_SYNC_STATUS_KEY = 'y360_dept_sync_status'
DEPT_NOTE_TEMPLATE = 'ALD Pro DN: {dn}'


def _db_conn():
    from database import get_connection
    return get_connection()


def dept_map_load() -> Dict[str, str]:
    """Загрузить соответствие DN OU ALD Pro -> id подразделения Яндекс 360."""
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT key, value FROM y360_sync_map WHERE key LIKE ?",
            (DEPT_MAP_PREFIX + '%',)).fetchall()
    finally:
        conn.close()
    return {r[0][len(DEPT_MAP_PREFIX):]: r[1] for r in rows}


def dept_map_save(dn: str, dept_id) -> None:
    """Сохранить одно соответствие 'dep:<dn>' -> id подразделения 360."""
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO y360_sync_map(key, value) VALUES(?, ?)",
            (DEPT_MAP_PREFIX + dn.lower(), str(dept_id)))
        conn.commit()
    finally:
        conn.close()


def dept_map_delete(dn: str) -> None:
    conn = _db_conn()
    try:
        conn.execute("DELETE FROM y360_sync_map WHERE key=?",
                     (DEPT_MAP_PREFIX + dn.lower(),))
        conn.commit()
    finally:
        conn.close()


def _ald_flatten_nodes(tree: Dict[str, Any]) -> List[dict]:
    """Список узлов дерева ALD Pro в порядке BFS (родители раньше детей)."""
    out: List[dict] = []
    queue = list(tree.get('roots') or [])
    while queue:
        n = queue.pop(0)
        out.append(n)
        queue.extend(n.get('children') or [])
    orphan = tree.get('orphan_node')
    if orphan is not None:
        orphan['_is_orphan_service'] = True
    return out


def _y360_parent_id_for(node: dict, roots_ids: set, dept_by_id: dict,
                       mapping: Dict[str, str], org_id: str,
                       configured_parent: str,
                       y360_root_id: Optional[int] = None) -> Optional[int]:
    """Вычислить parentDepartmentId для OU по его месту в дереве ALD Pro.

    Корневые OU: настроенный родитель (parent_department_id); если он не
    задан — автоматически определённое корневое подразделение организации
    (y360_root_id, см. find_y360_root_department: в пустой структуре Я360
    это «Все сотрудники», id=1, parentID=0). ВАЖНО: ни id самой организации
    (orgId), ни 0 сервером DepartmentService_Create как родитель НЕ
    принимаются (HTTP 400 про обязательность parentId), поэтому они из
    этой функции не возвращаются.

    Некорневые: id подразделения-родителя из маппинга; если родителя ещё
    нет в 360 (не должен случаться при обходе сверху вниз) — fallback на
    root-родитель (настроенный или корневое подразделение 360).
    """
    def _root_fallback() -> Optional[int]:
        pid = _to_int(configured_parent)
        if pid is not None:
            return pid
        return y360_root_id

    if node['childID'] == 0 or node['dn'].lower() in roots_ids:
        return _root_fallback()
    parent_dn = node.get('parent_dn') or ''
    pid = _to_int(mapping.get(parent_dn.lower()))
    # привязка/создание родителя ещё не выполнено (planned id недоступен
    # на этапе плана) — не возвращаем orgId, а уходим в root-fallback
    if pid is None or pid not in dept_by_id:
        pid = _root_fallback()
    return pid


# Подразделение «Все сотрудники» — стандартный корень структуры, который
# Яндекс 360 создаёт в каждой организации автоматически. В выгрузке
# DepartmentService_List оно выглядит так: id=1, parentID=0,
# name="Все сотрудники". Именно оно (а НЕ id организации) является родителем
# для корневых элементов плана синхронизации.
Y360_DEFAULT_ROOT_NAME = 'Все сотрудники'


def find_y360_root_department(departments: List[dict], org_id) -> Optional[int]:
    """Определить id КОРНЕВОГО подразделения организации Яндекс 360.

    Пустая структура организации Яндекс 360 содержит одно подразделение:
        id=1, parentID=0, name="Все сотрудники"
    Поэтому для корневых элементов плана синхронизации parentDepartmentId
    должен быть равен id этого подразделения (обычно 1), а НЕ 0 и НЕ orgId.

    Порядок поиска:
      1. подразделение с именем «Все сотрудники», родитель которого 0/orgId
         (или отсутствует) — гарантированный корень;
      2. единственное подразделение верхнего уровня (parentDepartmentId
         отсутствует/0/равен orgId);
      3. иначе None — требуется задать родителя вручную.

    Важно: id организации (orgId) НЕ принимается сервером в качестве
    родителя — POST /departments с parentDepartmentId == orgId возвращает
    HTTP 400 вида «Ошибка проверки поля "parentId": Это поле является
    обязательным» (родитель по такому id не находится).

    Возвращает None, если корневое подразделение найти не удалось
    (например, структура в 360 пуста).
    """
    org = _to_int(org_id)
    by_id = {_to_int(d.get('id')): d for d in departments
             if _to_int(d.get('id')) is not None}

    def _is_top_level(d: dict) -> bool:
        pid = _to_int(d.get('parentDepartmentId'))
        return pid is None or pid == 0 or (org is not None and pid == org)

    # 1) стандартный корень «Все сотрудники»
    for did, d in by_id.items():
        if did == org:
            continue
        if (d.get('name') or '').strip().lower() == Y360_DEFAULT_ROOT_NAME \
                and _is_top_level(d):
            return did

    # 2) единственное подразделение верхнего уровня
    candidates = [did for did, d in by_id.items()
                  if did != org and _is_top_level(d)]
    if len(candidates) == 1:
        return candidates[0]
    # 3) несколько верхних уровней без «Все сотрудники» — корень определить
    # нельзя (нужна настройка parent_department_id вручную)
    return None


def y360_parent_exists(parent_id: Optional[int], dept_by_id: dict,
                       org_id) -> bool:
    """Существует ли ожидаемый родитель-подразделение в каталоге 360.

    id организации (orgId) родителем быть не может — см.
    find_y360_root_department(). Возвращает False для None и для orgId,
    чтобы синхронизация заранее сообщала о некорректной настройке, а не
    получала непонятный HTTP 400 от Yandex.
    """
    if parent_id is None:
        return False
    org = _to_int(org_id)
    if parent_id == org:
        return False
    return parent_id in dept_by_id


def _build_name_index(departments: List[dict]) -> Dict[Tuple[int, str], int]:
    """Индекс (parentId, нижний регистр имени) -> id подразделения 360.

    Используется для первичной привязки уже существующих подразделений
    (чтобы не создавать дубли, если структура в 360 собрана вручную).
    """
    idx: Dict[Tuple[int, str], int] = {}
    for d in departments:
        did = _to_int(d.get('id'))
        name = (d.get('name') or '').strip().lower()
        if did is None or not name:
            continue
        pid = _to_int(d.get('parentDepartmentId'))
        idx.setdefault((pid if pid is not None else 0, name), did)
    return idx


async def plan_departments_sync(base_ou_dn: Optional[str] = None) -> Dict[str, Any]:
    """Рассчитать план синхронизации структуры (dry-run, без записи).

    Возвращает dict:
      {'actions': [ <действие>, ... ], 'ald_tree': ..., 'y360_departments': ...,
       'stats': {...}}
    Действие: {'type': 'create'|'rename'|'move'|'bind'|'stale'|'noop', ...}
    """
    settings = get_ald_sync_settings()
    org_id = _require_configured()
    configured_parent = str(settings.get('parent_department_id') or '').strip()
    if configured_parent and not str(configured_parent).isdigit():
        raise RuntimeError('Настройка «Родительский департамент в Яндекс 360» '
                           'должна содержать числовой id подразделения')

    ald_tree, y360_deps = await asyncio.gather(
        fetch_ald_pro_tree(base_ou_dn), fetch_departments(org_id))

    mapping = dept_map_load()                      # dn(lower) -> id dept 360
    dept_by_id = {_to_int(d.get('id')): d for d in y360_deps
                  if _to_int(d.get('id')) is not None}
    mapped_ids = {int(v) for v in mapping.values() if str(v).isdigit()}
    name_index = _build_name_index(y360_deps)
    roots_ids = {n['dn'].lower() for n in (ald_tree.get('roots') or [])}
    # корневое подразделение организации Яндекс 360 — родитель для корневых
    # OU. В пустой структуре Я360 это «Все сотрудники» (id=1, parentID=0);
    # ни orgId, ни 0 родителем в DepartmentService_Create не являются.
    y360_root_id = find_y360_root_department(y360_deps, org_id)
    if configured_parent and _to_int(configured_parent) == _to_int(org_id):
        raise RuntimeError('В настройках «Родительский департамент в Яндекс '
                           '360» указан id организации (%s). API Яндекс 360 '
                           'не принимает организацию родителем подразделения; '
                           'укажите id существующего подразделения (для '
                           'корней структуры — «Все сотрудники», обычно id=1)'
                           % configured_parent)
    if not configured_parent and y360_root_id is None:
        raise RuntimeError(
            'Не определён родительский элемент структуры Яндекс 360: '
            'не задан «Родительский департамент» в настройках и не '
            'удалось автоматически найти корневое подразделение '
            'организации («Все сотрудники»). Укажите id '
            'подразделения-родителя в настройках синхронизации.')

    actions: List[dict] = []
    planned: Dict[str, str] = {}                   # dn(lower) -> id (сущ./план)
    stats = {'create': 0, 'rename': 0, 'move': 0, 'bind': 0, 'stale': 0,
             'noop': 0, 'errors': 0}

    nodes = [n for n in _ald_flatten_nodes(ald_tree)
             if not n.get('_is_orphan_service')]

    for node in nodes:
        dn = node['dn']
        key = dn.lower()
        name = (node['name'] or '').strip() or dn.split(',')[0]
        expected_parent = _y360_parent_id_for(node, roots_ids, dept_by_id,
                                              {**mapping, **planned}, org_id,
                                              configured_parent,
                                              y360_root_id)
        dept_id = mapping.get(key) or planned.get(key)
        dept = dept_by_id.get(_to_int(dept_id)) if dept_id else None

        if dept is not None:
            actual_name = (dept.get('name') or '').strip()
            actual_parent = _to_int(dept.get('parentDepartmentId'))
            if actual_name != name:
                actions.append({'type': 'rename', 'dn': dn, 'name': name,
                                'id': str(dept_id),
                                'from': actual_name, 'to': name})
                stats['rename'] += 1
            elif (expected_parent is not None and actual_parent is not None
                    and actual_parent != expected_parent):
                actions.append({'type': 'move', 'dn': dn, 'name': name,
                                'id': str(dept_id),
                                'from': actual_parent, 'to': expected_parent})
                stats['move'] += 1
            else:
                stats['noop'] += 1
            planned[key] = str(dept_id)
            continue

        # соответствия нет — ищем свободное подразделение с тем же именем
        # внутри ожидаемого родителя (первичная привязка вместо дубля)
        existing_free = name_index.get((expected_parent, name.lower()))
        if existing_free is not None and existing_free not in mapped_ids \
                and str(existing_free) not in planned.values():
            actions.append({'type': 'bind', 'dn': dn, 'name': name,
                            'id': str(existing_free)})
            planned[key] = str(existing_free)
            stats['bind'] += 1
            continue

        if expected_parent is None:
            actions.append({'type': 'error', 'dn': dn, 'name': name,
                            'detail': 'Не удалось определить '
                                      'parentDepartmentId'})
            stats['errors'] += 1
            continue
        if not y360_parent_exists(expected_parent, dept_by_id, org_id):
            actions.append({'type': 'error', 'dn': dn, 'name': name,
                            'detail': ('Родитель id=%s не найден среди '
                                       'подразделений Яндекс 360'
                                       % expected_parent)})
            stats['errors'] += 1
            continue

        actions.append({'type': 'create', 'dn': dn, 'name': name,
                        'parentDepartmentId': expected_parent})
        stats['create'] += 1
        # id станет известен после фактического создания (execute-этап);
        # здесь placeholder, чтобы дети не остались без родителя в плане
        planned[key] = ''

    # подразделения 360, закреплённые за несуществующими OU ALD Pro
    live_dns = {n['dn'].lower() for n in nodes}
    for dn_l, dep_id in mapping.items():
        if dn_l in live_dns:
            continue
        dept = dept_by_id.get(_to_int(dep_id))
        if dept is None:
            # в 360 удалено вручную — чистим маппинг (в плане)
            actions.append({'type': 'unmap', 'dn': dn_l, 'id': dep_id,
                            'detail': 'подразделение отсутствует в Яндекс 360'})
            continue
        actions.append({'type': 'stale', 'dn': dn_l, 'id': dep_id,
                        'name': dept.get('name')})
        stats['stale'] += 1

    # --- второй проход (как в execute): перемещения существующих ------------
    # В первом проходе id ещё не созданных родителей были неизвестны; теперь
    # planned/сущ. id известны для всех синхронизированных OU, и ожидаемый
    # родитель вычисляется корректно.
    full_map = {**mapping, **{k: v for k, v in planned.items() if v}}
    for node in nodes:
        key = node['dn'].lower()
        dept_id = _to_int(full_map.get(key))
        dept = dept_by_id.get(dept_id) if dept_id is not None else None
        if dept is None:
            continue
        expected_parent = _y360_parent_id_for(
            node, roots_ids, dept_by_id, full_map, org_id, configured_parent,
            y360_root_id)
        actual_parent = _to_int(dept.get('parentDepartmentId'))
        if (expected_parent is None or actual_parent is None
                or actual_parent == expected_parent
                or not y360_parent_exists(expected_parent, dept_by_id, org_id)):
            continue
        # не дублируем действие, уже запланированное в первом проходе
        if any(a.get('type') == 'move' and a.get('id') == str(dept_id)
               for a in actions):
            continue
        actions.append({'type': 'move', 'dn': node['dn'],
                        'name': node.get('name'), 'id': str(dept_id),
                        'from': actual_parent, 'to': expected_parent})
        stats['move'] += 1

    return {
        'actions': actions,
        'stats': stats,
        'ald_base_dn': ald_tree.get('base_dn'),
        'ald_stats': ald_tree.get('stats'),
        'y360_departments_total': len(y360_deps),
        'y360_root_department_id': y360_root_id,
        'org_id': org_id,
    }


async def execute_departments_sync(base_ou_dn: Optional[str] = None,
                                   dry_run: bool = False) -> Dict[str, Any]:
    """Синхронизировать структуру подразделений ALD Pro -> Яндекс 360.

    dry_run=True — только план без изменения каталога 360.
    Результат каждой операции попадает в 'results'; статус сохраняется в БД
    (для отображения в UI и диагностики фонового задачи).
    """
    started = datetime.now().isoformat(timespec='seconds')
    summary = {'created': 0, 'renamed': 0, 'moved': 0, 'bound': 0,
               'noop': 0, 'stale': 0, 'errors': 0}
    results: List[dict] = []
    error_text = None
    org_id = None
    base_dn = None

    try:
        settings = get_ald_sync_settings()
        org_id = _require_configured()
        write_token = yandex360.get_write_token()
        if not write_token:
            raise RuntimeError('Не задан OAuth-токен (нужны права '
                               'directory:read_departments и '
                               'directory:write_departments)')

        ald_tree, y360_deps = await asyncio.gather(
            fetch_ald_pro_tree(base_ou_dn), fetch_departments(org_id))
        base_dn = ald_tree.get('base_dn')

        mapping = dept_map_load()
        dept_by_id = {_to_int(d.get('id')): d for d in y360_deps
                      if _to_int(d.get('id')) is not None}
        mapped_ids = {int(v) for v in mapping.values() if str(v).isdigit()}
        name_index = _build_name_index(y360_deps)
        roots_ids = {n['dn'].lower() for n in (ald_tree.get('roots') or [])}
        configured_parent = str(settings.get('parent_department_id') or '').strip()
        # корневое подразделение организации Яндекс 360 (родитель для
        # корневых OU). В пустой структуре Я360 это «Все сотрудники»
        # (id=1, parentID=0); ни orgId, ни 0 родителем в
        # DepartmentService_Create не являются — сервер отвечает HTTP 400
        # про обязательность parentId.
        y360_root_id = find_y360_root_department(y360_deps, org_id)
        if configured_parent and _to_int(configured_parent) == _to_int(org_id):
            raise RuntimeError(
                'В настройках «Родительский департамент в Яндекс 360» указан '
                'id организации (%s). API Яндекс 360 не принимает организацию '
                'родителем подразделения; укажите id существующего '
                'подразделения (для корней структуры — «Все сотрудники», '
                'обычно id=1)' % configured_parent)
        if not configured_parent and y360_root_id is None:
            raise RuntimeError(
                'Не определён родительский элемент структуры Яндекс 360: '
                'не задан «Родительский департамент» в настройках и не '
                'удалось автоматически найти корневое подразделение '
                'организации («Все сотрудники»). Укажите id '
                'подразделения-родителя в настройках синхронизации.')

        nodes = [n for n in _ald_flatten_nodes(ald_tree)
                 if not n.get('_is_orphan_service')]
        # узлы, чей предок не был создан/привязан (ошибка выше по дереву) —
        # их потомков не обрабатываем, чтобы не плодить записи с неверным
        # родителем
        blocked_dns: set = set()

        # --- удаления из 360 вручную: чистим устаревший маппинг -------------
        live_dns = {n['dn'].lower() for n in nodes}
        for dn_l, dep_id in list(mapping.items()):
            if dn_l in live_dns:
                continue
            if _to_int(dep_id) not in dept_by_id:
                dept_map_delete(dn_l)
                mapping.pop(dn_l, None)
                results.append({'type': 'unmap', 'dn': dn_l, 'id': dep_id,
                                'success': True,
                                'detail': 'маппинг удалён: подразделения нет в 360'})

        # --- обход OU сверху вниз -------------------------------------------
        for node in nodes:
            dn = node['dn']
            key = dn.lower()
            name = (node['name'] or '').strip() or dn.split(',')[0]
            parent_dn_l = (node.get('parent_dn') or '').lower()
            if parent_dn_l and parent_dn_l in blocked_dns:
                summary['errors'] += 1
                blocked_dns.add(key)
                results.append({'type': 'error', 'dn': dn, 'name': name,
                                'success': False,
                                'detail': 'Пропущено: родитель не синхронизирован'})
                continue

            dept_id = _to_int(mapping.get(key))
            dept = dept_by_id.get(dept_id) if dept_id is not None else None

            if dept is not None:
                # существующее маппингом подразделение: сверяем ИМЯ; родителя
                # корректируем в отдельном проходе ниже (к тому моменту все
                # родительские подразделения уже созданы/привязаны)
                actual_name = (dept.get('name') or '').strip()
                if actual_name != name and not dry_run:
                    r = await yandex360.update_department(
                        org_id, dept_id, name=name,
                        token=write_token)
                    results.append({'type': 'rename', 'dn': dn, 'id': str(dept_id),
                                    'from': actual_name, 'to': name, **r})
                    if r.get('success'):
                        dept['name'] = name
                        summary['renamed'] += 1
                    else:
                        summary['errors'] += 1
                        continue
                elif actual_name != name:
                    results.append({'type': 'rename', 'dn': dn, 'id': str(dept_id),
                                    'from': actual_name, 'to': name,
                                    'success': True, 'planned': True})
                    summary['renamed'] += 1
                mapping[key] = str(dept_id)
                continue

            expected_parent = _y360_parent_id_for(
                node, roots_ids, dept_by_id, mapping, org_id, configured_parent,
                y360_root_id)
            if expected_parent is None:
                summary['errors'] += 1
                blocked_dns.add(key)
                results.append({'type': 'error', 'dn': dn, 'name': name,
                                'success': False,
                                'detail': 'Не определён parentDepartmentId'})
                continue
            if not y360_parent_exists(expected_parent, dept_by_id, org_id):
                summary['errors'] += 1
                blocked_dns.add(key)
                results.append({'type': 'error', 'dn': dn, 'name': name,
                                'success': False,
                                'detail': ('Родитель id=%s не найден среди '
                                           'подразделений Яндекс 360'
                                           % expected_parent)})
                continue

            # привязка существующего свободного подразделения (без дубля)
            free_id = name_index.get((expected_parent, name.lower()))
            if free_id is not None and free_id not in mapped_ids:
                if not dry_run:
                    dept_map_save(dn, free_id)
                mapping[key] = str(free_id)
                mapped_ids.add(free_id)
                summary['bound'] += 1
                results.append({'type': 'bind', 'dn': dn, 'name': name,
                                'id': str(free_id), 'success': True,
                                'planned': dry_run})
                continue

            # создание нового подразделения (DepartmentService_Create)
            if dry_run:
                summary['create'] += 1
                results.append({'type': 'create', 'dn': dn, 'name': name,
                                'parentDepartmentId': expected_parent,
                                'success': True, 'planned': True})
                continue
            r = await yandex360.create_department(
                org_id, name=name, parent_department_id=expected_parent,
                note=DEPT_NOTE_TEMPLATE.format(dn=dn), token=write_token)
            res = {'type': 'create', 'dn': dn, 'name': name,
                   'parentDepartmentId': expected_parent, **r}
            results.append(res)
            if r.get('success') and r.get('id'):
                new_id = _to_int(r['id'])
                dept_by_id[new_id] = {'id': new_id, 'name': name,
                                      'parentDepartmentId': expected_parent}
                mapped_ids.add(new_id)
                name_index[(expected_parent, name.lower())] = new_id
                dept_map_save(dn, new_id)
                mapping[key] = str(new_id)
                summary['created'] += 1
            else:
                summary['errors'] += 1
                blocked_dns.add(key)
                logger.error('Яндекс 360: не удалось создать подразделение '
                             '%r (родитель %s): %s', name, expected_parent,
                             r.get('detail'))
                # потомки этого узла не обрабатываются (см. blocked_dns) —
                # иначе они создались бы с неверным родителем

        # --- второй проход: перемещения существующих подразделений -----------
        # Выполняется ПОСЛЕ создания/привязки всех родительских подразделений:
        # к этому моменту mapping содержит реальные id, и ожидаемый родитель
        # вычисляется корректно (в первом проходе planned-иды детей были ещё
        # неизвестны).
        for node in nodes:
            dn = node['dn']
            key = dn.lower()
            if key in blocked_dns:
                continue
            dept_id = _to_int(mapping.get(key))
            dept = dept_by_id.get(dept_id) if dept_id is not None else None
            if dept is None:
                continue
            expected_parent = _y360_parent_id_for(
                node, roots_ids, dept_by_id, mapping, org_id, configured_parent,
                y360_root_id)
            actual_parent = _to_int(dept.get('parentDepartmentId'))
            if (expected_parent is None or actual_parent is None
                    or actual_parent == expected_parent
                    or not y360_parent_exists(expected_parent, dept_by_id,
                                             org_id)):
                continue
            if dry_run:
                results.append({'type': 'move', 'dn': dn, 'id': str(dept_id),
                                'from': actual_parent, 'to': expected_parent,
                                'success': True, 'planned': True})
                summary['moved'] += 1
                continue
            r = await yandex360.update_department(
                org_id, dept_id, parent_department_id=expected_parent,
                token=write_token)
            results.append({'type': 'move', 'dn': dn, 'name': node.get('name'),
                            'id': str(dept_id),
                            'from': actual_parent, 'to': expected_parent, **r})
            if r.get('success'):
                dept['parentDepartmentId'] = expected_parent
                summary['moved'] += 1
            else:
                summary['errors'] += 1
                logger.error('Яндекс 360: не удалось переместить подразделение '
                             'id=%s (новый родитель %s): %s', dept_id,
                             expected_parent, r.get('detail'))

        # --- устаревшие (OU удалён из ALD Pro, подразделение живёт в 360) ----
        for dn_l, dep_id in list(mapping.items()):
            if dn_l in live_dns:
                continue
            dept = dept_by_id.get(_to_int(dep_id))
            if dept is not None:
                summary['stale'] += 1
                results.append({'type': 'stale', 'dn': dn_l, 'id': dep_id,
                                'name': dept.get('name'), 'success': True,
                                'detail': 'OU отсутствует в ALD Pro — '
                                          'подразделение оставлено в 360'})

        report_lines = render_dept_sync_report(results, summary, base_dn,
                                               dry_run)
    except Exception as e:
        logger.exception('Ошибка синхронизации подразделений ALD Pro -> Яндекс 360')
        error_text = str(e)
        report_lines = f'Ошибка: {e}'

    status = {
        'success': error_text is None,
        'started': started,
        'finished': datetime.now().isoformat(timespec='seconds'),
        'dry_run': bool(dry_run),
        'org_id': org_id,
        'base_dn': base_dn,
        'summary': summary,
        'error': error_text,
    }
    try:
        set_module_settings(DEPT_SYNC_STATUS_KEY, status)
    except Exception as e:
        logger.warning('Не удалось сохранить статус синхронизации: %s', e)

    return {'success': error_text is None, 'dry_run': bool(dry_run),
            'summary': summary, 'results': results,
            'text': report_lines, 'error': error_text, 'status': status}


def render_dept_sync_report(results: List[dict], summary: Dict[str, int],
                            base_dn: str, dry_run: bool) -> str:
    """Человекочитаемый отчёт синхронизации структуры подразделений."""
    lines = [f"Синхронизация структуры подразделений (базовый OU: {base_dn or '—'})"
             + (' — ПРЕДПРОСМОТР, без изменений' if dry_run else '')]
    lines.append('Итог: создано=%(created)d, переименовано=%(renamed)d, '
                 'перемещено=%(moved)d, привязано=%(bound)d, '
                 'устаревших=%(stale)d, ошибок=%(errors)d' % summary)
    interesting = [r for r in results
                   if r.get('type') in ('create', 'rename', 'move', 'bind',
                                         'stale', 'error', 'unmap')
                   or r.get('success') is False]
    if not interesting:
        lines.append('Изменений нет — структура Яндекс 360 соответствует ALD Pro.')
    for r in interesting:
        mark = '✓' if r.get('success') else '✗'
        t = r.get('type')
        if t == 'create':
            lines.append(f"{mark} СОЗДАНО: {r.get('name')} "
                         f"(родитель id={r.get('parentDepartmentId')}, OU={r.get('dn')})"
                         + ('' if r.get('planned')
                            else f" -> id={r.get('id')}" if r.get('id') else
                            f" ОШИБКА: {(r.get('detail') or '')[:200]}"))
        elif t == 'rename':
            lines.append(f"{mark} ПЕРЕИМЕНОВАНО: id={r.get('id')}: "
                         f"«{r.get('from')}» -> «{r.get('to')}»")
        elif t == 'move':
            lines.append(f"{mark} ПЕРЕМЕЩЕНО: id={r.get('id')}: "
                         f"родитель {r.get('from')} -> {r.get('to')}")
        elif t == 'bind':
            lines.append(f"{mark} ПРИВЯЗАНО: существующее подразделение "
                         f"id={r.get('id')} -> OU {r.get('dn')}")
        elif t == 'stale':
            lines.append(f"! БЕЗ ПАРЫ В ALD PRO: id={r.get('id')} "
                         f"«{r.get('name')}» (OU {r.get('dn')} удалён) — "
                         f"в Яндекс 360 сохранено")
        elif t == 'unmap':
            lines.append(f"{mark} МАППИНГ УДАЛЁН: id={r.get('id')} "
                         f"(подразделения нет в Яндекс 360)")
        else:
            lines.append(f"✗ ОШИБКА: {r.get('dn')}: "
                         f"{(r.get('detail') or '')[:200]}")
    return '\n'.join(lines)


def get_dept_sync_last_status() -> Dict[str, Any]:
    status = get_module_settings(DEPT_SYNC_STATUS_KEY)
    return status if isinstance(status, dict) else {}


# ---------------------------------------------------------------------------
# ВЫГРУЗКА структуры подразделений Яндекс 360 (с указанием parentID)
#
# Источник — только официальный метод DepartmentService_List:
#   GET https://api360.yandex.net/directory/v1/org/{orgId}/departments
#       ?limit=N&page_index=M
# Каждый элемент ответа содержит id, name и parentDepartmentId — именно эти
# поля выводятся в файле выгрузки.
# ---------------------------------------------------------------------------

def _y360_parent_id_for_export(dep: dict, org_id) -> int:
    """Нормализовать parentDepartmentId для выгрузки.

    В API 360 родителем подразделений верхнего уровня является сама
    организация (parentDepartmentId == orgId). В выгрузке для корневых
    подразделений parentID = 0, как и в визуальном дереве модуля.
    """
    pid = _to_int(dep.get('parentDepartmentId'))
    oid = _to_int(org_id)
    if pid is None or pid == 0 or (oid is not None and pid == oid):
        return 0
    return pid


def build_y360_departments_flat(departments: List[dict], org_id) -> List[dict]:
    """Плоский список подразделений Я360: id, name, parentID, depth, path."""
    nodes_by_id: Dict[int, dict] = {}
    order: List[int] = []
    for d in departments:
        did = _to_int(d.get('id'))
        if did is None:
            continue
        nodes_by_id[did] = {
            'id': did,
            'name': (d.get('name') or '(без названия)').strip(),
            'parentID': _y360_parent_id_for_export(d, org_id),
        }
        order.append(did)

    # глубина и путь: идём от корней (parentID == 0 или родитель вне выборки)
    def _resolve(node: dict, guard: set) -> Tuple[int, str]:
        depth, path = 1, node['name']
        pid = node['parentID']
        while pid and pid in nodes_by_id and pid not in guard:
            guard.add(pid)
            parent = nodes_by_id[pid]
            path = f"{parent['name']}\\{path}"
            depth += 1
            pid = parent['parentID']
        return depth, path

    result: List[dict] = []
    for did in order:
        node = nodes_by_id[did]
        depth, path = _resolve(node, {did})
        result.append({**node, 'depth': depth, 'path': path,
                       'childrenCount': sum(1 for n in nodes_by_id.values()
                                             if n['parentID'] == did)})
    return result


def render_y360_departments_csv(rows: List[dict], org_id) -> str:
    """CSV-выгрузка: id, name, parentID, depth, path, childrenCount."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    writer.writerow(['id', 'name', 'parentID', 'depth', 'path',
                     'childrenCount'])
    for r in rows:
        writer.writerow([r['id'], r['name'], r['parentID'], r['depth'],
                         r['path'], r['childrenCount']])
    return buf.getvalue()


def render_y360_departments_text(rows: List[dict], org_id,
                                 tree: Dict[str, Any]) -> str:
    """Текстовое представление выгрузки с явным указанием parentID."""
    stats = tree.get('stats', {})
    lines = [
        'Выгрузка структуры подразделений Яндекс 360 '
        '(DepartmentService_List)',
        f'orgId={org_id} · дата выгрузки='
        f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · '
        f'подразделений={len(rows)} (корневых='
        f'{stats.get("root_departments", 0)}, сотрудников='
        f'{stats.get("users_total", 0)}, без подразделения='
        f'{stats.get("users_without_department", 0)})',
        'Формат: id=<ID подразделения>, parentID=<ID родителя; 0 — '
        'родитель = организация>',
        '-' * 72,
    ]
    for r in sorted(rows, key=lambda x: (x['path'].lower())):
        indent = '    ' * (r['depth'] - 1)
        lines.append(f"{indent}id={r['id']}, parentID={r['parentID']}, "
                     f"name=\"{r['name']}\" "
                     f"(дочерних: {r['childrenCount']})")
    return '\n'.join(lines)


async def export_y360_departments(fmt: str = 'json') -> Dict[str, Any]:
    """Выгрузить структуру подразделений Яндекс 360 с parentID.

    Args:
        fmt: 'json' | 'csv' | 'text' — формат содержимого файла выгрузки.

    Returns:
        {'success': bool, 'org_id', 'count', 'rows', 'tree', 'content',
         'filename', 'format'} либо {'success': False, 'error': ...}.
    """
    org_id = _require_configured()
    deps = await fetch_departments(org_id)
    users: List[dict] = []
    try:
        users = await fetch_users(org_id)
    except Exception as e:  # сотрудники не критичны для выгрузки структуры
        logger.warning('Яндекс 360: не удалось получить пользователей '
                       'для выгрузки: %s', e)
    tree = build_department_tree(deps, users, org_id)
    rows = build_y360_departments_flat(deps, org_id)

    fmt = (fmt or 'json').strip().lower()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if fmt == 'csv':
        content = render_y360_departments_csv(rows, org_id)
        filename = f'y360_departments_{org_id}_{stamp}.csv'
        content_type = 'text/csv; charset=utf-8'
    elif fmt == 'text':
        content = render_y360_departments_text(rows, org_id, tree)
        filename = f'y360_departments_{org_id}_{stamp}.txt'
        content_type = 'text/plain; charset=utf-8'
    else:
        fmt = 'json'
        payload = {
            'source': 'Yandex 360 Directory API — DepartmentService_List',
            'org_id': _to_int(org_id),
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'note': ('parentID = 0 означает, что родитель подразделения — '
                     'сама организация (верхний уровень)'),
            'stats': tree.get('stats', {}),
            'departments': rows,
            'tree': tree_to_json(tree),
        }
        import json as _json
        content = _json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f'y360_departments_{org_id}_{stamp}.json'
        content_type = 'application/json; charset=utf-8'

    return {
        'success': True,
        'org_id': org_id,
        'count': len(rows),
        'rows': rows,
        'tree': tree_to_json(tree),
        'stats': tree.get('stats', {}),
        'format': fmt,
        'filename': filename,
        'content_type': content_type,
        'content': content,
    }


def dept_sync_is_running() -> bool:
    return bool(_dept_sync_lock.locked())


# Лок защиты от параллельных запусков синхронизации (фон + ручная кнопка).
_dept_sync_lock = threading.Lock()


async def run_departments_sync_once(base_ou_dn: Optional[str] = None) -> Dict[str, Any]:
    """Один полный цикл синхронизации структуры с защитой от параллельных запусков."""
    if _dept_sync_lock.locked():
        return {'success': False,
                'error': 'Синхронизация уже выполняется — повторный запуск '
                        'отменён',
                'busy': True}
    with _dept_sync_lock:
        return await execute_departments_sync(base_ou_dn)


async def run_departments_sync_loop(stop_event: asyncio.Event) -> None:
    """Фоновая задача: периодически синхронизировать структуру подразделений.

    Интервал — настройка sync_interval_minutes (минимум 1 мин). Завершается
    по внешнему stop_event (остановка приложения FastAPI).
    """
    logger.info('[y360-dept-sync] Фоновая задача синхронизации запущена')
    while not stop_event.is_set():
        interval = 60
        try:
            interval = max(1, int(get_ald_sync_settings()
                                  .get('sync_interval_minutes') or 60))
            settings_ready = (str(get_settings().get('org_id') or '').strip()
                              and str(get_settings().get('oauth_token') or '').strip()
                              and str(get_ald_sync_settings()
                                      .get('root_ou_dn') or '').strip())
            if not settings_ready:
                logger.debug('[y360-dept-sync] Настройки интеграции неполные — '
                             'пропуск цикла')
            else:
                res = await run_departments_sync_once()
                s = res.get('summary') or {}
                logger.info('[y360-dept-sync] Цикл завершён: success=%s %s',
                            res.get('success'), s)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error('[y360-dept-sync] Ошибка фонового цикла: %s', e)
        try:
            await asyncio.wait_for(stop_event.wait,
                                   timeout=interval * 60)
            break  # stop_event установлен — выходим
        except asyncio.TimeoutError:
            continue
    logger.info('[y360-dept-sync] Фоновая задача остановлена')
