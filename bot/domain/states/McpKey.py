from aiogram.fsm.state import StatesGroup, State


class McpKeyState(StatesGroup):
    label = State()
    categories = State()
    confirm = State()
