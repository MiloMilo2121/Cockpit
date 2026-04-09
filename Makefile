COMPOSE = docker compose

.PHONY: up down restart ps logs logs-core validate

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) down
	$(COMPOSE) up -d --build

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200

logs-core:
	$(COMPOSE) logs -f --tail=200 cockpit-api cockpit-worker

validate:
	$(COMPOSE) config > /dev/null
	@echo "compose validation: OK"
