"""Integration, end-to-end and security tests against a real Postgres."""
import base64
import uuid

import numpy as np
import pytest

from core import db, jobs, scenario
from wr import world

from .conftest import bearer

MANAGER, DISPATCHER, VIEWER, OTHER = bearer("city", "ops_manager"), bearer("city", "dispatcher"), bearer("city", "viewer"), bearer("other", "ops_manager")


def test_nothing_works_before_a_district_is_loaded(client):
    assert client.get("/v1/containers/risk", headers=VIEWER).json()["code"] == "no_district" and client.post("/v1/routes/optimize", headers=DISPATCHER, json={}).json()["code"] == "no_district"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    assert load["containers"] == 500 == one("SELECT count(*) AS n FROM containers")["n"] and load["fill_model"]["backtest_24h"]["mae_weekday_rates"] < load["fill_model"]["backtest_24h"]["mae_constant_rate"]
    poll = demo["poll"]["result"]
    assert poll["hour"] == world.NOW + 6 and poll["readings"] > 800 and one("SELECT now_hour FROM districts")["now_hour"] == world.NOW + 6 and one("SELECT count(*) AS n FROM fill_readings")["n"] == load["readings"] + poll["readings"] + 1
    assert len(demo["push"]["accepted"]) == 1 and [r["why"] for r in demo["push"]["rejected"]] == ["unknown sensor", "fill_pct must be 0-100"]
    k = demo["risk"]
    assert k["items"][0]["p_overflow_24h"] >= 0.9 and k["by_site_type"]["venue"] > k["by_site_type"]["residential"] and k["simulation_truth"]["flagged_and_true"] > k["simulation_truth"]["flagged_not_true"]
    r = demo["routes"]
    assert r["unserved"] == 0 and r["km"] < r["static"]["km"] and r["simulation_truth"]["dynamic"]["overflows_left_tomorrow"] < r["simulation_truth"]["static"]["overflows_left_tomorrow"] and one("SELECT count(*) AS n FROM pickups WHERE plan_id IS NOT NULL")["n"] == r["containers"]
    for route in r["routes"]:
        assert len(route["streams"]) == 1 and route["minutes"] <= 240
    f = demo["fleet"]
    assert f["plan_id"] == r["plan_id"] and sum(len(v["stops"]) for v in f["vehicles"]) == r["containers"]
    v = demo["vision"]
    assert v["classified"] == 12 and v["by_class"]["contamination"] == 3 and v["alert"] and one("SELECT count(*) AS n FROM sort_observations")["n"] == 12 and "toy" in v["model"]["method"]
    assert all(x["sent_today_kg"] == 0 for x in demo["cap0"]["facilities"])
    d = demo["dest"]
    assert d["applied"] and d["status"] == "optimal" and d["totals"]["optimised"]["material_value"] > d["totals"]["nearest"]["material_value"] and len(d["redirected"]) >= 1
    after = {x["name"]: x for x in demo["cap1"]["facilities"]}
    assert after["Northgate MRF"]["sent_today_kg"] == 500 and 0 < after["Riverbend MRF"]["sent_today_kg"] <= 600 and after["Transfer Station"]["sent_today_kg"] > 10000 and all(x["sent_today_kg"] <= x["daily_capacity_kg"] for x in after.values())
    assert demo["bad"]["code"] == "no_loads" and demo["audit"]["chain_valid"] and {"district.loaded", "telemetry.polled", "telemetry.received", "routes.planned", "contamination.alert", "materials.routed"} <= {e["action"] for e in demo["audit"]["events"]}


def test_validation(client, demo):
    post = lambda path, body, h=DISPATCHER: client.post(path, headers=h, json=body)     # noqa: E731
    sid = world.build()["containers"][0]["sensor_id"]
    out = post("/v1/telemetry/fill", {"readings": [{"sensor_id": sid, "ts": "2026-10-03T05:00:00Z", "fill_pct": 40}, {"sensor_id": sid, "ts": "2026-10-03T20:00:00Z", "fill_pct": 40}, {"sensor_id": sid, "ts": "2026-10-03T06:10:00", "fill_pct": 40}]}).json()
    assert [r["why"][:12] for r in out["rejected"]] == ["older than t", "timestamp is", "timestamp ne"]
    assert post("/v1/vision/classify", {"facility": "Nowhere", "images_png_base64": ["QUJD"]}).json()["code"] == "facility_not_found"
    bad = post("/v1/vision/classify", {"facility": "Glass Plant", "images_png_base64": [base64.b64encode(b"not a png at all").decode()]}).json()
    assert bad["classified"] == 0 and "unreadable" in bad["results"][0]["error"]
    assert post("/v1/materials/destination", {"loads": [{"stream": "plastic", "kg": 10, "at": [1, 1]}]}).status_code == 422
    assert post("/v1/telemetry:poll", {"hours": 48}).json()["code"] == "end_of_data"


def test_roles(client, demo):
    assert client.get("/v1/containers/risk").status_code == 401 and client.get("/v1/containers/risk", headers=VIEWER).status_code == 200
    for path in ("/v1/telemetry/fill", "/v1/routes/optimize", "/v1/vision/classify", "/v1/materials/destination", "/v1/telemetry:poll"):
        assert client.post(path, headers=VIEWER, json={}).status_code == 403, path
    assert client.post("/v1/district:load", headers=DISPATCHER, json={}).status_code == 403 and client.get("/v1/audit", headers=DISPATCHER).status_code == 403 and client.get("/v1/audit", headers=MANAGER).status_code == 200


def test_another_operator_sees_nothing(client, demo, seeded):
    assert client.get("/v1/facilities/capacity", headers=OTHER).json()["code"] == "no_district"
    for table in ("districts", "containers", "fill_readings", "vehicles", "pickups", "plans", "facilities", "deliveries", "sort_observations", "material_prices"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["city"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO districts (id, tenant_id, name, model_seed, now_hour, start_date) VALUES (%s,%s,'x',1,0,'2026-08-03')", [uuid.uuid4(), seeded["city"]])


def test_idempotent_writes(client, demo):
    body = {"policy": {"p_overflow_24h": 0.3, "expected_24h": 80}, "time_limit_s": 8}
    again = client.post("/v1/routes/optimize", headers={**DISPATCHER, "Idempotency-Key": "demo-1:routes"}, json=body)
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["plan_id"] == demo["routes"]["plan_id"] and one("SELECT count(*) AS n FROM plans")["n"] == 1
    assert client.post("/v1/routes/optimize", headers={**DISPATCHER, "Idempotency-Key": "demo-1:routes"}, json={**body, "time_limit_s": 2}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/district:load", headers=MANAGER, json={"seed": 17}).json()["code"] == "district_exists"
