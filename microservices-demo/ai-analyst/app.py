import json
import logging
import os
import statistics
import threading
import time

import anthropic
import requests
from flask import Flask, jsonify, render_template_string
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-analyst")

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
INTERVAL_SECONDS = int(os.environ.get("ANALYST_INTERVAL_SECONDS", "30"))
MOCK_MODE = os.environ.get("LLM_MOCK_MODE", "false").lower() == "true"

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

ANOMALY_GAUGE = Gauge("ai_analyst_anomaly_detected", "1 if the AI analyst flagged an anomaly, else 0")
SEVERITY_GAUGE = Gauge("ai_analyst_severity", "Anomaly severity: 0=none 1=low 2=medium 3=high")

latest_insight = {
    "timestamp": None,
    "anomaly": False,
    "severity": "none",
    "summary": "No analysis run yet.",
}
SEVERITY_LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3}


def prom_query(query: str):
    """Runs a Prometheus instant query, returns a float or None on failure."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
        result = resp.json()["data"]["result"]
        if not result:
            return 0.0
        return float(result[0]["value"][1])
    except Exception as exc:
        logger.warning("Prometheus query failed (%s): %s", query, exc)
        return None


def prom_query_vector(query: str):
    """Runs a Prometheus instant query, returns a list of (labels, value) pairs for every
    matching series — unlike prom_query, which collapses everything into one number."""
    try:
        resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
        result = resp.json()["data"]["result"]
        return [(item["metric"], float(item["value"][1])) for item in result]
    except Exception as exc:
        logger.warning("Prometheus vector query failed (%s): %s", query, exc)
        return []


def service_graph_edges():
    """Real (client, server) call pairs derived by Tempo from actual traces — this is
    genuine observed topology, not something we hand-wrote or guessed."""
    series = prom_query_vector("traces_service_graph_request_total")
    edges = set()
    for labels, _ in series:
        client = labels.get("client")
        server = labels.get("server")
        if client and server and client != "user":
            edges.add((client, server))
    return list(edges)


def prom_query_range(query: str, minutes: int = 60, step_seconds: int = 60):
    """Runs a Prometheus range query, returns a list of (timestamp, float) values
    over the last `minutes` minutes, one point every `step_seconds`."""
    end = time.time()
    start = end - minutes * 60
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step_seconds},
            timeout=10,
        )
        result = resp.json()["data"]["result"]
        if not result:
            return []
        return [(ts, float(val)) for ts, val in result[0]["values"]]
    except Exception as exc:
        logger.warning("Prometheus range query failed (%s): %s", query, exc)
        return []


def zscore_anomaly(query: str, minutes: int = 60, step_seconds: int = 60):
    """Compares the latest value in a time series against its own trailing history.
    Returns a dict with latest/mean/stdev/zscore, or None if there isn't enough history yet."""
    points = prom_query_range(query, minutes=minutes, step_seconds=step_seconds)
    if len(points) < 5:
        return None

    values = [v for _, v in points]
    latest = values[-1]
    history = values[:-1]

    mean = statistics.mean(history)
    stdev = statistics.pstdev(history) if len(history) > 1 else 0.0

    if stdev == 0:
        zscore = 0.0 if latest == mean else float("inf")
    else:
        zscore = (latest - mean) / stdev

    return {"latest": latest, "mean": mean, "stdev": stdev, "zscore": zscore}


def loki_recent_warnings(minutes: int = 5, limit: int = 20):
    """Fetches recent warning/error log lines across all services."""
    end_ns = time.time_ns()
    start_ns = end_ns - minutes * 60 * 1_000_000_000
    query = '{service_name=~".+"} |~ "(?i)error|warn"'
    try:
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "start": start_ns, "end": end_ns, "limit": limit, "direction": "backward"},
            timeout=5,
        )
        streams = resp.json()["data"]["result"]
        lines = []
        for stream in streams:
            for _, line in stream.get("values", []):
                lines.append(line)
        return lines[:limit]
    except Exception as exc:
        logger.warning("Loki query failed: %s", exc)
        return []


def gather_signals() -> dict:
    return {
        "service_a_error_rate": prom_query('sum(rate(service_a_requests_total{http_status=~"5.."}[5m]))'),
        "service_b_error_rate": prom_query('sum(rate(service_b_requests_total{http_status=~"5.."}[5m]))'),
        "service_b_downstream_errors": prom_query("sum(service_b_downstream_errors_total)"),
        "llm_service_error_rate": prom_query('sum(rate(llm_requests_total{status!="success"}[5m]))'),
        "recent_warning_or_error_logs": loki_recent_warnings(),
    }


