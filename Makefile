.PHONY: install test lint demo serve ui ui-dev package
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
# Self-contained wheel (backend + built UI): dist at backend/dist/*.whl. Install anywhere with `pip install <wheel>`.
package: ui
	rm -rf backend/specforge/web backend/dist backend/build
	cp -r frontend/dist backend/specforge/web
	cd backend && pip wheel . --no-deps -w dist
