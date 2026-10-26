import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app import app, db, Station, User, SavedRoute, SearchHistory, AuthSession, SEED_PATH


@pytest.fixture(autouse=True)
def clean_database():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
    with app.app_context():
        db.drop_all()
        db.create_all()
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for order, (key, station) in enumerate(seed.items()):
            db.session.add(Station(station_key=key, name=station.get("name", key), lat=station.get("lat"),
                                   lon=station.get("lon"), data_json=station, sort_order=order))
        db.session.commit()
    yield
    with app.app_context():
        db.session.remove()
        db.drop_all()


def csrf(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    return response.get_json()["csrfToken"]


def register(client, name="Passenger One", email="one@example.com", password="StrongPass123"):
    token = csrf(client)
    return client.post("/api/auth/register", json={"name": name, "email": email, "password": password},
                       headers={"X-CSRF-Token": token})


def test_guest_can_browse_but_cannot_read_or_write_personal_data():
    client = app.test_client()
    assert client.get("/api/stations").status_code == 200
    assert len(client.get("/api/stations").get_json()) == 9
    assert client.get("/api/saved").status_code == 401
    assert client.get("/api/history").status_code == 401
    token = csrf(client)
    response = client.post("/api/history", json={"stationName": "Airoli", "featureTitle": "Platform"},
                           headers={"X-CSRF-Token": token})
    assert response.status_code == 401
    with app.app_context():
        assert SearchHistory.query.count() == 0


def test_registration_login_and_logout():
    client = app.test_client()
    response = register(client)
    assert response.status_code == 201
    assert response.get_json()["user"]["email"] == "one@example.com"
    assert client.get("/api/profile").status_code == 200

    logout_token = response.get_json()["csrfToken"]
    logout = client.post("/api/auth/logout", json={}, headers={"X-CSRF-Token": logout_token})
    assert logout.status_code == 200
    assert client.get("/api/profile").status_code == 401

    login_token = csrf(client)
    login = client.post("/api/auth/login", json={"email": "one@example.com", "password": "StrongPass123"},
                        headers={"X-CSRF-Token": login_token})
    assert login.status_code == 200
    assert client.get("/api/profile").status_code == 200


def test_passwords_are_hashed_and_duplicate_email_is_rejected():
    client = app.test_client()
    assert register(client).status_code == 201
    with app.app_context():
        user = User.query.filter_by(email="one@example.com").one()
        assert user.password_hash != "StrongPass123"
        assert "StrongPass123" not in user.password_hash
    other = app.test_client()
    duplicate = register(other, name="Second Person", email="ONE@example.com")
    assert duplicate.status_code == 409


def test_saved_routes_and_history_are_isolated_per_account():
    first = app.test_client()
    second = app.test_client()
    assert register(first, "First", "first@example.com").status_code == 201
    assert register(second, "Second", "second@example.com").status_code == 201

    first_me = first.get("/api/auth/me").get_json()
    first_token = first_me["csrfToken"]
    saved = first.post("/api/saved", json={
        "stationKey": "AIROLI", "stationName": "Airoli Railway Station",
        "platform": "Platform 1", "featureTitle": "Exit navigation", "steps": ["Follow signs"]
    }, headers={"X-CSRF-Token": first_token})
    assert saved.status_code == 201

    history = first.post("/api/history", json={"stationName": "Airoli Railway Station", "featureTitle": "Exit navigation"},
                         headers={"X-CSRF-Token": first_token})
    assert history.status_code == 201
    assert len(first.get("/api/saved").get_json()["items"]) == 1
    assert len(first.get("/api/history").get_json()["items"]) == 1
    assert second.get("/api/saved").get_json()["items"] == []
    assert second.get("/api/history").get_json()["items"] == []


def test_csrf_required_for_mutations():
    client = app.test_client()
    assert register(client).status_code == 201
    response = client.post("/api/history", json={"stationName": "Airoli", "featureTitle": "Platform"})
    assert response.status_code == 403


def test_invalid_password_and_short_password_are_rejected():
    client = app.test_client()
    response = register(client, password="short")
    assert response.status_code == 400
    assert register(client).status_code == 201
    token = csrf(client)
    bad_login = client.post("/api/auth/login", json={"email": "one@example.com", "password": "wrong-password"},
                            headers={"X-CSRF-Token": token})
    assert bad_login.status_code == 401
