from aiogram import Router

from .admin import router as admin_router
from .admin_panel import router as admin_panel_router
from .admin_settings import router as admin_settings_router
from .connect import router as connect_router
from .core import router as core_router
from .errors import router as errors_router
from .fallback import router as fallback_router
from .support import router as support_router
from ..keyboards.user.account import router as account_router
from ..keyboards.user.cabinet import router as cabinet_router
from ..keyboards.user.lessons import router as lessons_router
from ..keyboards.user.main_menu import router as main_menu_router
from ..keyboards.user.purchases import router as purchases_router

router = Router(name="bot")
router.include_router(errors_router)
# /start c_<token> (вход на сайт через бота) — раньше обычного /start.
router.include_router(connect_router)
router.include_router(core_router)
router.include_router(main_menu_router)
router.include_router(admin_router)
# Ввод текста в админ-сценариях — раньше FSM-обработчиков поддержки/профиля.
router.include_router(admin_panel_router)
router.include_router(admin_settings_router)
router.include_router(support_router)
router.include_router(account_router)
router.include_router(lessons_router)
router.include_router(cabinet_router)
router.include_router(purchases_router)
router.include_router(fallback_router)

__all__ = ["router"]
