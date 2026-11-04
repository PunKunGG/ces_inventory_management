import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import current_app, g


MAX_SQL_INTEGER = (1 << 63) - 1
THAI_TIME = timezone(timedelta(hours=7))
ROLE_BY_ACTION = {
    "stock_in": "technician",
    "stock_out": "technician",
    "loan": "technician",
    "return": "technician",
    "adjust": "admin",
    "status": "admin",
}
INPUTS_BY_ACTION = {
    "stock_in": ("item_id", "quantity", "tool_code", "serial", "source", "note"),
    "stock_out": ("item_id", "quantity", "requester_id", "purpose", "work_order", "note"),
    "loan": ("tool_unit_id", "requester_id", "responsible_id", "due_at", "note"),
    "return": ("tool_unit_id", "loan_id", "condition", "note"),
    "adjust": ("item_id", "quantity", "reason", "reference_id"),
    "status": ("tool_unit_id", "status", "reason", "reference_id"),
}


class MovementError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def connect_db(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA recursive_triggers = ON")
    return db


def get_db():
    if "db" not in g:
        g.db = connect_db(current_app.config["DATABASE"])
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db=None):
    if db is None:
        db = get_db()
    db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))


def init_app(app):
    app.teardown_appcontext(close_db)


def _text(value, label, *, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str) and type(value) is not int:
        raise MovementError(f"{label} ไม่ถูกต้อง")
    value = str(value).strip()
    if len(value) > limit or "\x00" in value:
        raise MovementError(f"{label} ไม่ถูกต้อง (สูงสุด {limit} ตัวอักษร)")
    if required and not value:
        raise MovementError(f"กรุณาระบุ{label}")
    return value


def _integer(value, label, *, minimum=1):
    if not re.fullmatch(r"[+-]?[0-9]{1,19}", value):
        raise MovementError(f"{label}ต้องเป็นจำนวนเต็ม")
    number = int(value)
    if not minimum <= number <= MAX_SQL_INTEGER:
        raise MovementError(f"{label}อยู่นอกช่วงที่รองรับ")
    return number


def _active_user(db, value, label):
    user_id = _integer(value, label)
    if db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone() is None:
        raise MovementError(f"{label}ต้องเป็นบัญชี CES ที่เปิดใช้งาน")
    return user_id


