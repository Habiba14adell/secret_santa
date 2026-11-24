from functools import wraps

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, session, url_for,
)
from werkzeug.security import check_password_hash

from . import service
from .db import db, write_transaction

pages = Blueprint("pages", __name__)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("pages.login"))
        return view(*args, **kwargs)
    return wrapped


def get_event(slug):
    event = db().execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
    if not event:
        abort(404)
    return event


@pages.get("/healthz")
def health():
    db().execute("SELECT 1")
    return "ok"


@pages.get("/")
def home():
    return render_template("home.html")


@pages.route("/admin/login", methods=["GET", "POST"])
def login():
    if session.get("admin"):
        return redirect(url_for("pages.admin"))
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) <= 1024 and check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], password):
            session.clear()
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("pages.admin"), code=303)
        error = "That password did not match."
    return render_template("login.html", error=error), 401 if error else 200


@pages.post("/admin/logout")
@admin_required
def logout():
    session.clear()
    return redirect(url_for("pages.login"), code=303)


@pages.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    error = None
    if request.method == "POST":
        try:
            slug = service.create_event(request.form.get("title", ""), request.form.get("names", ""))
            return redirect(url_for("pages.dashboard", slug=slug), code=303)
        except service.RuleError as exc:
            error = str(exc)
    events = db().execute("SELECT * FROM events ORDER BY id DESC").fetchall()
    return render_template("admin.html", events=events, error=error), 400 if error else 200


@pages.get("/admin/events/<slug>")
@admin_required
def dashboard(slug):
    event = get_event(slug)
    people = service.roster(event["id"])
    joined = sum(bool(person["registered_at"]) for person in people)
    base = current_app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
    invite = base + url_for("pages.join", slug=slug)
    return render_template(
        "dashboard.html", event=event, people=people, joined=joined,
        invite=invite, logs=service.activity(event["id"]),
    )


@pages.post("/admin/events/<slug>/generate")
@admin_required
def generate(slug):
    event = get_event(slug)
    try:
        service.generate(event["id"])
        flash("The draw is complete.")
    except service.RuleError as exc:
        return render_template("error.html", message=str(exc), back=url_for("pages.dashboard", slug=slug)), 409
    return redirect(url_for("pages.dashboard", slug=slug), code=303)


@pages.get("/admin/events/<slug>/export")
@admin_required
def export(slug):
    from .export import workbook

    event = get_event(slug)
    return send_file(
        workbook(event["id"]), as_attachment=True, download_name="secret-santa.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@pages.route("/join/<slug>", methods=["GET", "POST"])
def join(slug):
    event = get_event(slug)
    cookie = "santa_" + slug
    token = request.cookies.get(cookie, "")
    person = service.identify(event["id"], token)
    error = recipient = None
    fresh = False
    status = 200
    if request.method == "POST":
        action = request.form.get("action")
        if action == "restore":
            candidate = request.form.get("token", "").strip()
            if service.identify(event["id"], candidate):
                return participant_redirect(slug, cookie, candidate)
            error, status = "That private access code is invalid.", 403
        elif action == "register":
            if person:
                return redirect(url_for("pages.join", slug=slug), code=303)
            try:
                token = service.register(event["id"], request.form.get("name", ""))
                return participant_redirect(slug, cookie, token, fresh=True)
            except service.RuleError as exc:
                error, status = str(exc), 400
        elif action == "reveal" and person:
            recipient = service.reveal(event["id"], person["id"])
            if recipient is None:
                error, status = "The organizer has yet to draw names.", 409
        else:
            abort(400)
    elif person:
        fresh = session.pop("fresh_participant", None) == slug
        if not fresh:
            with write_transaction() as connection:
                service.log(connection, event["id"], "Participant returned", person["id"])
    return render_template(
        "join.html", event=event, person=person, token=token if person else "",
        error=error, recipient=recipient, fresh=fresh,
    ), status


def participant_redirect(slug, cookie, token, fresh=False):
    if fresh:
        session["fresh_participant"] = slug
    path = url_for("pages.join", slug=slug)
    response = redirect(path, code=303)
    response.set_cookie(
        cookie, token, max_age=365 * 24 * 60 * 60, httponly=True, samesite="Lax",
        secure=current_app.config["SESSION_COOKIE_SECURE"], path=path,
    )
    return response
