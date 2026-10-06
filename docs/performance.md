# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 500 containers, 110,000 fill readings, 11,600 pickups,
one plan of 155 stops. Reproduce with `make demo`, then `make load-test`.

## Reads: the risk list, facility capacity and today's fleet, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 735 | 73.5 req/s | 6.7 ms | 22.4 ms | 26.4 ms | 0.00% |
| 8 | 2508 | 249.9 req/s | 29.9 ms | 55.4 ms | 64.7 ms | 0.00% |
| 32 | 1905 | 188.2 req/s | 125.1 ms | 149.0 ms | 162.9 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met. The first run had a p95 of 402 ms at 32 concurrent: the risk list ran a
`DISTINCT ON` over 110,000 readings and 1,500 projections on every call. It is now recomputed only when a reading has arrived or the
clock has moved (the cache key is the readings' max timestamp and count, one cheap query), and the fill rates and the no-collection
truth are cached per clock. The fleet view is one join over the plan's pickups.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Eight weeks of the district, 110,000 readings | 0.8 s |
| Fit fill rates for 500 containers | 0.15 s |
| Risk list, 500 containers × 3 horizons | 20 ms |
| VRP, 155 containers, 10 trips, guided local search | 8 s (the time limit) |
| Fixed-schedule plan, nearest-first | 50 ms |
| Train the toy classifier on 700 frames | 1.8 s |
| Destination LP, 8 loads × 5 facilities | 5 ms |
| Evaluation: three districts, five days each | about a minute |

Not measured: a real sensor feed arriving one reading at a time (the push endpoint checks each reading against the last stored, one
query each, which would want batching at thousands a minute), or a VRP over a whole city.
