# Thin wrappers around ./rag
.PHONY: up down status logs doctor seed eval reset dev test
up down status logs doctor seed eval reset dev:
	./rag $@
test:
	. venv/bin/activate && python -m unittest discover -s tests
