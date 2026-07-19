# Payment Service

`payment-service` — сервис надёжной отправки платёжных операций внешнему
провайдеру. Он сохраняет состояние операции и намерение отправки в PostgreSQL,
безопасно повторяет запросы с тем же ключом идемпотентности и принимает финальный
результат только через callback-квитанцию провайдера.

Проект реализован на Python 3.14, FastAPI, SQLAlchemy asyncio, PostgreSQL и
запускается вместе с
`ghcr.io/fintech-dev-lab/internship-provider-simulator:v0.2.0` через Docker
Compose.

## Платёжный сценарий

1. Клиент создаёт операцию через `POST /operations`. Начальный статус —
   `CREATED`.
2. Первый `POST /operations/{operationId}/submit` в одной транзакции:
   - переводит операцию в `PROCESSING`;
   - создаёт единственный durable submission intent;
   - сохраняет неизменный payload, `Idempotency-Key` и
     `X-Correlation-ID`;
   - создаёт событие `PROCESSING`.
3. Фоновый `DispatchWorker` забирает intent и вызывает реальный
   `provider-simulator` по `POST /payments`. HTTP-запрос выполняется после
   фиксации intent и вне транзакции БД.
4. Ответ провайдера `202 Accepted` сохраняет `providerPaymentId`, но не
   завершает операцию.
5. Simulator отправляет квитанцию на `POST /receipts`. Только она переводит
   операцию в `COMPLETED` или `REJECTED`.
6. Повторные submit, HTTP-запросы провайдеру и callback обрабатываются
   идемпотентно.
7. После перезапуска незавершённые intent восстанавливаются, а операции,
   платежные идентификаторы и события остаются в PostgreSQL.

## Архитектура

```text
Client
  |
  v
candidate-service (FastAPI, port 8080)
  |        ^
  |        | POST /receipts
  v        |
PostgreSQL |  operations
           |  submission_intents
           |  operation_events
           |  receipts
  ^
  | claim / finalize
DispatchWorker
  |
  | POST /payments
  v
provider-simulator (port 8081)
```

- **candidate-service** — API, транзакционная бизнес-логика, provider client и
  фоновый worker.
- **PostgreSQL** — источник истины и постоянное хранилище. Named volume
  `postgres-data` сохраняет данные между пересозданиями контейнеров.
- **provider-simulator** — внешний HTTP-провайдер. Получает callback URL
  `http://candidate-service:8080/receipts`.
- **DispatchWorker** — последовательно выбирает готовые intent, устанавливает
  lease, выполняет HTTP-запрос и отдельной короткой транзакцией сохраняет
  результат или планирует retry.
- **submission_intents** — transactional outbox: один intent на одну операцию,
  сохранённый request payload, ключи идемпотентности, состояние попыток, lease и
  время следующего retry.
- **operation_events** — упорядоченная история переходов операции. `eventId`
  начинается с 1 и монотонно растёт в пределах операции.
- **receipts** — аудит применённых, повторных, проигнорированных и конфликтующих
  callback.

Все Compose-сервисы находятся в общей сети, которую Docker Compose создаёт
автоматически.

Compose передаёт candidate-service
`PROVIDER_URL=http://provider-simulator:8081`, а simulator —
`CALLBACK_URL=http://candidate-service:8080/receipts`.

## Требования и запуск

Для запуска нужны только:

- Docker Desktop либо Docker Engine с Docker Compose;
- свободные TCP-порты `8080` и `8081`;
- доступ к публичному образу simulator и Python package index во время сборки.

Локальная установка Python или PostgreSQL для запуска приложения не требуется.
Из корня репозитория выполните:

```bash
docker compose up --build -d
```

Compose собирает `candidate-service`, запускает PostgreSQL, дожидается его
готовности, автоматически выполняет `alembic upgrade head`, затем запускает
Uvicorn. Ручное создание таблиц и локальные `.env`-файлы не нужны.

### Проверка состояния

```bash
docker compose ps
curl -i http://localhost:8080/health
curl -i http://localhost:8081/health
docker compose logs --tail=100 candidate-service
docker compose logs --tail=100 provider-simulator
```

Оба health endpoint в исправном состоянии возвращают:

```json
{"status":"ok"}
```

Health candidate-service также проверяет соединение с PostgreSQL и при
недоступной БД возвращает `503 Service Unavailable`.

## API

Базовый URL candidate-service: `http://localhost:8080`.

### `POST /operations`

Создаёт новую операцию и событие `CREATED`.

```bash
curl -i -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d '{"operationId":"operation-123","amount":"1000.00","currency":"RUB","description":"Оплата заказа"}'
```

