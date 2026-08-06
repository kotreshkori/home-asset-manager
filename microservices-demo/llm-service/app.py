import logging
import os
import random
import time

import anthropic
from flask import Flask, jsonify, request
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from otel_setup import setup_telemetry

app = Flask(__name__)
tracer = setup_telemetry("llm-service", flask_app=app)
logger = logging.getLogger("llm-service")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MOCK_MODE = os.environ.get("LLM_MOCK_MODE", "false").lower() == "true"
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

LLM_REQUESTS = Counter("llm_requests_total", "Total LLM requests", ["status"])
LLM_LATENCY = Histogram("llm_request_latency_seconds", "LLM call latency in seconds")
LLM_INPUT_TOKENS = Counter("llm_input_tokens_total", "Total input tokens sent to the LLM")
LLM_OUTPUT_TOKENS = Counter("llm_output_tokens_total", "Total output tokens received from the LLM")


def fake_response(question: str) -> dict:
    """Simulates an LLM call without spending real API credits — same shape, same span
    attributes, fake content. Useful for testing the observability pipeline for free."""
    time.sleep(random.uniform(0.2, 0.6))  # pretend it took a moment, like a real call would
    input_tokens = max(1, len(question.split()))
    answer_text = f"[MOCK RESPONSE] This is a simulated answer to: '{question[:60]}'"
    output_tokens = len(answer_text.split())
    return {
        "answer": answer_text,
        "model": f"{MODEL} (mock)",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "finish_reason": "end_turn",
    }


def ask_llm(question: str, max_tokens: int = 300) -> dict:
    """Calls Claude (or a mock, if LLM_MOCK_MODE=true) and wraps it in a span following
    OTel GenAI semantic conventions."""
    with tracer.start_as_current_span("chat anthropic") as span:
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.request.model", MODEL)
        span.set_attribute("gen_ai.request.max_tokens", max_tokens)
        span.set_attribute("gen_ai.mock_mode", MOCK_MODE)

        start = time.time()

        if MOCK_MODE:
            result = fake_response(question)
            duration = time.time() - start
        else:
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": question}],
                )
            except anthropic.APIError as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise

            duration = time.time() - start
            answer_text = "".join(block.text for block in response.content if block.type == "text")
            result = {
                "answer": answer_text,
                "model": response.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "finish_reason": response.stop_reason or "",
            }

        span.set_attribute("gen_ai.response.model", result["model"])
        span.set_attribute("gen_ai.usage.input_tokens", result["input_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", result["output_tokens"])
        span.set_attribute("gen_ai.response.finish_reasons", [result["finish_reason"]])

        LLM_INPUT_TOKENS.inc(result["input_tokens"])
        LLM_OUTPUT_TOKENS.inc(result["output_tokens"])
        LLM_LATENCY.observe(duration)

        result["duration_seconds"] = round(duration, 3)
        return result


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    question = payload.get("question", "").strip()

    if not question:
        LLM_REQUESTS.labels(status="bad_request").inc()
        return jsonify({"error": "expected JSON body: {\"question\": \"...\"}"}), 400

    try:
        result = ask_llm(question)
        LLM_REQUESTS.labels(status="success").inc()
        logger.info("LLM call succeeded, tokens in=%d out=%d", result["input_tokens"], result["output_tokens"])
        return jsonify(result)
    except anthropic.AuthenticationError:
        LLM_REQUESTS.labels(status="auth_error").inc()
        logger.error("LLM call failed: invalid ANTHROPIC_API_KEY")
        return jsonify({"error": "invalid or missing ANTHROPIC_API_KEY"}), 500
    except anthropic.APIError as exc:
        LLM_REQUESTS.labels(status="api_error").inc()
        logger.error("LLM call failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
