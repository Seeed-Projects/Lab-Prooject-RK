# RK3588 Demo Hub Backend

FastAPI control plane for existing RK3588 demos. The Hub does not copy or import demo business code; adapters invoke each project's official entry point.

Production deployment, boot-time startup, and troubleshooting are documented in [../docs/deployment.md](../docs/deployment.md).

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

API documentation is available at `http://localhost:8080/docs`; the unified stream is `ws://localhost:8080/api/v1/stream`.

Run dependency-free core tests with:

```bash
python3 -m unittest discover -s tests -v
```

The default runtime data directory is `./var`. Override locations with `DEMO_HUB_PLUGIN_DIR` and `DEMO_HUB_DATA_DIR`.
