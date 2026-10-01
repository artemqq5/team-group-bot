from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.data.repositories.UserRepository import UserRepository
from bot.domain.filters.IsGroup import IsGropFilter
from bot.domain.middlewares.IsPrivateChatAdmin import IsPrivateChatAdmin
from bot.domain.routers.admin.chat.menu import show_, message_, statistic_, admins_, groups_
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import MainMenuCallback, kb_main

router = Router()
router.include_routers(
    show_.router,
    message_.router,
    statistic_.router,
    admins_.router,
    groups_.router,
)

router.message.middleware(IsPrivateChatAdmin())
router.callback_query.middleware(IsPrivateChatAdmin())


async def _main_menu_text() -> str:
    chats_list = await ChatRepository.all_chats() or []
    users_count = await UserRepository.count_users_total() or 0
    admins_list = await AdminRepository.get_all_admins() or []
    return (
        "<b>Привіт! Це бот для розсилки сповіщень.</b>\n\n"
        f"<b>{len(chats_list)} груп</b>  |  <b>{len(admins_list)} адмінів</b>  |  <b>{users_count} юзерів</b>\n\n"
        "Щоб додати групу — додайте бота в групу (вона з'явиться автоматично) "
        "або напишіть /start у групі."
    )


@router.message(Command("start"), IsGropFilter(False))
async def start(message: Message, state: FSMContext):
    await state.clear()
    admin = await AdminRepository.is_admin(message.from_user.id)
    text = await _main_menu_text()
    await message.answer(text, reply_markup=kb_main(admin))


@router.callback_query(MainMenuCallback.filter(F.action.in_(["back", "cancel"])))
async def go_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    admin = await AdminRepository.is_admin(callback.from_user.id)
    text = await _main_menu_text()
    await edit_or_answer(callback, text, reply_markup=kb_main(admin))
    await callback.answer()
