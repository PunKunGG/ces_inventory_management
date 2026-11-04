from contextlib import contextmanager
import csv
import io
import re
import secrets
import sqlite3

from flask import Response, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from . import LABELS, format_time, require_roles
from . import db, queries


ADMIN = 'admin'
TECH = 'technician'
SUPERVISOR = 'supervisor'
OPERATIONS = {'stock_in': TECH, 'stock_out': TECH, 'loan': TECH, 'return': TECH, 'adjust': ADMIN, 'status': ADMIN}


def string(data, key, limit=150, required=False):
    value = data.get(key, '').strip()
    if len(value) > limit or (required and not value):
        raise ValueError('กรุณาตรวจสอบช่องที่จำเป็นและความยาวของข้อมูล')
    return value


def number(data, key, *, optional=False, minimum=1):
    value = data.get(key, '')
    if optional and not value:
        return None
    if not re.fullmatch(r'[0-9]{1,9}', value) or int(value) < minimum:
        raise ValueError('กรุณาระบุจำนวนหรือเลือกรายการให้ถูกต้อง')
    return int(value)


def record(connection, table, entry_id):
    if not 1 <= entry_id <= db.MAX_SQL_INTEGER:
        return None
    return connection.execute(f'SELECT * FROM {table} WHERE id=?', (entry_id,)).fetchone()


def categories(connection, active_only=True):
    return connection.execute('SELECT * FROM categories WHERE (?=0 OR active=1) ORDER BY name', (active_only,)).fetchall()


def users(connection, active_only=True):
    return connection.execute(
        'SELECT id, username, name, role, active FROM users WHERE (?=0 OR active=1) ORDER BY name', (active_only,)
    ).fetchall()


@contextmanager
def admin_write(connection):
    try:
        connection.execute('BEGIN IMMEDIATE')
        actor = record(connection, 'users', g.user['id'])
        if not actor or not actor['active'] or actor['role'] != ADMIN or actor['session_version'] != g.user['session_version']:
            abort(403)
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def form_error(error):
    if isinstance(error, sqlite3.IntegrityError):
        return 'รหัสหรือชื่อซ้ำ หรือข้อมูลที่เลือกไม่สามารถใช้งานได้'
    return str(error)


def filters(*keys):
    return {key: request.args.get(key, '').strip()[:150] for key in keys}


def save_item(connection, item, form):
    kind = form.get('kind')
    if kind not in ('part', 'tool') or (item and kind != item['kind']):
        raise ValueError('เลือกประเภทให้ถูกต้อง ประเภทของรายการเดิมแก้ไขไม่ได้')
    data = {
        'code': string(form, 'code', 48, True).upper(), 'name': string(form, 'name', 150, True),
        'kind': kind, 'category_id': number(form, 'category_id'), 'location_id': number(form, 'location_id'),
        'unit': string(form, 'unit', 32, True), 'min_quantity': number(form, 'min_quantity', minimum=0) if kind == 'part' else 0,
        'brand': string(form, 'brand', 100), 'model': string(form, 'model', 100),
        'description': string(form, 'description', 1000), 'active': int(form.get('active') == '1'),
    }
    for field, table in (('category_id', 'categories'), ('location_id', 'locations')):
        target = record(connection, table, data[field])
        if not target or (not target['active'] and (data['active'] or not item or target['id'] != item[field])):
            raise ValueError('กรุณาเลือกหมวดหมู่และตำแหน่งที่เปิดใช้งาน')
    if item and not data['active']:
        open_loan = connection.execute('''SELECT 1 FROM item_loans l JOIN tool_units t ON t.id=l.tool_unit_id
            WHERE t.item_id=? AND l.returned_at IS NULL''', (item['id'],)).fetchone()
        if item['quantity'] or open_loan:
            raise ValueError('ปิดใช้งานได้เมื่อรายการไม่มียอดอะไหล่หรือใบยืมที่ค้างอยู่')
    return save_row(connection, 'items', item['id'] if item else None, data)


def save_row(connection, table, entry_id, data):
    values = tuple(data.values())
    if entry_id:
        columns = ', '.join(f'{key}=?' for key in data)
        connection.execute(f'UPDATE {table} SET {columns} WHERE id=?', (*values, entry_id))
        return entry_id
    columns = ', '.join(data)
    placeholders = ', '.join('?' for _ in data)
    return connection.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', values).lastrowid


