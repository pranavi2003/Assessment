.PHONY: install run test lint check-python

# Use the same interpreter for installation, execution, and tests.
# Override with: make test PYTHON=/path/to/python
PYTHON ?= python3

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

run:
	$(PYTHON) -m uvicorn app.gateway:app --host 127.0.0.1 --port 8000

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m compileall -q app tests

check-python:
	$(PYTHON) -c 'import sys; print("Python:", sys.executable); import mcp; print("MCP:", mcp.__file__)'
