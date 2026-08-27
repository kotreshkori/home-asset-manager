"""
IDP Portal — service catalog.

Reads services.yaml (the single source of truth for the platform) and
renders it as a browsable catalog with deep links into the observability
stack (Grafana / Tempo / Loki) for each service.

This is intentionally a thin read layer for now. The golden-path scaffolder
writes new entries into services.yaml directly; the SLO/error-budget layer
(added later) will populate the `budget` field per service by querying
Prometheus. Until that's wired in, budget is shown as "not tracked".
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template

app = Flask(__name__)

CATALOG_PATH = Path(
    os.environ.get("CATALOG_PATH", Path(__file__).resolve().parent.parent / "services.yaml")
)


def load_catalog() -> list[dict]:
    """Load and lightly validate services.yaml. Missing file -> empty catalog,
    not a crash, so the portal is still usable while the catalog is being set up."""
    if not CATALOG_PATH.exists():
        return []

    with open(CATALOG_PATH, "r") as f:
        data = yaml.safe_load(f) or {}

    services = data.get("services", [])
    for svc in services:
        # Fields the SLO layer will fill in later. Default to "not tracked"
        # so the UI has an honest state instead of a fake number.
        svc.setdefault("budget_remaining_pct", None)
    return services


@app.route("/")
def catalog_view():
    services = load_catalog()
    tiers = sorted({s.get("tier", "unassigned") for s in services})
    return render_template("index.html", services=services, tiers=tiers)


@app.route("/api/services")
def api_services():
    """JSON endpoint — the golden-path scaffolder and SLO tracker read/write
    against services.yaml directly today, but this gives any future tool
    (or a live dashboard refresh) a stable read path."""
    return jsonify(load_catalog())


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "services_loaded": len(load_catalog())})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
