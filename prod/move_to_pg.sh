#!/bin/sh
# Переїзд team-group-bot з MySQL на PostgreSQL на сервері.
# Покласти поруч з docker-compose.yaml і .env та запустити: sh move_to_pg.sh
set -eu
cd "$(dirname "$0")"

echo "== 1/6 резерв старого бота для відкату"
docker image inspect artemqq5/team-group-bot:mysql-fallback >/dev/null 2>&1 || \
  docker tag "$(docker inspect --format '{{.Image}}' team-group-bot)" artemqq5/team-group-bot:mysql-fallback

echo "== 2/6 завантаження нової версії"
docker compose config -q
docker compose pull

echo "== 3/6 старт PostgreSQL"
docker compose up -d --wait postgres

echo "== 4/6 пробне копіювання (старий бот працює)"
docker compose run --rm --entrypoint "" team-group-bot python -m scripts.mysql_to_pg --dry-run

echo "== 5/6 зупинка бота і перенос"
docker compose stop team-group-bot
docker compose run --rm --entrypoint "" team-group-bot sh -c "alembic upgrade head && python -m scripts.mysql_to_pg"

echo "== 6/6 запуск нового бота"
docker compose up -d team-group-bot
sleep 10
docker compose logs --tail=40 team-group-bot
echo "ГОТОВО"
