from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
import textwrap
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import click
from flask import current_app
from werkzeug.security import generate_password_hash

from . import db


def database_path():
    return Path(current_app.config['DATABASE']).resolve()


def checked_copy(source, destination):
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise click.ClickException(f'Database not found: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise click.ClickException(f'Destination already exists: {destination}')
    with NamedTemporaryFile(dir=destination.parent, prefix='.ces-copy-', delete=False) as temporary_file:
        temporary = Path(temporary_file.name)
    try:
        with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)) as original, closing(db.connect_db(temporary)) as copy:
            original.backup(copy)
            integrity = copy.execute('PRAGMA integrity_check').fetchall()
            if [row[0] for row in integrity] != ['ok'] or copy.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.DatabaseError('integrity check failed')
            tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'users', 'user_sessions', 'categories', 'locations', 'items', 'tool_units', 'item_loans', 'stock_movements'}
            if copy.execute('PRAGMA user_version').fetchone()[0] != 1 or not required.issubset(tables):
                raise sqlite3.DatabaseError('unsupported CES schema')
            with copy:
                copy.execute('DELETE FROM user_sessions')
        if os.name == 'nt':
            temporary.rename(destination)  # Windows rename refuses an existing destination.
        else:
            os.link(temporary, destination)
    except (OSError, sqlite3.Error) as error:
        raise click.ClickException(f'Database copy failed ({error})') from error
    finally:
        temporary.unlink(missing_ok=True)


def make_qr(url, output):
    import qrcode
    from qrcode.image.svg import SvgPathFillImage

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise click.ClickException('Invalid URL') from error
    if (len(url) > 240 or not url.isascii() or any(ord(char) <= 32 for char in url)
        or parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
        or parts.path not in ('', '/') or parts.query or parts.fragment or port == 0
        or parts.hostname in ('localhost', '0.0.0.0', '::', '::1') or parts.hostname.startswith('127.')):
        raise click.ClickException('Use an entry URL reachable by phones, such as http://192.168.1.50:8080/')
    if output.suffix.lower() != '.svg':
        raise click.ClickException('Output must have the .svg extension')
    image = qrcode.make(url, image_factory=SvgPathFillImage, border=4)
    root = ET.fromstring(image.to_string())
    _, _, width, height = map(float, root.attrib['viewBox'].split())
    lines = textwrap.wrap(url, width=44, break_on_hyphens=False)
    full_height = height + 5 * len(lines) + 3
    root.set('height', f'{full_height:g}mm')
    root.set('viewBox', f'0 0 {width:g} {full_height:g}')
    ns = 'http://www.w3.org/2000/svg'
    ET.register_namespace('', ns)
    title = ET.Element(f'{{{ns}}}title')
    title.text = 'CES Inventory · ' + url
    root.insert(0, title)
    for index, line in enumerate(lines):
        label = ET.SubElement(root, f'{{{ns}}}text', x=f'{width / 2:g}', y=f'{height + 3 + 5 * index:g}',
                             fill='#111827', attrib={'text-anchor': 'middle', 'font-family': 'monospace',
                             'font-size': f'{min(2.7, width / max(1, len(line)) / 0.66):.2f}'})
        label.text = line
    try:
        with output.open('xb') as file:
            ET.ElementTree(root).write(file, encoding='utf-8', xml_declaration=True)
    except OSError as error:
        raise click.ClickException(f'Unable to create QR ({error})') from error


