# Evaluation

Everything here is measured on synthetic districts generated with seeds the demo does not use (`(101, 102, 103)`), on the five weekdays before
the demo's Saturday, with the service knowing only the readings and pickups published by 06:00 each day. Reproduce with
`python -m wr.evaluate` (about two minutes). The numbers say how well the methods do against the district as the simulator plays it.
They are not a claim about a real city: real sensors drift and lie, roads are not a grid, and real conveyor frames are not colour blobs.

## 1. Fill forecast and overflow risk, 24 hours ahead

Error in percentage points of fill (capped at 130) against the no-collection truth; the constant-rate baseline uses each container's
overall median rate instead of its weekday rates. The risk list flags containers with at least a 50% chance of overflowing in 24 hours.

| district (seed) | forecasts | error, weekday rates | error, constant rate | would overflow | flagged | caught | precision | recall |
|---|---|---|---|---|---|---|---|---|
| 101 | 2494 | 3.6 | 3.3 | 570 | 562 | 554 | 99% | 97% |
| 102 | 2494 | 3.8 | 3.4 | 575 | 563 | 543 | 96% | 94% |
| 103 | 2499 | 4.0 | 3.7 | 452 | 438 | 423 | 97% | 94% |

Weekday rates did not help on these weekdays: a median over seven or eight same-weekday increments is noisier than a median over
all of them, and the weekday pattern only matters where weekends differ (parks, the venue), which a Monday-to-Friday test does not
exercise. The demo's own back-test, which includes a weekend, has them ahead; the gain is real but small, and most of the error is the
sensor's noise and the hour-to-hour randomness of what people throw away. On ordinary weekdays the list catches about
95% of the containers that would overflow with 97% precision: an ordinary container near full is easy to call. The demo's festival
weekend is the hard case, and there the list misses a quarter, because those containers fill faster than anything in their history.

## 2. Dynamic routes against the fixed schedule

Each weekday: the risk policy's containers routed by the VRP, against the fixed weekly schedule run nearest-first; kilometres, containers
served, and overflows each plan leaves for the next morning under the no-collection truth. Means over five days.

| district (seed) | km, dynamic | km, fixed | served, dynamic | served, fixed | overflows left, dynamic | overflows left, fixed |
|---|---|---|---|---|---|---|
| 101 | 136.7 | 206.7 | 166 | 214 | 1.8 | 8.0 |
| 102 | 135.0 | 220.7 | 166 | 224 | 3.4 | 6.8 |
| 103 | 135.3 | 205.2 | 147 | 207 | 3.0 | 8.6 |

Dynamic routing drives about 36% fewer kilometres and leaves fewer containers to overflow, by visiting fewer containers: the fixed schedule empties
containers that are a third full. On a weekday the two are closer than on the festival weekend the demo shows. Fuel is kilometres
times a constant, so it moves the same way.

## 3. The toy classifier

| district (seed) | test frames | accuracy | majority class | contamination recall |
|---|---|---|---|---|
| 101 | 200 | 100% | 22% | 100% |
| 102 | 200 | 100% | 22% | 100% |
| 103 | 200 | 100% | 20% | 100% |

Perfect or near-perfect scores say the schematic frames are easy, not that the classifier is good; a real conveyor camera would need a
real model and real labelled images, and this slice does not pretend to have either.

## 4. Material destinations

Ten random sets of six loads per district: the optimiser's value less haul cost against the nearest-accepting-facility rule (whose overflow goes on to
the transfer station at the gate fee), and the tonnage that rule has to divert.

| district (seed) | load sets | gain per set ($) | diverted by the nearest rule (kg) |
|---|---|---|---|
| 101 | 10 | 509 | 7058 |
| 102 | 10 | 640 | 3545 |
| 103 | 10 | 445 | 3955 |

The gain is mostly the nearest rule's habit of sending organics and recycling to the transfer station because it is close, where they
pay a gate fee instead of earning anything. The optimiser is a small linear programme; its value is in the constraint, not the maths.

## 5. What is not measured

- Sensor faults beyond missed readings; the simulator's sensors are honest with noise.
- Traffic, road geometry and real service times: the grid at a constant speed is the whole road model.
- Anything about real images.
