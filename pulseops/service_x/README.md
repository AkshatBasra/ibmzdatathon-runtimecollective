# Service X

Service X is a separate, real HTTP process used to demonstrate PulseOps live monitoring.
It is intentionally generic and uses only the Python standard library.

## Run

From the repository root:

```bash
python3 -m service_x.server --port 9100 --rate 12 --workers 2 --delay-ms 80
```

Check it:

```bash
curl http://127.0.0.1:9100/health
curl http://127.0.0.1:9100/metrics
```

Cause an incident by increasing offered traffic:

```bash
curl -X POST http://127.0.0.1:9100/control \
  -H 'Content-Type: application/json' \
  -d '{"traffic_multiplier":4}'
```

Restore normal traffic:

```bash
curl -X POST http://127.0.0.1:9100/control \
  -H 'Content-Type: application/json' \
  -d '{"traffic_multiplier":1,"service_delay_multiplier":1}'
```

Service X is a real process with real threads, queueing, sleep-based work,
request latency, errors and `/proc`-observable CPU/memory. It is not the PulseOps
simulator and can be replaced by another service implementing the same JSON API.
