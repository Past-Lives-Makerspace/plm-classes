"""
Database migration for PLM Classes.

Run this to create or update the classes.db schema:

    python3 migrate.py

Safe to run multiple times — uses IF NOT EXISTS and catches
already-existing columns. Each time you deploy or set up a
new machine, just run this before starting the app.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "classes.db"


def migrate(db_path: Path = DB_PATH) -> None:
    """Create all tables and apply column migrations."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")

    # ── Core schema ───────────────────────────────────────────────────────────

    con.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            slug        TEXT NOT NULL UNIQUE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            image_url   TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS instructors (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            slug           TEXT UNIQUE,
            email          TEXT UNIQUE,
            phone          TEXT,
            bio            TEXT,
            photo_url      TEXT,
            website        TEXT,
            social_handle  TEXT,
            login_token    TEXT UNIQUE,
            is_active      INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS classes (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            title                  TEXT NOT NULL,
            slug                   TEXT NOT NULL UNIQUE,
            category_id            INTEGER NOT NULL REFERENCES categories(id),
            instructor_id          INTEGER NOT NULL REFERENCES instructors(id),
            description            TEXT,
            prerequisites          TEXT,
            materials_included     TEXT,
            materials_to_bring     TEXT,
            safety_requirements    TEXT,
            age_minimum            INTEGER,
            age_guardian_note      TEXT,
            price_cents            INTEGER NOT NULL,
            member_discount_pct    INTEGER NOT NULL DEFAULT 10,
            capacity               INTEGER NOT NULL DEFAULT 6,
            scheduling_model       TEXT NOT NULL DEFAULT 'fixed'
                                       CHECK(scheduling_model IN ('fixed', 'flexible')),
            flexible_note          TEXT,
            is_private             INTEGER NOT NULL DEFAULT 0,
            private_for_name       TEXT,
            recurring_pattern      TEXT,
            image_url              TEXT,
            requires_model_release INTEGER NOT NULL DEFAULT 0,
            status                 TEXT NOT NULL DEFAULT 'draft'
                                       CHECK(status IN ('draft', 'pending', 'published', 'archived')),
            created_by             INTEGER REFERENCES instructors(id),
            approved_by            TEXT,
            published_at           TEXT,
            created_at             TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_classes_category ON classes(category_id);
        CREATE INDEX IF NOT EXISTS idx_classes_instructor ON classes(instructor_id);
        CREATE INDEX IF NOT EXISTS idx_classes_status ON classes(status);

        CREATE TABLE IF NOT EXISTS sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id         INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
            session_date     TEXT NOT NULL,
            start_time       TEXT NOT NULL,
            end_time         TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            sort_order       INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_class ON sessions(class_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(session_date);

        CREATE TABLE IF NOT EXISTS students (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name    TEXT NOT NULL,
            last_name     TEXT NOT NULL,
            pronouns      TEXT,
            email         TEXT NOT NULL UNIQUE,
            phone         TEXT,
            address_line1 TEXT,
            address_city  TEXT,
            address_state TEXT,
            address_zip   TEXT,
            is_member     INTEGER NOT NULL DEFAULT 0,
            member_code   TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_students_email ON students(email);

        CREATE TABLE IF NOT EXISTS registrations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id          INTEGER NOT NULL REFERENCES classes(id),
            student_id        INTEGER REFERENCES students(id),
            first_name        TEXT NOT NULL,
            last_name         TEXT NOT NULL,
            pronouns          TEXT,
            email             TEXT NOT NULL,
            phone             TEXT,
            address_line1     TEXT,
            address_city      TEXT,
            address_state     TEXT,
            address_zip       TEXT,
            is_member         INTEGER NOT NULL DEFAULT 0,
            prior_experience  TEXT,
            looking_for       TEXT,
            discount_code     TEXT,
            amount_paid_cents INTEGER,
            status            TEXT NOT NULL DEFAULT 'pending'
                                  CHECK(status IN ('pending', 'confirmed', 'cancelled', 'waitlisted')),
            stripe_session_id TEXT,
            stripe_payment_id TEXT,
            registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
            confirmed_at      TEXT,
            cancelled_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reg_class ON registrations(class_id);
        CREATE INDEX IF NOT EXISTS idx_reg_email ON registrations(email);
        CREATE INDEX IF NOT EXISTS idx_reg_student ON registrations(student_id);

        CREATE TABLE IF NOT EXISTS waivers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id      INTEGER NOT NULL REFERENCES students(id),
            registration_id INTEGER REFERENCES registrations(id),
            waiver_type     TEXT NOT NULL CHECK(waiver_type IN ('liability', 'model_release')),
            waiver_text     TEXT NOT NULL,
            signed_at       TEXT NOT NULL DEFAULT (datetime('now')),
            ip_address      TEXT,
            signature_text  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_waivers_student ON waivers(student_id);

        CREATE TABLE IF NOT EXISTS discount_codes (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            code                 TEXT NOT NULL UNIQUE,
            description          TEXT,
            discount_pct         INTEGER,
            discount_fixed_cents INTEGER,
            valid_from           TEXT,
            valid_until          TEXT,
            max_uses             INTEGER,
            use_count            INTEGER NOT NULL DEFAULT 0,
            is_active            INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name  TEXT NOT NULL,
            row_id      INTEGER NOT NULL,
            action      TEXT NOT NULL,
            changed_by  TEXT,
            old_value   TEXT,
            new_value   TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_table_row ON audit_log(table_name, row_id);
    """)

    # ── Column migrations (safe to re-run) ────────────────────────────────────
    # These handle DBs created before a column was added to the schema above.

    column_migrations = [
        "ALTER TABLE classes ADD COLUMN image_url TEXT",
        "ALTER TABLE classes ADD COLUMN requires_model_release INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE categories ADD COLUMN image_url TEXT",
        "ALTER TABLE registrations ADD COLUMN student_id INTEGER REFERENCES students(id)",
        "ALTER TABLE registrations ADD COLUMN stripe_session_id TEXT",
        "ALTER TABLE registrations ADD COLUMN stripe_payment_id TEXT",
        "ALTER TABLE instructors ADD COLUMN slug TEXT",
        "ALTER TABLE instructors ADD COLUMN photo_url TEXT",
    ]
    for sql in column_migrations:
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists

    con.commit()
    con.close()
    print(f"✓ Database ready at {db_path}")


if __name__ == "__main__":
    migrate()
