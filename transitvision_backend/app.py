from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, make_response, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
SEED_PATH = BASE_DIR / "stations_seed.json"
if os.environ.get("RENDER", "").lower() == "true" and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("Set SECRET_KEY in the Render environment before starting TransitVision.")
DEFAULT_DB = f"sqlite:///{(BASE_DIR / 'instance' / 'transitvision.sqlite3').as_posix()}"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DB)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-this-before-deployment"),
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true" or os.environ.get("RENDER", "") == "true",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    JSON_SORT_KEYS=False,
)
db = SQLAlchemy(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per day", "80 per hour"], storage_uri="memory://")
SESSION_HOURS = int(os.environ.get("LOGIN_SESSION_HOURS", "12"))


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Station(db.Model):
    __tablename__ = "stations"
    station_key = db.Column(db.String(80), primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    data_json = db.Column(db.JSON, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class AuthSession(db.Model):
    __tablename__ = "auth_sessions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    csrf_token = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class SavedRoute(db.Model):
    __tablename__ = "saved_routes"
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    station_key = db.Column(db.String(80))
    station_name = db.Column(db.String(160), nullable=False)
    platform = db.Column(db.String(120))
    feature_title = db.Column(db.String(240), nullable=False)
    route_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint("user_id", "station_name", "feature_title", name="uq_saved_user_station_feature"),)


class SearchHistory(db.Model):
    __tablename__ = "search_history"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    station_name = db.Column(db.String(160), nullable=False)
    feature_title = db.Column(db.String(240), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_csrf() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify(error="Security token missing or expired. Refresh the page and try again."), 403
        return fn(*args, **kwargs)
    return wrapped


def current_auth_session():
    raw = request.cookies.get("tv_session", "")
    if not raw or len(raw) > 256:
        return None
    record = AuthSession.query.filter_by(token_hash=token_digest(raw)).first()
    if not record:
        return None
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= utc_now():
        db.session.delete(record)
        db.session.commit()
        return None
    return record


def current_user():
    auth = current_auth_session()
    return db.session.get(User, auth.user_id) if auth else None


def require_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify(error="Sign in to access your personal routes and history.", code="login_required"), 401
        return fn(user, *args, **kwargs)
    return wrapped


def validate_text(value, label, max_length):
    value = str(value or "").strip()
    if not value:
        return None, f"{label} is required."
    if len(value) > max_length:
        return None, f"{label} must be {max_length} characters or fewer."
    return value, None


def user_payload(user):
    return {"id": user.id, "name": user.name, "email": user.email}


def create_login_session(user):
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    record = AuthSession(
        user_id=user.id,
        token_hash=token_digest(raw_token),
        csrf_token=csrf_token,
        expires_at=utc_now() + timedelta(hours=SESSION_HOURS),
    )
    db.session.add(record)
    db.session.commit()
    session.clear()
    session["csrf_token"] = csrf_token
    response = make_response(jsonify(user=user_payload(user), csrfToken=csrf_token, expiresInSeconds=SESSION_HOURS * 3600))
    response.set_cookie(
        "tv_session", raw_token, max_age=SESSION_HOURS * 3600,
        httponly=True, secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax", path="/",
    )
    return response


@app.get("/")
def home():
    ensure_csrf()
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", service="TransitVision API", database="configured")


@app.get("/api/stations")
def get_stations():
    rows = Station.query.order_by(Station.sort_order).all()
    return jsonify({row.station_key: row.data_json for row in rows})


@app.get("/api/stations/<station_key>")
def station_detail(station_key):
    row = db.session.get(Station, station_key.upper())
    if not row:
        return jsonify(error="Station not found."), 404
    return jsonify(row.data_json)


@app.get("/api/auth/me")
def auth_me():
    csrf_token = ensure_csrf()
    auth = current_auth_session()
    if not auth:
        return jsonify(user=None, csrfToken=csrf_token)
    # Keep the CSRF token tied to the current authenticated server-side session.
    session["csrf_token"] = auth.csrf_token
    return jsonify(user=user_payload(db.session.get(User, auth.user_id)), csrfToken=auth.csrf_token,
                   expiresAt=auth.expires_at.isoformat())


@app.post("/api/auth/register")
@csrf_protected
@limiter.limit("5 per hour")
def register():
    payload = request.get_json(silent=True) or {}
    name, err = validate_text(payload.get("name"), "Name", 80)
    if err: return jsonify(error=err), 400
    email, err = validate_text(payload.get("email"), "Email", 254)
    if err: return jsonify(error=err), 400
    email = email.lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or "." not in email.rsplit("@", 1)[-1]:
        return jsonify(error="Enter a valid email address."), 400
    password = str(payload.get("password") or "")
    if len(password) < 8 or len(password) > 128:
        return jsonify(error="Password must be between 8 and 128 characters."), 400
    if User.query.filter_by(email=email).first():
        return jsonify(error="An account with this email already exists. Please sign in."), 409
    user = User(name=name, email=email, password_hash=generate_password_hash(password, method="scrypt"))
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(error="Could not create the account. Please try again."), 500
    return create_login_session(user), 201


@app.post("/api/auth/login")
@csrf_protected
@limiter.limit("10 per minute")
def login():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    user = User.query.filter_by(email=email).first() if email else None
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(error="Email or password is incorrect."), 401
    # Invalidate any previous active session for this browser cookie, then issue a fresh one.
    old = current_auth_session()
    if old:
        db.session.delete(old)
        db.session.commit()
    return create_login_session(user)


@app.post("/api/auth/logout")
@csrf_protected
def logout():
    auth = current_auth_session()
    if auth:
        db.session.delete(auth)
        db.session.commit()
    session.clear()
    response = make_response(jsonify(status="ok"))
    response.delete_cookie("tv_session", path="/", httponly=True, secure=app.config["SESSION_COOKIE_SECURE"], samesite="Lax")
    return response


@app.get("/api/profile")
@require_auth
def profile(user):
    history_rows = db.session.query(SearchHistory.station_name, func.count(SearchHistory.id).label("uses"))\
        .filter_by(user_id=user.id).group_by(SearchHistory.station_name).order_by(func.count(SearchHistory.id).desc()).limit(5).all()
    return jsonify(user=user_payload(user), frequentlyUsedStations=[{"stationName": r.station_name, "uses": r.uses} for r in history_rows])


@app.get("/api/saved")
@require_auth
def get_saved(user):
    rows = SavedRoute.query.filter_by(user_id=user.id).order_by(SavedRoute.created_at.desc()).all()
    return jsonify(items=[{"id": r.id, "stationKey": r.station_key, "stationName": r.station_name,
                           "platform": r.platform, "featureTitle": r.feature_title,
                           "route": r.route_json, "createdAt": r.created_at.isoformat()} for r in rows])


@app.post("/api/saved")
@csrf_protected
@require_auth
def add_saved(user):
    payload = request.get_json(silent=True) or {}
    station_name, err = validate_text(payload.get("stationName"), "Station", 160)
    if err: return jsonify(error=err), 400
    feature_title, err = validate_text(payload.get("featureTitle"), "Feature", 240)
    if err: return jsonify(error=err), 400
    existing = SavedRoute.query.filter_by(user_id=user.id, station_name=station_name, feature_title=feature_title).first()
    if existing:
        return jsonify(message="This route is already saved.", id=existing.id, duplicate=True)
    row = SavedRoute(id=secrets.token_hex(16), user_id=user.id, station_key=str(payload.get("stationKey") or "")[:80] or None,
                     station_name=station_name, platform=str(payload.get("platform") or "")[:120] or None,
                     feature_title=feature_title, route_json=payload)
    db.session.add(row)
    db.session.commit()
    return jsonify(message="Route saved successfully.", id=row.id), 201


@app.delete("/api/saved")
@csrf_protected
@require_auth
def delete_saved_all(user):
    SavedRoute.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(status="ok")


@app.delete("/api/saved/<route_id>")
@csrf_protected
@require_auth
def delete_saved(user, route_id):
    row = SavedRoute.query.filter_by(id=route_id, user_id=user.id).first()
    if not row: return jsonify(error="Saved route not found."), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify(status="ok")


@app.get("/api/history")
@require_auth
def get_history(user):
    rows = SearchHistory.query.filter_by(user_id=user.id).order_by(SearchHistory.id.desc()).limit(10).all()
    return jsonify(items=[{"id": r.id, "stationName": r.station_name, "featureTitle": r.feature_title,
                           "timestamp": r.created_at.astimezone().strftime("%I:%M %p"), "createdAt": r.created_at.isoformat()} for r in rows])


@app.post("/api/history")
@csrf_protected
@require_auth
def add_history(user):
    payload = request.get_json(silent=True) or {}
    station_name, err = validate_text(payload.get("stationName"), "Station", 160)
    if err: return jsonify(error=err), 400
    feature_title, err = validate_text(payload.get("featureTitle"), "Feature", 240)
    if err: return jsonify(error=err), 400
    db.session.add(SearchHistory(user_id=user.id, station_name=station_name, feature_title=feature_title))
    db.session.commit()
    return jsonify(status="ok"), 201


@app.delete("/api/history")
@csrf_protected
@require_auth
def delete_history(user):
    SearchHistory.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(status="ok")


@app.errorhandler(429)
def rate_limited(_error):
    return jsonify(error="Too many attempts. Please wait a little before trying again."), 429


@app.errorhandler(500)
def internal_error(_error):
    db.session.rollback()
    return jsonify(error="Unexpected server error. Please retry."), 500


def initialize_database():
    with app.app_context():
        # The previous prototype stored routes/history by browser ID, not by account.
        # Drop those legacy tables rather than incorrectly assigning that data to a new user.
        inspector = inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        with db.engine.begin() as connection:
            for table_name in ("saved_routes", "search_history"):
                if table_name in existing_tables:
                    columns = {column["name"] for column in inspect(db.engine).get_columns(table_name)}
                    if "user_id" not in columns:
                        connection.execute(text(f"DROP TABLE {table_name}"))
            if "stations" in existing_tables:
                station_columns = {column["name"] for column in inspect(db.engine).get_columns("stations")}
                if "sort_order" not in station_columns:
                    connection.execute(text("ALTER TABLE stations ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"))
        db.create_all()
        if not SEED_PATH.exists():
            raise RuntimeError(f"Station seed file missing: {SEED_PATH}")
        seed_data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        if Station.query.count() == 0:
            for sort_order, (key, station) in enumerate(seed_data.items()):
                db.session.add(Station(station_key=key, name=station.get("name", key), lat=station.get("lat"),
                                       lon=station.get("lon"), data_json=station, sort_order=sort_order, updated_at=utc_now()))
            db.session.commit()
        else:
            # Keep existing station content untouched, but restore the supplied dropdown order.
            for sort_order, key in enumerate(seed_data.keys()):
                row = db.session.get(Station, key)
                if row:
                    row.sort_order = sort_order
            db.session.commit()
        # Expired sessions are cleaned during startup as well as when used.
        AuthSession.query.filter(AuthSession.expires_at < utc_now()).delete(synchronize_session=False)
        db.session.commit()


initialize_database()

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