def seed_demo(connection, password):
    demo_users = (('admin', 'ผู้ดูแลระบบตัวอย่าง', 'admin'), ('technician', 'ช่าง CES ตัวอย่าง', 'technician'),
                  ('supervisor', 'หัวหน้างานตัวอย่าง', 'supervisor'))
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise click.ClickException('Seed demo only in an empty database')
        connection.executemany('INSERT INTO users (username,name,role,password_hash) VALUES (?,?,?,?)',
                               [(username, name, role, generate_password_hash(password)) for username, name, role in demo_users])
        connection.executemany('INSERT INTO categories (id,name) VALUES (?,?)',
                               [(1, 'อะไหล่อุปกรณ์การแพทย์'), (2, 'เครื่องมือช่าง'), (3, 'วัสดุสิ้นเปลือง')])
        connection.executemany('INSERT INTO locations (id,name,parent_id) VALUES (?,?,?)',
                               [(1, 'ห้องช่าง CES', None), (2, 'ตู้ A', 1), (3, 'ชั้น 2', 2), (4, 'ช่อง 01', 3), (5, 'ตู้เครื่องมือ', 1)])
        connection.executemany('''INSERT INTO items (id,code,name,kind,category_id,location_id,unit,min_quantity,brand,model)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', [
                (1, 'SP-001', 'ฟิวส์แก้ว 2A', 'part', 1, 4, 'ชิ้น', 10, '', '2A'),
                (2, 'SP-002', 'สายไฟ AC', 'part', 1, 3, 'เส้น', 5, '', ''),
                (3, 'SP-003', 'ขั้วต่อสายไฟ', 'part', 3, 2, 'ชิ้น', 6, '', ''),
                (4, 'TL-001', 'ดิจิทัลมัลติมิเตอร์', 'tool', 2, 5, 'เครื่อง', 0, '', 'DM-100'),
                (5, 'TL-002', 'ชุดไขควงช่าง', 'tool', 2, 5, 'ชุด', 0, '', ''),
            ])
        tech_id = connection.execute("SELECT id FROM users WHERE username='technician'").fetchone()['id']
        samples = [
            {'item_id': 1, 'quantity': 24}, {'item_id': 2, 'quantity': 3},
            {'item_id': 4, 'tool_code': 'TL-001-01', 'serial': 'DEMO-DM-001'},
            {'item_id': 4, 'tool_code': 'TL-001-02', 'serial': 'DEMO-DM-002'},
            {'item_id': 5, 'tool_code': 'TL-002-01'},
        ]
        for index, sample in enumerate(samples):
            db.record_movement(connection, tech_id, 'stock_in', dict(sample, source='ข้อมูลจำลอง'),
                               f'demo-stock-in-{index}', transaction_open=True)


def register(app):
    @app.cli.command('init-db')
    def init_db_command():
        database_path().parent.mkdir(parents=True, exist_ok=True)
        db.init_db()
        click.echo('Initialized CES database.')

    @app.cli.command('create-admin')
    @click.password_option()
    def create_admin_command(password):
        if not 12 <= len(password) <= 256:
            raise click.ClickException('Use a password of 12–256 characters')
        database_path().parent.mkdir(parents=True, exist_ok=True)
        db.init_db()
        connection = db.get_db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                raise click.ClickException('Use create-admin to bootstrap an empty database')
            connection.execute('INSERT INTO users (username,name,role,password_hash) VALUES (?,?,?,?)',
                               ('admin', 'ผู้ดูแลระบบ', 'admin', generate_password_hash(password)))
        click.echo('Created the initial admin account.')

    @app.cli.command('seed-demo')
    @click.option('--password', envvar='CES_DEMO_PASSWORD', prompt=True, hide_input=True, confirmation_prompt=True)
    def seed_demo_command(password):
        if not 12 <= len(password) <= 256:
            raise click.ClickException('Use a demo password of 12–256 characters')
        database_path().parent.mkdir(parents=True, exist_ok=True)
        db.init_db()
        seed_demo(db.get_db(), password)
        click.echo('Created demo data and accounts admin, technician, supervisor.')

    @app.cli.command('make-qr')
    @click.argument('url')
    @click.argument('output', type=click.Path(path_type=Path))
    def make_qr_command(url, output):
        make_qr(url, output)
        click.echo(f'Created QR: {output}')

    @app.cli.command('backup-db')
    @click.argument('output', type=click.Path(path_type=Path))
    def backup_command(output):
        checked_copy(database_path(), output)
        click.echo(f'Backup created: {output}')

    @app.cli.command('restore-db')
    @click.argument('source', type=click.Path(path_type=Path))
    def restore_command(source):
        checked_copy(source, database_path())
        click.echo(f'Database restored: {database_path()}')
