"""Маршруты `/auth` — тонкая обёртка модуля identity.

До шага миграции 02 роуты жили здесь с логикой фазы 2; теперь канон —
`app/modules/identity/router.py` (фаза 3), а этот файл сохраняет путь импорта
для `app.main` и обратной совместимости. Логика не дублируется.

См. docs/migration/02-backend-identity.md.
"""

from app.modules.identity.router import *  # noqa: F401,F403 — реэкспорт маршрутов
from app.modules.identity.router import router  # noqa: F401 — явный реэкспорт
