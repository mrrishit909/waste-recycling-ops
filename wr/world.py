"""An invented city district: 500 smart containers on a street grid, a depot, four recovery facilities, six trucks, eight weeks of
fill readings and pickups under a fixed weekly schedule, and a weekend festival that fills the containers near the venue three times
as fast. Material prices move weekly. Conveyor images are schematic colour blobs. Everything is a pure function of a seed; nothing here
is a real city, a real sensor or a real image."""
import functools

import numpy as np

GRID_KM = 6.0                                  # the district is a 6 km square; streets run on a grid
STREAMS = {"residual": {"density": 0.12, "share": 0.40}, "recycling": {"density": 0.05, "share": 0.30}, "organics": {"density": 0.25, "share": 0.20}, "glass": {"density": 0.25, "share": 0.10}}      # kg per litre when full
SITE_TYPES = {"residential": (0.9, 0.55), "commercial": (1.4, 0.35), "park": (0.7, 0.07), "venue": (1.2, 0.03)}        # (base fill rate %/6h at 1100 l, share of containers)
DOW = {"residential": [1.0, 0.95, 0.95, 0.95, 1.0, 1.15, 1.2], "commercial": [1.2, 1.2, 1.2, 1.2, 1.25, 0.6, 0.3], "park": [0.6, 0.6, 0.6, 0.6, 0.9, 1.8, 1.9], "venue": [0.5, 0.5, 0.5, 0.6, 1.2, 2.0, 1.8]}   # Monday first
READ_EVERY_H = 6
WEEKS = 8
NOW = (WEEKS * 7 + 5) * 24                     # hours: the Saturday after eight full weeks (hour 0 is a Monday 00:00); the demo's "today"
EVENT = {"centre": (4.6, 4.4), "radius_km": 0.9, "from_h": NOW - 30, "to_h": NOW + 42, "factor": 3.0}      # a festival from Friday 18:00 to Sunday 18:00
DEPOT = (0.4, 0.4)
FACILITIES = {"Riverbend MRF": {"type": "recycling", "at": (5.6, 1.0), "accepts": ["recycling"], "daily_kg": 600, "yield": 0.82},
              "Compost Works": {"type": "composting", "at": (0.8, 5.6), "accepts": ["organics"], "daily_kg": 13_000, "yield": 0.9},
              "Glass Plant": {"type": "glass", "at": (5.8, 5.4), "accepts": ["glass"], "daily_kg": 2_200, "yield": 0.95},
              "Transfer Station": {"type": "transfer", "at": (0.2, 3.0), "accepts": ["residual", "recycling", "organics", "glass"], "daily_kg": 200_000, "yield": 0.0},
              "Northgate MRF": {"type": "recycling", "at": (3.0, 5.9), "accepts": ["recycling"], "daily_kg": 500, "yield": 0.78}}
PRICES0 = {"recycling": 85.0, "organics": 12.0, "glass": 25.0, "residual": -95.0}        # $/t received (negative is a gate fee)
HAUL_COST_PER_KM = 2.6
FUEL_L_PER_KM = 0.42
SPEED_KMH = 22.0
SERVICE_MIN = 3.0
VEHICLES = [{"name": "T1", "capacity_kg": 9000, "streams": ["residual"]}, {"name": "T2", "capacity_kg": 9000, "streams": ["residual"]}, {"name": "T3", "capacity_kg": 7000, "streams": ["recycling"]}, {"name": "T4", "capacity_kg": 7000, "streams": ["recycling", "glass"]}, {"name": "T5", "capacity_kg": 8000, "streams": ["organics"]}, {"name": "T6", "capacity_kg": 8000, "streams": ["residual", "recycling", "organics", "glass"]}]
SHIFT = (6, 14)                                # hours


