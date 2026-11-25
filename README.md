# AdRobot

AdRobot — проект создателя кампаний и редактора привязок офферов для Keitaro.
Сейчас доступен каркас на FastAPI, SQLAlchemy и SQLite с миграциями Alembic,
стартовой страницей, проверкой доступности и схемой OpenAPI. Интеграция Keitaro
предстоит. Для запуска каркаса ключ и идентификаторы Keitaro не требуются.

## Локальный запуск

Нужен Python 3.12. Выполняйте команды из корня репозитория. Копируйте шаблон
настроек только при первом запуске. Храните секреты в переменных окружения
или локальном `.env` и не публикуйте ключи. Переменные окружения имеют
приоритет над `.env`, который читается как UTF-8.

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
alembic upgrade head
python -m uvicorn app.main:app --reload
```

Если PowerShell блокирует активацию, вызывайте
`.\.venv\Scripts\python.exe` напрямую с `-m pip`, `-m alembic`,
`-m uvicorn` или `-m pytest`.

### Linux и macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -e .env || cp -n .env.example .env
alembic upgrade head
python -m uvicorn app.main:app --reload
```

- Стартовая страница — http://127.0.0.1:8000/
- Liveness — http://127.0.0.1:8000/health
- Документация OpenAPI — http://127.0.0.1:8000/docs

`/health` проверяет, что HTTP-приложение отвечает. `/docs` показывает схему
OpenAPI локально и содержит ссылку на `/openapi.json`.

## База данных

Приложение и Alembic используют `DATABASE_URL`. Стандартный путь
`sqlite:///./adrobot.db` разрешается относительно рабочей директории.
Применяйте миграции командой `alembic upgrade head`. Сохраняйте существующую
базу при повторном запуске. Откат первой миграции удаляет таблицу
`managed_campaigns` вместе с её данными.

Таблица `managed_campaigns` учитывает операции AdRobot. `created_at` и
`updated_at` при вставке заполняет SQLite через `CURRENT_TIMESTAMP` с
точностью до секунды. Python получает значения UTC без смещения часового
пояса. SQLAlchemy обновляет `updated_at` при записи через модель, если
значение не задано явно. Для прямого SQL автоматического обновления нет.

## Тесты

В активированном виртуальном окружении выполните:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Тесты используют временные файлы SQLite и локальный FastAPI TestClient.

## Роль AI

Codex подготовил каркас и автоматические проверки по согласованному заданию.
Технический архитектор проверяет изменения, владелец проекта принимает решения.
