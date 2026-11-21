"""Isolated session model and migration tests."""

from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
import unittest
from uuid import UUID, uuid4

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as ORMSession

from app.db.base import Base
from app.models import Session, User

BACKEND = Path(__file__).resolve().parents[1]


def migration():
    spec = spec_from_file_location(
        "sessions_migration", BACKEND / "alembic/versions/0004_create_sessions_table.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionModelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.db = ORMSession(self.engine)
        self.addCleanup(self.db.close)
        user = User(email="session@example.test", password_hash="unused")
        self.db.add(user)
        self.db.commit()
        self.user_id = user.id

    def row(self, **overrides):
        values = dict(user_id=self.user_id, token_hash="a" * 64,
                      expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        values.update(overrides)
        return Session(**values)

    def test_defaults_and_revocation(self):
        first, second = self.row(), self.row(token_hash="b" * 64)
        self.db.add_all([first, second])
        self.db.commit()
        self.assertIsInstance(first.id, UUID)
        self.assertEqual(first.id.version, 4)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNotNone(first.created_at)
        self.assertIsNone(first.revoked_at)
        self.assertEqual(first.user_id, self.user_id)
        revoked = datetime.now(timezone.utc)
        first.revoked_at = revoked
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(first.revoked_at.replace(tzinfo=timezone.utc), revoked)

    def test_constraints(self):
        self.db.add(self.row())
        self.db.commit()
        for overrides in ({}, {"user_id": uuid4(), "token_hash": "b" * 64},
                          {"user_id": None, "token_hash": "c" * 64},
                          {"token_hash": None}, {"expires_at": None, "token_hash": "d" * 64}):
            with self.subTest(overrides=overrides):
                self.db.add(self.row(**overrides))
                with self.assertRaises(IntegrityError):
                    self.db.commit()
                self.db.rollback()

    def test_metadata(self):
        table = Session.__table__
        self.assertIs(table.metadata, Base.metadata)
        self.assertEqual(table.c.token_hash.type.length, 64)
        for name in ("created_at", "expires_at", "revoked_at"):
            self.assertTrue(table.c[name].type.timezone)
        self.assertEqual(str(table.c.created_at.server_default.arg), "now()")
        self.assertEqual({(i.name, i.unique) for i in table.indexes},
                         {("ix_sessions_token_hash", True), ("ix_sessions_user_id", False)})


class SessionMigrationTests(unittest.TestCase):
    def test_revision_chain(self):
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "alembic"))
        scripts = ScriptDirectory.from_config(config)
        self.assertEqual(scripts.get_current_head(), "0004")
        self.assertEqual(scripts.get_revision("0004").down_revision, "0003")

    def test_upgrade_and_downgrade(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            User.__table__.create(connection)
            with Operations.context(MigrationContext.configure(connection)):
                migration().upgrade()
            schema = inspect(connection)
            columns = {c["name"]: c for c in schema.get_columns("sessions")}
            self.assertEqual(set(columns), set(Session.__table__.columns.keys()))
            for column in Session.__table__.columns:
                self.assertEqual(columns[column.name]["nullable"], column.nullable)
                self.assertEqual(str(columns[column.name]["type"]),
                                 str(column.type.compile(dialect=connection.dialect)))
            self.assertEqual(schema.get_pk_constraint("sessions")["constrained_columns"], ["id"])
            fk = schema.get_foreign_keys("sessions")[0]
            self.assertEqual((fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]),
                             (["user_id"], "users", ["id"]))
            self.assertEqual({(i["name"], tuple(i["column_names"]), bool(i["unique"]))
                              for i in schema.get_indexes("sessions")},
                             {(i.name, tuple(c.name for c in i.columns), i.unique)
                              for i in Session.__table__.indexes})
            self.assertEqual(columns["created_at"]["default"], "now()")
            with Operations.context(MigrationContext.configure(connection)):
                migration().downgrade()
            self.assertEqual(inspect(connection).get_table_names(), ["users"])

    def test_postgresql_ddl(self):
        output = StringIO()
        context = MigrationContext.configure(dialect_name="postgresql", opts={
            "as_sql": True, "output_buffer": output,
        })
        with Operations.context(context):
            migration().upgrade()
        sql = output.getvalue()
        self.assertEqual(sql.count("TIMESTAMP WITH TIME ZONE"), 3)
        self.assertIn("created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL", sql)
        self.assertIn("id UUID NOT NULL", sql)
        self.assertIn("CREATE UNIQUE INDEX ix_sessions_token_hash", sql)
