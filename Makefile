.PHONY: install sample reformation all clean

VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip

$(PYTHON): requirements.txt
	@if [ ! -d "$(VENV)" ]; then \
		python3 -m venv "$(VENV)"; \
	fi
	@$(PYTHON) -m pip install --upgrade pip > /dev/null
	@$(PIP) install -r requirements.txt

install: $(PYTHON)

sample: $(PYTHON)
	@$(PYTHON) generate_book.py --config content/pages.yaml

reformation: $(PYTHON)
	@$(PYTHON) generate_book.py --config books/protestant-reformation/pages.yaml

all: sample reformation

clean:
	@rm -rf build/*.pdf