Ответ `201 Created`:

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "CREATED",
  "providerPaymentId": null,
  "createdAt": "2026-07-19T12:00:00Z",
  "updatedAt": "2026-07-19T12:00:00Z"
}
```

Правила валидации:

- `operationId` — непустая строка длиной не более 128 символов;
- `amount` — именно JSON-строка с положительным десятичным числом и не более
  чем двумя знаками после точки;
- поддерживается только точное значение `RUB`;
- `description` необязателен, допускает `null` и имеет длину не более 500
  символов;
- сумма в ответе нормализуется до двух десятичных знаков.

Основные статусы:

- `201 Created` — операция создана;
- `409 Conflict` — такой `operationId` уже существует;
- `422 Unprocessable Entity` — ошибка структуры или валидации.

### `POST /operations/{operationId}/submit`

Надёжно планирует отправку. Тело запроса отсутствует.

```bash
curl -i -X POST http://localhost:8080/operations/operation-123/submit
```

Первый вызов возвращает `202 Accepted` и текущее представление операции:

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "PROCESSING",
  "providerPaymentId": null,
  "createdAt": "2026-07-19T12:00:00Z",
  "updatedAt": "2026-07-19T12:00:01Z"
}
```

Основные статусы:

- `202 Accepted` — первый submit создал intent и переход `PROCESSING`;
- `200 OK` — операция уже `PROCESSING`, `COMPLETED` или `REJECTED`;
  возвращается её текущее состояние без нового intent;
- `404 Not Found` — операция не найдена.

### `GET /operations/{operationId}`

Возвращает актуальное состояние операции.

```bash
curl -i http://localhost:8080/operations/operation-123
```

Пример финального ответа `200 OK`:

```json
{
  "operationId": "operation-123",
  "amount": "1000.00",
  "currency": "RUB",
  "description": "Оплата заказа",
  "status": "COMPLETED",
  "providerPaymentId": "aa5b7856-e9f2-4fd5-955b-38b1f28d9c57",
  "createdAt": "2026-07-19T12:00:00Z",
  "updatedAt": "2026-07-19T12:00:03Z"
}
```

Основные статусы: `200 OK` и `404 Not Found`.

### `GET /operations/{operationId}/events`

Возвращает события по возрастанию `eventId`.

```bash
curl -i http://localhost:8080/operations/operation-123/events
```

Пример ответа `200 OK`:

```json
[
  {
    "eventId": 1,
    "type": "CREATED",
    "fromStatus": null,
    "toStatus": "CREATED",
    "message": "Operation created",
    "occurredAt": "2026-07-19T12:00:00Z"
  },
  {
    "eventId": 2,
    "type": "PROCESSING",
    "fromStatus": "CREATED",
    "toStatus": "PROCESSING",
    "message": "Operation submitted",
    "occurredAt": "2026-07-19T12:00:01Z"
  },
  {
    "eventId": 3,
    "type": "COMPLETED",
    "fromStatus": "PROCESSING",
    "toStatus": "COMPLETED",
    "message": "Payment completed",
    "occurredAt": "2026-07-19T12:00:03Z"
  }
]
```

Основные статусы: `200 OK` и `404 Not Found`. Повторная или поздняя
квитанция не создаёт ещё один финальный переход.

### `POST /receipts`

Callback endpoint для provider-simulator. Успешная обработка возвращает пустой
`204 No Content`.

```bash
curl -i -X POST http://localhost:8080/receipts \
  -H 'Content-Type: application/json' \
  -d '{"providerPaymentId":"aa5b7856-e9f2-4fd5-955b-38b1f28d9c57","operationId":"operation-123","result":"COMPLETED","message":"Payment completed","occurredAt":"2026-07-19T12:00:03Z"}'
```

Успешный ответ:

```http
HTTP/1.1 204 No Content
```

`result` принимает только `COMPLETED` или `REJECTED`, а `occurredAt`
должен содержать часовой пояс.

Основные статусы:

- `204 No Content` — квитанция применена, повторена или проигнорирована после
  финализации;
- `404 Not Found` — операция не найдена;
- `409 Conflict` — операция ещё `CREATED` либо получен другой
  `providerPaymentId`;
- `422 Unprocessable Entity` — callback не соответствует схеме.

### `GET /health`

Проверяет приложение и доступность PostgreSQL.

```bash
curl -i http://localhost:8080/health
```

Ответ `200 OK`:

```json
{"status":"ok"}
```

При недоступной базе возвращается `503 Service Unavailable` с
`{"detail":"Database unavailable"}`.

## Полный E2E-сценарий

Следующий блок рассчитан на Bash/zsh и не требует `jq`. Simulator версии
`v0.2.0` по умолчанию работает в success-режиме.