def ask_llm_for_assessment(signals: dict) -> dict:
    if MOCK_MODE:
        checks = {
            "service-a": 'sum(rate(service_a_requests_total{http_status=~"5.."}[5m]))',
            "service-b": 'sum(rate(service_b_downstream_errors_total[5m]))',
            "llm-service": 'sum(rate(llm_requests_total{status!="success"}[5m]))',
        }

        zscores = {}
        for name, query in checks.items():
            result = zscore_anomaly(query, minutes=30, step_seconds=30)
            if result and result["zscore"] > 0:
                zscores[name] = result["zscore"]

        if not zscores:
            return {"anomaly": False, "severity": "none",
                    "summary": "[MOCK] Not enough history yet to judge what's normal."}

        edges = service_graph_edges()
        for client, server in edges:
            if client in zscores and server in zscores:
                root_z = zscores[server]
                downstream_z = zscores[client]
                if root_z > 2 and downstream_z > 2:
                    severity = "high" if root_z > 5 else "medium" if root_z > 3 else "low"
                    return {
                        "anomaly": severity != "low",
                        "severity": severity,
                        "summary": (
                            f"[MOCK] Correlated incident: {server}'s error rate is elevated "
                            f"(z={root_z:.1f}) and {client}, which calls it directly, is also "
                            f"affected (z={downstream_z:.1f}) \u2014 likely one root cause in "
                            f"{server}, not two separate issues."
                        ),
                    }

        worst_service = max(zscores, key=zscores.get)
        worst_z = zscores[worst_service]
        if worst_z > 5:
            return {"anomaly": True, "severity": "high",
                    "summary": f"[MOCK] {worst_service}'s error rate is far above its own recent baseline (z={worst_z:.1f})."}
        elif worst_z > 3:
            return {"anomaly": True, "severity": "medium",
                    "summary": f"[MOCK] {worst_service}'s error rate is notably above its recent baseline (z={worst_z:.1f})."}
        elif worst_z > 2:
            return {"anomaly": False, "severity": "low",
                    "summary": f"[MOCK] {worst_service}'s error rate is slightly elevated versus its own baseline (z={worst_z:.1f})."}
        return {"anomaly": False, "severity": "none", "summary": "[MOCK] All services within their normal range."}

    prompt = f"""You are monitoring a small microservices system. Here is a snapshot of its current
metrics and recent log lines:

{json.dumps(signals, indent=2)}

Respond with ONLY a JSON object (no markdown, no prose outside the JSON) with these exact keys:
{{"anomaly": true or false, "severity": "none" | "low" | "medium" | "high", "summary": "one or two plain-English sentences describing the system's health and anything notable"}}

Baseline context: this system intentionally simulates ~10% random failures as a demo, so a small,
steady trickle of errors is normal, not an anomaly. Only flag an anomaly if something looks like a
genuine spike, a new failure pattern, or a downstream service becoming unreachable."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text").strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("Could not parse LLM response as JSON: %s", raw_text[:200])
        parsed = {"anomaly": False, "severity": "none", "summary": "Analysis unavailable (parse error)."}

    return parsed


def analysis_loop():
    global latest_insight
    while True:
        try:
            signals = gather_signals()
            assessment = ask_llm_for_assessment(signals)

            severity = assessment.get("severity", "none")
            anomaly = bool(assessment.get("anomaly", False))

            latest_insight = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "anomaly": anomaly,
                "severity": severity,
                "summary": assessment.get("summary", ""),
            }

            ANOMALY_GAUGE.set(1 if anomaly else 0)
            SEVERITY_GAUGE.set(SEVERITY_LEVELS.get(severity, 0))

            log_fn = logger.warning if anomaly else logger.info
            log_fn("AI analysis: anomaly=%s severity=%s summary=%s", anomaly, severity, latest_insight["summary"])

        except anthropic.AuthenticationError:
            logger.error("AI analysis failed: invalid ANTHROPIC_API_KEY")
        except Exception as exc:
            logger.error("AI analysis loop error: %s", exc)

        time.sleep(INTERVAL_SECONDS)


@app.route("/insights")
def insights():
    return jsonify(latest_insight)


@app.route("/")
def dashboard():
    return render_template_string(
        """
        <html><head><meta http-equiv="refresh" content="15">
        <style>
          body { font-family: -apple-system, sans-serif; max-width: 640px; margin: 40px auto; color: #222; }
          .badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-weight: 600; color: white; }
          .none, .low { background: #2e7d32; }
          .medium { background: #ef6c00; }
          .high { background: #c62828; }
          .summary { margin-top: 16px; font-size: 1.1em; line-height: 1.5; }
          .ts { color: #888; font-size: 0.9em; margin-top: 24px; }
        </style></head><body>
        <h2>AI Analyst</h2>
        <span class="badge {{ severity }}">{{ severity | upper }}{% if anomaly %} - ANOMALY{% endif %}</span>
        <p class="summary">{{ summary }}</p>
        <p class="ts">Last checked: {{ timestamp }} (auto-refreshes every 15s)</p>
        </body></html>
        """,
        **latest_insight,
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    thread = threading.Thread(target=analysis_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=5000)
