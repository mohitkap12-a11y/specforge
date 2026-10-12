.PHONY: install test lint demo serve ui ui-dev
install:
	pip install -e "backend[dev]"
test:
	python -m pytest backend/tests -W ignore
lint:
	ruff check backend
demo:
	specforge make-demo ./demo
serve:
	specforge serve
ui:
	cd frontend && npm ci && npm run build
ui-dev:
	cd frontend && npm run dev
