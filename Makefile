.PHONY: bootstrap assets verify build start stop status test

bootstrap:
	./scripts/bootstrap.sh

assets:
	./scripts/download-assets.sh

verify:
	./scripts/verify.sh

build:
	cd frontend && npm run build

start:
	./scripts/start.sh

stop:
	./scripts/stop.sh

status:
	./scripts/status.sh

test:
	cd backend && .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q
