"""Measure the fill model, the overflow risk list, the planners, the toy classifier and the destination optimiser on districts the demo
never uses. Writes docs/evaluation.md.

    python -m wr.evaluate
"""
import pathlib

import numpy as np

from . import engine, world

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evaluation.md"
SEEDS = (101, 102, 103)
DAYS = (world.NOW // 24 - 5 + k for k in range(5))          # the Monday to Friday before the demo's Saturday


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def main():
    fill_rows, route_rows, vis_rows, dest_rows = [], [], [], []
    for seed in SEEDS:
        w = world.build(seed)
        sim = engine.history(seed)
        n = len(w["containers"])
        err, errc, caught, flagged, truly = [], [], 0, 0, 0
        km_d, km_s, ov_d, ov_s, served_d, served_s = [], [], [], [], [], []
        for day in list(world.NOW // 24 - 5 + k for k in range(5)):
            now = day * 24 + 6
            rd, pk = engine.observed(sim, now)
            rates, sd, last = engine.fit_rates(n, rd, pk, now)
            truth = world.simulate(w, world.NOW + 48, pickups_until_h=now)["fill"]
            R = engine.risk(w, rates, sd, last, now)
            for r in R:
                if r.get("stale"):
                    continue
                i = r["container"]
                x = last[i]
                e, _ = engine.project(x["fill_pct"], x["hour"], rates[i], sd[i], now + 24 - x["hour"])
                ec, _ = engine.project(x["fill_pct"], x["hour"], np.full(7, float(np.median(rates[i]))), sd[i], now + 24 - x["hour"])
                t = truth[now + 24][i]
                err.append(abs(min(e, 130) - t))
                errc.append(abs(min(ec, 130) - t))
                over = t > 100
                truly += over
                if r["p_overflow_24h"] >= 0.5:
                    flagged += 1
                    caught += over
            fill = {i: 50.0 for i in range(n)}
            fill.update({r["container"]: r["fill_pct"] for r in R if not r.get("stale")})
            due = [r["container"] for r in R if not r.get("stale") and (r["p_overflow_24h"] >= 0.3 or r["expected_24h"] >= 80)]
            plan = engine.vrp(w, due, fill, time_limit_s=3)
            static = engine.static_plan(w, day % 7, fill)
            sd_, ss = engine.score_plan({"fill": truth}, set(plan["served"]), day), engine.score_plan({"fill": truth}, set(static["served"]), day)
            km_d.append(plan["km"])
            km_s.append(static["km"])
            ov_d.append(sd_["overflows_left_tomorrow"])
            ov_s.append(ss["overflows_left_tomorrow"])
            served_d.append(len(plan["served"]))
            served_s.append(len(static["served"]))
        fill_rows.append([seed, len(err), round(float(np.mean(err)), 1), round(float(np.mean(errc)), 1), truly, flagged, caught, f"{caught / max(1, flagged):.0%}", f"{caught / max(1, truly):.0%}"])
        route_rows.append([seed, round(np.mean(km_d), 1), round(np.mean(km_s), 1), round(np.mean(served_d)), round(np.mean(served_s)), round(np.mean(ov_d), 1), round(np.mean(ov_s), 1)])
        m = engine.vision_model(seed)
        vis_rows.append([seed, m["metrics"]["tested_on"], f"{m['metrics']['accuracy']:.0%}", f"{m['metrics']['majority_class']:.0%}", f"{m['metrics']['contamination_recall']:.0%}"])
        r = np.random.default_rng(seed)
        prices = {k: w["prices"][k][world.WEEKS] for k in world.STREAMS}
        gains, turned = [], []
        for k in range(10):
            loads = [{"stream": str(r.choice(list(world.STREAMS))), "kg": float(r.uniform(300, 7000)), "from": (float(r.uniform(0, 6)), float(r.uniform(0, 6)))} for _ in range(6)]
            out = engine.destinations(loads, prices, {})
            gains.append((out["totals"]["optimised"]["material_value"] - out["totals"]["optimised"]["haul_cost"]) - (out["totals"]["nearest"]["material_value"] - out["totals"]["nearest"]["haul_cost"]))
            turned.append(out["totals"]["nearest"]["turned_away_kg"])
        dest_rows.append([seed, 10, round(float(np.mean(gains))), round(float(np.mean(turned)))])
    doc = f"""# Evaluation

Everything here is measured on synthetic districts generated with seeds the demo does not use (`{SEEDS}`), on the five weekdays before
the demo's Saturday, with the service knowing only the readings and pickups published by 06:00 each day. Reproduce with
`python -m wr.evaluate` (about two minutes). The numbers say how well the methods do against the district as the simulator plays it.
They are not a claim about a real city: real sensors drift and lie, roads are not a grid, and real conveyor frames are not colour blobs.

## 1. Fill forecast and overflow risk, 24 hours ahead

Error in percentage points of fill (capped at 130) against the no-collection truth; the constant-rate baseline uses each container's
overall median rate instead of its weekday rates. The risk list flags containers with at least a 50% chance of overflowing in 24 hours.

{table(["district (seed)", "forecasts", "error, weekday rates", "error, constant rate", "would overflow", "flagged", "caught", "precision", "recall"], fill_rows)}

Weekday rates did not help on these weekdays: a median over seven or eight same-weekday increments is noisier than a median over
all of them, and the weekday pattern only matters where weekends differ (parks, the venue), which a Monday-to-Friday test does not
exercise. The demo's own back-test, which includes a weekend, has them ahead; the gain is real but small, and most of the error is the
sensor's noise and the hour-to-hour randomness of what people throw away. On ordinary weekdays the list catches about
{np.mean([float(r[8][:-1]) for r in fill_rows]):.0f}% of the containers that would overflow with {np.mean([float(r[7][:-1]) for r in fill_rows]):.0f}% precision: an ordinary container near full is easy to call. The demo's festival
weekend is the hard case, and there the list misses a quarter, because those containers fill faster than anything in their history.

## 2. Dynamic routes against the fixed schedule

Each weekday: the risk policy's containers routed by the VRP, against the fixed weekly schedule run nearest-first; kilometres, containers
served, and overflows each plan leaves for the next morning under the no-collection truth. Means over five days.

{table(["district (seed)", "km, dynamic", "km, fixed", "served, dynamic", "served, fixed", "overflows left, dynamic", "overflows left, fixed"], route_rows)}

Dynamic routing drives about {1 - np.mean([r[1] for r in route_rows]) / np.mean([r[2] for r in route_rows]):.0%} fewer kilometres and leaves fewer containers to overflow, by visiting fewer containers: the fixed schedule empties
containers that are a third full. On a weekday the two are closer than on the festival weekend the demo shows. Fuel is kilometres
times a constant, so it moves the same way.

## 3. The toy classifier

{table(["district (seed)", "test frames", "accuracy", "majority class", "contamination recall"], vis_rows)}

Perfect or near-perfect scores say the schematic frames are easy, not that the classifier is good; a real conveyor camera would need a
real model and real labelled images, and this slice does not pretend to have either.

## 4. Material destinations

Ten random sets of six loads per district: the optimiser's value less haul cost against the nearest-accepting-facility rule (whose overflow goes on to
the transfer station at the gate fee), and the tonnage that rule has to divert.

{table(["district (seed)", "load sets", "gain per set ($)", "diverted by the nearest rule (kg)"], dest_rows)}

The gain is mostly the nearest rule's habit of sending organics and recycling to the transfer station because it is close, where they
pay a gate fee instead of earning anything. The optimiser is a small linear programme; its value is in the constraint, not the maths.

## 5. What is not measured

- Sensor faults beyond missed readings; the simulator's sensors are honest with noise.
- Traffic, road geometry and real service times: the grid at a constant speed is the whole road model.
- Anything about real images.
"""
    OUT.write_text(doc)
    print(doc)


if __name__ == "__main__":
    main()