```bash
OPERATION_ID="readme-e2e-$(date +%s)-$$"
payload="$(printf '{"operationId":"%s","amount":"1000.00","currency":"RUB","description":"README E2E check"}' "$OPERATION_ID")"

curl -fsS -X POST http://localhost:8080/operations \
  -H 'Content-Type: application/json' \
  -d "$payload"
printf '\n'

curl -fsS -X POST "http://localhost:8080/operations/$OPERATION_ID/submit"
printf '\n'

attempt=1
while [ "$attempt" -le 30 ]; do
  operation_json="$(curl -fsS "http://localhost:8080/operations/$OPERATION_ID")"
  printf '%s\n' "$operation_json"
  case "$operation_json" in
    *'"status":"COMPLETED"'*) break ;;
    *'"status":"REJECTED"'*) break ;;
  esac
  attempt=$((attempt + 1))
  sleep 1
done

case "$operation_json" in
  *'"status":"COMPLETED"'*) ;;
  *) echo "Operation did not reach COMPLETED" >&2; exit 1 ;;
esac

curl -fsS "http://localhost:8080/operations/$OPERATION_ID/events"
printf '\n'

docker compose logs --tail=500 provider-simulator | grep -F "$OPERATION_ID"
```

Ожидаемый результат по умолчанию:

- операция проходит `CREATED → PROCESSING → COMPLETED`;
- в истории находятся ровно три перехода с `eventId` 1, 2 и 3;
- в логах simulator есть `payment accepted` с `replay:false`, один
  `providerPaymentId` и `callback delivered` с `result:"COMPLETED"` и
  `statusCode:204`.

В PowerShell используйте `curl.exe`, а не возможный alias `curl`. Уникальный
ID можно создать так:

```powershell
$operationId = "readme-e2e-$([guid]::NewGuid())"
```

Для фильтрации логов в PowerShell:

```powershell
docker compose logs --tail=500 provider-simulator |
  Select-String -SimpleMatch $operationId
```

## Идемпотентность

- `operations.operation_id` уникален: один `operationId` соответствует одной
  локальной операции.
- На `submission_intents.operation_id` действует уникальное ограничение:
  конкурентные и повторные submit не создают второй intent.
- Первый submit сохраняет request payload до HTTP-вызова. Все повторы используют
  именно этот снимок, а не формируют новое тело.
- `Idempotency-Key` и `X-Correlation-ID` всегда равны `operationId`.
- Повторный submit возвращает текущее состояние с `200 OK` и не создаёт новый
  переход.
- Повторная отправка `POST /payments` допустима: прежний ключ и payload
  позволяют simulator вернуть тот же `providerPaymentId`, не создавая второй
  фактический платёж.

Гарантируется не один HTTP-запрос, а не более одного платежа провайдера на
операцию.

## Retry и восстановление

Timeout, network error и любой ответ провайдера `5xx` считаются retryable:

- операция остаётся `PROCESSING`;
- intent становится `RETRY_WAIT`;
- сохраняются ошибка, номер попытки и `next_attempt_at`;
- повтор использует прежние ключи и payload;
- применяется exponential backoff с jitter;
- задержка увеличивается до максимума 15 минут.

Количество retryable-попыток кодом не ограничено. Ответы `4xx` и
некорректный ответ `202` не устанавливают `REJECTED`: intent переходит во
внутренний статус `BLOCKED`, а операция остаётся `PROCESSING`.

При захвате intent worker переводит его в `IN_FLIGHT` и устанавливает
30-секундный lease. HTTP выполняется без открытой транзакции и без удержания
блокировки операции. Intent с истёкшим lease может быть захвачен повторно.

Lifespan полностью завершает короткую startup recovery транзакцию до создания
задачи `DispatchWorker`:

- `PENDING` не изменяется, а worker подбирает его при наступившем
  `next_attempt_at`;
- `RETRY_WAIT` сохраняет исходное расписание: наступивший retry подбирается
  worker, а будущий не ускоряется;
- `IN_FLIGHT` intent операции `PROCESSING` считается оставшимся от
  предыдущего процесса и немедленно переводится в готовый `RETRY_WAIT`;
- `ACCEPTED` intent операции `PROCESSING` также переводится в готовый
  `RETRY_WAIT`, сохраняя `providerPaymentId`;
- `RESOLVED`, `BLOCKED` и intent финальных операций не восстанавливаются.

Payload, `Idempotency-Key`, `X-Correlation-ID` и `attempt_count` при
startup recovery не меняются. Поэтому остановка во время HTTP-запроса или между
принятием платежа провайдером и локальным сохранением ответа приводит к
безопасному повтору того же платежа.

При остановке приложения задача worker отменяется, его HTTP-клиент закрывается,
а незавершённая отправка остаётся в PostgreSQL для следующего запуска.

## Callback-квитанции

- Первая валидная квитанция для `PROCESSING` устанавливает
  `providerPaymentId` при его отсутствии и переводит операцию в
  `COMPLETED` или `REJECTED`.
- Callback может прийти раньше ответа `202`. Поздний ответ не возвращает
  финальную операцию в `PROCESSING`.