def save_setting(connection, section, entry, form):
    entry_id = entry['id'] if entry else None
    data = {'name': string(form, 'name', 120, True), 'active': int(form.get('active') == '1')}
    if section == 'users':
        username = string(form, 'username', 64, True).lower()
        role = form.get('role')
        password = form.get('password', '')
        if not re.fullmatch(r'[a-z0-9_.-]+', username) or role not in (ADMIN, TECH, SUPERVISOR):
            raise ValueError('ชื่อบัญชีใช้ตัวอักษรอังกฤษ ตัวเลข จุด ขีด หรือขีดล่าง และต้องเลือกบทบาท')
        if (password or not entry) and not 12 <= len(password) <= 256:
            raise ValueError('รหัสผ่านต้องมี 12–256 ตัวอักษร')
        if entry_id == g.user['id'] and (not data['active'] or role != ADMIN):
            raise ValueError('เปลี่ยนบทบาทหรือปิดบัญชีที่กำลังใช้งานไม่ได้')
        data.update(username=username, role=role)
        if password:
            data['password_hash'] = generate_password_hash(password)
        if entry and (password or role != entry['role'] or data['active'] != entry['active']):
            data['session_version'] = entry['session_version'] + 1
            connection.execute('DELETE FROM user_sessions WHERE user_id=?', (entry_id,))
    if section == 'locations':
        parent_id = number(form, 'parent_id', optional=True)
        if parent_id:
            parent = next((row for row in queries.locations(connection) if row['id'] == parent_id), None)
            if not parent or (data['active'] and not parent['active']) or (entry_id and f'/{entry_id}/' in parent['id_path']):
                raise ValueError('ตำแหน่งแม่ไม่ถูกต้องหรือทำให้ผังวนกลับมาที่ตัวเอง')
        data['parent_id'] = parent_id
        if entry_id and not data['active'] and connection.execute(
            'SELECT 1 FROM locations WHERE parent_id=? AND active=1', (entry_id,)
        ).fetchone():
            raise ValueError('ยังมีตำแหน่งย่อยที่เปิดใช้งานอยู่')
    if section in ('categories', 'locations') and entry_id and not data['active']:
        column = 'category_id' if section == 'categories' else 'location_id'
        if connection.execute(f'SELECT 1 FROM items WHERE {column}=? AND active=1', (entry_id,)).fetchone():
            raise ValueError('ยังมีรายการคลังที่ใช้งานข้อมูลนี้อยู่')
    return save_row(connection, section, entry_id, data)


def report_data(connection):
    args = filters('kind', 'item_id', 'category_id', 'start', 'end')
    kind = args.pop('kind') or 'balance'
    if kind in ('balance', 'low'):
        if kind == 'low':
            return kind, [row for row in queries.inventory(connection, args) if row['stock_state'] in ('low', 'out')]
        return kind, queries.inventory(connection, args)
    if kind in ('in', 'out'):
        args['action'] = 'stock_in' if kind == 'in' else 'stock_out'
        return kind, queries.movements(connection, args)
    raise ValueError('ประเภทรายงานไม่ถูกต้อง')


def csv_value(value):
    if isinstance(value, str) and value.lstrip().startswith(tuple('=+-@＝＋－＠')):
        return '\t' + value
    if isinstance(value, str) and value.startswith(('\t', '\r', '\n')):
        return value.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ').lstrip()
    return value


