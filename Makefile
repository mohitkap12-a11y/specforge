.PHONY: install test lint demo serve
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
