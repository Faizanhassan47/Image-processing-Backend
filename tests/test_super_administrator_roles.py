from uuid import uuid4

from fastapi.testclient import TestClient

from core.database import connect
from core.security import create_access_token
from main import app


client = TestClient(app)


def headers(username, role):
    return {"Authorization": f"Bearer {create_access_token({'sub': username, 'role': role})}"}


def test_exact_bootstrap_users_are_super_administrators():
    with connect() as db:
        rows = db.execute("SELECT username,role FROM users WHERE role='Super Administrator' ORDER BY id").fetchall()
    assert [(row["username"], row["role"]) for row in rows] == [
        ("ETDDev", "Super Administrator"),
        ("ETDadmin", "Super Administrator"),
    ]


def test_system_administrator_cannot_create_an_administrator():
    username = f"denied_admin_{uuid4().hex[:8]}"
    response = client.post("/api/users", headers=headers("Admin", "System Administrator"), json={
        "username": username, "display_name": "Denied Admin", "password": "ValidPass123!", "role": "System Administrator",
    })
    assert response.status_code == 403
    with connect() as db:
        assert db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() is None


def test_system_administrator_can_create_and_delete_pathologist():
    username = f"managed_doc_{uuid4().hex[:8]}"
    response = client.post("/api/users", headers=headers("Admin", "System Administrator"), json={
        "username": username, "display_name": "Managed Pathologist", "password": "ValidPass123!", "role": "Pathologist",
    })
    assert response.status_code == 200
    user_id = response.json()["id"]
    deactivated = client.patch(f"/api/users/{user_id}/status", headers=headers("Admin", "System Administrator"), json={"is_active": False})
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    activated = client.patch(f"/api/users/{user_id}/status", headers=headers("Admin", "System Administrator"), json={"is_active": True})
    assert activated.status_code == 200
    deleted = client.delete(f"/api/users/{user_id}", headers=headers("Admin", "System Administrator"))
    assert deleted.status_code == 200


def test_system_administrator_cannot_list_protected_super_administrators():
    response = client.get("/api/users", headers=headers("Admin", "System Administrator"))
    assert response.status_code == 200
    usernames = {account["username"].casefold() for account in response.json()}
    assert "etddev" not in usernames
    assert "etdadmin" not in usernames
    assert all(account["role"] != "Super Administrator" for account in response.json())


def test_super_administrator_can_create_and_delete_system_administrator():
    username = f"managed_admin_{uuid4().hex[:8]}"
    response = client.post("/api/users", headers=headers("ETDDev", "Super Administrator"), json={
        "username": username, "display_name": "Managed Administrator", "password": "ValidPass123!", "role": "System Administrator",
    })
    assert response.status_code == 200
    user_id = response.json()["id"]
    denied = client.delete(f"/api/users/{user_id}", headers=headers("Admin", "System Administrator"))
    assert denied.status_code == 403
    deleted = client.delete(f"/api/users/{user_id}", headers=headers("ETDDev", "Super Administrator"))
    assert deleted.status_code == 200


def test_third_super_administrator_is_rejected():
    username = f"third_super_{uuid4().hex[:8]}"
    response = client.post("/api/users", headers=headers("ETDDev", "Super Administrator"), json={
        "username": username, "display_name": "Third Super", "password": "ValidPass123!", "role": "Super Administrator",
    })
    assert response.status_code == 409
    with connect() as db:
        assert db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() is None
