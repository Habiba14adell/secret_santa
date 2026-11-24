import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timezone

from .db import db, write_transaction

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class RuleError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def valid_name(text):
    return 1 <= len(text) <= 100 and all(
        unicodedata.category(char) != "Cc" and char not in "\ufffe\uffff" for char in text
    )


def create_event(title, names):
    title = clean_name(title)
    names = [clean_name(line) for line in names.splitlines() if line.strip()]
    if not valid_name(title):
        raise RuleError("Enter an event name up to 100 characters.")
    if not 2 <= len(names) <= 500 or any(not valid_name(name) for name in names):
        raise RuleError("Enter 2 to 500 participant names, one per line, up to 100 characters each.")
    keys = [name.casefold() for name in names]
    if len(set(keys)) != len(keys):
        raise RuleError("Use a different name for each participant.")
    slug = secrets.token_urlsafe(18)
    with write_transaction() as connection:
        event_id = connection.execute(
            "INSERT INTO events (title, slug) VALUES (?, ?) RETURNING id", (title, slug)
        ).fetchone()["id"]
        connection.executemany(
            "INSERT INTO participants (event_id, name, name_key) VALUES (?, ?, ?)",
            [(event_id, name, key) for name, key in zip(names, keys)],
        )
    return slug


def roster(event_id):
    return db().execute(
        """SELECT p.id, p.name, p.registered_at, r.name AS recipient,
           CASE WHEN p.registered_at IS NULL THEN 'Not Joined' ELSE 'Joined' END AS status,
           CASE WHEN a.giver_id IS NULL THEN 'Pending'
                WHEN EXISTS (SELECT 1 FROM activity l WHERE l.event_id = p.event_id
                             AND l.participant_id = p.id AND l.action = 'Assignment revealed')
                THEN 'Revealed' ELSE 'Assigned' END AS assignment_status
           FROM participants p
           LEFT JOIN assignments a ON a.event_id = p.event_id AND a.giver_id = p.id
           LEFT JOIN participants r ON r.id = a.recipient_id
           WHERE p.event_id = ? ORDER BY p.id""",
        (event_id,),
    ).fetchall()


def log(connection, event_id, action, participant_id=None, status="Joined"):
    connection.execute(
        """INSERT INTO activity (event_id, participant_id, action, status, occurred_at)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, participant_id, action, status, now()),
    )


def activity(event_id):
    return db().execute(
        """SELECT l.occurred_at, p.name, l.action, l.status FROM activity l
           LEFT JOIN participants p ON p.id = l.participant_id
           WHERE l.event_id = ? ORDER BY l.id DESC""",
        (event_id,),
    ).fetchall()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def identify(event_id, token):
    if not TOKEN_PATTERN.fullmatch(token):
        return None
    return db().execute(
        "SELECT id, name FROM participants WHERE event_id = ? AND token_hash = ?",
        (event_id, digest(token)),
    ).fetchone()


def register(event_id, name):
    name = clean_name(name)
    message = "Unable to join with that name. Check the spelling or use your private link if you already joined."
    if not valid_name(name):
        raise RuleError(message)
    with write_transaction() as connection:
        person = connection.execute(
            "SELECT id, registered_at FROM participants WHERE event_id = ? AND name_key = ?",
            (event_id, name.casefold()),
        ).fetchone()
        if not person or person["registered_at"]:
            raise RuleError(message)
        token = secrets.token_urlsafe(32)
        connection.execute(
            "UPDATE participants SET registered_at = ?, token_hash = ? WHERE id = ?",
            (now(), digest(token), person["id"]),
        )
        log(connection, event_id, "Participant registered", person["id"])
    return token


def generate(event_id):
    with write_transaction() as connection:
        event = connection.execute("SELECT generated_at FROM events WHERE id = ?", (event_id,)).fetchone()
        if event["generated_at"]:
            raise RuleError("Assignments have already been generated.")
        people = roster(event_id)
        if len(people) < 2 or any(not person["registered_at"] for person in people):
            raise RuleError("Everyone on the list must join before you can draw names.")
        givers = [person["id"] for person in people]
        recipients = givers.copy()
        random = secrets.SystemRandom()
        # Rejection sampling gives each valid derangement the same probability.
        while True:
            random.shuffle(recipients)
            if all(giver != recipient for giver, recipient in zip(givers, recipients)):
                break
        connection.executemany(
            "INSERT INTO assignments VALUES (?, ?, ?)",
            [(event_id, giver, recipient) for giver, recipient in zip(givers, recipients)],
        )
        connection.execute("UPDATE events SET generated_at = ? WHERE id = ?", (now(), event_id))
        log(connection, event_id, "Assignments generated", status="Draw complete")


def reveal(event_id, person_id):
    with write_transaction() as connection:
        recipient = connection.execute(
            """SELECT p.name FROM assignments a JOIN participants p ON p.id = a.recipient_id
               WHERE a.event_id = ? AND a.giver_id = ?""",
            (event_id, person_id),
        ).fetchone()
        if recipient:
            log(connection, event_id, "Assignment revealed", person_id)
            return recipient["name"]
    return None
