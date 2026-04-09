COMPOSE = docker compose

.PHONY: up down restart ps logs logs-core logs-watcher backup healthcheck validate

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

logs-watcher:
	$(COMPOSE) logs -f --tail=200 file-watcher

backup:
	./scripts/backup.sh

healthcheck:
	./scripts/healthcheck.sh

validate:
	$(COMPOSE) config > /dev/null
	@echo "compose validation: OK"