def dist(a, b):
    """Street distance on the grid, km."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


@functools.lru_cache(maxsize=8)
def build(seed=17):
    r = np.random.default_rng(seed)
    n = 500
    types = r.choice(list(SITE_TYPES), n, p=[v[1] for v in SITE_TYPES.values()])
    conts = []
    for i in range(n):
        t = types[i]
        if t == "venue":
            at = (float(np.clip(EVENT["centre"][0] + r.normal(0, 0.35), 0.1, GRID_KM - 0.1)), float(np.clip(EVENT["centre"][1] + r.normal(0, 0.35), 0.1, GRID_KM - 0.1)))
        elif t == "commercial":
            at = (float(r.uniform(1.5, 4.5)), float(r.uniform(1.0, 3.5)))
        else:
            at = (float(r.uniform(0.1, GRID_KM - 0.1)), float(r.uniform(0.1, GRID_KM - 0.1)))
        stream = r.choice(list(STREAMS), p=[v["share"] for v in STREAMS.values()])
        cap = int(r.choice([1100, 3200], p=[0.7, 0.3]))
        every = 1 if (t == "commercial" and stream == "residual") else 2 if stream in ("residual", "organics") else 7
        steps = {1: 4, 2: 9.3, 7: 28}[every]                                                    # six-hour steps between collections
        # the fixed schedule was designed for the container: it reaches about three quarters full between collections, on an ordinary week
        base = 65.0 * float(r.uniform(0.7, 1.15)) / steps / float(np.mean(DOW[t]))
        conts.append({"idx": i, "sensor_id": f"SB-{seed:03d}-{i:04d}", "site_type": t, "stream": str(stream), "capacity_l": cap, "at": (round(at[0], 3), round(at[1], 3)), "rate": base,
                      "schedule_day": int(r.integers(0, 6)), "schedule_every": every, "battery": float(r.uniform(0.3, 1.0))})
    prices = {}
    for m, p0 in PRICES0.items():
        path, p = [], p0
        for w in range(WEEKS + 2):
            p = p + r.normal(0, abs(p0) * 0.04)
            path.append(round(float(p), 2))
        prices[m] = path
    return {"seed": seed, "containers": conts, "prices": prices}


def event_factor(c, h):
    if EVENT["from_h"] <= h < EVENT["to_h"] and dist(c["at"], EVENT["centre"]) <= EVENT["radius_km"]:
        return EVENT["factor"]
    return 1.0


def scheduled(c, day):
    """Is this container on the fixed weekly plan for `day` (0 = Monday)?"""
    e = c["schedule_every"]
    if day == 6:
        return False                                                                            # no collections on Sunday
    if e == 7:
        return day == c["schedule_day"]
    if e == 1:
        return True
    return day in ((c["schedule_day"] % 2, (c["schedule_day"] % 2) + 2, (c["schedule_day"] % 2) + 4))


def simulate(w, until_h, event=True, dynamic_pickups=None, pickups_until_h=None):
    """Fill levels hour by hour for every container, pickups under the fixed schedule (collections at 08:00 on a scheduled weekday) or
    under `dynamic_pickups` {day_index: set(container idx)}, and six-hourly sensor readings with noise. -> dict
    Days in `dynamic_pickups` use the given sets; days not in it keep the fixed schedule."""
    r = np.random.default_rng([w["seed"], 99])
    n = len(w["containers"])
    fill = np.zeros((until_h + 1, n))
    readings, pickups = [], []
    level = np.array([float(r.uniform(5, 60)) for _ in range(n)])
    for h in range(1, until_h + 1):
        day, hour = (h // 24) % 7, h % 24
        for i, c in enumerate(w["containers"]):
            inc = c["rate"] * DOW[c["site_type"]][day] * event_factor(c, h) * (1 if event else 1 / event_factor(c, h)) / READ_EVERY_H * float(np.exp(r.normal(0, 0.25)))
            level[i] = min(130.0, level[i] + inc)                                              # past 100% it overflows beside the container
        if hour == 8 and (pickups_until_h is None or h <= pickups_until_h):              # a counterfactual: no collections after `pickups_until_h`
            d = h // 24
            due = dynamic_pickups[d] if dynamic_pickups is not None and d in dynamic_pickups else {i for i, c in enumerate(w["containers"]) if scheduled(c, day)}
            for i in due:
                kg = round(float(min(level[i], 100.0) / 100 * w["containers"][i]["capacity_l"] * STREAMS[w["containers"][i]["stream"]]["density"]), 1)
                pickups.append({"container": i, "hour": h, "weight_kg": kg, "overflowed": bool(level[i] > 100.0)})
                level[i] = float(r.uniform(0, 4))
        fill[h] = level
        if hour % READ_EVERY_H == 0:
            for i, c in enumerate(w["containers"]):
                if r.random() < 0.985 * c["battery"] ** 0.2:                                      # weak batteries miss readings
                    readings.append({"container": i, "hour": h, "fill_pct": round(float(np.clip(level[i] + r.normal(0, 2.5), 0, 100)), 1), "temp_c": round(float(14 + 6 * np.sin(2 * np.pi * (hour - 8) / 24) + (8 if c["stream"] == "organics" else 0) + r.normal(0, 1)), 1), "battery_pct": round(float(100 * max(0.05, c["battery"] - h / (24 * 400))), 1)})
    return {"fill": fill, "readings": readings, "pickups": pickups}


def conveyor_image(r, cls, size=48):
    """A schematic conveyor frame: a dark belt with coloured fragments of one material class; contamination is black film with red specks."""
    palette = {"paper": [(205, 190, 150), (225, 215, 185), (190, 170, 130)], "plastic": [(40, 120, 220), (230, 60, 60), (60, 200, 90), (240, 220, 50)], "metal": [(150, 150, 160), (190, 190, 200), (110, 110, 120)],
               "organics": [(90, 60, 30), (60, 110, 40), (120, 80, 40)], "glass": [(60, 150, 110), (110, 170, 150), (40, 110, 80)], "contamination": [(15, 15, 15), (25, 20, 20), (200, 30, 30)]}
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:] = (45, 45, 48)
    for k in range(int(r.integers(4, 9))):
        col = palette[cls][int(r.integers(0, len(palette[cls])))]
        x, y = int(r.integers(0, size - 8)), int(r.integers(0, size - 8))
        wdt, hgt = int(r.integers(5, 16)), int(r.integers(5, 16))
        img[y:y + hgt, x:x + wdt] = np.clip(np.array(col) + r.normal(0, 8, 3), 0, 255).astype(np.uint8)
    img = np.clip(img.astype(int) + r.normal(0, 4, img.shape), 0, 255).astype(np.uint8)
    return img


def image_bytes(img):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def image_set(seed, n):
    """Labelled schematic frames for training and testing the toy classifier. -> (images, labels)"""
    r = np.random.default_rng([seed, 7])
    classes = ["paper", "plastic", "metal", "organics", "glass", "contamination"]
    labels = [classes[int(r.integers(0, 6))] for _ in range(n)]
    return [conveyor_image(r, c) for c in labels], labels
