"""Unit tests for the simulator, the fill-rate model, the planners, the toy classifier and the destination optimiser: no database."""
import numpy as np

from wr import engine, world

W = world.build()
SIM = engine.history(17)
NOW = world.NOW + 6


def test_world_is_deterministic_and_the_festival_is_where_it_says():
    assert world.build()["containers"] == W["containers"] and len(W["containers"]) == 500 and NOW % 24 == 6 and (NOW // 24) % 7 == 5       # Saturday 06:00
    zone = [c["idx"] for c in W["containers"] if world.dist(c["at"], world.EVENT["centre"]) <= world.EVENT["radius_km"]]
    f = SIM["fill"]
    quiet = world.simulate(W, world.NOW + 48, event=False)["fill"]
    assert 10 <= len(zone) <= 40 and f[NOW][zone].mean() > quiet[NOW][zone].mean() + 15 and abs(f[NOW - 48].mean() - quiet[NOW - 48].mean()) < 1e-6
    assert 0.02 <= np.mean([p["overflowed"] for p in SIM["pickups"]]) <= 0.08 and all(0 <= x["fill_pct"] <= 100 for x in SIM["readings"][:5000])
    assert not any(world.scheduled(c, 6) for c in W["containers"])                                                                      # no Sunday collections


def test_fill_rates_beat_a_constant_rate_and_risk_is_ordered():
    rd, pk = engine.observed(SIM, NOW)
    rates, sd, last = engine.fit_rates(500, rd, pk, NOW)
    assert rates.shape == (500, 7) and (rates >= -5).all() and all(l is not None for l in last[:100])
    err, errc = [], []
    for i in range(0, 500, 5):
        x = last[i]
        for hz in (24,):
            e, s = engine.project(x["fill_pct"], x["hour"], rates[i], sd[i], hz)
            ec, _ = engine.project(x["fill_pct"], x["hour"], np.full(7, float(np.median(rates[i]))), sd[i], hz)
            truth = world.simulate(W, world.NOW + 48, pickups_until_h=NOW)["fill"][x["hour"] + hz][i]
            err.append(abs(min(e, 130) - truth))
            errc.append(abs(min(ec, 130) - truth))
            assert s > 0
    assert np.mean(err) <= np.mean(errc) + 0.5
    R = engine.risk(W, rates, sd, last, NOW)
    ps = [r["p_overflow_24h"] for r in R if not r.get("stale")]
    assert ps == sorted(ps, reverse=True) and all(r["p_overflow_8h"] <= r["p_overflow_24h"] + 1e-9 <= r["p_overflow_48h"] + 1e-9 for r in R if not r.get("stale"))


def test_vrp_respects_capacity_streams_and_beats_the_fixed_schedule_on_overflows():
    rd, pk = engine.observed(SIM, NOW)
    rates, sd, last = engine.fit_rates(500, rd, pk, NOW)
    R = engine.risk(W, rates, sd, last, NOW)
    fill = {i: 50.0 for i in range(500)}
    fill.update({r["container"]: r["fill_pct"] for r in R if not r.get("stale")})
    due = [r["container"] for r in R if not r.get("stale") and (r["p_overflow_24h"] >= 0.3 or r["expected_24h"] >= 80)]
    plan = engine.vrp(W, due, fill, time_limit_s=3)
    assert plan and plan["unserved"] == [] and sorted(plan["served"]) == sorted(due)
    for r in plan["routes"]:
        v = next(v for v in world.VEHICLES if v["name"] == r["vehicle"])
        assert r["kg"] <= v["capacity_kg"] + 1 and r["stream"] in v["streams"] and all(W["containers"][i]["stream"] == r["stream"] for i in r["containers"]) and r["minutes"] <= 240
    static = engine.static_plan(W, 5, fill)
    truth = {"fill": world.simulate(W, world.NOW + 48, pickups_until_h=NOW)["fill"]}
    sd_, ss = engine.score_plan(truth, set(plan["served"]), NOW // 24), engine.score_plan(truth, set(static["served"]), NOW // 24)
    assert sd_["overflows_left_tomorrow"] < ss["overflows_left_tomorrow"] and plan["km"] < static["km"]


def test_toy_classifier_and_destinations():
    m = engine.vision_model(17)
    assert m["metrics"]["accuracy"] > 0.9 and m["metrics"]["contamination_recall"] > 0.9 and "toy" in m["method"]
    r = np.random.default_rng(3)
    assert engine.classify(m, world.image_bytes(world.conveyor_image(r, "contamination")))["contamination"]
    prices = {"recycling": 85.0, "organics": 12.0, "glass": 25.0, "residual": -95.0}
    loads = [{"stream": "recycling", "kg": 900, "from": (3.0, 2.0), "vehicle": "T3"}, {"stream": "organics", "kg": 3000, "from": (2.0, 3.0), "vehicle": "T5"}]
    out = engine.destinations(loads, prices, {"Riverbend MRF": 400.0})
    assert out["status"] == "optimal" and abs(sum(a["kg"] for a in out["assignments"]) - 3900) < 1
    assert sum(a["kg"] for a in out["assignments"] if a["facility"] == "Riverbend MRF") <= 200 + 1 and sum(a["kg"] for a in out["assignments"] if a["facility"] == "Northgate MRF") <= 500 + 1        # what each can still take
    assert out["totals"]["optimised"]["material_value"] > out["totals"]["nearest"]["material_value"] and any(n["facility"] == "Transfer Station" for n in out["nearest"])
