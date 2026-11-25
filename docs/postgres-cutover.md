# Перехід MySQL -> PostgreSQL: runbook

Усі команди — на сервері, у каталозі `prod/` (де лежить `docker-compose.yaml` бота). Бойовий env-файл бота
(поряд з compose) тут не показується і не вставляється в чати/тікети. Порт `8019:8019` у цьому релізі не змінюється.

## 0. Перед початком

- Образ зібрано з main; у Docker Hub є тег `artemqq5/team-group-bot:<sha коміту>` (незмінний) і `:latest`.
- Тихий час, власник поруч. Орієнтир простою: 5-10 хвилин; повідомлення Telegram за цей час не губляться
  (вебхук без `drop_pending_updates`, Telegram повторює доставку до 24 год).
- У MySQL створено окремого користувача **лише з правом SELECT** на схему бота (не root):
  `CREATE USER 'tgb_ro'@'%' IDENTIFIED BY '...'; GRANT SELECT ON <bot_db>.* TO 'tgb_ro'@'%';`
  Скрипт додатково відкриває сесію `READ ONLY` і ніколи не чіпає інші схеми.

## 1. Знімок «до» (для порівняння після)

Скриншот адмін-статистики бота: групи по категоріях, користувачі, кількість записів в історії розсилок.

## 2. Резерв старого стану (до `docker compose pull`)

```sh
# тегуємо образ, що ЗАРАЗ працює (а не те, що зараз :latest у реєстрі)
docker tag "$(docker inspect --format '{{.Image}}' team-group-bot)" artemqq5/team-group-bot:mysql-fallback
cp docker-compose.yaml docker-compose.mysql.yaml
```

Власник додатково копіює env-файл бота у `<env>.mysql-backup` (одним `cp`, вміст не показувати). За бажання:
`mysqldump --single-transaction <bot_db> > bot_before.sql`.

## 3. Нова конфігурація

