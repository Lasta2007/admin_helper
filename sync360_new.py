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
  * run_full_sync(trigger)               — заглушка до ЭТАПА 2
  * get_last_sync_result()               — статус последней операции
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

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

async def get_y360_tree() -> Dict[str, Any]:
    """Получить подразделения и пользователей Я360 и построить дерево.

    Возвращает {'success', 'json', 'text', 'stats'}; при ошибке —
    {'success': False, 'error': ...}. Результат сохраняется в статус.
    """
    started = datetime.now().isoformat(timespec='seconds')
    try:
        org_id = _require_configured()
        departments, users = await asyncio.gather(
            fetch_departments(org_id),
            fetch_users(org_id))
        tree = build_department_tree(departments, users, org_id)
        payload = tree_to_json(tree)
        text = render_tree_text(tree)
        status = {
            'success': True,
            'started': started,
            'finished': datetime.now().isoformat(timespec='seconds'),
            'stats': tree['stats'],
            'error': None,
        }
        result = {'success': True, 'json': payload, 'text': text,
                  'stats': tree['stats']}
    except Exception as e:
        logger.exception('Ошибка построения дерева Яндекс 360')
        status = {
            'success': False,
            'started': started,
            'finished': datetime.now().isoformat(timespec='seconds'),
            'stats': None,
            'error': str(e),
        }
        result = {'success': False, 'error': str(e)}
    try:
        set_module_settings(STATUS_KEY, status)
    except Exception as e:  # сохранение статуса не должно ломать операцию
        logger.warning('Не удалось сохранить статус построения дерева: %s', e)
    return result


def get_last_status() -> Dict[str, Any]:
    status = get_module_settings(STATUS_KEY)
    return status if isinstance(status, dict) else {}


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


def get_ald_sync_settings() -> Dict[str, Any]:
    """Настройки выгрузки (базовый OU ALD Pro, домен почты и т.д.)."""
    import sync360 as _legacy
    try:
        return dict(_legacy.get_sync_settings())
    except Exception:
        s = get_module_settings(ALD_SETTINGS_KEY)
        return s if isinstance(s, dict) else {}


def save_ald_sync_settings(root_ou_dn: str = None, email_domain: str = None,
                           parent_department_id: str = None,
                           sync_interval_minutes: int = None,
                           block_missing_users: bool = None) -> Dict[str, Any]:
    """Частичное обновление настроек выгрузки (сохраняются в общую БД)."""
    import sync360 as _legacy
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
    _legacy.save_sync_settings(new)
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
    domain = str(settings.get('email_domain') or '').strip().lstrip('@').lower()

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

    root = await walk(base_dn, '', '', 0)

    # --- пользователи каждого узла ------------------------------------------
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
            if not email:
                email = f"{login.lower()}@{domain}" if domain \
                    else f"{login.lower()}@mail.local"
                source = 'generated'
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
        'users': [],
    }

    stats = {
        'base_dn': base_dn,
        'departments': len(all_nodes),
        'root_departments': 1,
        'users_total': sum(len(n['users']) for n in all_nodes),
        'users_matched': sum(len(n['users']) for n in all_nodes),
        'users_without_department': len(orphan['users']),
        'emails_generated': sum(
            1 for n in all_nodes for u in n['users']
            if u['email_source'] == 'generated'),
    }
    return {
        'base_dn': base_dn,
        'roots': [root],
        'orphan_node': orphan,
        'all_nodes': all_nodes,
        'node_by_dn': nodes_by_dn,
        'stats': stats,
    }


def render_ald_tree_text(tree: Dict[str, Any]) -> str:
    """ASCII-представление дерева ALD Pro: Название (id=N, childID=M)."""
    lines: List[str] = []
    stats = tree.get('stats', {})
    lines.append('ALD Pro: дерево от базового OU "%s" '
                 '(подразделений=%d, пользователей=%d)'
                 % (stats.get('base_dn', ''), stats.get('departments', 0),
                    stats.get('users_total', 0)))
    lines.append('Формат: Название (id=ID узла в дереве, childID=ID родителя; '
                 'для корневого OU childID=0)')

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


# Совместимость с текущим api.py/main.py (до ЭТАПА 2 полная синхронизация
# выполняется старым модулем sync360.py; здесь — заглушки).

def get_last_sync_result() -> Dict[str, Any]:
    return get_last_status()


async def run_full_sync(trigger: str = 'manual') -> Dict[str, Any]:
    """Заглушка ЭТАПА 1: полная синхронизация будет реализована далее."""
    raise NotImplementedError(
        'Синхронизация с нуля: пока реализован только ЭТАП 1 '
        '(получение дерева подразделений и пользователей). '
        'Используйте get_y360_tree(); полные правила синхронизации '
        'будут добавлены на следующих этапах.')
