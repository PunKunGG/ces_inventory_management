from datetime import date, datetime, time, timedelta, timezone

from . import THAI_TIME


LOCATION_TREE = '''
WITH RECURSIVE location_tree(id, path, id_path) AS (
    SELECT id, name, printf('/%d/', id) FROM locations WHERE parent_id IS NULL
    UNION ALL
    SELECT child.id, tree.path || ' › ' || child.name, tree.id_path || child.id || '/'
    FROM locations child JOIN location_tree tree ON tree.id=child.parent_id
)
'''

ITEM_SELECT = LOCATION_TREE + '''
, unit_totals AS (
    SELECT item_id, COUNT(*) AS total,
           SUM(status='available') AS available, SUM(status='loaned') AS loaned,
           SUM(status='repair') AS repair, SUM(status='damaged') AS damaged
    FROM tool_units WHERE active=1 GROUP BY item_id
), inventory_view AS (
    SELECT i.*, c.name AS category_name, tree.path AS location_path, tree.id_path,
        COALESCE(t.total, 0) AS tool_total,
        COALESCE(t.loaned, 0) AS loaned_count, COALESCE(t.repair, 0) AS repair_count,
        COALESCE(t.damaged, 0) AS damaged_count,
        CASE WHEN i.kind='tool' THEN COALESCE(t.available, 0) ELSE i.quantity END AS available_quantity,
        CASE WHEN i.kind='part' AND i.quantity=0 THEN 'out'
             WHEN i.kind='part' AND i.quantity<=i.min_quantity THEN 'low' ELSE 'ok' END AS stock_state
    FROM items i JOIN categories c ON c.id=i.category_id
    JOIN location_tree tree ON tree.id=i.location_id LEFT JOIN unit_totals t ON t.item_id=i.id
) SELECT v.* FROM inventory_view v WHERE 1=1
'''

MOVEMENT_SELECT = '''
SELECT m.*, i.code AS item_code, i.name AS item_name, i.kind AS item_kind,
       i.unit, c.name AS category_name, a.name AS actor_name,
       u.name AS requester_name, t.code AS tool_code
FROM stock_movements m JOIN items i ON i.id=m.item_id
JOIN categories c ON c.id=i.category_id JOIN users a ON a.id=m.actor_id
LEFT JOIN users u ON u.id=m.requester_id LEFT JOIN tool_units t ON t.id=m.tool_unit_id
WHERE 1=1
'''

UNIT_SELECT = '''
SELECT t.*, i.name AS item_name, i.code AS item_code, l.id AS loan_id,
       l.due_at, l.loaned_at, b.name AS borrower_name, r.name AS responsible_name
FROM tool_units t JOIN items i ON i.id=t.item_id
LEFT JOIN item_loans l ON l.tool_unit_id=t.id AND l.returned_at IS NULL
LEFT JOIN users b ON b.id=l.borrower_id LEFT JOIN users r ON r.id=l.responsible_id
WHERE 1=1
'''


def locations(connection, active_only=False):
    return connection.execute(LOCATION_TREE + '''
        SELECT l.*, tree.path, tree.id_path FROM locations l
        JOIN location_tree tree ON tree.id=l.id WHERE (?=0 OR l.active=1) ORDER BY tree.path
    ''', (active_only,)).fetchall()


def inventory(connection, filters=None, *, item_id=None, include_inactive=False):
    filters = filters or {}
    sql = ITEM_SELECT
    params = []
    if not include_inactive:
        sql += ' AND v.active=1'
    if item_id is not None or filters.get('item_id'):
        sql += ' AND v.id=?'
        params.append(item_id or filters['item_id'])
    for field in ('kind', 'category_id'):
        if filters.get(field):
            sql += f' AND v.{field}=?'
            params.append(filters[field])
    if filters.get('location_id'):
        sql += " AND instr(v.id_path, '/' || ? || '/')>0"
        params.append(filters['location_id'])
    if filters.get('q'):
        sql += ''' AND (instr(lower(v.code || ' ' || v.name || ' ' || v.brand || ' ' || v.model
                       || ' ' || v.category_name || ' ' || v.location_path), lower(?))>0
                       OR EXISTS (SELECT 1 FROM tool_units t WHERE t.item_id=v.id AND t.active=1
                       AND instr(lower(t.code || ' ' || COALESCE(t.serial, '')), lower(?))>0))'''
        params.extend([filters['q']] * 2)
    status = filters.get('status')
    if status in ('ok', 'low', 'out'):
        sql += ' AND v.kind=\'part\' AND v.stock_state=?'
        params.append(status)
    if status in ('available', 'loaned', 'repair', 'damaged'):
        column = 'available_quantity' if status == 'available' else f'{status}_count'
        sql += f" AND v.kind='tool' AND v.{column}>0"
    return connection.execute(sql + ' ORDER BY v.code', params).fetchall()


def date_filter(filters):
    start = date.fromisoformat(filters['start']) if filters.get('start') else None
    end = date.fromisoformat(filters['end']) if filters.get('end') else None
    if start and end and start > end:
        raise ValueError('วันเริ่มต้นต้องไม่เกินวันสิ้นสุด')

    def utc_day(day):
        return datetime.combine(day, time.min, THAI_TIME).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    return (utc_day(start) if start else None, utc_day(end + timedelta(days=1)) if end else None)


def movements(connection, filters=None, *, item_id=None, limit=None):
    filters = filters or {}
    sql = MOVEMENT_SELECT
    params = []
    for value, column in ((item_id or filters.get('item_id'), 'm.item_id'), (filters.get('actor_id'), 'm.actor_id'),
                          (filters.get('category_id'), 'i.category_id'), (filters.get('action'), 'm.action')):
        if value:
            sql += f' AND {column}=?'
            params.append(value)
    if filters.get('q'):
        sql += ''' AND instr(lower(i.code || ' ' || i.name || ' ' || a.name || ' ' ||
                    COALESCE(u.name, '') || ' ' || COALESCE(t.code, '') || ' ' ||
                    COALESCE(m.reason, '') || ' ' || COALESCE(m.work_order, '')), lower(?))>0'''
        params.append(filters['q'])
    start, end = date_filter(filters)
    if start:
        sql += ' AND m.created_at>=?'
        params.append(start)
    if end:
        sql += ' AND m.created_at<?'
        params.append(end)
    sql += ' ORDER BY m.created_at DESC, m.id DESC'
    if limit is not None:
        sql += ' LIMIT ?'
        params.append(limit)
    return connection.execute(sql, params).fetchall()


def units(connection, *, item_id=None, statuses=(), active_only=False):
    sql = UNIT_SELECT
    params = []
    if item_id is not None:
        sql += ' AND t.item_id=?'
        params.append(item_id)
    if active_only:
        sql += ' AND t.active=1 AND i.active=1'
    if statuses:
        sql += f" AND t.status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
    return connection.execute(sql + ' ORDER BY t.code', params).fetchall()