- Точный повтор определяется по SHA-256 канонического payload, увеличивает
  счётчик доставок, возвращает `204` и не создаёт второй переход.
- Квитанция с тем же ID после финализации сохраняется в аудите как
  проигнорированная и возвращает `204`.
- Поздний противоположный результат также возвращает `204`, фиксируется в
  таблице `receipts` и не меняет финальный статус.
- Другой `providerPaymentId` фиксируется как конфликт, возвращает `409` и не
  меняет операцию.

Финальный статус неизменяем, а `operation_events` содержит только фактические
переходы состояния.

## Проверка после restart

После завершения E2E-сценария сохраните его `operationId` и выполните:

```bash
docker compose restart candidate-service

curl -i http://localhost:8080/health
curl -i "http://localhost:8080/operations/$OPERATION_ID"
curl -i "http://localhost:8080/operations/$OPERATION_ID/events"
curl -i -X POST "http://localhost:8080/operations/$OPERATION_ID/submit"
```

После restart операция и история должны сохраниться, а повторный submit
финальной операции должен вернуть `200 OK` без второго платежа. Для этой
проверки не выполняйте `docker compose down -v`: флаг `-v` удаляет
PostgreSQL volume.

## Миграции

При каждом старте candidate-service Dockerfile сначала выполняет:

```bash
alembic upgrade head
```

Текущую revision можно проверить внутри контейнера:

```bash
docker compose exec candidate-service alembic current
```

Для текущей схемы ожидается:

```text
0001_initial_schema (head)
```

## Тесты и Ruff

Локальный запуск тестов требует:

- Python 3.14;
- доступную PostgreSQL, заданную через `DATABASE_URL`;
- отдельную тестовую БД без ценных данных.

Создайте виртуальное окружение удобным для вашей ОС способом, активируйте его и
установите runtime- и dev-зависимости из `pyproject.toml`:

```bash
python -m pip install -e ".[dev]"
```

Перед тестами задайте URL тестовой PostgreSQL. Например, для локальной БД:

```bash
export DATABASE_URL='postgresql+asyncpg://postgres:postgres@localhost:5432/candidate_test'
```

PowerShell:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/candidate_test"
```

Команды проверки:

```bash
alembic upgrade head
pytest
ruff check .
ruff format --check .
```

Тесты используют PostgreSQL-транзакции и блокировки; SQLite не является
поддерживаемой заменой. Recovery-тесты создают собственную временную PostgreSQL
schema и удаляют её после выполнения.

## Полезные Docker-команды

```bash
# Логи всех сервисов
docker compose logs --tail=200

# Логи конкретного сервиса
docker compose logs -f candidate-service
docker compose logs -f provider-simulator

# Перезапуск только приложения
docker compose restart candidate-service

# Остановка контейнеров без удаления
docker compose stop

# Удаление контейнеров и сети с сохранением named volume
docker compose down
```

Осторожно:

```bash
docker compose down -v
```

Команда с `-v` удаляет `postgres-data` и все сохранённые операции, intent,
квитанции и события.

## Гарантии и ограничения

- Intent, переход в `PROCESSING` и событие создаются атомарно до обращения к
  провайдеру.
- Внешний HTTP-запрос выполняется вне транзакции БД; callback не блокируется на
  время ожидания провайдера.
- Только callback устанавливает финальный статус. Timeout, network error,
  `5xx`, `4xx` или ответ `202` не создают локальный `REJECTED`.
- Финальный статус не откатывается поздним ответом или callback.
- Simulator `v0.2.0` по умолчанию работает в success-режиме.
- У использованного образа simulator не найден документированный admin/audit
  API. В ручном E2E аудит количества платежей выполняется по
  `providerPaymentId`, `replay` и строкам callback в логах контейнера.
- В README намеренно не указаны неподтверждённые параметры для принудительных
  сценариев `REJECTED`, `503`, lost response или early callback.
- Аутентификация callback исходным контрактом не предусмотрена.
- Метрики и отдельный административный API не реализованы.

## Структура проекта

```text
app/
  api/                 HTTP-роутеры health, operations и receipts
  provider/client.py   HTTP-клиент provider-simulator
  services/            операции, dispatch/recovery и callback
  worker/dispatcher.py фоновый цикл DispatchWorker
  config.py            настройки из переменных окружения
  database.py          async engine и session factory
  models.py            SQLAlchemy-модели
  schemas.py           Pydantic-схемы API
alembic/
  versions/            миграции PostgreSQL
tests/                 API, provider, dispatch, callback и recovery-тесты
compose.yaml            candidate-service, provider-simulator и PostgreSQL
Dockerfile              образ Python 3.14 и автоматический запуск Alembic
pyproject.toml          зависимости и настройки Pytest/Ruff
```
