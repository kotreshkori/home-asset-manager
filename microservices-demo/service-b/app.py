import logging
import os
import time

import requests
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from otel_setup import setup_telemetry

app = Flask(__name__)
tracer = setup_telemetry("service-b", flask_app=app)
logger = logging.getLogger("service-b")

SERVICE_A_URL = os.environ.get("SERVICE_A_URL", "http://service-a:5000")
LLM_SERVICE_URL = os.environ.get("LLM_SERVICE_URL", "http://llm-service:5000")

REQUEST_COUNT = Counter(
    "service_b_requests_total", "Total requests received", ["endpoint", "http_status"]
)
REQUEST_LATENCY = Histogram(
    "service_b_request_latency_seconds", "Request latency in seconds", ["endpoint"]
)
DOWNSTREAM_ERRORS = Counter(
    "service_b_downstream_errors_total", "Errors calling downstream service-a"
)


@app.route("/")
def home():
    start = time.time()
    payload = {"service": "service-b", "message": "hello from service B"}
    REQUEST_LATENCY.labels(endpoint="/").observe(time.time() - start)
    REQUEST_COUNT.labels(endpoint="/", http_status=200).inc()
    return jsonify(payload)


@app.route("/call-a")
def call_a():
    start = time.time()
    status = 200
    try:
        resp = requests.get(f"{SERVICE_A_URL}/work", timeout=3)
        data = resp.json()
        if resp.status_code >= 500:
            DOWNSTREAM_ERRORS.inc()
            logger.warning("downstream service-a returned an error: %s", data)
        else:
            logger.info("downstream call to service-a succeeded")
    except requests.RequestException as exc:
        status = 502
        data = {"error": str(exc)}
        DOWNSTREAM_ERRORS.inc()
        logger.error("downstream call to service-a failed: %s", exc)

    REQUEST_LATENCY.labels(endpoint="/call-a").observe(time.time() - start)
    REQUEST_COUNT.labels(endpoint="/call-a", http_status=status).inc()
    return jsonify({"service": "service-b", "downstream_response": data}), status


@app.route("/summarize")
def summarize():
    """Calls service-a, then asks the LLM service to explain the result in plain English.
    A realistic 3-hop trace: service-b -> service-a -> service-b -> llm-service."""
    start = time.time()
    status = 200

    try:
        a_resp = requests.get(f"{SERVICE_A_URL}/work", timeout=3)
        a_data = a_resp.json()
    except requests.RequestException as exc:
        REQUEST_LATENCY.labels(endpoint="/summarize").observe(time.time() - start)
        REQUEST_COUNT.labels(endpoint="/summarize", http_status=502).inc()
        logger.error("summarize: call to service-a failed: %s", exc)
        return jsonify({"error": f"service-a unreachable: {exc}"}), 502

    question = f"In one short sentence, explain this API response to a non-technical person: {a_data}"

    try:
        llm_resp = requests.post(f"{LLM_SERVICE_URL}/ask", json={"question": question}, timeout=30)
        llm_data = llm_resp.json()
        if llm_resp.status_code >= 400:
            status = llm_resp.status_code
            logger.warning("summarize: llm-service returned an error: %s", llm_data)
        else:
            logger.info("summarize: LLM explanation generated successfully")
    except requests.RequestException as exc:
        status = 502
        llm_data = {"error": str(exc)}
        logger.error("summarize: call to llm-service failed: %s", exc)

    REQUEST_LATENCY.labels(endpoint="/summarize").observe(time.time() - start)
    REQUEST_COUNT.labels(endpoint="/summarize", http_status=status).inc()
    return jsonify({"service_a_result": a_data, "llm_explanation": llm_data}), status


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
