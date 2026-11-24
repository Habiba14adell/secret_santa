import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, abort, render_template, request, session

from .db import close_db, migrate


def create_app(config=None):
    load_dotenv()
    app = Flask(__name__)
    public_url = (os.getenv("SANTA_PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
    hosts = os.getenv("SANTA_TRUSTED_HOSTS", "localhost,127.0.0.1").split(",")
    if public_url:
        origin = urlsplit(public_url)
        if (
            origin.scheme not in {"http", "https"} or not origin.hostname
            or origin.path or origin.query or origin.fragment or origin.username is not None
        ):
            raise RuntimeError("Set SANTA_PUBLIC_BASE_URL to the public origin only.")
        hosts.append(origin.hostname)
    app.config.from_mapping(
        DATABASE_URL=os.getenv("DATABASE_URL", ""),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        SESSION_REFRESH_EACH_REQUEST=False,
        SECRET_KEY=os.getenv("SANTA_SECRET_KEY"),
        ADMIN_PASSWORD_HASH=os.getenv("SANTA_ADMIN_PASSWORD_HASH"),
        DATABASE=os.getenv("SANTA_DATABASE", str(Path(app.instance_path) / "santa.sqlite3")),
        PUBLIC_BASE_URL=public_url,
        TRUSTED_HOSTS=[host.strip() for host in hosts if host.strip()],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SANTA_SECURE_COOKIES", "1") == "1",
        MAX_CONTENT_LENGTH=300_000,
        MAX_FORM_MEMORY_SIZE=300_000,
    )
    if config:
        app.config.update(config)
    if len(app.config["SECRET_KEY"] or "") < 32 or not app.config["ADMIN_PASSWORD_HASH"]:
        raise RuntimeError("Set SANTA_SECRET_KEY (at least 32 characters) and SANTA_ADMIN_PASSWORD_HASH.")
    if os.getenv("RENDER") and not app.config["DATABASE_URL"]:
        raise RuntimeError("Render deployments require DATABASE_URL for persistent storage.")

    @app.cli.command("migrate")
    def migrate_command():
        """Apply pending database migrations."""
        migrate()

    app.teardown_appcontext(close_db)
    with app.app_context():
        if not app.config["DATABASE_URL"]:
            Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
        migrate()

    from .views import pages
    app.register_blueprint(pages)

    @app.context_processor
    def inject_helpers():
        def csrf_token():
            if "csrf" not in session:
                session["csrf"] = secrets.token_urlsafe(32)
            return session["csrf"]

        def date_label(value):
            return value[:16].replace("T", " ") + " UTC" if value else "—"

        return {"csrf_token": csrf_token, "date_label": date_label}

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.form.get("csrf", "")
            if not expected or not supplied.isascii() or not hmac.compare_digest(expected, supplied):
                abort(400, "Refresh the page and try again.")

    @app.after_request
    def protect_response(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error_page(error):
        messages = {
            400: "Refresh the page and try again.",
            404: "This page or invitation could not be found.",
            413: "That submission is too large.",
            500: "Something went wrong. Please try again.",
        }
        return render_template("error.html", message=messages[error.code]), error.code

    return app
