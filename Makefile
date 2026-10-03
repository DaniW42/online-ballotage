.PHONY: test up down
test:
	docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm --build test
up:
	docker compose up -d --build
down:
	docker compose down
