import pytest
from fastapi.testclient import TestClient

from mock_bank.app import DEMO_PASSWORD, DEMO_USER, create_app


def signed_in(variant: str = "pinnacle") -> TestClient:
    client = TestClient(create_app(variant))
    r = client.post("/login", data={"userid": DEMO_USER, "passwd": DEMO_PASSWORD})
    assert r.status_code == 200 and "<frameset" in r.text
    return client


@pytest.fixture
def client() -> TestClient:
    return signed_in()


def search(client: TestClient, mbrno: str):  # type: ignore[no-untyped-def]
    return client.post("/inquiry", data={"mbrno": mbrno})


def test_content_requires_session() -> None:
    client = TestClient(create_app())
    assert "Teller Sign On" in client.get("/main.html").text
    assert "Your session has expired" in client.get("/inquiry").text


def test_bad_login_is_rejected() -> None:
    r = TestClient(create_app()).post("/login", data={"userid": DEMO_USER, "passwd": "wrong"})
    assert r.status_code == 401 and "Invalid user ID or password" in r.text


def test_member_lookup_happy_path(client: TestClient) -> None:
    r = search(client, "12345")
    assert "Member Summary" in r.text and "Jane Q. Sample" in r.text
    assert "Share Savings" in r.text and "$4,210.37" in r.text


@pytest.mark.parametrize(
    ("mbrno", "status", "text"),
    [
        ("99999", 200, "No member found for member number 99999"),
        ("12a", 422, "Invalid member number"),
        ("34567", 403, "Access denied"),
        ("23456", 200, "MEMBER ALERT"),
    ],
)
def test_member_lookup_runtime_conditions(
    client: TestClient, mbrno: str, status: int, text: str
) -> None:
    r = search(client, mbrno)
    assert r.status_code == status and text in r.text
    assert "Member Summary" not in r.text


def test_alert_acknowledge_continues_to_detail(client: TestClient) -> None:
    r = client.post("/inquiry/ack", data={"mbrno": "23456"})
    assert "Member Summary" in r.text and "Robert T. Example" in r.text


def test_open_sub_account_flow_is_single_use(client: TestClient) -> None:
    form = {"m": "12345", "accttype": "Money Market", "nick": "Rainy day", "initdep": "25"}
    assert "at least $5.00" in client.post("/subacct/review", data={**form, "initdep": "1"}).text

    review = client.post("/subacct/review", data=form)
    assert "Review Sub-Account" in review.text
    token = review.text.split('name="token" value="')[1].split('"')[0]

    done = client.post("/subacct/confirm", data={"token": token})
    assert "Sub-account opened successfully" in done.text and "SA-100231" in done.text
    assert "Money Market" in search(client, "12345").text

    replayed = client.post("/subacct/confirm", data={"token": token})
    assert replayed.status_code == 409 and "already been processed" in replayed.text


def test_injected_faults(client: TestClient) -> None:
    client.put("/__admin/faults", json={
        "transient_failures": 1, "fatal_errors": 1, "broadcast_notices": 1,
    })
    assert search(client, "12345").status_code == 503
    assert "SYSTEM NOTICE" in search(client, "12345").text
    assert "SYS-0042" in search(client, "12345").text
    assert "Member Summary" in search(client, "12345").text

    client.post("/__admin/expire-sessions")
    assert "Your session has expired" in search(client, "12345").text


def test_variants_share_flow_but_differ_in_presentation() -> None:
    pinnacle = search(signed_in("pinnacle"), "12345").text
    riverbend = search(signed_in("riverbend"), "12345").text
    for text in (pinnacle, riverbend):
        assert "Member Summary" in text and "$4,210.37" in text
    assert "<th>Balance</th>" in pinnacle
    assert "<th>Current Bal.</th>" in riverbend and "<th>Available</th>" in riverbend
    assert "Member #" in signed_in("riverbend").get("/inquiry").text
    assert "Pinnacle" in signed_in("pinnacle").get("/banner").text
