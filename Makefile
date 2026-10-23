.PHONY: install db migrate seed run test cov docs postman

install:
	pip install -r requirements.txt

migrate:
	alembic upgrade head

seed:
	python -m scripts.seed

run:
	uvicorn app.main:app --reload

test:
	python -m pytest

cov:
	python -m pytest --cov=app --cov-report=term-missing

# regenerate generated docs (needs a migrated DATABASE_URL for the OpenAPI import only)
docs:
	python -m scripts.capture_api_samples
	python -m scripts.generate_api_docs
	python -m scripts.generate_er
	python -m scripts.generate_postman

postman:
	python -m scripts.generate_postman