def _active_item(db, value):
    item_id = _integer(value, "รายการคลัง")
    item = db.execute("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone()
    if item is None:
        raise MovementError("รายการคลังไม่พร้อมทำรายการ", 409)
    return item


def _active_tool(db, value):
    tool_id = _integer(value, "เครื่องมือ")
    tool = db.execute(
        """SELECT t.* FROM tool_units t JOIN items i ON i.id = t.item_id
           WHERE t.id = ? AND t.active = 1 AND i.active = 1 AND i.kind = 'tool'""",
        (tool_id,),
    ).fetchone()
    if tool is None:
        raise MovementError("เครื่องมือไม่พร้อมทำรายการ", 409)
    return tool


def _available_tools(db, item_id):
    return db.execute(
        "SELECT COUNT(*) FROM tool_units WHERE item_id = ? AND status = 'available' AND active = 1",
        (item_id,),
    ).fetchone()[0]


def _set_reference(db, data, movement):
    if not data["reference_id"]:
        return
    reference_id = _integer(data["reference_id"], "เลขอ้างอิง")
    reference = db.execute(
        "SELECT item_id, tool_unit_id FROM stock_movements WHERE id = ?", (reference_id,)
    ).fetchone()
    if reference is None or (reference["item_id"], reference["tool_unit_id"]) != (
        movement["item_id"], movement["tool_unit_id"]
    ):
        raise MovementError("เลขอ้างอิงต้องเป็นธุรกรรมของรายการเดียวกัน")
    movement["reference_id"] = reference_id


def _register_tool(db, item, data, movement):
    code = _text(data["tool_code"], "รหัสเครื่องมือรายชิ้น", limit=80, required=True)
    serial = _text(data["serial"], "Serial Number", limit=120)
    if db.execute("SELECT 1 FROM tool_units WHERE code = ?", (code,)).fetchone():
        raise MovementError("รหัสเครื่องมือรายชิ้นนี้ถูกใช้แล้ว", 409)
    before = _available_tools(db, item["id"])
    tool_id = db.execute(
        "INSERT INTO tool_units (item_id, code, serial) VALUES (?, ?, ?)",
        (item["id"], code, serial),
    ).lastrowid
    movement.update(tool_unit_id=tool_id, before_quantity=before, delta=1, new_status="available")


def _post_item_movement(db, action, data, movement):
    item = _active_item(db, data["item_id"])
    movement["item_id"] = item["id"]
    if action == "stock_in":
        movement["source"] = _text(data["source"], "แหล่งที่มา", limit=200)
        if item["kind"] == "tool":
            _register_tool(db, item, data, movement)
            return
    if item["kind"] != "part":
        raise MovementError("ใช้รายการยืม คืน หรือเปลี่ยนสถานะสำหรับเครื่องมือ")

    minimum = -MAX_SQL_INTEGER if action == "adjust" else 1
    quantity = _integer(data["quantity"], "จำนวน", minimum=minimum)
    if quantity == 0:
        raise MovementError("จำนวนที่ปรับต้องแตกต่างจากศูนย์")
    if action == "stock_out":
        quantity = -quantity
        movement["requester_id"] = _active_user(db, data["requester_id"], "ผู้เบิก")
        movement["reason"] = _text(data["purpose"], "วัตถุประสงค์", limit=300, required=True)
        movement["work_order"] = _text(data["work_order"], "เลขใบงาน", limit=100)
    if action == "adjust":
        movement["reason"] = _text(data["reason"], "เหตุผล", limit=300, required=True)
        _set_reference(db, data, movement)

    before = item["quantity"]
    after = before + quantity
    if after < 0:
        raise MovementError(f"จำนวนคงเหลือไม่เพียงพอ (คงเหลือ {before})", 409)
    if after > MAX_SQL_INTEGER:
        raise MovementError("ยอดคงเหลือเกินช่วงที่รองรับ")
    db.execute("UPDATE items SET quantity = ? WHERE id = ?", (after, item["id"]))
    movement.update(before_quantity=before, delta=quantity)


def _open_loan(db, tool, data, movement, now):
    if tool["status"] != "available":
        raise MovementError("เครื่องมือชิ้นนี้ไม่อยู่ในสถานะพร้อมใช้งาน", 409)
    borrower = _active_user(db, data["requester_id"], "ผู้ยืม")
    responsible = _active_user(db, data["responsible_id"], "ผู้รับผิดชอบ")
    try:
        due = date.fromisoformat(data["due_at"])
    except ValueError as error:
        raise MovementError("กรุณาระบุกำหนดคืนให้ถูกต้อง") from error
    if due.isoformat() != data["due_at"] or due < now.astimezone(THAI_TIME).date():
        raise MovementError("กำหนดคืนต้องเป็นวันนี้หรือวันที่หลังจากวันนี้")
    loan_id = db.execute(
        """INSERT INTO item_loans (tool_unit_id, borrower_id, responsible_id, loaned_by, loaned_at, due_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (tool["id"], borrower, responsible, movement["actor_id"], movement["created_at"], due.isoformat()),
    ).lastrowid
    movement.update(loan_id=loan_id, requester_id=borrower)
    return "loaned"


def _return_loan(db, tool, data, movement):
    loan_id = _integer(data["loan_id"], "รายการยืม")
    loan = db.execute(
        "SELECT borrower_id FROM item_loans WHERE id = ? AND tool_unit_id = ? AND returned_at IS NULL",
        (loan_id, tool["id"]),
    ).fetchone()
    if loan is None or tool["status"] != "loaned":
        raise MovementError("รายการยืมนี้ปิดแล้วหรือไม่ตรงกับเครื่องมือ", 409)
    condition = data["condition"]
    if condition not in {"good", "damaged"}:
        raise MovementError("กรุณาระบุสภาพเครื่องมือเมื่อคืน")
    db.execute(
        "UPDATE item_loans SET returned_by = ?, returned_at = ?, condition = ? WHERE id = ?",
        (movement["actor_id"], movement["created_at"], condition, loan_id),
    )
    movement.update(loan_id=loan_id, requester_id=loan["borrower_id"])
    return "available" if condition == "good" else "damaged"


def _tool_status(db, tool, data, movement):
    status = data["status"]
    if status not in {"available", "repair", "damaged"}:
        raise MovementError("กรุณาระบุสถานะเครื่องมือที่ถูกต้อง")
    if tool["status"] == "loaned":
        raise MovementError("ต้องคืนเครื่องมือก่อนเปลี่ยนสถานะ", 409)
    if tool["status"] == status:
        raise MovementError("สถานะใหม่ต้องต่างจากสถานะปัจจุบัน")
    movement["reason"] = _text(data["reason"], "เหตุผล", limit=300, required=True)
    _set_reference(db, data, movement)
    return status


def _post_tool_movement(db, action, data, movement, now):
    tool = _active_tool(db, data["tool_unit_id"])
    item_id = tool["item_id"]
    previous = tool["status"]
    before = _available_tools(db, item_id)
    movement.update(item_id=item_id, tool_unit_id=tool["id"], previous_status=previous)
    if action == "loan":
        status = _open_loan(db, tool, data, movement, now)
    elif action == "return":
        status = _return_loan(db, tool, data, movement)
    else:
        status = _tool_status(db, tool, data, movement)
    db.execute("UPDATE tool_units SET status = ? WHERE id = ?", (status, tool["id"]))
    delta = int(status == "available") - int(previous == "available")
    movement.update(before_quantity=before, delta=delta, new_status=status)


def _insert_movement(db, movement):
    return db.execute(
        """INSERT INTO stock_movements (
               action, item_id, tool_unit_id, loan_id, actor_id, requester_id,
               before_quantity, delta, after_quantity, reason, note, source,
               work_order, reference_id, previous_status, new_status, created_at,
               request_key, payload_hash
           ) VALUES (
               :action, :item_id, :tool_unit_id, :loan_id, :actor_id, :requester_id,
               :before_quantity, :delta, :after_quantity, :reason, :note, :source,
               :work_order, :reference_id, :previous_status, :new_status, :created_at,
               :request_key, :payload_hash
           )""",
        movement,
    ).lastrowid


def record_movement(db, actor_id, action, data, request_key, *, session_version=None, transaction_open=False):
    """Post a stock change and append its audit entry, or return a successful replay."""
    if action not in ROLE_BY_ACTION:
        raise MovementError("ไม่รู้จักประเภทธุรกรรม")
    key = _text(request_key, "รหัสการส่งรายการ", limit=120, required=True)
    inputs = {
        field: _text(data.get(field), field, limit=1000)
        for field in INPUTS_BY_ACTION[action]
    }
    payload = json.dumps([action, inputs], sort_keys=True, ensure_ascii=False).encode("utf-8")
    payload_hash = hashlib.sha256(payload).hexdigest()

    if db.in_transaction != transaction_open:
        raise RuntimeError("Unexpected stock movement transaction state")
    try:
        if not transaction_open:
            db.execute("BEGIN IMMEDIATE")
        actor = db.execute("SELECT role, active, session_version FROM users WHERE id = ?", (actor_id,)).fetchone()
        if (actor is None or not actor["active"] or actor["role"] != ROLE_BY_ACTION[action]
            or (session_version is not None and actor["session_version"] != session_version)):
            raise MovementError("บัญชีนี้ไม่มีสิทธิ์ทำรายการ", 403)
        existing = db.execute(
            "SELECT id, payload_hash FROM stock_movements WHERE actor_id = ? AND request_key = ?",
            (actor_id, key),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise MovementError("รหัสการส่งรายการถูกใช้กับข้อมูลอื่นแล้ว", 409)
            if not transaction_open:
                db.commit()
            return existing["id"]

        now = datetime.now(timezone.utc)
        movement = {
            "action": action,
            "actor_id": actor_id,
            "tool_unit_id": None,
            "loan_id": None,
            "requester_id": None,
            "reason": "",
            "note": _text(inputs.get("note"), "หมายเหตุ", limit=1000),
            "source": "",
            "work_order": "",
            "reference_id": None,
            "previous_status": None,
            "new_status": None,
            "created_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "request_key": key,
            "payload_hash": payload_hash,
        }
        if action in {"stock_in", "stock_out", "adjust"}:
            _post_item_movement(db, action, inputs, movement)
        else:
            _post_tool_movement(db, action, inputs, movement, now)
        movement["after_quantity"] = movement["before_quantity"] + movement["delta"]
        movement_id = _insert_movement(db, movement)
        if not transaction_open:
            db.commit()
        return movement_id
    except Exception as error:
        if not transaction_open:
            db.rollback()
        if isinstance(error, sqlite3.OperationalError):
            result_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if result_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise MovementError("คลังกำลังบันทึกรายการอื่น กรุณาลองส่งอีกครั้ง", 409) from error
        raise
