import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

OTEL_COLLECTOR_ENDPOINT = os.environ.get("OTEL_COLLECTOR_ENDPOINT", "http://otel-collector:4317")


def setup_telemetry(service_name: str, flask_app=None):
    """Wires up tracing and log export for one service. Call this once at startup."""
    resource = Resource.create({"service.name": service_name})

    # --- Tracing: every request becomes a "span" sent to the collector -> Tempo ---
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_COLLECTOR_ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    # --- Logging: Python's normal logging module, mirrored to the collector -> Loki ---
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTEL_COLLECTOR_ENDPOINT, insecure=True))
    )
    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(otel_handler)
    root_logger.setLevel(logging.INFO)

    # --- Auto-instrument Flask (incoming requests) and requests (outgoing calls) ---
    if flask_app is not None:
        FlaskInstrumentor().instrument_app(flask_app)
    RequestsInstrumentor().instrument()

    return trace.get_tracer(service_name)
