import csv
import io
import sqlite3
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered
from werkzeug.security import generate_password_hash

from ces import create_app
from ces.db import MovementError, connect_db, init_db, record_movement


PASSWORD = "Ces-local-test-password"
PASSWORD_HASH = generate_password_hash(PASSWORD)
THAI_TIMEZONE = timezone(timedelta(hours=7))


class InventoryFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="ces-acceptance-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "acceptance.db"
        self.config = {
            "TESTING": True,
            "DATABASE": str(self.path),
            "SECRET_KEY": "acceptance-test-key",
        }
        self.db = connect_db(self.path)
        self.addCleanup(self.db.close)
        init_db(self.db)
        self.db.executemany(
            """INSERT INTO users (id, username, name, password_hash, role, active)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (1, "admin", "ผู้ดูแลทดสอบ", PASSWORD_HASH, "admin", 1),
                (2, "tech", "ผู้บันทึกทดสอบ", PASSWORD_HASH, "technician", 1),
                (3, "supervisor", "ผู้ทบทวนทดสอบ", PASSWORD_HASH, "supervisor", 1),
                (4, "borrower", "ผู้ยืมทดสอบ", PASSWORD_HASH, "technician", 1),
                (5, "inactive", "บัญชีระงับทดสอบ", PASSWORD_HASH, "technician", 0),
            ],
        )
        self.db.executemany(
            "INSERT INTO categories (id, name, active) VALUES (?, ?, 1)",
            [(1, "อะไหล่ชุดทดสอบ"), (2, "เครื่องมือทดสอบ")],
        )
        self.db.executemany(
            "INSERT INTO locations (id, name, parent_id, active) VALUES (?, ?, ?, 1)",
            [(1, "ห้องเหนือ", None), (2, "ตู้ N17", 1), (3, "ชั้น E37", 2), (4, "ห้องใต้", None)],
        )
        self.db.executemany(
            """INSERT INTO items
                   (id, code, name, kind, category_id, location_id, unit, quantity,
                    min_quantity, brand, model, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            [
                (1, "PRT-EXACT", "ฟิวส์ทดสอบ", "part", 1, 3, "ชิ้น", 2, "Brand17", "Model77", 1),
                (2, "PRT-EMPTY", "สายไฟทดสอบ", "part", 1, 4, "ชิ้น", 0, "", "", 1),
                (3, "PRT-HEALTHY", "ขั้วทดสอบ", "part", 1, 3, "ชิ้น", 1, "", "", 1),
                (4, "TOOL-REG", "มัลติมิเตอร์ทดสอบ", "tool", 2, 3, "เครื่อง", 0, "", "", 1),
                (5, "PRT-INACTIVE", "รายการเลิกใช้", "part", 1, 3, "ชิ้น", 2, "", "", 0),
                (6, "PRT-OTHER", "=CSV, รายการทดสอบ", "part", 2, 4, "ชิ้น", 0, "", "", 1),
            ],
        )
        self.db.commit()
        self.request_no = 0

    @property
    def due_date(self):
        return (datetime.now(THAI_TIMEZONE).date() + timedelta(days=1)).isoformat()

    def action(self, action, data, *, actor=2, key=None, db=None):
        self.request_no += 1
        return record_movement(
            self.db if db is None else db,
            actor,
            action,
            data,
            key or f"test-action-{self.request_no:04d}",
        )

    def receive(self, quantity, item_id=1):
        return self.action("stock_in", {"item_id": item_id, "quantity": quantity})

    def issue_data(self, quantity=1, item_id=1):
        return {
            "item_id": item_id,
            "quantity": quantity,
            "requester_id": 4,
            "purpose": "ซ่อมเครื่องทดสอบ",
        }

    def register_tool(self):
        movement = self.action(
            "stock_in",
            {"item_id": 4, "tool_code": "CES-TOOL-A", "serial": "SERIAL-361"},
        )
        return self.movement(movement)["tool_unit_id"]

    def loan_data(self, tool_id):
        return {
            "tool_unit_id": tool_id,
            "requester_id": 4,
            "responsible_id": 3,
            "due_at": self.due_date,
        }

    def movement(self, movement_id):
        return self.db.execute(
            "SELECT * FROM stock_movements WHERE id = ?", (movement_id,)
        ).fetchone()

    def quantity(self, item_id=1):
        return self.db.execute("SELECT quantity FROM items WHERE id = ?", (item_id,)).fetchone()[0]

    def tool_status(self, tool_id):
        return self.db.execute(
            "SELECT status FROM tool_units WHERE id = ?", (tool_id,)
        ).fetchone()[0]

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def client(self, role="tech"):
        client = self.app.test_client()
        response = self.login(client, role)
        self.assertEqual(response.status_code, 303)
        return client

    def login(self, client, username, password=PASSWORD):
        self.assertEqual(client.get("/login").status_code, 200)
        return self.post(client, "/login", {"username": username, "password": password})

    def post(self, client, route, data):
        with client.session_transaction() as session:
            token = session["csrf_token"]
        return client.post(route, data={"_csrf": token, **data})


class MovementAcceptance(InventoryFixture):
    def test_stock_movements_and_validation(self):
        received = self.movement(self.receive(2))
        issued = self.movement(
            self.action("stock_out", {**self.issue_data(), "work_order": "WO-432"})
        )
        self.assertEqual(
            tuple(received[field] for field in ("before_quantity", "delta", "after_quantity")),
            (0, 2, 2),
        )
        self.assertEqual(
            tuple(issued[field] for field in (
                "action", "item_id", "actor_id", "requester_id", "work_order",
                "before_quantity", "delta", "after_quantity",
            )),
            ("stock_out", 1, 2, 4, "WO-432", 2, -1, 1),
        )
        for data in (
            {},
            {"item_id": 1},
            {"item_id": 1, "quantity": ""},
            {"item_id": 1, "quantity": 0},
            {"item_id": 1, "quantity": -1},
            {"item_id": 1, "quantity": "1.5"},
            {"item_id": 5, "quantity": 1},
            {"item_id": 999, "quantity": 1},
        ):
            with self.subTest(data=data), self.assertRaises(MovementError):
                self.action("stock_in", data)
        for data in (
            self.issue_data(2),
            {**self.issue_data(), "requester_id": 5},
            {**self.issue_data(), "requester_id": 999},
            {**self.issue_data(), "purpose": "   "},
            self.issue_data(item_id=4),
        ):
            with self.subTest(data=data), self.assertRaises(MovementError):
                self.action("stock_out", data)
        self.assertEqual((self.quantity(), self.count("stock_movements")), (1, 2))

    def test_idempotency_adjustment_and_immutable_history(self):
        key = "retry-exact-receive-001"
        payload = {"item_id": 1, "quantity": 3, "source": "คลังทดสอบ"}
        first = self.action("stock_in", payload, key=key)
        self.assertEqual(self.action("stock_in", payload, key=key), first)
        with self.assertRaises(MovementError) as failure:
            self.action("stock_in", {**payload, "quantity": 4}, key=key)
        self.assertEqual(failure.exception.status, 409)
        adjustment = {"item_id": 1, "quantity": -1, "reason": "แก้ยอดรับ", "reference_id": first}
        with self.assertRaises(MovementError) as failure:
            self.action("adjust", adjustment)
        self.assertEqual(failure.exception.status, 403)
        with self.assertRaises(MovementError):
            self.action("adjust", {**adjustment, "reason": "   "}, actor=1)
        corrected = self.movement(self.action("adjust", adjustment, actor=1))
        self.assertEqual(
            tuple(corrected[field] for field in (
                "actor_id", "reference_id", "before_quantity", "delta", "after_quantity",
            )),
            (1, first, 3, -1, 2),
        )
        for statement in (
            "UPDATE stock_movements SET delta = 2 WHERE id = ?",
            "DELETE FROM stock_movements WHERE id = ?",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                self.db.execute(statement, (first,))
            self.db.rollback()
        self.assertEqual(self.movement(first)["delta"], 3)
        self.assertEqual((self.quantity(), self.count("stock_movements")), (2, 2))

    def test_rollback_and_retry(self):
        self.receive(2)
        self.db.execute("""
            CREATE TRIGGER acceptance_fail_ledger BEFORE INSERT ON stock_movements
            BEGIN SELECT RAISE(ABORT, 'injected ledger write failure'); END
        """)
        self.db.commit()
        key = "rollback-and-retry-001"
        with self.assertRaises((sqlite3.DatabaseError, MovementError)):
            self.action("stock_out", self.issue_data(), key=key)
        self.assertFalse(self.db.in_transaction)
        with closing(connect_db(self.path)) as observer:
            self.assertEqual(observer.execute("SELECT quantity FROM items WHERE id=1").fetchone()[0], 2)
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0], 1)
        self.db.execute("DROP TRIGGER acceptance_fail_ledger")
        self.db.commit()
        self.action("stock_out", self.issue_data(), key=key)
        self.assertEqual((self.quantity(), self.count("stock_movements")), (1, 2))

    def test_tool_lifecycle_and_stale_return(self):
        tool_id = self.register_tool()
        self.assertEqual(self.tool_status(tool_id), "available")
        for patch in (
            {"due_at": "2000-01-01"},
            {"requester_id": 5},
            {"tool_unit_id": 999},
        ):
            with self.subTest(patch=patch), self.assertRaises(MovementError):
                self.action("loan", {**self.loan_data(tool_id), **patch})
        loan = self.movement(self.action("loan", self.loan_data(tool_id)))
        self.assertEqual((loan["before_quantity"], loan["delta"], loan["after_quantity"]), (1, -1, 0))
        self.assertEqual(self.tool_status(tool_id), "loaned")
        self.action("return", {"tool_unit_id": tool_id, "loan_id": loan["loan_id"], "condition": "good"})
        self.assertEqual(self.tool_status(tool_id), "available")
        second_loan = self.movement(self.action("loan", self.loan_data(tool_id)))
        with self.assertRaises(MovementError) as failure:
            self.action("return", {"tool_unit_id": tool_id, "loan_id": loan["loan_id"], "condition": "good"})
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.tool_status(tool_id), "loaned")
        damage = {"tool_unit_id": tool_id, "loan_id": second_loan["loan_id"], "condition": "damaged"}
        self.action("return", damage)
        self.assertEqual(self.tool_status(tool_id), "damaged")
        with self.assertRaises(MovementError):
            self.action("return", damage)
        with self.assertRaises(MovementError):
            self.action("loan", self.loan_data(tool_id))
        for new_status in ("repair", "available"):
            self.action("status", {"tool_unit_id": tool_id, "status": new_status, "reason": "ทดสอบ"}, actor=1)
            self.assertEqual(self.tool_status(tool_id), new_status)
        closed = self.db.execute(
            "SELECT borrower_id, responsible_id, returned_by, condition, returned_at FROM item_loans ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row)[:4] for row in closed], [(4, 3, 2, "good"), (4, 3, 2, "damaged")])
        self.assertTrue(all(row["returned_at"] for row in closed))

    def race(self, action, data):
        start = threading.Barrier(2)

        def submit(number):
            db = connect_db(self.path)
            try:
                start.wait(timeout=5)
                record_movement(db, 2, action, data, f"race-request-{number:04d}")
                return 303
            except MovementError as error:
                return error.status
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(pool.map(submit, (1, 2)))

    def test_concurrent_issue_of_last_part(self):
        self.receive(1)
        self.assertEqual(self.race("stock_out", self.issue_data()), [303, 409])
        self.assertEqual((self.quantity(), self.count("stock_movements")), (0, 2))

    def test_concurrent_loan_of_same_tool(self):
        tool_id = self.register_tool()
        self.assertEqual(self.race("loan", self.loan_data(tool_id)), [303, 409])
        self.assertEqual((self.tool_status(tool_id), self.count("item_loans")), ("loaned", 1))
        self.assertEqual(self.count("stock_movements"), 2)


class WebAcceptance(InventoryFixture):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.config)

    @contextmanager
    def rendered_context(self):
        contexts = []

        def record(sender, template, context, **extra):
            contexts.append(context)

        with template_rendered.connected_to(record, self.app):
            yield contexts

    def test_entrance_login_logout_and_session_invalidation(self):
        client = self.app.test_client()
        entrance = client.get("/")
        self.assertEqual((entrance.status_code, entrance.location), (302, "/login"))
        for username, password in (("tech", "wrong"), ("inactive", PASSWORD), ("unknown", PASSWORD)):
            with self.subTest(username=username):
                self.assertEqual(self.login(client, username, password).status_code, 401)
                self.assertEqual(client.get("/dashboard").status_code, 302)
        signed_in = self.login(client, "tech")
        self.assertEqual((signed_in.status_code, signed_in.location), (303, "/dashboard"))
        self.assertEqual(client.get("/dashboard").status_code, 200)
        entrance = client.get("/")
        self.assertEqual((entrance.status_code, entrance.location), (302, "/dashboard"))
        self.assertEqual(client.get("/logout").status_code, 405)
        self.assertEqual(client.post("/logout").status_code, 400)
        self.assertEqual(self.post(client, "/logout", {}).status_code, 303)
        self.assertEqual(client.get("/dashboard").status_code, 302)
        self.assertEqual(self.login(client, "tech").status_code, 303)
        self.db.execute("UPDATE users SET active=0 WHERE id=2")
        self.db.commit()
        self.assertEqual(client.get("/dashboard").status_code, 302)

    def test_logout_revokes_copied_cookie_without_revoking_other_browser(self):
        client = self.client()
        other_browser = self.client()
        self.assertEqual(client.get("/dashboard").status_code, 200)
        name = self.app.config["SESSION_COOKIE_NAME"]
        copied_cookie = client.get_cookie(name).value
        replay = self.app.test_client()
        replay.set_cookie(name, copied_cookie)
        self.assertEqual(replay.get("/dashboard").status_code, 200)
        self.assertEqual(self.post(client, "/logout", {}).status_code, 303)
        replay.set_cookie(name, copied_cookie)
        self.assertEqual(replay.get("/dashboard").status_code, 302)
        self.assertEqual(other_browser.get("/dashboard").status_code, 200)

    def test_roles_and_csrf(self):
        tech = self.client()
        supervisor = self.client("supervisor")
        admin = self.client("admin")
        self.assertEqual(tech.get("/operations/stock_in").status_code, 200)
        self.assertEqual(tech.get("/history").status_code, 403)
        self.assertEqual(tech.get("/reports/export.csv").status_code, 403)
        self.assertEqual(tech.get("/settings/users").status_code, 403)
        self.assertEqual(self.post(tech, "/settings/users", {"name": "forbidden"}).status_code, 403)
        self.assertEqual(supervisor.get("/history").status_code, 200)
        self.assertEqual(supervisor.get("/reports").status_code, 200)
        self.assertEqual(supervisor.get("/operations/stock_in").status_code, 403)
        self.assertEqual(self.post(supervisor, "/operations/stock_in", {}).status_code, 403)
        self.assertEqual(admin.get("/settings/users").status_code, 200)
        self.assertEqual(admin.get("/operations/adjust").status_code, 200)
        for token in ({}, {"_csrf": ""}, {"_csrf": "wrong-token"}):
            response = tech.post(
                "/operations/stock_in",
                data={"item_id": 1, "quantity": 1, "request_key": "csrf-test-123", **token},
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual((self.quantity(), self.count("stock_movements")), (0, 0))
        self.assertEqual(self.app.test_client().get("/reports/export.csv").status_code, 302)

    def test_inventory_search_and_detail(self):
        self.register_tool()
        self.receive(2)
        client = self.client()
        for term in ("PRT-EXACT", "ฟิวส์ทดสอบ", "Brand17", "Model77", "ห้องเหนือ", "ชั้น E37"):
            with self.subTest(term=term):
                page = client.get("/inventory", query_string={"q": term})
                self.assertEqual(page.status_code, 200)
                self.assertIn("PRT-EXACT", page.get_data(as_text=True))
                self.assertNotIn("PRT-EMPTY", page.get_data(as_text=True))
        page = client.get("/inventory", query_string={"q": "SERIAL-361", "kind": "tool"})
        self.assertIn("TOOL-REG", page.get_data(as_text=True))
        self.assertNotIn("PRT-EXACT", page.get_data(as_text=True))
        detail = client.get("/items/4")
        self.assertEqual(detail.status_code, 200)
        for label in ("CES-TOOL-A", "SERIAL-361", "ห้องเหนือ", "ตู้ N17", "ชั้น E37"):
            self.assertIn(label, detail.get_data(as_text=True))
        filtered = client.get("/inventory", query_string={"kind": "part", "category_id": 2})
        self.assertIn("PRT-OTHER", filtered.get_data(as_text=True))
        self.assertNotIn("PRT-EXACT", filtered.get_data(as_text=True))
        self.assertNotIn("TOOL-REG", filtered.get_data(as_text=True))
        for route in (
            "/items", "/items/", "/items/999", "/items/999/", "/items/4/",
            "/items/999999999999999999999999999",
        ):
            self.assertEqual(client.get(route).status_code, 404, route)

    def test_catalog_admin_forms(self):
        admin = self.client("admin")
        new_category = {"name": "หมวดใหม่", "active": 1}
        self.assertEqual(self.post(admin, "/settings/categories", new_category).status_code, 303)
        category_id = self.db.execute("SELECT id FROM categories WHERE name='หมวดใหม่'").fetchone()[0]
        self.assertEqual(
            admin.get("/settings/categories?edit=999999999999999999999999999").status_code,
            404,
        )
        self.assertEqual(admin.get("/items/999999999999999999999999999/edit").status_code, 404)
        self.assertEqual(
            self.post(admin, "/settings/categories", {**new_category, "id": category_id, "name": "หมวดแก้ไข"}).status_code,
            303,
        )
        new_location = {"name": "ช่องใหม่", "active": 1, "parent_id": 3}
        self.assertEqual(self.post(admin, "/settings/locations", new_location).status_code, 303)
        location_id = self.db.execute("SELECT id FROM locations WHERE name='ช่องใหม่'").fetchone()[0]
        self.assertEqual(
            self.post(admin, "/settings/locations", {**new_location, "id": location_id, "parent_id": location_id}).status_code,
            400,
        )
        self.assertEqual(self.db.execute("SELECT parent_id FROM locations WHERE id=?", (location_id,)).fetchone()[0], 3)
        data = {
            "code": "PRT-NEW",
            "name": "ท่อทดสอบ",
            "kind": "part",
            "category_id": category_id,
            "location_id": location_id,
            "unit": "ชิ้น",
            "min_quantity": 2,
            "active": 1,
        }
        self.assertEqual(self.post(admin, "/items/new", data).status_code, 303)
        item_id = self.db.execute("SELECT id FROM items WHERE code='PRT-NEW'").fetchone()[0]
        self.assertEqual(
            self.post(admin, f"/items/{item_id}/edit", {**data, "name": "ท่อแก้ไข"}).status_code, 303
        )
        for payload in (
            data,
            {**data, "code": "BAD", "name": " "},
            {**data, "code": "BAD", "min_quantity": -1},
            {**data, "code": "BAD", "category_id": 999},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(admin, "/items/new", payload).status_code, 400)
        self.assertEqual(self.count("items"), 7)
        row = self.db.execute("SELECT name, quantity, min_quantity FROM items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(tuple(row), ("ท่อแก้ไข", 0, 2))

    def test_admin_account_create_change_role_and_disable(self):
        admin = self.client("admin")
        data = {
            "username": "new-staff",
            "name": "บัญชีใหม่ทดสอบ",
            "password": "a" * 12,
            "role": "technician",
            "active": 1,
        }
        self.assertEqual(
            self.post(admin, "/settings/users", {**data, "password": "a" * 11}).status_code,
            400,
        )
        self.assertEqual(self.count("users"), 5)
        self.assertEqual(self.post(admin, "/settings/users", data).status_code, 303)
        user = self.db.execute("SELECT * FROM users WHERE username='new-staff'").fetchone()
        self.assertEqual((user["role"], user["active"]), ("technician", 1))
        self.assertNotEqual(user["password_hash"], data["password"])
        self.assertEqual(self.post(admin, "/settings/users", data).status_code, 400)
        staff = self.app.test_client()
        self.assertEqual(self.login(staff, data["username"], data["password"]).status_code, 303)
        self.assertEqual(staff.get("/operations/stock_in").status_code, 200)
        changes = {**data, "id": user["id"], "password": "", "role": "supervisor"}
        self.assertEqual(self.post(admin, "/settings/users", changes).status_code, 303)
        self.assertEqual(staff.get("/dashboard").status_code, 302)
        self.assertEqual(self.login(staff, data["username"], data["password"]).status_code, 303)
        self.assertEqual(staff.get("/reports").status_code, 200)
        self.assertEqual(staff.get("/operations/stock_in").status_code, 403)
        self.assertEqual(self.post(admin, "/settings/users", {**changes, "active": 0}).status_code, 303)
        self.assertEqual(staff.get("/dashboard").status_code, 302)
        self.assertEqual(self.login(staff, data["username"], data["password"]).status_code, 401)

    def test_web_stock_mutations(self):
        tech = self.client()
        receive = {"item_id": 1, "quantity": 2, "actor_id": 3, "request_key": "web-receive-001"}
        for _ in range(2):
            self.assertEqual(self.post(tech, "/operations/stock_in", receive).status_code, 303)
        issue = {**self.issue_data(3), "request_key": "web-issue-001"}
        self.assertEqual(self.post(tech, "/operations/stock_out", issue).status_code, 409)
        self.assertEqual((self.quantity(), self.count("stock_movements")), (2, 1))
        self.assertEqual(self.db.execute("SELECT actor_id FROM stock_movements").fetchone()[0], 2)
        self.assertEqual(
            self.post(tech, "/operations/stock_out", {**issue, "quantity": 2}).status_code, 303
        )
        self.assertEqual((self.quantity(), self.count("stock_movements")), (0, 2))
        tool_id = self.register_tool()
        self.assertEqual(
            self.post(tech, "/operations/loan", {**self.loan_data(tool_id), "request_key": "web-loan-001"}).status_code,
            303,
        )
        loan_id = self.db.execute("SELECT id FROM item_loans WHERE tool_unit_id=?", (tool_id,)).fetchone()[0]
        return_data = {
            "tool_unit_id": tool_id,
            "loan_id": loan_id,
            "condition": "damaged",
            "request_key": "web-return-001",
        }
        self.assertEqual(self.post(tech, "/operations/return", return_data).status_code, 303)
        self.assertEqual(self.tool_status(tool_id), "damaged")
        self.assertEqual(self.post(tech, "/operations/return", {**return_data, "request_key": "web-return-002"}).status_code, 409)
        self.assertEqual(self.count("item_loans"), 1)

    def test_stale_return_form_cannot_retarget_a_new_loan(self):
        tech = self.client()
        tool_id = self.register_tool()
        first = self.movement(self.action("loan", self.loan_data(tool_id)))["loan_id"]
        self.action("return", {"tool_unit_id": tool_id, "loan_id": first, "condition": "good"})
        current = self.movement(self.action("loan", self.loan_data(tool_id)))["loan_id"]
        stale = {
            "tool_unit_id": tool_id,
            "loan_id": first,
            "condition": "damaged",
            "note": "หมายเหตุยังอยู่",
            "request_key": "stale-form-return-001",
        }
        response = self.post(tech, "/operations/return", stale)
        self.assertEqual(response.status_code, 409)
        html = response.get_data(as_text=True)
        self.assertIn('name="loan_id" value=""', html)
        self.assertIn("หมายเหตุยังอยู่", html)
        self.assertNotRegex(html, r'<option [^>]*value="damaged"[^>]*selected')
        self.assertEqual(self.tool_status(tool_id), "loaned")
        still_open = self.db.execute("SELECT id FROM item_loans WHERE returned_at IS NULL").fetchone()
        self.assertEqual(still_open["id"], current)

    def test_dashboard_and_alert_boundaries(self):
        self.receive(2)
        self.receive(8, item_id=3)
        self.receive(1, item_id=6)
        client = self.client("supervisor")
        with self.rendered_context() as rendered:
            dashboard = client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        html = dashboard.get_data(as_text=True)
        for value in ("ฟิวส์ทดสอบ", "สายไฟทดสอบ"):
            self.assertIn(value, html)
        self.assertNotIn("รายการเลิกใช้", html)
        self.assertEqual(
            rendered[0]["stats"],
            {"tools": 1, "parts": 4, "available_tools": 0, "low": 1, "out": 1},
        )
        self.assertEqual(
            {item["code"]: item["stock_state"] for item in rendered[0]["low_items"]},
            {"PRT-EXACT": "low", "PRT-EMPTY": "out"},
        )
        low = client.get("/reports/export.csv", query_string={"kind": "low"})
        self.assertEqual(low.status_code, 200)
        low_rows = list(csv.DictReader(io.StringIO(low.data.decode("utf-8-sig"))))
        self.assertEqual({row["รหัส"] for row in low_rows}, {"PRT-EXACT", "PRT-EMPTY"})

    def test_history_recent_and_local_day_boundaries(self):
        class TestClock(datetime):
            value = None

            @classmethod
            def now(cls, tz=None):
                return cls.value.astimezone(tz)

        # UTC 17:00 is the start of the next calendar date in Thailand.
        with patch("ces.db.datetime", TestClock):
            TestClock.value = datetime.fromisoformat("2026-02-05T16:59:59.999999+00:00")
            latest_id = self.receive(1, item_id=3)
            TestClock.value = datetime.fromisoformat("2026-02-04T17:00:00+00:00")
            first_id = self.receive(1, item_id=1)
            TestClock.value = datetime.fromisoformat("2026-02-05T17:00:00+00:00")
            outside_id = self.receive(1, item_id=6)
        supervisor = self.client("supervisor")
        with self.rendered_context() as rendered:
            response = supervisor.get("/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in rendered[0]["movements"]], [outside_id, latest_id, first_id])
        with self.rendered_context() as rendered:
            response = supervisor.get(
                "/history", query_string={"start": "2026-02-05", "end": "2026-02-05"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in rendered[0]["movements"]], [latest_id, first_id])
        for endpoint in ("/history", "/reports", "/reports/export.csv"):
            response = supervisor.get(
                endpoint,
                query_string={"kind": "in", "start": "2026-02-06", "end": "2026-02-05"},
            )
            self.assertEqual(response.status_code, 400)

    def test_reports_and_csv(self):
        self.receive(2)
        self.receive(7, item_id=3)
        self.receive(5, item_id=6)
        self.action("stock_out", {**self.issue_data(), "note": "รายงานเบิก"})
        supervisor = self.client("supervisor")
        today = datetime.now(THAI_TIMEZONE).date()
        filter_values = {"kind": "in", "category_id": 1, "item_id": 1, "start": today.isoformat(), "end": today.isoformat()}
        report = supervisor.get("/reports", query_string=filter_values)
        self.assertEqual(report.status_code, 200)
        export = supervisor.get("/reports/export.csv", query_string=filter_values)
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.mimetype, "text/csv")
        self.assertIn("attachment", export.headers["Content-Disposition"])
        self.assertTrue(export.data.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(export.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][:7], ["เลขอ้างอิง", "วันที่", "รหัส", "ชื่อรายการ", "หมวดหมู่", "จำนวน", "หน่วย"])
        self.assertIn("PRT-EXACT", rows[1])
        self.assertIn("ฟิวส์ทดสอบ", rows[1])
        self.assertIn("PRT-EXACT", report.get_data(as_text=True))
        previous = (today - timedelta(days=1)).isoformat()
        export = supervisor.get(
            "/reports/export.csv", query_string={**filter_values, "start": previous, "end": previous}
        )
        self.assertEqual(len(list(csv.reader(io.StringIO(export.data.decode("utf-8-sig"))))), 1)
        issue = supervisor.get("/reports/export.csv", query_string={**filter_values, "kind": "out"})
        self.assertIn("PRT-EXACT", issue.data.decode("utf-8-sig"))
        balance = supervisor.get("/reports/export.csv", query_string={"kind": "balance", "category_id": 2})
        rows = list(csv.reader(io.StringIO(balance.data.decode("utf-8-sig"))))
        matching_row = next(row for row in rows if "PRT-OTHER" in row)
        risky_text = next(field for field in matching_row if "CSV, รายการทดสอบ" in field)
        self.assertFalse(risky_text.startswith(("=", "+", "-", "@")))

    def test_persistence_backup_and_restore(self):
        self.receive(12)
        tool_id = self.register_tool()
        self.action("loan", self.loan_data(tool_id))
        signed_in = self.client()
        cookie_name = self.app.config["SESSION_COOKIE_NAME"]
        copied_cookie = signed_in.get_cookie(cookie_name).value
        self.assertEqual(self.count("user_sessions"), 1)
        backup_path = self.path.with_name("backup.db")
        restore_path = self.path.with_name("restored.db")
        self.assertEqual(self.app.test_cli_runner().invoke(args=["backup-db", str(backup_path)]).exit_code, 0)
        self.assertTrue(backup_path.is_file())
        with closing(connect_db(backup_path)) as backup:
            self.assertEqual(backup.execute("SELECT count(*) FROM user_sessions").fetchone()[0], 0)
        self.assertEqual(signed_in.get("/dashboard").status_code, 200)
        self.assertEqual(
            create_app(self.config).test_cli_runner().invoke(args=["restore-db", str(backup_path)]).exit_code,
            1,
        )
        restored_app = create_app({**self.config, "DATABASE": str(restore_path)})
        runner = restored_app.test_cli_runner()
        for source in (backup_path.with_name("missing.db"), backup_path.with_name("invalid.db")):
            if source.name == "invalid.db":
                source.write_text("invalid database", encoding="utf-8")
            self.assertNotEqual(runner.invoke(args=["restore-db", str(source)]).exit_code, 0)
            self.assertFalse(restore_path.exists())
        result = runner.invoke(args=["restore-db", str(backup_path)])
        self.assertEqual(result.exit_code, 0, result.output)
        with closing(connect_db(restore_path)) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            for table in ("items", "tool_units", "item_loans", "stock_movements"):
                original_rows = [tuple(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY id")]
                restored_rows = [tuple(row) for row in restored.execute(f"SELECT * FROM {table} ORDER BY id")]
                self.assertEqual(original_rows, restored_rows, table)
            self.assertEqual(restored.execute("SELECT quantity FROM items WHERE id=1").fetchone()[0], 12)
            self.assertEqual(restored.execute("SELECT count(*) FROM item_loans WHERE returned_at IS NULL").fetchone()[0], 1)
            self.assertEqual(restored.execute("SELECT count(*) FROM user_sessions").fetchone()[0], 0)
        restored_client = restored_app.test_client()
        restored_client.set_cookie(cookie_name, copied_cookie)
        self.assertEqual(restored_client.get("/dashboard").status_code, 302)
        reopened = create_app(self.config).test_client()
        self.assertEqual(self.login(reopened, "tech").status_code, 303)
        self.assertIn("PRT-EXACT", reopened.get("/items/1").get_data(as_text=True))

    def test_qr_cli_writes_svg_with_printable_entrance_url(self):
        url = "http://192.168.1.50:8080/"
        output = self.path.with_name("entrance.svg")
        runner = self.app.test_cli_runner()
        for invalid in ("http://localhost:8080/", "http://127.0.0.1:8080/", url + "?token=unsafe"):
            result = runner.invoke(args=["make-qr", invalid, str(output)])
            self.assertNotEqual(result.exit_code, 0)
            self.assertFalse(output.exists())
        result = runner.invoke(args=["make-qr", url, str(output)])
        self.assertEqual(result.exit_code, 0, result.output)
        original = output.read_bytes()
        svg = ET.fromstring(original)
        ns = {"svg": "http://www.w3.org/2000/svg"}
        self.assertTrue(svg.find("svg:path", ns).attrib["d"])
        self.assertEqual(svg.find("svg:text", ns).text, url)
        self.assertNotEqual(
            runner.invoke(args=["make-qr", "https://ces.example.test/", str(output)]).exit_code,
            0,
        )
        self.assertEqual(output.read_bytes(), original)

    def test_prototype_response_time(self):
        self.db.executemany(
            """INSERT INTO items (code, name, kind, category_id, location_id, unit, quantity, min_quantity, active)
               VALUES (?, ?, 'part', 1, 3, 'ชิ้น', 0, 0, 1)""",
            [(f"PERF-{index:04d}", f"อะไหล่ทดสอบ {index}",) for index in range(1000)],
        )
        self.db.commit()
        client = self.client()
        for route in ("/inventory?q=PERF-0733", "/items/1"):
            start = time.perf_counter()
            response = client.get(route)
            elapsed = time.perf_counter() - start
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(elapsed, 3, f"{route} took {elapsed:.3f}s on a 1,006-item fixture")
        self.assertIn("PERF-0733", client.get("/inventory?q=PERF-0733").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