1. Покласти новий `docker-compose.yaml` (з сервісом `postgres`).
2. В env-файлі бота: додати `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (літери/цифри, напр. `openssl rand -hex 24`);
   `DB_LINK_CONNECTION=postgresql+asyncpg://USER:PASSWORD@postgres:5432/DB` (ті самі значення);
   `MYSQL_SOURCE_URL=mysql+aiomysql://tgb_ro:...@host.docker.internal:3306/<bot_db>?charset=utf8mb4`
   (обов'язково `charset=utf8mb4`, інакше скрипт зупиниться).
3. Перевірка файлу без виводу секретів: `docker compose config -q` (НЕ без `-q` — друкує підставлені паролі).
4. `docker compose pull`

## 4. Репетиція (старий бот ще працює)

```sh
docker compose up -d postgres            # чекаємо статус healthy: docker compose ps
docker compose run --rm --entrypoint "" team-group-bot python -m scripts.mysql_to_pg --dry-run
```

`--dry-run` читає MySQL (знімок, read-only), створює тимчасову БД на тому ж PG-сервері, застосовує міграцію,
копіює, звіряє і видаляє її. Перевіряє мережу/права з нового контейнера, час і дані. Очікується
`OK (dry-run, nothing committed)`. Якщо `ABORT`/`VERIFY FAILED` — не переходимо, розбираємося.

## 5. Перехід

1. `docker compose stop team-group-bot` — початок простою.
2. Ще раз `docker compose ps` (postgres healthy).
3. ```sh
   docker compose run --rm --entrypoint "" team-group-bot sh -c "alembic upgrade head && python -m scripts.mysql_to_pg"
   ```
   Очікується `OK (committed)`, код виходу 0. Скрипт перевіряє кількість рядків, sha256 по відсортованих рядках
   і незалежні агрегати (кількість, сума довжин текстів, мін/макс дат) у MySQL та PG; при розбіжності відкочує PG
   і виходить з кодом 1. Звіт про зміни (NUL прибрано, нульові дати, ролі в верхній регістр) друкується без текстів.
   Повторний запуск на непорожній цілі зупиняється; `--truncate` очищає лише таблиці PG.
4. `docker compose up -d team-group-bot` — кінець простою.

## 6. Перевірка після переходу

- Логи: `docker compose logs --tail=100 team-group-bot` — «Webhook set», без помилок БД.
- Адмін `/start`: меню працює; групи по категоріях і статистика користувачів збігаються зі знімком «до».
- Історія розсилок на місці; тестова розсилка отримує id = (старий максимум + 1).
- Повідомлення в тестовій групі захоплюється; створити MCP-ключ, виконати `list_chats`, у журналі аудиту є запис.

## 7. Відкат (вікно — 1 доба)

```sh
docker compose down
cp docker-compose.mysql.yaml docker-compose.yaml     # з тегом образу :mysql-fallback
cp <env>.mysql-backup <env-файл бота>
docker compose up -d
```

Чесні межі: усе, що записано лише в PostgreSQL після переходу (нові групи/користувачі, зміни категорій,
розсилки, захоплені повідомлення, MCP-ключі), при відкаті **втрачається**; зворотного копіювання PG -> MySQL немає.
Функція MCP зникає разом зі старим образом. Практично відкат доречний лише в перші години.
Старий MySQL-схему бота не видаляти ~тиждень; видаляє власник, лише схему цього бота (на сервері є інші БД).

## 8. Резервні копії PostgreSQL (щодня, 14 днів)

`/srv/backups/team-group-bot/backup.sh` (змінні `POSTGRES_*` існують лише всередині контейнера — тому `sh -c`):

```sh
#!/bin/sh
set -eu
DIR=/srv/backups/team-group-bot
OUT="$DIR/tgb_$(date +%F).dump"
mkdir -p "$DIR"
docker exec team-group-bot-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$OUT.tmp"
if [ ! -s "$OUT.tmp" ]; then echo "$(date -Is) backup EMPTY" >&2; rm -f "$OUT.tmp"; exit 1; fi
mv "$OUT.tmp" "$OUT"
echo "$(date -Is) backup ok $(wc -c < "$OUT") bytes"
find "$DIR" -name 'tgb_*.dump' -mtime +14 -delete
```

Cron: `30 3 * * * /srv/backups/team-group-bot/backup.sh >> /srv/backups/team-group-bot/backup.log 2>&1`

Ручна копія перед кожним деплоєм: запустити цей же скрипт. Відновлення (бот зупинений):

```sh
docker compose stop team-group-bot
docker exec -i team-group-bot-db sh -c 'pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /srv/backups/team-group-bot/tgb_YYYY-MM-DD.dump
docker compose start team-group-bot
```

Не копіювати живий `./pgdata`. Увага: дамп містить тексти повідомлень і після їх видалення за retention
живе в резервних копіях ще до 14 днів. Позасерверної копії, сповіщень про збій бекапу і моніторингу диска поки немає
(свідоме рішення власника): лог `backup.log` треба іноді дивитись.

## 9. Зміна пароля PostgreSQL

`POSTGRES_PASSWORD` застосовується лише при першій ініціалізації `./pgdata`. Зміна значення в env-файлі
сама пароль не змінює. Порядок: `docker exec -it team-group-bot-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`,
у psql `\password <користувач>`; потім оновити `POSTGRES_PASSWORD` і пароль у `DB_LINK_CONNECTION`
(літери/цифри, інакше url-кодувати) і `docker compose up -d team-group-bot`.

## 10. Розробка: тестова БД

```sh
docker run -d --name tgb-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=tgb_test postgres:16-alpine
.venv/bin/python -m pytest -q tests/
```

`TEST_DATABASE_URL` (за замовчуванням `postgresql+asyncpg://postgres:test@127.0.0.1:55432/tgb_test`); ім'я БД
мусить містити `test` — на старті схема `public` скидається і створюється `alembic upgrade head`.

## 11. Після тижня без відкату (окрема задача)

Прибрати `aiomysql`/`PyMySQL` з `requirements.txt`, `scripts/mysql_to_pg.py`, `MYSQL_SOURCE_URL`, `extra_hosts`
з compose; власник видаляє схему бота у MySQL.
