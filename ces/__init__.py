from datetime import datetime, timedelta, timezone
from functools import wraps
from hashlib import sha256
import hmac
import os
from pathlib import Path
import secrets
import time

from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db


THAI_TIME = timezone(timedelta(hours=7))
LABELS = {
    'admin': 'ผู้ดูแลระบบ', 'technician': 'เจ้าหน้าที่ช่าง', 'supervisor': 'หัวหน้างาน',
    'part': 'อะไหล่', 'tool': 'เครื่องมือ', 'stock_in': 'รับเข้าคลัง',
    'stock_out': 'เบิกอะไหล่', 'loan': 'ยืมเครื่องมือ', 'return': 'คืนเครื่องมือ',
    'adjust': 'ปรับยอด', 'status': 'เปลี่ยนสถานะ', 'available': 'พร้อมใช้งาน',
    'loaned': 'ถูกยืม', 'repair': 'ส่งซ่อม', 'damaged': 'ชำรุด',
    'out': 'หมดสต็อก', 'low': 'ใกล้หมด', 'ok': 'มีในคลัง', 'good': 'สภาพดี',
}


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


def format_time(value):
    if not value:
        return '—'
    return datetime.fromisoformat(value).astimezone(THAI_TIME).strftime('%d/%m/%Y %H:%M')


def session_hash(token):
    return sha256(token.encode('ascii')).hexdigest()


def require_roles(*roles):
    def decorate(view):
        @wraps(view)
        def protected(*args, **kwargs):
            if g.user is None:
                return redirect(url_for('login'))
            if roles and g.user['role'] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return protected
    return decorate


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('CES_SECRET_KEY'),
        DATABASE=os.environ.get('CES_DATABASE', str(Path(app.instance_path) / 'ces.sqlite3')),
        SESSION_COOKIE_NAME='ces_session', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('CES_HTTPS') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=64 * 1024, MAX_FORM_MEMORY_SIZE=64 * 1024,
    )
    if test_config is not None:
        app.config.update(test_config)
    if not app.secret_key:
        raise RuntimeError('Set CES_SECRET_KEY before starting CES. See README.md.')

    db.init_app(app)
    app.jinja_env.globals.update(csrf_token=csrf_token, labels=LABELS, format_time=format_time)
    dummy_hash = generate_password_hash(secrets.token_urlsafe(24))

    @app.before_request
    def check_session_and_csrf():
        g.user = None
        if request.endpoint == 'static':
            return
        if 'session_token' in session:
            user = db.get_db().execute('''SELECT u.* FROM user_sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',
                (session_hash(session['session_token']), int(time.time()))).fetchone()
            if user and user['session_version'] == session.get('session_version'):
                g.user = user
            else:
                session.clear()
        if request.method == 'POST':
            expected = session.get('csrf_token')
            supplied = request.form.get('_csrf', '')
            if not expected or not supplied.isascii() or not hmac.compare_digest(expected, supplied):
                abort(400, 'ฟอร์มหมดอายุ กรุณาเปิดหน้าใหม่แล้วลองอีกครั้ง')

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; img-src 'self' data:; form-action 'self'; "
            "base-uri 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/')
    def home():
        return redirect(url_for('dashboard' if g.user else 'login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if g.user:
            return redirect(url_for('dashboard'))
        username = ''
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            user = db.get_db().execute(
                'SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1', (username,)
            ).fetchone()
            if len(password) <= 256 and check_password_hash(user['password_hash'] if user else dummy_hash, password) and user:
                token = secrets.token_urlsafe(32)
                now = int(time.time())
                expiry = now + int(app.permanent_session_lifetime.total_seconds())
                connection = db.get_db()
                with connection:
                    connection.execute('DELETE FROM user_sessions WHERE expires_at<=?', (now,))
                    connection.execute('INSERT INTO user_sessions VALUES (?,?,?)', (session_hash(token), user['id'], expiry))
                session.clear()
                session.permanent = True
                session.update(session_token=token, session_version=user['session_version'])
                csrf_token()
                return redirect(url_for('dashboard'), code=303)
            return render_template('login.html', username=username, error='ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'), 401
        return render_template('login.html', username=username)

    @app.post('/logout')
    @require_roles()
    def logout():
        connection = db.get_db()
        with connection:
            connection.execute('DELETE FROM user_sessions WHERE token_hash=?', (session_hash(session['session_token']),))
        session.clear()
        return redirect(url_for('login'), code=303)

    messages = {400: 'ข้อมูลไม่ถูกต้อง', 403: 'คุณไม่มีสิทธิ์เข้าถึงหน้านี้',
                404: 'ไม่พบหน้าหรือรายการที่ต้องการ', 413: 'ข้อมูลที่ส่งมีขนาดใหญ่เกินกำหนด',
                503: 'ระบบกำลังทำรายการอื่น กรุณาลองอีกครั้ง'}

    def show_error(error):
        detail = error.description if error.code == 400 else messages[error.code]
        return render_template('error.html', code=error.code, message=detail), error.code

    for code in messages:
        app.register_error_handler(code, show_error)

    from . import commands, views
    commands.register(app)
    views.register(app)
    return app
