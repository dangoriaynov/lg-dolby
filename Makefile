SHELL_SCRIPTS := grab setup.sh host-setup.sh

.PHONY: check lint test

check: lint test

lint:
	for f in $(SHELL_SCRIPTS); do bash -n "$$f"; done
	uvx --from shellcheck-py shellcheck -S warning $(SHELL_SCRIPTS)
	python3 -m py_compile scripts/grab.py scripts/subs-translate.py

test:
	uv run --no-project --python 3.12 --with pytest --with pyyaml pytest -q tests