def export_report(kind, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    if kind in ('balance', 'low'):
        writer.writerow(('รหัส', 'ชื่อรายการ', 'ประเภท', 'หมวดหมู่', 'ตำแหน่ง', 'ยอดพร้อมใช้', 'หน่วย', 'ขั้นต่ำ', 'สถานะ'))
        for row in rows:
            writer.writerow(map(csv_value, (row['code'], row['name'], LABELS[row['kind']], row['category_name'],
                row['location_path'], row['available_quantity'], row['unit'], row['min_quantity'],
                LABELS[row['stock_state']] if row['kind'] == 'part' else '')))
    else:
        writer.writerow(('เลขอ้างอิง', 'วันที่', 'รหัส', 'ชื่อรายการ', 'หมวดหมู่', 'จำนวน', 'หน่วย',
                         'ผู้บันทึก', 'ผู้เบิก', 'แหล่งที่มา', 'เหตุผล', 'ใบงาน', 'หมายเหตุ'))
        for row in rows:
            writer.writerow(map(csv_value, (row['id'], format_time(row['created_at']), row['item_code'],
                row['item_name'], row['category_name'], abs(row['delta']), row['unit'], row['actor_name'],
                row['requester_name'], row['source'], row['reason'], row['work_order'], row['note'])))
    return Response('\ufeff' + output.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="ces-{kind}.csv"'})


def register(app):
    @app.get('/dashboard')
    @require_roles()
    def dashboard():
        connection = db.get_db()
        items = queries.inventory(connection)
        low_items = sorted((row for row in items if row['stock_state'] in ('low', 'out')),
                           key=lambda row: (row['available_quantity'], row['code']))
        stats = {
            'tools': sum(row['kind'] == 'tool' for row in items),
            'parts': sum(row['kind'] == 'part' for row in items),
            'available_tools': sum(row['available_quantity'] for row in items if row['kind'] == 'tool'),
            'low': sum(row['stock_state'] == 'low' for row in low_items),
            'out': sum(row['stock_state'] == 'out' for row in low_items),
        }
        scope = {'actor_id': g.user['id']} if g.user['role'] == TECH else {}
        return render_template('dashboard.html', stats=stats, low_items=low_items[:8],
                               recent=queries.movements(connection, scope, limit=8))

    @app.get('/inventory')
    @require_roles()
    def inventory():
        connection = db.get_db()
        args = filters('q', 'kind', 'status', 'category_id', 'location_id')
        if args['status'] == 'inactive' and g.user['role'] != ADMIN:
            abort(403)
        inactive = args['status'] == 'inactive'
        items = queries.inventory(connection, args, include_inactive=inactive)
        if inactive:
            items = [row for row in items if not row['active']]
        return render_template('inventory.html', items=items, categories=categories(connection, not inactive),
                               locations=queries.locations(connection, not inactive))

    @app.get('/items/<int:item_id>')
    @require_roles()
    def item_detail(item_id):
        if not 1 <= item_id <= db.MAX_SQL_INTEGER:
            abort(404)
        connection = db.get_db()
        items = queries.inventory(connection, item_id=item_id, include_inactive=g.user['role'] == ADMIN)
        if not items:
            abort(404)
        scope = {'actor_id': g.user['id']} if g.user['role'] == TECH else {}
        return render_template('item_detail.html', item=items[0], units=queries.units(connection, item_id=item_id),
                               movements=queries.movements(connection, scope, item_id=item_id, limit=30))

    @app.route('/items/new', methods=['GET', 'POST'])
    @app.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
    @require_roles(ADMIN)
    def item_form(item_id=None):
        connection = db.get_db()
        item = record(connection, 'items', item_id) if item_id else None
        if item_id and item is None:
            abort(404)
        original = item
        status = 200
        if request.method == 'POST':
            try:
                with admin_write(connection):
                    item = record(connection, 'items', item_id) if item_id else None
                    saved_id = save_item(connection, item, request.form)
                flash('บันทึกทะเบียนเรียบร้อย', 'success')
                return redirect(url_for('item_detail', item_id=saved_id), code=303)
            except (ValueError, sqlite3.IntegrityError) as error:
                flash(form_error(error), 'danger')
                item = dict(request.form, id=item_id, active=request.form.get('active') == '1')
                status = 400
        return render_template('item_form.html', item=item, is_edit=bool(item_id),
            categories=[row for row in categories(connection, False) if row['active'] or original and row['id'] == original['category_id']],
            locations=[row for row in queries.locations(connection) if row['active'] or original and row['id'] == original['location_id']]), status

    @app.route('/settings/<section>', methods=['GET', 'POST'])
    @require_roles(ADMIN)
    def settings(section):
        connection = db.get_db()
        if section not in ('categories', 'locations', 'users'):
            abort(404)
        entry_id = request.args.get('edit', type=int)
        entry = record(connection, section, entry_id) if entry_id else None
        if request.method == 'GET' and 'edit' in request.args and not entry:
            abort(404)
        status = 200
        if request.method == 'POST':
            try:
                entry_id = number(request.form, 'id', optional=True)
                with admin_write(connection):
                    entry = record(connection, section, entry_id) if entry_id else None
                    if entry_id and not entry:
                        raise ValueError('ไม่พบรายการที่ต้องการแก้ไข')
                    save_setting(connection, section, entry, request.form)
                flash('บันทึกการตั้งค่าเรียบร้อย', 'success')
                return redirect(url_for('settings', section=section), code=303)
            except (ValueError, sqlite3.IntegrityError) as error:
                flash(form_error(error), 'danger')
                entry = {key: value for key, value in request.form.items() if key != 'password'}
                entry.update(id=entry_id, active=request.form.get('active') == '1')
                status = 400
        location_rows = queries.locations(connection)
        if section == 'locations':
            rows = location_rows
        elif section == 'categories':
            rows = categories(connection, False)
        else:
            rows = users(connection, False)
        return render_template('settings.html', section=section, rows=rows, entry=entry,
            locations=[row for row in location_rows if row['active'] and (not entry_id or f'/{entry_id}/' not in row['id_path'])]), status

    @app.route('/operations/<action>', methods=['GET', 'POST'])
    @require_roles()
    def operations(action):
        role = OPERATIONS.get(action)
        if role is None:
            abort(404)
        if role != g.user['role']:
            abort(403)
        connection = db.get_db()
        status = 200
        request_key = secrets.token_urlsafe(24)
        values = filters('item_id', 'tool_unit_id')
        if request.method == 'POST':
            values = request.form
            request_key = request.form.get('request_key', '')
            try:
                movement_id = db.record_movement(connection, g.user['id'], action, request.form, request_key,
                                                 session_version=g.user['session_version'])
                flash(f'บันทึกสำเร็จ · เลขอ้างอิง #{movement_id}', 'success')
                movement = record(connection, 'stock_movements', movement_id)
                return redirect(url_for('item_detail', item_id=movement['item_id']), code=303)
            except db.MovementError as error:
                flash(str(error), 'danger')
                status = error.status
        item_filters = {'kind': 'part'} if action in ('stock_out', 'adjust') else None
        statuses = {'loan': ('available',), 'return': ('loaned',), 'status': ('available', 'repair', 'damaged')}
        choices = queries.units(connection, statuses=statuses[action], active_only=True) if action in statuses else []
        if action == 'return' and status == 409 and not any(
            str(unit['id']) == values.get('tool_unit_id') and str(unit['loan_id']) == values.get('loan_id')
            for unit in choices
        ):
            values = {'note': values.get('note', '')}
        return render_template('operations.html', action=action, request_key=request_key, values=values,
                               items=queries.inventory(connection, item_filters), units=choices,
                               users=users(connection)), status

    @app.get('/history')
    @require_roles(ADMIN, SUPERVISOR)
    def history():
        connection = db.get_db()
        try:
            rows = queries.movements(connection, filters('q', 'action', 'item_id', 'start', 'end'))
        except (ValueError, OverflowError):
            abort(400, 'ช่วงวันที่ไม่ถูกต้อง')
        return render_template('history.html', movements=rows, items=queries.inventory(connection, include_inactive=True))

    @app.get('/reports')
    @require_roles(ADMIN, SUPERVISOR)
    def reports():
        connection = db.get_db()
        try:
            kind, rows = report_data(connection)
        except (ValueError, OverflowError):
            abort(400, 'ประเภทรายงานหรือช่วงวันที่ไม่ถูกต้อง')
        return render_template('reports.html', kind=kind, rows=rows,
                               items=queries.inventory(connection, include_inactive=True), categories=categories(connection, False))

    @app.get('/reports/export.csv')
    @require_roles(ADMIN, SUPERVISOR)
    def report_csv():
        try:
            kind, rows = report_data(db.get_db())
        except (ValueError, OverflowError):
            abort(400, 'ประเภทรายงานหรือช่วงวันที่ไม่ถูกต้อง')
        return export_report(kind, rows)
