"""Public API (modular monolith). Blueprint services map to: iot-gateway (telemetry in, and a poll that stands in for the feed),
bin-state and fill-forecast (risk), route-optimizer (VRP against the fixed schedule), vision-classifier (a toy on schematic frames),
facility-capacity, commodity-value and the material destination optimiser, contamination alerts, fleet and analytics.
Not built: real sensors, a road network, real images, a mobile app.

    uvicorn wr.api:app          python -m core.jobs wr.api      # the worker
"""
import base64
import datetime
import functools
import uuid
from typing import Literal

import numpy as np
from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"district:read", "jobs:read"}
WORK = READ | {"telemetry:write", "routes:plan", "vision:classify", "materials:route"}
PERMISSIONS = {"viewer": READ, "dispatcher": WORK, "ops_manager": WORK | {"district:load", "audit:read"}}
app = create_app("waste-recycling-ops", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
START = datetime.datetime(2026, 8, 3, 0, 0, tzinfo=datetime.timezone.utc)        # hour 0 is a Monday 00:00
LOAD_HOUR = world.NOW - 6                                                          # the demo loads on Friday 18:00 and polls the gateway to Saturday 06:00
REGION = "district"


def ts(h):
    return START + datetime.timedelta(hours=float(h))


def hour_of(t):
    return (t - START).total_seconds() / 3600


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def district(c):
    d = c.execute("SELECT * FROM districts").fetchone()
    if not d:
        raise Problem(409, "no_district", "load a district first: POST /v1/district:load")
    return d, world.build(d["model_seed"])


def cid(t, i):
    return uuid.uuid5(t, f"container-{i}")


@functools.lru_cache(maxsize=8)
def rates_at(seed, now_h):
    """Fill rates per container and weekday from the readings and pickups the service has seen by `now_h`."""
    sim = engine.history(seed)
    rd, pk = engine.observed(sim, now_h)
    rates, sd, _ = engine.fit_rates(len(world.build(seed)["containers"]), rd, pk, now_h)
    return rates, sd


@functools.lru_cache(maxsize=4)
def no_collection_truth(seed, now_h):
    """The hidden truth if nothing were collected after `now_h`: what each container would do on its own."""
    return world.simulate(world.build(seed), world.NOW + 48, pickups_until_h=now_h)["fill"]


def last_readings(c, t, w):
    rows = c.execute("SELECT DISTINCT ON (container_id) container_id, ts, fill_pct, battery_pct FROM fill_readings ORDER BY container_id, ts DESC").fetchall()
    by = {r["container_id"]: r for r in rows}
    last = []
    for i in range(len(w["containers"])):
        r = by.get(cid(t, i))
        last.append({"hour": hour_of(r["ts"]), "fill_pct": float(r["fill_pct"]), "battery_pct": float(r["battery_pct"]) if r["battery_pct"] is not None else None} if r else None)
    return last


_RISK = {}


def risk_now(c, d, w):
    """The risk list is recomputed only when a reading has arrived or the clock has moved. ponytail: a dict capped at 16 entries"""
    m = c.execute("SELECT max(ts) AS t, count(*) AS n FROM fill_readings").fetchone()
    key = (d["tenant_id"], d["now_hour"], m["t"], m["n"])
    if key not in _RISK:
        if len(_RISK) >= 16:
            _RISK.pop(next(iter(_RISK)))
        rates, sd = rates_at(d["model_seed"], d["now_hour"])
        _RISK[key] = engine.risk(w, rates, sd, last_readings(c, d["tenant_id"], w), d["now_hour"])
    return _RISK[key]


def prices_today(c, d):
    rows = c.execute("SELECT DISTINCT ON (material) material, price_per_ton, date FROM material_prices WHERE date <= %s ORDER BY material, date DESC", [ts(d["now_hour"]).date()]).fetchall()
    return {r["material"]: float(r["price_per_ton"]) for r in rows}


# ---------------------------------------------------------------- load and the gateway

class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/district:load", status_code=202, tags=["district"], summary="(+) Load the invented district on a Friday evening: 500 smart containers, eight weeks of readings and pickups under the fixed schedule, trucks, facilities and material prices; fit the fill rates and back-test them")
def load_district(body: LoadIn, ctx: Ctx = Depends(auth("district:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM districts LIMIT 1").fetchone():
            raise Problem(409, "district_exists")
        return 202, jobs.enqueue(c, ctx, "district.load", body.model_dump())
    return run(ctx, idem, body, work)


def reading_rows(t, w, readings):
    return [(cid(t, x["container"]), ts(x["hour"]), x["fill_pct"], x["temp_c"], x["battery_pct"], t, "gateway") for x in readings]


@jobs.handler("district.load")
def load_job(c, job):
    t, seed, now = job["tenant_id"], job["payload"]["seed"], LOAD_HOUR
    w = world.build(seed)
    sim = engine.history(seed)
    did = uuid.uuid5(t, "district")
    c.execute("INSERT INTO districts (id, tenant_id, name, model_seed, now_hour, start_date) VALUES (%s,%s,'Riverbend East district',%s,%s,%s)", [did, t, seed, now, START.date()])
    sites = {s: uuid.uuid5(t, f"site-{s}") for s in world.SITE_TYPES}
    db.load(c, "sites", ["id", "tenant_id", "name", "site_type"], [(v, t, f"{k} sites", k) for k, v in sites.items()])
    db.load(c, "containers", ["id", "tenant_id", "site_id", "stream", "capacity_l", "geom_x", "geom_y", "sensor_id", "sim_index", "schedule_every", "schedule_day"], [(cid(t, i), t, sites[x["site_type"]], x["stream"], x["capacity_l"], x["at"][0], x["at"][1], x["sensor_id"], i, x["schedule_every"], x["schedule_day"]) for i, x in enumerate(w["containers"])])
    db.load(c, "vehicles", ["id", "tenant_id", "depot_id", "capacity_kg", "stream_capabilities", "shift_start", "shift_end", "name"], [(uuid.uuid5(t, f"veh-{v['name']}"), t, uuid.uuid5(t, "depot"), v["capacity_kg"], v["streams"], datetime.time(world.SHIFT[0]), datetime.time(world.SHIFT[1]), v["name"]) for v in world.VEHICLES])
    db.load(c, "facilities", ["id", "tenant_id", "name", "facility_type", "geom_x", "geom_y", "accepted_streams", "daily_capacity_kg", "recovery_yield"], [(uuid.uuid5(t, f"fac-{k}"), t, k, v["type"], v["at"][0], v["at"][1], v["accepts"], v["daily_kg"], v["yield"]) for k, v in world.FACILITIES.items()])
    rd, pk = engine.observed(sim, now)
    db.load(c, "fill_readings", ["container_id", "ts", "fill_pct", "temp_c", "battery_pct", "tenant_id", "source"], reading_rows(t, w, rd))
    db.load(c, "pickups", ["id", "tenant_id", "container_id", "vehicle_id", "planned_at", "completed_at", "weight_kg", "overflowed"], [(uuid.uuid5(t, f"pickup-{p['container']}-{p['hour']}"), t, cid(t, p["container"]), None, ts(p["hour"]), ts(p["hour"]), p["weight_kg"], p["overflowed"]) for p in pk])
    db.load(c, "material_prices", ["material", "region", "date", "price_per_ton", "tenant_id"], [(m, REGION, (START + datetime.timedelta(weeks=k)).date(), p, t) for m, path in w["prices"].items() for k, p in enumerate(path)])
    rates, sd = rates_at(seed, now)
    # back-test on the last week: the expected fill 24 hours after a reading, for containers not collected in between, against the truth
    err, errc = [], []
    f = sim["fill"]
    by = {}
    for x in rd:
        by.setdefault(x["container"], []).append(x)
    picked = {(p["container"], p["hour"]) for p in pk}
    for i, xs in by.items():
        for x in xs:
            if now - 7 * 24 <= x["hour"] <= now - 24 and not any((i, h) in picked for h in range(x["hour"] + 1, x["hour"] + 25)):
                e, _ = engine.project(x["fill_pct"], x["hour"], rates[i], sd[i], 24)
                ec, _ = engine.project(x["fill_pct"], x["hour"], np.full(7, float(np.median(rates[i]))), sd[i], 24)
                truth = min(f[x["hour"] + 24][i], 100.0)
                err.append(abs(min(e, 100) - truth))
                errc.append(abs(min(ec, 100) - truth))
    out = {"district_id": did, "clock": str(ts(now)), "hour": now, "containers": len(w["containers"]), "readings": len(rd), "pickups": len(pk), "vehicles": len(world.VEHICLES), "facilities": list(world.FACILITIES),
           "by_stream": {s: sum(1 for x in w["containers"] if x["stream"] == s) for s in world.STREAMS}, "by_site_type": {s: sum(1 for x in w["containers"] if x["site_type"] == s) for s in world.SITE_TYPES},
           "overflowed_at_pickup_share": round(float(np.mean([p["overflowed"] for p in pk])), 3), "kg_per_weekday": round(sum(p["weight_kg"] for p in pk if (p["hour"] // 24) % 7 < 6) / (world.WEEKS * 6)),
           "fill_model": {"version": engine.VERSION, "what": "median six-hour fill increment per container and weekday, from readings with no pickup between them", "backtest_24h": {"pairs": len(err), "mae_weekday_rates": round(float(np.mean(err)), 1), "mae_constant_rate": round(float(np.mean(errc)), 1)}},
           "simulation_truth": {"what": "the festival the service does not know about: containers near the venue fill three times as fast from Friday 18:00 to Sunday 18:00", "containers_in_zone": sum(1 for x in w["containers"] if world.dist(x["at"], world.EVENT["centre"]) <= world.EVENT["radius_km"])}}
    audit.record(c, worker_ctx(job), "district.loaded", "district", did, {"containers": len(w["containers"]), "readings": len(rd)})
    return out


class PollIn(BaseModel):
    hours: int = Field(ge=1, le=48)


@app.post("/v1/telemetry:poll", status_code=202, tags=["telemetry"], summary="(+) The gateway delivers the next hours of readings and the clock moves; stands in for the sensor feed")
def poll(body: PollIn, ctx: Ctx = Depends(auth("telemetry:write")), idem: str | None = IdemKey):
    def work(c):
        d, _ = district(c)
        if d["now_hour"] + body.hours > world.NOW + 48:
            raise Problem(422, "end_of_data", "the simulator holds data to Monday 00:00")
        return 202, jobs.enqueue(c, ctx, "telemetry.poll", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("telemetry.poll")
def poll_job(c, job):
    t = job["tenant_id"]
    d, w = district(c)
    sim = engine.history(d["model_seed"])
    now0, now = d["now_hour"], d["now_hour"] + job["payload"]["hours"]
    new = [x for x in sim["readings"] if now0 < x["hour"] <= now]
    db.load(c, "fill_readings", ["container_id", "ts", "fill_pct", "temp_c", "battery_pct", "tenant_id", "source"], reading_rows(t, w, new))
    newp = [p for p in sim["pickups"] if now0 < p["hour"] <= now]
    db.load(c, "pickups", ["id", "tenant_id", "container_id", "vehicle_id", "planned_at", "completed_at", "weight_kg", "overflowed"], [(uuid.uuid5(t, f"pickup-{p['container']}-{p['hour']}"), t, cid(t, p["container"]), None, ts(p["hour"]), ts(p["hour"]), p["weight_kg"], p["overflowed"]) for p in newp])
    c.execute("UPDATE districts SET now_hour = %s WHERE id = %s", [now, d["id"]])
    audit.record(c, worker_ctx(job), "telemetry.polled", "district", d["id"], {"from_hour": now0, "to_hour": now, "readings": len(new)})
    reporting = len({x["container"] for x in new})
    return {"clock": str(ts(now)), "hour": now, "readings": len(new), "containers_reporting": reporting, "silent": len(w["containers"]) - reporting, "pickups_completed": len(newp), "mean_fill_pct": round(float(np.mean([x["fill_pct"] for x in new if x["hour"] == now])), 1) if new else None, "over_90_pct": sum(1 for x in new if x["hour"] == now and x["fill_pct"] >= 90)}


class Reading(BaseModel):
    sensor_id: str = Field(max_length=40)
    ts: datetime.datetime
    fill_pct: float
    temp_c: float | None = None
    battery_pct: float | None = Field(None, ge=0, le=100)


class TelemetryIn(BaseModel):
    readings: list[Reading] = Field(min_length=1, max_length=2000)


@app.post("/v1/telemetry/fill", status_code=201, tags=["telemetry"], summary="Fill readings from sensors: unknown sensors, impossible values, future timestamps and readings older than the last one stored are refused one by one")
def telemetry(body: TelemetryIn, ctx: Ctx = Depends(auth("telemetry:write")), idem: str | None = IdemKey):
    def work(c):
        d, w = district(c)
        known = {x["sensor_id"]: i for i, x in enumerate(w["containers"])}
        accepted, rejected = [], []
        for r in body.readings:
            if r.sensor_id not in known:
                rejected.append({"sensor_id": r.sensor_id, "why": "unknown sensor"})
                continue
            if not 0 <= r.fill_pct <= 100:
                rejected.append({"sensor_id": r.sensor_id, "why": "fill_pct must be 0-100"})
                continue
            if r.ts.tzinfo is None:
                rejected.append({"sensor_id": r.sensor_id, "why": "timestamp needs a timezone"})
                continue
            if hour_of(r.ts) > d["now_hour"] + 1:
                rejected.append({"sensor_id": r.sensor_id, "why": "timestamp is in the future"})
                continue
            i = known[r.sensor_id]
            latest = c.execute("SELECT max(ts) AS t FROM fill_readings WHERE container_id = %s", [cid(ctx.tenant_id, i)]).fetchone()["t"]
            if latest and r.ts <= latest:
                rejected.append({"sensor_id": r.sensor_id, "why": f"older than the last reading stored ({latest.isoformat()})"})
                continue
            c.execute("INSERT INTO fill_readings (container_id, ts, fill_pct, temp_c, battery_pct, tenant_id, source) VALUES (%s,%s,%s,%s,%s,%s,'push')", [cid(ctx.tenant_id, i), r.ts, r.fill_pct, r.temp_c, r.battery_pct, ctx.tenant_id])
            accepted.append({"sensor_id": r.sensor_id, "container": i, "fill_pct": r.fill_pct})
        audit.record(c, ctx, "telemetry.received", "district", d["id"], {"accepted": len(accepted), "rejected": len(rejected)})
        return 201, {"accepted": accepted, "rejected": rejected}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- risk, routes, fleet

@app.get("/v1/containers/risk", tags=["containers"], summary="Overflow risk per container at 8, 24 and 48 hours from its last reading and its own fill rates by weekday; stale sensors flagged")
def containers_risk(min_p: float = Query(0.0, ge=0, le=1), limit: int = Query(60, ge=1, le=500), ctx: Ctx = Depends(auth("district:read"))):
    with db.tx(ctx.tenant_id) as c:
        d, w = district(c)
        R = risk_now(c, d, w)
    truth = no_collection_truth(d["model_seed"], d["now_hour"])
    h24 = min(d["now_hour"] + 24, truth.shape[0] - 1)
    items = [r for r in R if not r.get("stale") and r["p_overflow_24h"] >= min_p]
    flagged = [r for r in R if not r.get("stale") and r["p_overflow_24h"] >= 0.5]
    truly = {i for i in range(len(w["containers"])) if truth[h24][i] > 100}
    return jsonable_encoder({"clock": str(ts(d["now_hour"])), "containers": len(R), "stale_sensors": sum(1 for r in R if r.get("stale")), "at_risk_24h": {"p_over_0_5": len(flagged), "p_over_0_3": sum(1 for r in R if not r.get("stale") and r["p_overflow_24h"] >= 0.3), "over_90_now": sum(1 for r in R if not r.get("stale") and r["fill_pct"] >= 90)},
                             "by_site_type": {s: round(float(np.mean([r["p_overflow_24h"] for r in R if not r.get("stale") and r["site_type"] == s])), 3) for s in world.SITE_TYPES}, "items": items[:limit], "model_version": engine.VERSION,
                             "simulation_truth": {"what": "containers that would really be over 100% in 24 hours if nothing were collected", "count": len(truly), "flagged_and_true": len(truly & {r["container"] for r in flagged}), "flagged_not_true": len({r["container"] for r in flagged} - truly), "missed": len(truly - {r["container"] for r in flagged})}})


class Policy(BaseModel):
    p_overflow_24h: float = Field(0.3, ge=0, le=1, description="collect if the chance of overflowing in 24 hours is at least this")
    expected_24h: float = Field(80, ge=0, le=130, description="or if the expected fill in 24 hours is at least this")


class RoutesIn(BaseModel):
    policy: Policy = Field(default_factory=Policy)
    time_limit_s: int = Field(8, ge=2, le=30)


@app.post("/v1/routes/optimize", status_code=201, tags=["routes"], summary="Today's collection: the containers the risk policy picks, routed by a capacitated VRP with stream and time constraints over two trips per truck; the fixed weekly schedule run nearest-first beside it, both scored on the hidden truth")
def optimize(body: RoutesIn, ctx: Ctx = Depends(auth("routes:plan")), idem: str | None = IdemKey):
    def work(c):
        d, w = district(c)
        R = risk_now(c, d, w)
        fill = {i: 50.0 for i in range(len(w["containers"]))}                            # a silent sensor is assumed half full
        fill.update({r["container"]: r["fill_pct"] for r in R if not r.get("stale")})
        due = [r["container"] for r in R if not r.get("stale") and (r["p_overflow_24h"] >= body.policy.p_overflow_24h or r["expected_24h"] >= body.policy.expected_24h)]
        day_index = d["now_hour"] // 24
        day = day_index % 7
        if day == 6:
            raise Problem(409, "no_collection_on_sunday")
        plan = engine.vrp(w, due, fill, time_limit_s=body.time_limit_s)
        if plan is None:
            raise Problem(500, "no_solution")
        static = engine.static_plan(w, day, fill)
        truth = no_collection_truth(d["model_seed"], d["now_hour"])
        sim = {"fill": truth}
        score = {"dynamic": engine.score_plan(sim, set(plan["served"]), day_index), "static": engine.score_plan(sim, set(static["served"]), day_index)}
        pid = uuid.uuid7()
        for_day = ts(d["now_hour"]).date()
        summary = {"km": plan["km"], "kg": plan["kg"], "trips": plan["trips"], "containers": len(plan["served"]), "fuel_l": round(plan["km"] * world.FUEL_L_PER_KM, 1), "unserved": len(plan["unserved"]), "over_shift": plan["over_shift"], "static": {"km": static["km"], "kg": static["kg"], "trips": static["trips"], "containers": len(static["served"]), "fuel_l": round(static["km"] * world.FUEL_L_PER_KM, 1), "over_shift": static["over_shift"], "unserved": len(static["unserved"])}, "policy": body.policy.model_dump(), "truth": score}
        c.execute("DELETE FROM pickups WHERE plan_id IS NOT NULL AND planned_at::date = %s", [for_day])
        c.execute("DELETE FROM plans WHERE for_day = %s", [for_day])
        c.execute("INSERT INTO plans (id, tenant_id, kind, for_day, created_by, summary, routes) VALUES (%s,%s,'dynamic',%s,%s,%s,%s)", [pid, ctx.tenant_id, for_day, ctx.actor_id, Jsonb(summary), Jsonb(plan["routes"])])
        rows = []
        for r in plan["routes"]:
            t0 = d["now_hour"] + (world.SHIFT[0] - d["now_hour"] % 24) + (r["trip"] - 1) * 4
            for k, i in enumerate(r["containers"]):
                rows.append((uuid.uuid7(), ctx.tenant_id, cid(ctx.tenant_id, i), uuid.uuid5(ctx.tenant_id, f"veh-{r['vehicle']}"), ts(t0 + k * (r["minutes"] / max(1, len(r["containers"]))) / 60), None, round(engine.demand_kg(w["containers"][i], fill[i]), 1), pid, k + 1, None))
        db.load(c, "pickups", ["id", "tenant_id", "container_id", "vehicle_id", "planned_at", "completed_at", "weight_kg", "plan_id", "sequence", "overflowed"], rows)
        audit.record(c, ctx, "routes.planned", "plan", pid, {"containers": len(plan["served"]), "km": plan["km"], "static_km": static["km"]})
        return 201, {"plan_id": pid, "for_day": str(for_day), "due": len(due), **summary, "routes": plan["routes"], "static_routes": static["routes"], "unserved_containers": plan["unserved"],
                     "simulation_truth": {"what": "under the no-collection truth: containers each plan leaves to overflow by tomorrow 08:00, and overflowing containers each plan does collect today", **score},
                     "how": f"VRP: OR-Tools routing, parallel cheapest insertion then guided local search for {body.time_limit_s} s; street-grid distances at {world.SPEED_KMH} km/h, {world.SERVICE_MIN} min a stop, four hours a trip, two trips a truck, trucks only take the streams they are built for; a dropped container costs 200 km. Static: the fixed weekly plan, each truck nearest-first until full"}
    return run(ctx, idem, body, work)


@app.get("/v1/fleet/today", tags=["routes"], summary="Each truck's trips today in order, with kilometres, load and the stops, from the stored plan")
def fleet(ctx: Ctx = Depends(auth("district:read"))):
    with db.tx(ctx.tenant_id) as c:
        d, w = district(c)
        plan = c.execute("SELECT * FROM plans WHERE for_day = %s ORDER BY created_at DESC LIMIT 1", [ts(d["now_hour"]).date()]).fetchone()
        if not plan:
            raise Problem(404, "no_plan_today", "POST /v1/routes/optimize first")
        stops = c.execute("SELECT v.name AS vehicle, p.sequence, p.planned_at, p.weight_kg, k.sensor_id, k.stream, k.geom_x, k.geom_y FROM pickups p JOIN vehicles v ON v.id = p.vehicle_id JOIN containers k ON k.id = p.container_id WHERE p.plan_id = %s ORDER BY v.name, p.planned_at", [plan["id"]]).fetchall()
    by = {}
    for s in stops:
        by.setdefault(s["vehicle"], []).append(dict(s))
    return jsonable_encoder({"for_day": str(plan["for_day"]), "plan_id": plan["id"], "summary": plan["summary"], "vehicles": [{"vehicle": v["name"], "capacity_kg": v["capacity_kg"], "streams": v["streams"], "trips": [r for r in plan["routes"] if r["vehicle"] == v["name"]], "stops": by.get(v["name"], [])} for v in world.VEHICLES], "depot": world.DEPOT, "facilities": {k: v["at"] for k, v in world.FACILITIES.items()}})


# ---------------------------------------------------------------- vision (toy), facilities, destinations

class VisionIn(BaseModel):
    facility: str = Field(max_length=60)
    images_png_base64: list[str] = Field(min_length=1, max_length=64)
    contamination_alert_share: float = Field(0.1, ge=0, le=1)


@app.post("/v1/vision/classify", status_code=201, tags=["vision"], summary="Conveyor frames classified as paper, plastic, metal, organics, glass or contamination by a toy classifier on schematic images; an alert when contamination in the batch passes a share")
def classify(body: VisionIn, ctx: Ctx = Depends(auth("vision:classify")), idem: str | None = IdemKey):
    def work(c):
        d, w = district(c)
        if body.facility not in world.FACILITIES:
            raise Problem(404, "facility_not_found", f"facilities: {list(world.FACILITIES)}")
        m = engine.vision_model(d["model_seed"])
        batch = uuid.uuid7()
        results, rows = [], []
        for k, b64 in enumerate(body.images_png_base64):
            try:
                png = base64.b64decode(b64, validate=True)
                r = engine.classify(m, png)
            except Exception as e:                       # noqa: BLE001  a bad image is a client error, not a crash
                results.append({"index": k, "error": f"unreadable image: {str(e)[:60]}"})
                continue
            results.append({"index": k, **r})
            rows.append((uuid.uuid7(), ctx.tenant_id, uuid.uuid5(ctx.tenant_id, f"fac-{body.facility}"), ts(d["now_hour"]), r["class"], r["confidence"], r["contamination"], f"frame://{batch}/{k}.png", batch, engine.VERSION))
        db.load(c, "sort_observations", ["id", "tenant_id", "facility_id", "captured_at", "class", "confidence", "contamination", "image_uri", "batch_id", "model_version"], rows)
        ok = [r for r in results if "class" in r]
        share = sum(r["contamination"] for r in ok) / max(1, len(ok))
        alert = share >= body.contamination_alert_share
        if alert:
            audit.record(c, ctx, "contamination.alert", "facility", uuid.uuid5(ctx.tenant_id, f"fac-{body.facility}"), {"share": round(share, 3), "frames": len(ok)})
        return 201, {"batch_id": batch, "facility": body.facility, "frames": len(body.images_png_base64), "classified": len(ok), "by_class": {k: sum(1 for r in ok if r["class"] == k) for k in m["classes"]}, "contamination_share": round(share, 3), "alert": alert, "alert_threshold": body.contamination_alert_share,
                     "results": results, "model": {"version": engine.VERSION, **m["metrics"], "method": m["method"]}}
    return run(ctx, idem, body, work)


@app.get("/v1/facilities/capacity", tags=["facilities"], summary="Each facility's daily capacity, what has been sent today, what remains, the streams it takes and today's material prices")
def capacity(ctx: Ctx = Depends(auth("district:read"))):
    with db.tx(ctx.tenant_id) as c:
        d, w = district(c)
        used = {r["name"]: float(r["kg"]) for r in c.execute("SELECT f.name, sum(dl.weight_kg) AS kg FROM deliveries dl JOIN facilities f ON f.id = dl.facility_id WHERE dl.for_day = %s GROUP BY f.name", [ts(d["now_hour"]).date()])}
        prices = prices_today(c, d)
    return jsonable_encoder({"clock": str(ts(d["now_hour"])), "facilities": [{"name": k, "type": v["type"], "accepts": v["accepts"], "daily_capacity_kg": v["daily_kg"], "sent_today_kg": round(used.get(k, 0.0)), "remaining_kg": round(v["daily_kg"] - used.get(k, 0.0)), "share_used": round(used.get(k, 0.0) / v["daily_kg"], 3), "recovery_yield": v["yield"], "at": v["at"]} for k, v in world.FACILITIES.items()],
                             "prices_per_ton": prices, "price_note": "dollars per tonne received; a negative price is a gate fee"})


class Load(BaseModel):
    vehicle: str | None = None
    stream: Literal["residual", "recycling", "organics", "glass"]
    kg: float = Field(gt=0, le=20000)
    at: list[float] = Field(min_length=2, max_length=2, description="where the truck is, km on the grid")


class DestinationIn(BaseModel):
    loads: list[Load] = Field(default_factory=list, max_length=40)
    from_plan: bool = Field(False, description="add today's planned trips as loads, one per trip and stream, from where their last stop is")
    apply: bool = Field(False, description="record the deliveries, so facility capacity is consumed")


@app.post("/v1/materials/destination", status_code=201, tags=["facilities"], summary="Send each load to the facility that maximises material value less haul cost within what each can still take today (a linear programme); the nearest accepting facility beside it, with what it would turn away")
def destination(body: DestinationIn, ctx: Ctx = Depends(auth("materials:route")), idem: str | None = IdemKey):
    def work(c):
        d, w = district(c)
        loads = [{"vehicle": L.vehicle, "stream": L.stream, "kg": L.kg, "from": (L.at[0], L.at[1])} for L in body.loads]
        if body.from_plan:
            plan = c.execute("SELECT routes FROM plans WHERE for_day = %s ORDER BY created_at DESC LIMIT 1", [ts(d["now_hour"]).date()]).fetchone()
            if not plan:
                raise Problem(404, "no_plan_today")
            R = risk_now(c, d, w)
            fill = {r["container"]: r["fill_pct"] for r in R if not r.get("stale")}
            for r in plan["routes"]:
                last = w["containers"][r["containers"][-1]]["at"]
                for s in r["streams"]:
                    kg = sum(engine.demand_kg(w["containers"][i], fill.get(i, 50)) for i in r["containers"] if w["containers"][i]["stream"] == s)
                    if kg > 0:
                        loads.append({"vehicle": f"{r['vehicle']} trip {r['trip']}", "stream": s, "kg": round(kg), "from": last})
        if not loads:
            raise Problem(422, "no_loads")
        for_day = ts(d["now_hour"]).date()
        used = {r["name"]: float(r["kg"]) for r in c.execute("SELECT f.name, sum(dl.weight_kg) AS kg FROM deliveries dl JOIN facilities f ON f.id = dl.facility_id WHERE dl.for_day = %s GROUP BY f.name", [for_day])}
        prices = prices_today(c, d)
        out = engine.destinations(loads, prices, used)
        if body.apply:
            for a in out["assignments"]:
                c.execute("INSERT INTO deliveries (id, tenant_id, facility_id, for_day, stream, weight_kg, vehicle, decided_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", [uuid.uuid7(), ctx.tenant_id, uuid.uuid5(ctx.tenant_id, f"fac-{a['facility']}"), for_day, a["stream"], a["kg"], a["vehicle"], ctx.actor_id])
            audit.record(c, ctx, "materials.routed", "district", d["id"], {"loads": len(loads), "kg": round(sum(L["kg"] for L in loads))})
        redirected = [a for a in out["assignments"] if any(n["load"] == a["load"] and n["facility"] != a["facility"] for n in out["nearest"])]
        return 201, {"applied": body.apply, "loads": loads, "prices_per_ton": prices, "capacity_used_before_kg": {k: round(v) for k, v in used.items()}, **out, "redirected": redirected,
                     "how": "minimise haul cost less material value (price × recovery yield; the transfer station charges its gate fee) subject to each load being placed and no facility taking more than it has left today; splits allowed. Nearest: the closest facility that accepts the stream, no capacity check"}
    return run(ctx, idem, body, work)
