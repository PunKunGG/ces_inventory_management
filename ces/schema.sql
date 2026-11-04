BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'technician', 'supervisor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 0 CHECK (session_version >= 0)
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS user_sessions_expiry ON user_sessions(expires_at);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    parent_id INTEGER REFERENCES locations(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE UNIQUE INDEX IF NOT EXISTS locations_root_name
    ON locations(name) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS locations_child_name
    ON locations(parent_id, name) WHERE parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('part', 'tool')),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    unit TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (typeof(quantity) = 'integer' AND quantity >= 0),
    min_quantity INTEGER NOT NULL DEFAULT 0 CHECK (typeof(min_quantity) = 'integer' AND min_quantity >= 0),
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (kind <> 'tool' OR quantity = 0)
);

CREATE TABLE IF NOT EXISTS tool_units (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    serial TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'loaned', 'repair', 'damaged')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (active = 1 OR status <> 'loaned')
);

CREATE TABLE IF NOT EXISTS item_loans (
    id INTEGER PRIMARY KEY,
    tool_unit_id INTEGER NOT NULL REFERENCES tool_units(id),
    borrower_id INTEGER NOT NULL REFERENCES users(id),
    responsible_id INTEGER NOT NULL REFERENCES users(id),
    loaned_by INTEGER NOT NULL REFERENCES users(id),
    loaned_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    returned_by INTEGER REFERENCES users(id),
    returned_at TEXT,
    condition TEXT CHECK (condition IN ('good', 'damaged')),
    CHECK (
        (returned_at IS NULL AND returned_by IS NULL AND condition IS NULL)
        OR (returned_at IS NOT NULL AND returned_by IS NOT NULL AND condition IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS item_loans_open_unit
    ON item_loans(tool_unit_id) WHERE returned_at IS NULL;

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('stock_in', 'stock_out', 'loan', 'return', 'adjust', 'status')),
    item_id INTEGER NOT NULL REFERENCES items(id),
    tool_unit_id INTEGER REFERENCES tool_units(id),
    loan_id INTEGER REFERENCES item_loans(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    requester_id INTEGER REFERENCES users(id),
    before_quantity INTEGER NOT NULL CHECK (typeof(before_quantity) = 'integer' AND before_quantity >= 0),
    delta INTEGER NOT NULL CHECK (typeof(delta) = 'integer'),
    after_quantity INTEGER NOT NULL CHECK (typeof(after_quantity) = 'integer' AND after_quantity >= 0),
    reason TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    work_order TEXT NOT NULL DEFAULT '',
    reference_id INTEGER REFERENCES stock_movements(id),
    previous_status TEXT CHECK (previous_status IN ('available', 'loaned', 'repair', 'damaged')),
    new_status TEXT CHECK (new_status IN ('available', 'loaned', 'repair', 'damaged')),
    created_at TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    UNIQUE (actor_id, request_key),
    CHECK (after_quantity = before_quantity + delta)
);

CREATE INDEX IF NOT EXISTS items_category ON items(category_id);
CREATE INDEX IF NOT EXISTS items_location ON items(location_id);
CREATE INDEX IF NOT EXISTS tool_units_item_status ON tool_units(item_id, active, status);
CREATE INDEX IF NOT EXISTS stock_movements_item_time ON stock_movements(item_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS stock_movements_time ON stock_movements(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS stock_movements_action_time ON stock_movements(action, created_at DESC, id DESC);

CREATE TRIGGER IF NOT EXISTS stock_movements_no_update
    BEFORE UPDATE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'Stock movements are append-only');
END;

CREATE TRIGGER IF NOT EXISTS stock_movements_no_delete
    BEFORE DELETE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'Stock movements are append-only');
END;

PRAGMA user_version = 1;
COMMIT;
