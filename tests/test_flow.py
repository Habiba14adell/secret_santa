import os
import re
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from santa import create_app
from santa.db import db


PASSWORD = "a test-only organizer password"
NAMES = ("Alice Frost", "Benny Snow", "=Clara")


class ExchangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash(PASSWORD)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        postgres_url = os.getenv("TEST_DATABASE_URL", "")
        if postgres_url:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import make_conninfo

            schema_name = "santa_test_" + uuid.uuid4().hex
            schema = sql.Identifier(schema_name)
            connection = psycopg.connect(postgres_url, autocommit=True)
            self.addCleanup(connection.close)
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            self.addCleanup(connection.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
            postgres_url = make_conninfo(postgres_url, options=f"-c search_path={schema_name}")
        self.config = {
            "TESTING": True, "SECRET_KEY": "test-only-secret-key-for-session-signing",
            "ADMIN_PASSWORD_HASH": self.password_hash, "SESSION_COOKIE_SECURE": True,
            "DATABASE": str(Path(folder.name) / "test.sqlite3"), "DATABASE_URL": postgres_url,
            "PUBLIC_BASE_URL": "https://exchange.example",
            "TRUSTED_HOSTS": ["exchange.example"],
        }
        self.app = create_app(self.config)
        self.admin = self.app.test_client()
        self.assertEqual(self.post(self.admin, "/admin/login", password=PASSWORD).status_code, 303)
        response = self.post(self.admin, "/admin", title="Test exchange", names="\n".join(NAMES))
        self.assertEqual(response.status_code, 303)
        self.dashboard = response.location
        self.join = "/join/" + self.dashboard.rsplit("/", 1)[1]
        self.generate = self.dashboard + "/generate"

    def get(self, client, path):
        return client.get(path, base_url="https://exchange.example")

    def post(self, client, path, csrf_page=None, **fields):
        page = self.get(client, csrf_page or path).get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        return client.post(path, base_url="https://exchange.example", data={"csrf": csrf, **fields})

    def register(self, client, name):
        return self.post(client, self.join, action="register", name=name)

    def token(self, client):
        page = self.get(client, self.join).get_data(as_text=True)
        return re.search(r'id="private" value="([^"]+)"', page)[1]

    def mappings(self):
        with self.app.app_context():
            return {
                row["giver"]: row["recipient"] for row in db().execute("""
                    SELECT g.name AS giver, r.name AS recipient
                    FROM assignments a JOIN participants g ON g.id = a.giver_id
                    JOIN participants r ON r.id = a.recipient_id
                """)
            }

    def test_registration_and_concurrent_duplicate(self):
        self.assertEqual(self.register(self.app.test_client(), "Unknown").status_code, 400)
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(
                lambda name: self.register(self.app.test_client(), name).status_code,
                ["Alice Frost", "  alice   FROST "],
            ))
        self.assertEqual(sorted(statuses), [303, 400])
        self.assertIn("1 of 3", self.get(self.admin, self.dashboard).get_data(as_text=True))
        self.assertEqual(self.post(self.admin, self.generate, self.dashboard).status_code, 409)
        self.assertEqual(self.mappings(), {})

    def test_draw_return_privacy_and_excel(self):
        clients = [self.app.test_client() for _ in NAMES]
        for client, name in zip(clients, NAMES):
            self.assertEqual(self.register(client, name).status_code, 303)
        alice = clients[0]
        token = self.token(alice)
        self.assertEqual(self.register(alice, NAMES[0]).status_code, 303)
        self.assertEqual(self.token(alice), token)
        self.assertEqual(self.mappings(), {})
        self.assertEqual(self.post(self.admin, self.generate, self.dashboard).status_code, 303)
        mapping = self.mappings()
        self.assertEqual(set(mapping), set(NAMES))
        self.assertEqual(set(mapping.values()), set(NAMES))
        self.assertTrue(all(giver != recipient for giver, recipient in mapping.items()))
        self.assertEqual(self.post(self.admin, self.generate, self.dashboard).status_code, 409)
        self.assertEqual(self.mappings(), mapping)

        restarted = create_app(self.config)
        stranger = restarted.test_client()
        public = self.get(stranger, self.join)
        self.assertTrue(all(name not in public.get_data(as_text=True) for name in NAMES))
        self.assertEqual(self.register(stranger, NAMES[0]).status_code, 400)
        self.assertEqual(self.get(stranger, self.dashboard).status_code, 302)
        self.assertEqual(self.get(stranger, self.dashboard + "/export").status_code, 302)
        self.assertEqual(self.post(stranger, self.join, action="reveal").status_code, 400)
        self.assertEqual(self.post(stranger, self.join, action="restore", token="x" * 43).status_code, 403)
        self.assertEqual(self.post(stranger, self.join, action="restore", token=token).status_code, 303)
        for client in (alice, stranger):
            response = self.post(client, self.join, action="reveal")
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertIn(mapping[NAMES[0]], page)
            other = set(NAMES) - {NAMES[0], mapping[NAMES[0]]}
            self.assertTrue(all(name not in page for name in other))
        with self.app.app_context():
            stored = db().execute("SELECT token_hash FROM participants WHERE name = ?", (NAMES[0],)).fetchone()
            self.assertEqual(len(stored["token_hash"]), 64)
            self.assertNotEqual(stored["token_hash"], token)

        download = self.get(self.admin, self.dashboard + "/export")
        self.assertEqual(download.status_code, 200)
        sheets = load_workbook(BytesIO(download.data))
        self.assertEqual(sheets.sheetnames, ["Participants", "Assignments", "Activity"])
        self.assertEqual(dict((row[0], row[1]) for row in list(sheets["Assignments"].values)[1:]), mapping)
        self.assertEqual(sheets["Participants"]["A4"].data_type, "s")
        self.assertEqual(sheets["Participants"]["A4"].value, NAMES[2])
        actions = [row[2] for row in list(sheets["Activity"].values)[1:]]
        self.assertEqual(actions.count("Participant registered"), 3)
        self.assertEqual(actions.count("Assignments generated"), 1)
        self.assertEqual(actions.count("Assignment revealed"), 2)
        self.assertIn("Participant returned", actions)

    def test_admin_authentication_and_csrf(self):
        client = self.app.test_client()
        self.assertEqual(self.post(client, "/admin/login", password="wrong").status_code, 401)
        self.assertEqual(self.get(client, "/admin").status_code, 302)
        self.assertEqual(client.post(self.join, base_url="https://exchange.example", data={
            "action": "register", "name": NAMES[0],
        }).status_code, 400)
        invite = "https://exchange.example" + self.join
        self.assertIn(invite, self.get(self.admin, self.dashboard).get_data(as_text=True))
        self.assertEqual(self.get(self.admin, "/healthz").status_code, 200)
