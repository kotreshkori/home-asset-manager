#!/usr/bin/env python3
"""
Golden-path scaffolder.

Generates a new Flask microservice pre-wired with OpenTelemetry tracing,
structured logging, and a Prometheus /metrics endpoint — the same pattern
used by service-a and service-b — and automatically wires it into the
platform so it's observable from the moment it exists, not after someone
remembers to set that up.

One command does all of this:
  1. Creates <service-name>/ with app.py, otel_setup.py, requirements.txt,
     Dockerfile — copied/rendered from golden-path/template/
  2. Appends an entry to services.yaml (shows up in the catalog immediately)
  3. Appends a service block to docker-compose.yml, on the same network as
     everything else
  4. Appends a scrape target to prometheus/prometheus.yml

Usage (run from the microservices-demo root):
  python golden-path/create_service.py order-service \
      --owner platform-team --tier tier-2 \
      --description "Handles order lifecycle and status transitions."

Port is auto-assigned (next free port after the highest one already in
services.yaml). Pass --port to override.

This script only appends — it never rewrites or reformats your existing
files, so hand-written comments and formatting in docker-compose.yml and
prometheus.yml are left exactly as they are.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # microservices-demo/
TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
SERVICES_YAML = ROOT / "services.yaml"
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROMETHEUS_FILE = ROOT / "prometheus" / "prometheus.yml"

VALID_TIERS = {"tier-1", "tier-2", "tier-3"}
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        fail(
            f"'{name}' is not a valid service name — use lowercase letters, "
            "digits, and hyphens only (e.g. order-service)."
        )
    if (ROOT / name).exists():
        fail(f"a directory named '{name}' already exists at {ROOT / name}")


def load_services() -> list[dict]:
    if not SERVICES_YAML.exists():
        fail(f"services.yaml not found at {SERVICES_YAML} — run this from the microservices-demo root")
    with open(SERVICES_YAML) as f:
        data = yaml.safe_load(f) or {}
    return data.get("services", [])


def next_free_port(services: list[dict], requested: int | None) -> int:
    used = {s["port"] for s in services if "port" in s}
    if requested is not None:
        if requested in used:
            fail(f"port {requested} is already used by another service in services.yaml")
        return requested
    candidate = max(used, default=5000) + 1
    while candidate in used:
        candidate += 1
    return candidate


def check_name_collision(services: list[dict], name: str) -> None:
    if any(s.get("name") == name for s in services):
        fail(f"'{name}' is already registered in services.yaml")


def render_service_files(name: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True)
    metric_prefix = name.replace("-", "_")

    app_tmpl = (TEMPLATE_DIR / "app.py.tmpl").read_text()
    app_rendered = app_tmpl.replace("{{SERVICE_NAME}}", name).replace(
        "{{METRIC_PREFIX}}", metric_prefix
    )
    (dest_dir / "app.py").write_text(app_rendered)

    for static_file in ("otel_setup.py", "requirements.txt", "Dockerfile"):
        (dest_dir / static_file).write_text((TEMPLATE_DIR / static_file).read_text())


def append_to_services_yaml(name: str, owner: str, tier: str, port: int, description: str) -> None:
    entry = f"""
  - name: {name}
    owner: {owner}
    repo: ./{name}
    language: python-flask
    port: {port}
    tier: {tier}
    description: {description}
    links:
      dashboard: http://localhost:3000/d/microservices-overview
      traces: http://localhost:3000/explore
      logs: http://localhost:3000/explore
    slo:
      sli: success_rate
      target: 99.0
      window_days: 30
"""
    with open(SERVICES_YAML, "a") as f:
        f.write(entry)


def append_to_compose(name: str, port: int) -> None:
    text = COMPOSE_FILE.read_text()
    marker = "networks:\n  monitoring:"
    if marker not in text:
        fail(
            "couldn't find the 'networks:' block in docker-compose.yml to insert before — "
            "add the service block manually, it's printed above for reference."
        )

    block = f"""  {name}:
    build: ./{name}
    container_name: {name}
    environment:
      - OTEL_COLLECTOR_ENDPOINT=http://otel-collector:4317
    ports:
      - "{port}:5000"
    depends_on:
      - otel-collector
    networks:
      - monitoring

"""
    updated = text.replace(marker, block + marker)
    COMPOSE_FILE.write_text(updated)


def append_to_prometheus(name: str) -> None:
    text = PROMETHEUS_FILE.read_text()
    block = f"""
  - job_name: "{name}"
    static_configs:
      - targets: ["{name}:5000"]
        labels:
          service: "{name}"
"""
    PROMETHEUS_FILE.write_text(text.rstrip("\n") + "\n" + block)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold a new pre-instrumented service.")
    parser.add_argument("name", help="Service name, e.g. order-service")
    parser.add_argument("--owner", default="platform-team")
    parser.add_argument("--tier", default="tier-2", choices=sorted(VALID_TIERS))
    parser.add_argument("--port", type=int, default=None, help="Host port (default: next free port)")
    parser.add_argument(
        "--description",
        default="Describe what this service does — edit services.yaml to update.",
    )
    args = parser.parse_args()

    validate_name(args.name)
    services = load_services()
    check_name_collision(services, args.name)
    port = next_free_port(services, args.port)

    dest_dir = ROOT / args.name
    render_service_files(args.name, dest_dir)
    append_to_services_yaml(args.name, args.owner, args.tier, port, args.description)
    append_to_compose(args.name, port)
    append_to_prometheus(args.name)

    print(f"✓ Created {dest_dir}/ (app.py, otel_setup.py, requirements.txt, Dockerfile)")
    print(f"✓ Registered '{args.name}' in services.yaml on port {port}")
    print(f"✓ Added '{args.name}' to docker-compose.yml, on the monitoring network")
    print(f"✓ Added a Prometheus scrape target for '{args.name}'")
    print()
    print("Next steps:")
    print(f"  docker compose up -d --build {args.name} prometheus")
    print(f"  open http://localhost:8080   # new card should already be in the catalog")


if __name__ == "__main__":
    main()
