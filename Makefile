COMPOSE = docker compose

.PHONY: up down restart ps logs validate

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

validate:
	$(COMPOSE) config > /dev/null
	@echo "compose validation: OK"
