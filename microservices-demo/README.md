# Microservices + Prometheus + Grafana Demo

A minimal but realistic setup: two Flask microservices that talk to each other,
each exposing Prometheus metrics, scraped by Prometheus, visualized in Grafana
(pre-provisioned — no manual dashboard setup needed).

## Stack
- **service-a**: standalone service with a `/work` endpoint (random latency + ~10% simulated 500s)
- **service-b**: calls `service-a` via `/call-a`; `/summarize` chains service-a's result into the LLM service for a plain-English explanation
- **llm-service**: calls Anthropic's Claude API via `/ask`, traced with OpenTelemetry GenAI conventions (model, token counts, latency) and its own Prometheus metrics
- **ai-analyst**: runs in the background, periodically pulls metrics from Prometheus + recent warn/error logs from Loki, asks Claude to assess system health, and serves the verdict at `/` (auto-refreshing page) and `/insights` (JSON)
- **Prometheus**: scrapes all services every 5s (metrics)
- **OTel Collector**: receives traces and logs from the apps via OTLP, routes them onward
- **Tempo**: stores traces, plus derives service-graph metrics and pushes them to Prometheus
- **Loki**: stores logs
- **Grafana**: auto-connected to Prometheus, Tempo, and Loki, with a ready-made dashboard and trace-to-log correlation

## Set up your API key (required for the AI features)

The `llm-service` and `ai-analyst` containers call Anthropic's API, which needs a key.

1. Get a key at https://console.anthropic.com/settings/keys
2. Copy the template and fill it in:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and paste your real key in place of `sk-ant-your-key-here`.
3. `docker compose` reads `.env` automatically — no other setup needed. Don't commit `.env` to git.

Note: these are real, billed API calls (small ones — a few hundred tokens each). Nothing calls the
API automatically except `ai-analyst`, which checks in every 30s by default; you can raise
`ANALYST_INTERVAL_SECONDS` in `docker-compose.yml` if you want it to check less often.

## Run it

```bash
cd microservices-demo
docker compose up --build
```

First build takes a minute or two (installing Python deps). Once it's up:

| Service     | URL                              |
|-------------|-----------------------------------|
| Service A   | http://localhost:5001            |
| Service B   | http://localhost:5002            |
| LLM Service | http://localhost:5003            |
| AI Analyst  | http://localhost:5004 (auto-refreshing health page) |
| Prometheus  | http://localhost:9090            |
| Tempo (traces API) | http://localhost:3200      |
| Loki (logs API)    | http://localhost:3100      |
| Grafana     | http://localhost:3000 (admin/admin) |

## Try it out

Generate some traffic so the dashboard has data to show:

```bash
# hit service-a directly
for i in $(seq 1 50); do curl -s http://localhost:5001/work > /dev/null; done

# hit service-b, which calls service-a internally
for i in $(seq 1 50); do curl -s http://localhost:5002/call-a > /dev/null; done
```

Then open Grafana at http://localhost:3000, log in with `admin` / `admin`,
and go to **Dashboards → Microservices Overview**. You'll see:
- Request rate per service/endpoint
- p95 latency
- 5xx error rate
- Downstream error count (service-b failing to reach service-a)

You can also query raw metrics directly in Prometheus at http://localhost:9090
— try `service_a_requests_total` or `rate(service_b_request_latency_seconds_sum[1m])`.

## Try the AI features

Ask the LLM service directly:
```bash
curl -X POST http://localhost:5003/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is a microservice, in one sentence?"}'
```

Trigger the 3-hop chain (service-b -> service-a -> service-b -> llm-service):
```bash
curl http://localhost:5002/summarize
```
Then look this trace up in Grafana Explore -> Tempo -> Search — it'll show all four hops in one
waterfall, including the LLM call's model name, token counts, and latency as span attributes
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`).

Check what the AI analyst currently thinks of your system's health:
```bash
open http://localhost:5004        # or just visit it in a browser
curl http://localhost:5004/insights
```
It re-checks every 30 seconds automatically — no need to trigger it manually. Its `anomaly` and
`severity` findings are also exposed as Prometheus metrics (`ai_analyst_anomaly_detected`,
`ai_analyst_severity`), so you could wire a Grafana alert on top of an LLM's own judgment call.

## Explore traces and logs (new)

After generating some traffic (see above), in Grafana:

1. Left sidebar → **Explore**
2. Pick the **Tempo** datasource (top-left dropdown), then **Search** → hit **Run query**.
   You'll see a list of traces. Click one to see the waterfall: service-b's request,
   and the nested call into service-a, with exact timings for each hop.
3. Inside a trace, click a span → **Logs for this span** to jump straight to the
   matching log lines in Loki (this is the trace-to-log correlation set up in
   `grafana/provisioning/datasources/datasource.yml`).
4. Or go directly to **Explore → Loki** and query `{service_name="service-a"}` to see
   raw log lines from both apps.

## Project layout

```
microservices-demo/
├── docker-compose.yml
├── .env.example          # copy to .env and add your Anthropic API key
├── service-a/            # Flask app + otel_setup.py + Dockerfile + requirements
├── service-b/            # Flask app (calls service-a, llm-service) + otel_setup.py + Dockerfile
├── llm-service/           # Flask app calling Claude, GenAI-convention traced + Dockerfile
├── ai-analyst/            # Background loop: Prometheus + Loki -> Claude -> health verdict
├── prometheus/
│   └── prometheus.yml           # scrape config
├── otel-collector/
│   └── otel-collector-config.yaml   # routes traces -> Tempo, logs -> Loki
├── tempo/
│   └── tempo.yaml                   # trace storage + service-graph metrics-generator
└── grafana/
    └── provisioning/
        ├── datasources/  # auto-adds Prometheus, Tempo, and Loki as datasources
        └── dashboards/   # auto-loads the overview dashboard
```

## Extending this

- **Add a third microservice**: copy the `service-a` folder, rename it, add it to
  `docker-compose.yml`, and add a scrape target in `prometheus/prometheus.yml`.
- **Different language/framework**: any language works as long as it exposes a
  `/metrics` endpoint in Prometheus text format. Client libraries exist for
  Node.js (`prom-client`), Go (`client_golang`), Java (`micrometer`), etc.
- **Alerting**: add an `alertmanager` service and alerting rules in Prometheus
  if you want Slack/email notifications on error-rate spikes.
- **Persistent data**: metrics and dashboards already persist across restarts
  via the `prometheus-data` and `grafana-data` volumes. Use
  `docker compose down -v` if you want a clean slate.

## Stopping

```bash
docker compose down       # stop containers, keep data
docker compose down -v    # stop containers and wipe volumes
```
