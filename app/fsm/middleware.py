"""
FSM Middleware (pybotx)

Middleware для инъекции FSM контекста в обработчики сообщений.
"""
from typing import Any, Callable, Dict, Optional
from uuid import UUID

from loguru import logger
from pybotx import Bot, ChatTypes, IncomingMessage

from .redis_storage import RedisFSMStorage
from .storage import get_fsm_storage


class FSMContext:
    """
    Контекст FSM для работы с состояниями в обработчиках.
    
    Предоставляет удобный интерфейс для управления состоянием
    пользователя.
    """
    
    def __init__(self, user_huid: UUID, storage: RedisFSMStorage):
        self.user_huid = user_huid
        self._storage = storage
        self._state: Optional[str] = None
        self._data: Dict[str, Any] = {}
        self._loaded = False
    
    async def _load(self) -> None:
        """Загружает состояние и данные из хранилища"""
        if self._loaded:
            return
        self._state, self._data = await self._storage.get_state_and_data(self.user_huid)
        self._loaded = True
    
    @property
    def current_state(self) -> Optional[str]:
        """Текущее состояние (требует предварительной загрузки)"""
        return self._state
    
    @property
    def data(self) -> Dict[str, Any]:
        """Данные состояния (требует предварительной загрузки)"""
        return self._data
    
    async def get_state(self) -> Optional[str]:
        """Получает текущее состояние"""
        await self._load()
        return self._state
    
    async def get_data(self) -> Dict[str, Any]:
        """Получает данные состояния"""
        await self._load()
        return self._data.copy()
    
    async def set_state(self, state: Optional[str]) -> None:
        """
        Устанавливает новое состояние.
        
        Args:
            state: Новое состояние (строка или None для сброса)
        """
        # Если передан Enum, получаем его значение
        if hasattr(state, 'value'):
            state = state.value
            
        await self._storage.set_state(self.user_huid, state)
        self._state = state
        logger.debug("State set", state=state, user_huid=self.user_huid)
    
    async def set_data(self, data: Dict[str, Any]) -> None:
        """
        Устанавливает данные состояния (полная замена).
        
        Args:
            data: Новые данные
        """
        await self._storage.set_data(self.user_huid, data)
        self._data = data
    
    async def update_data(self, **kwargs) -> Dict[str, Any]:
        """
        Обновляет данные состояния (добавление/изменение).
        
        Args:
            **kwargs: Данные для обновления
        
        Returns:
            Обновленные данные
        """
        self._data = await self._storage.update_data(self.user_huid, **kwargs)
        return self._data
    
    async def clear(self) -> None:
        """Очищает состояние и данные"""
        await self._storage.clear(self.user_huid)
        self._state = None
        self._data = {}
        logger.debug("State cleared", user_huid=self.user_huid)


# Тип для функции обработчика
IncomingMessageHandlerFunc = Callable[[IncomingMessage, Bot], Any]


async def personal_chat_only(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """
    Middleware для фильтрации по типу чата.

    Пропускает только сообщения из личных чатов.
    Групповые чаты, каналы и треды игнорируются без ответа.
    """
    if message.chat.type != ChatTypes.PERSONAL_CHAT:
        logger.debug(
            "Сообщение из неличного чата проигнорировано: chat_id={}, type={}",
            message.chat.id, message.chat.type,
        )
        return
    await call_next(message, bot)


async def fsm_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """
    Middleware для инъекции FSM контекста в сообщение.
    
    Добавляет атрибут `fsm` к объекту message.state,
    предоставляющий доступ к управлению состояниями.

    Middleware не открывает DB-сессий к PostgreSQL — работа с данными
    пользователя (chat_id, last_activity и т.п.) выполняется внутри
    хендлеров в их собственной сессии.
    
    Usage:
        @collector.command("/start", middlewares=[fsm_middleware])
        async def start_handler(message: IncomingMessage, bot: Bot):
            fsm = message.state.fsm
            await fsm.set_state(UserReg.user_reg_name)
    """
    storage = get_fsm_storage()
    fsm_context = FSMContext(message.sender.huid, storage)
    
    # Lazy-load: состояние загружается при первом обращении к get_state()/get_data()
    # Это экономит round-trip к Redis для обработчиков, не использующих FSM.
    
    # Инъектируем FSM контекст в message.state
    message.state.fsm = fsm_context
    message.state.current_state = None  # Обновится при вызове get_state() в обработчике
    
    # Вызываем следующий обработчик
    await call_next(message, bot)


class FSMMiddleware:
    """
    Класс middleware для FSM.
    
    Альтернативный способ использования middleware,
    позволяющий настроить дополнительные параметры.
    """
    
    def __init__(self, storage: Optional[RedisFSMStorage] = None):
        self._storage = storage or get_fsm_storage()
    
    async def __call__(
        self,
        message: IncomingMessage,
        bot: Bot,
        call_next: IncomingMessageHandlerFunc,
    ) -> None:
        """Обработка сообщения с инъекцией FSM контекста"""
        fsm_context = FSMContext(message.sender.huid, self._storage)
        
        # Lazy-load: состояние загружается при первом обращении к get_state()/get_data()
        
        # Инъектируем FSM контекст
        message.state.fsm = fsm_context
        message.state.current_state = None  # Обновится при вызове get_state()
        
        await call_next(message, bot)
