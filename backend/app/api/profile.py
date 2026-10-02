"""Маршруты `/profile` — тонкая обёртка модуля profile.

До шага миграции 03 здесь была адаптированная копия роутера фазы 3 с логикой
фазы 2; теперь канон — `app/modules/profile/router.py` (фаза 3), а этот файл
сохраняет путь импорта для `app.main` и обратной совместимости. Логика не
дублируется.

См. docs/migration/03-backend-domain.md.
"""

from app.modules.profile.router import *  # noqa: F401,F403 — реэкспорт маршрутов
from app.modules.profile.router import router  # noqa: F401 — явный реэкспорт
