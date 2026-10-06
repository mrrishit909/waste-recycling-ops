"""Fill-rate estimates and overflow risk, the dynamic collection VRP against the fixed schedule, the toy conveyor classifier and
the material destination optimiser."""
import functools
import io

import numpy as np

from . import world

VERSION = "waste-ops-1"
STEP = world.READ_EVERY_H


@functools.lru_cache(maxsize=4)
def history(seed):
    """The district simulated through the demo's Sunday, with the festival. The service sees readings and pickups up to its clock."""
    w = world.build(seed)
    return world.simulate(w, world.NOW + 48)


def observed(sim, upto_h):
    return [x for x in sim["readings"] if x["hour"] <= upto_h], [p for p in sim["pickups"] if p["hour"] <= upto_h]


def fit_rates(n, readings, pickups, upto_h):
    """Per container and weekday, the median six-hour fill increment between consecutive readings with no pickup between them;
    the overall median and the spread as fallbacks. -> rates[n, 7], sd[n], last reading per container"""
    by = [[] for _ in range(n)]
    for x in readings:
        by[x["container"]].append(x)
    picked = {}
    for p in pickups:
        picked.setdefault(p["container"], []).append(p["hour"])
    rates, sd, last = np.zeros((n, 7)), np.zeros(n), [None] * n
    for i in range(n):
        xs = sorted(by[i], key=lambda x: x["hour"])
        last[i] = xs[-1] if xs else None
        inc = {d: [] for d in range(7)}
        allinc = []
        ph = sorted(picked.get(i, []))
        for a, b in zip(xs, xs[1:]):
            if b["hour"] - a["hour"] != STEP or any(a["hour"] < h <= b["hour"] for h in ph):
                continue
            d = (b["hour"] // 24) % 7
            v = b["fill_pct"] - a["fill_pct"]
            inc[d].append(v)
            allinc.append(v)
        med = float(np.median(allinc)) if allinc else 2.0
        for d in range(7):
            rates[i, d] = float(np.median(inc[d])) if len(inc[d]) >= 3 else med
        sd[i] = float(np.std(allinc)) if len(allinc) > 5 else 3.0
    return rates, sd, last


def project(fill0, hour0, rates_i, sd_i, horizon_h):
    """Expected fill after `horizon_h` hours and the standard deviation of that forecast, stepping six hours at a time."""
    f, var, h = fill0, 0.0, hour0
    while h < hour0 + horizon_h:
        d = int(h // 24) % 7
        frac = min(STEP, hour0 + horizon_h - h) / STEP
        f += max(0.0, rates_i[d]) * frac
        var += (sd_i * frac) ** 2
        h += STEP
    return f, float(np.sqrt(var))


def risk(w, rates, sd, last, now_h, horizons=(8, 24, 48)):
    """Overflow risk per container at the horizons, from its last reading. -> list sorted by the 24-hour risk"""
    from math import erf, sqrt
    out = []
    for i, c in enumerate(w["containers"]):
        x = last[i]
        if x is None:
            out.append({"container": i, "stale": True})
            continue
        age = now_h - x["hour"]
        row = {"container": i, "sensor_id": c["sensor_id"], "stream": c["stream"], "site_type": c["site_type"], "capacity_l": c["capacity_l"], "geom_x": c["at"][0], "geom_y": c["at"][1], "fill_pct": x["fill_pct"], "reading_age_h": round(age, 1), "stale": age > 2 * STEP, "battery_pct": x["battery_pct"], "rate_pct_per_6h": round(float(rates[i, int(now_h // 24) % 7]), 2)}
        for hz in horizons:
            f, s = project(x["fill_pct"], x["hour"], rates[i], sd[i], age + hz)
            p = 0.5 * (1 - erf((100 - f) / (sqrt(2) * max(s, 1e-6))))
            row[f"expected_{hz}h"] = round(float(min(f, 130)), 1)
            row[f"p_overflow_{hz}h"] = round(float(p), 3)
        out.append(row)
    return sorted(out, key=lambda r: -(r.get("p_overflow_24h", 0)))


def demand_kg(c, fill_pct):
    return min(fill_pct, 100) / 100 * c["capacity_l"] * world.STREAMS[c["stream"]]["density"]


def vrp(w, due, fill, trips_per_truck=2, trip_hours=4, time_limit_s=10):
    """Capacitated VRP with stream compatibility and a time cap per trip, OR-Tools routing. Each truck may run two trips from the depot.
    -> plan with routes, km, kg, minutes; unserved containers"""
    from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    nodes = [world.DEPOT] + [w["containers"][i]["at"] for i in due]
    n = len(nodes)
    trips = [(v, s) for v in world.VEHICLES for s in (v["streams"] if len(v["streams"]) > 1 else v["streams"] * trips_per_truck)]      # a trip carries one stream; a single-stream truck gets two trips
    mgr = pywrapcp.RoutingIndexManager(n, len(trips), 0)
    routing = pywrapcp.RoutingModel(mgr)
    D = [[int(world.dist(a, b) * 1000) for b in nodes] for a in nodes]
    transit = routing.RegisterTransitCallback(lambda i, j: D[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
    routing.SetArcCostEvaluatorOfAllVehicles(transit)
    dem = [0] + [int(demand_kg(w["containers"][i], fill[i])) for i in due]
    demand_cb = routing.RegisterUnaryTransitCallback(lambda i: dem[mgr.IndexToNode(i)])
    routing.AddDimensionWithVehicleCapacity(demand_cb, 0, [v["capacity_kg"] for v, _ in trips], True, "kg")
    svc = int(world.SERVICE_MIN)
    time_cb = routing.RegisterTransitCallback(lambda i, j: int(D[mgr.IndexToNode(i)][mgr.IndexToNode(j)] / 1000 / world.SPEED_KMH * 60) + (svc if mgr.IndexToNode(i) != 0 else 0))
    routing.AddDimension(time_cb, 0, trip_hours * 60, True, "min")
    for k, i in enumerate(due, start=1):
        allowed = [t for t, (v, st) in enumerate(trips) if w["containers"][i]["stream"] == st]
        routing.VehicleVar(mgr.NodeToIndex(k)).SetValues([-1] + allowed)
        routing.AddDisjunction([mgr.NodeToIndex(k)], 200_000)                                    # dropping a container costs 200 km-equivalents
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = time_limit_s
    sol = routing.SolveWithParameters(params)
    if sol is None:
        return None
    routes, served, nth = [], set(), {}
    for t, (v, st) in enumerate(trips):
        idx = routing.Start(t)
        seq, km, kg, minutes = [], 0.0, 0.0, 0.0
        while not routing.IsEnd(idx):
            nxt = sol.Value(routing.NextVar(idx))
            a, b = mgr.IndexToNode(idx), mgr.IndexToNode(nxt)
            km += D[a][b] / 1000
            minutes += int(D[a][b] / 1000 / world.SPEED_KMH * 60) + (svc if a != 0 else 0)      # the same arithmetic as the solver's time dimension
            if b != 0:
                ci = due[b - 1]
                seq.append(ci)
                served.add(ci)
                kg += dem[b]
            idx = nxt
        if seq:
            nth[v["name"]] = nth.get(v["name"], 0) + 1
            routes.append({"vehicle": v["name"], "trip": nth[v["name"]], "stream": st, "containers": seq, "km": round(km, 2), "kg": round(kg), "minutes": round(minutes), "capacity_kg": v["capacity_kg"], "streams": [st]})
    return {"routes": routes, "km": round(sum(r["km"] for r in routes), 1), "kg": round(sum(r["kg"] for r in routes)), "served": sorted(served), "unserved": [i for i in due if i not in served], "trips": len(routes), "solver_seconds": time_limit_s,
            "over_shift": [v for v in {r["vehicle"] for r in routes} if sum(r["minutes"] for r in routes if r["vehicle"] == v) > (world.SHIFT[1] - world.SHIFT[0]) * 60]}


def static_plan(w, day, fill):
    """The fixed weekly schedule as the crews run it: each truck takes its streams' scheduled containers nearest-first, returning to
    the depot when full. -> same shape as vrp()"""
    due = [i for i, c in enumerate(w["containers"]) if world.scheduled(c, day)]
    remaining = set(due)
    routes = []
    for v in world.VEHICLES:
        trip = 0
        for st in (v["streams"] if len(v["streams"]) > 1 else v["streams"] * 2):
            mine = [i for i in remaining if w["containers"][i]["stream"] == st]
            trip += 1
            pos, load, km, minutes, seq = world.DEPOT, 0.0, 0.0, 0.0, []
            while True:
                cands = [i for i in mine if i in remaining and load + demand_kg(w["containers"][i], fill[i]) <= v["capacity_kg"]]
                if not cands:
                    break
                nxt = min(cands, key=lambda i: world.dist(pos, w["containers"][i]["at"]))
                d = world.dist(pos, w["containers"][nxt]["at"])
                km += d
                minutes += d / world.SPEED_KMH * 60 + world.SERVICE_MIN
                load += demand_kg(w["containers"][nxt], fill[nxt])
                seq.append(nxt)
                remaining.discard(nxt)
                pos = w["containers"][nxt]["at"]
                if minutes > 4 * 60:
                    break
            if seq:
                km += world.dist(pos, world.DEPOT)
                routes.append({"vehicle": v["name"], "trip": trip, "stream": st, "containers": seq, "km": round(km, 2), "kg": round(load), "minutes": round(minutes + world.dist(pos, world.DEPOT) / world.SPEED_KMH * 60), "capacity_kg": v["capacity_kg"], "streams": [st]})
    return {"routes": routes, "km": round(sum(r["km"] for r in routes), 1), "kg": round(sum(r["kg"] for r in routes)), "served": sorted(set(due) - remaining), "unserved": sorted(remaining), "trips": len(routes),
            "over_shift": [v for v in {r["vehicle"] for r in routes} if sum(r["minutes"] for r in routes if r["vehicle"] == v) > (world.SHIFT[1] - world.SHIFT[0]) * 60]}


def score_plan(sim, served, day_index):
    """What the hidden truth says: containers over 100% at tomorrow 08:00 that the plan did not serve (overflows the plan leaves), and
    tonnes collected if every served container is emptied at 08:00."""
    h_tomorrow = (day_index + 1) * 24 + 8
    f = sim["fill"][min(h_tomorrow, len(sim["fill"]) - 1)]
    f_today = sim["fill"][day_index * 24 + 8]
    overflow_unserved = [i for i in range(f.shape[0]) if i not in served and f[i] > 100]
    return {"overflows_left_tomorrow": len(overflow_unserved), "overflowing_now_served": int(sum(f_today[i] > 100 for i in served)), "containers_served": len(served)}


# ---------------------------------------------------------------- vision (toy)

def features(img):
    """Colour histogram features of a frame: 4 bins per channel plus mean saturation and the dark share."""
    x = img.reshape(-1, 3).astype(float)
    hist = np.concatenate([np.histogram(x[:, ch], bins=4, range=(0, 255))[0] / len(x) for ch in range(3)])
    mx, mn = x.max(axis=1), x.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    return np.concatenate([hist, [sat.mean(), (mx < 40).mean(), ((x[:, 0] > 150) & (x[:, 1] < 70) & (x[:, 2] < 70)).mean()]])


@functools.lru_cache(maxsize=4)
def vision_model(seed):
    from sklearn.ensemble import HistGradientBoostingClassifier
    imgs, labels = world.image_set(seed, 900)
    X = np.array([features(i) for i in imgs])
    y = np.array(labels)
    cut = 700
    m = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_depth=4, random_state=0).fit(X[:cut], y[:cut])
    pred = m.predict(X[cut:])
    acc = float((pred == y[cut:]).mean())
    maj = float((y[cut:] == max(set(y[:cut]), key=list(y[:cut]).count)).mean())
    cont = y[cut:] == "contamination"
    return {"model": m, "classes": list(m.classes_), "metrics": {"accuracy": round(acc, 3), "majority_class": round(maj, 3), "tested_on": int(len(y) - cut), "contamination_recall": round(float((pred[cont] == "contamination").mean()), 3), "contamination_precision": round(float((y[cut:][pred == "contamination"] == "contamination").mean()), 3) if (pred == "contamination").any() else None},
            "method": "colour histograms of a schematic conveyor frame into gradient-boosted trees; a toy on synthetic images, not a vision model for real cameras"}


def classify(m, png_bytes):
    from PIL import Image
    img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    p = m["model"].predict_proba(features(img)[None])[0]
    k = int(np.argmax(p))
    return {"class": m["classes"][k], "confidence": round(float(p[k]), 3), "contamination": m["classes"][k] == "contamination"}


# ---------------------------------------------------------------- destinations

def destinations(loads, prices, used_kg):
    """Send each load (stream, kg, from) to the accepting facility that maximises commodity value less haul cost, within what each
    facility can still take today: a linear programme. Beside it, the nearest accepting facility with no capacity check. -> dict"""
    from scipy.optimize import linprog
    fac = list(world.FACILITIES)
    pairs = [(li, f) for li, L in enumerate(loads) for f in fac if L["stream"] in world.FACILITIES[f]["accepts"]]
    if not pairs:
        return {"assignments": [], "nearest": []}
    cost = []
    for li, f in pairs:
        L, F = loads[li], world.FACILITIES[f]
        km = world.dist(L["from"], F["at"])
        value = (prices[L["stream"]] * F["yield"] if F["type"] != "transfer" else prices["residual"])              # $/t received; the transfer station charges the gate fee
        cost.append(km * world.HAUL_COST_PER_KM - value * L["kg"] / 1000)                              # per full load: haul $ minus material $
    # x[p] in [0,1]: share of load li sent to f
    A_eq = np.zeros((len(loads), len(pairs)))
    for p, (li, f) in enumerate(pairs):
        A_eq[li, p] = 1
    A_ub, b_ub = [], []
    for f in fac:
        row = np.zeros(len(pairs))
        for p, (li, ff) in enumerate(pairs):
            if ff == f:
                row[p] = loads[li]["kg"]
        A_ub.append(row)
        b_ub.append(max(0.0, world.FACILITIES[f]["daily_kg"] - used_kg.get(f, 0.0)))
    res = linprog(np.array(cost), A_ub=np.array(A_ub), b_ub=np.array(b_ub), A_eq=A_eq, b_eq=np.ones(len(loads)), bounds=[(0, 1)] * len(pairs), method="highs")
    assignments = []
    if res.success:
        for p, (li, f) in enumerate(pairs):
            if res.x[p] > 1e-6:
                L, F = loads[li], world.FACILITIES[f]
                kg = L["kg"] * res.x[p]
                value = (prices[L["stream"]] * F["yield"] if F["type"] != "transfer" else prices["residual"]) * kg / 1000
                assignments.append({"load": li, "vehicle": L.get("vehicle"), "stream": L["stream"], "facility": f, "kg": round(kg), "km": round(world.dist(L["from"], F["at"]), 1), "haul_cost": round(world.dist(L["from"], F["at"]) * world.HAUL_COST_PER_KM, 1), "material_value": round(value, 1), "recovered": F["type"] != "transfer"})
    nearest, used_n, diverted = [], dict(used_kg), {}
    for li, L in enumerate(loads):
        f = min([f for f in fac if L["stream"] in world.FACILITIES[f]["accepts"]], key=lambda f: world.dist(L["from"], world.FACILITIES[f]["at"]))
        F = world.FACILITIES[f]
        room = max(0.0, F["daily_kg"] - used_n.get(f, 0.0))
        taken = min(L["kg"], room)
        used_n[f] = used_n.get(f, 0.0) + taken
        value = (prices[L["stream"]] * F["yield"] if F["type"] != "transfer" else prices["residual"]) * taken / 1000
        haul = world.dist(L["from"], F["at"]) * world.HAUL_COST_PER_KM
        if L["kg"] - taken > 1e-6:                                                                   # turned away at the gate: on to the transfer station, gate fee and the extra leg
            rest = L["kg"] - taken
            T = world.FACILITIES["Transfer Station"]
            diverted[f] = diverted.get(f, 0.0) + rest
            value += prices["residual"] * rest / 1000
            haul += world.dist(F["at"], T["at"]) * world.HAUL_COST_PER_KM
        nearest.append({"load": li, "stream": L["stream"], "facility": f, "kg": L["kg"], "accepted_kg": round(taken), "diverted_kg": round(L["kg"] - taken), "km": round(world.dist(L["from"], F["at"]), 1), "haul_cost": round(haul, 1), "material_value": round(value, 1)})
    turned_away = {f: round(v) for f, v in diverted.items()}
    return {"assignments": assignments, "status": "optimal" if res.success else res.message, "nearest": nearest, "nearest_turned_away_kg": turned_away,
            "totals": {"optimised": {"haul_cost": round(sum(a["haul_cost"] for a in assignments), 1), "material_value": round(sum(a["material_value"] for a in assignments), 1), "recovered_kg": round(sum(a["kg"] for a in assignments if a["recovered"]))},
                       "nearest": {"haul_cost": round(sum(a["haul_cost"] for a in nearest), 1), "material_value": round(sum(a["material_value"] for a in nearest), 1), "turned_away_kg": round(sum(turned_away.values())), "what": "the closest accepting facility; what it cannot take goes on to the transfer station at the gate fee"}}}
