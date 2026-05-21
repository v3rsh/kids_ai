"""
Автоматическая миграция базы данных.

Сравнивает схему моделей SQLAlchemy с реальной структурой БД
и безопасно добавляет недостающие колонки, индексы и enum-значения
при старте бота. Не удаляет ничего — только добавляет.

Вызывается из main.py после Base.metadata.create_all().
"""
from typing import Any

from loguru import logger
from sqlalchemy import text

from database.db import engine
from database.models import Base


# Маппинг типов SQLAlchemy -> PostgreSQL для ADD COLUMN
SQLALCHEMY_TO_PG_TYPE = {
    "INTEGER": "INTEGER",
    "BIGINT": "BIGINT",
    "SMALLINT": "SMALLINT",
    "VARCHAR": "VARCHAR(255)",
    "TEXT": "TEXT",
    "BOOLEAN": "BOOLEAN",
    "DATETIME": "TIMESTAMP",
    "DATE": "DATE",
    "FLOAT": "FLOAT",
    "NUMERIC": "NUMERIC",
    "UUID": "UUID",
    "ARRAY": "INTEGER[]",  # Дефолтный тип для ARRAY
}


def get_model_schema() -> dict[str, dict[str, Any]]:
    """Извлекает ожидаемую структуру из SQLAlchemy моделей."""
    schema: dict[str, dict[str, Any]] = {}

    for table_name, table in Base.metadata.tables.items():
        columns = {}
        for column in table.columns:
            col_type = str(column.type)

            default = None
            if column.default is not None:
                if hasattr(column.default, 'arg'):
                    arg = column.default.arg
                    if callable(arg):
                        default = "NOW()"
                    elif isinstance(arg, bool):
                        default = str(arg).upper()
                    elif isinstance(arg, (int, float)):
                        default = str(arg)
                    elif isinstance(arg, str):
                        default = f"'{arg}'"

            columns[column.name] = {
                "type": col_type,
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "default": default,
            }

        schema[table_name] = columns

    return schema


async def get_db_schema() -> dict[str, dict[str, Any]]:
    """Получает текущую структуру БД из PostgreSQL information_schema."""
    schema: dict[str, dict[str, Any]] = {}

    query = text(
        """
        SELECT
            table_name,
            column_name,
            data_type,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )

    async with engine.begin() as conn:
        result = await conn.execute(query)
        rows = result.fetchall()

    for row in rows:
        table_name = row[0]
        column_name = row[1]

        if table_name not in schema:
            schema[table_name] = {}

        schema[table_name][column_name] = {
            "type": row[2],
            "nullable": row[3] == "YES",
            "default": row[4],
        }

    return schema


def get_pg_type_for_column(column) -> str:
    """Преобразует тип колонки SQLAlchemy в PostgreSQL тип."""
    col_type = type(column.type).__name__.upper()

    if col_type == "ARRAY":
        item_type = type(column.type.item_type).__name__.upper()
        if item_type == "INTEGER":
            return "INTEGER[]"
        elif item_type in ("VARCHAR", "STRING"):
            return "VARCHAR[]"
        return "TEXT[]"

    if col_type == "VARCHAR":
        length = getattr(column.type, 'length', None)
        if length:
            return f"VARCHAR({length})"
        return "VARCHAR(255)"

    if col_type == "ENUM":
        # PostgreSQL enum создаётся отдельно, используем VARCHAR
        return "VARCHAR(50)"

    if col_type == "UUID":
        return "UUID"

    return SQLALCHEMY_TO_PG_TYPE.get(col_type, "TEXT")


def get_default_clause(column) -> str:
    """Формирует DEFAULT clause для PostgreSQL."""
    if column.default is None:
        return ""

    if hasattr(column.default, 'arg'):
        arg = column.default.arg
        if callable(arg):
            return " DEFAULT NOW()"
        elif isinstance(arg, bool):
            return f" DEFAULT {str(arg).upper()}"
        elif isinstance(arg, (int, float)):
            return f" DEFAULT {arg}"
        elif isinstance(arg, str):
            return f" DEFAULT '{arg}'"

    return ""


async def add_missing_columns(
    table_name: str,
    missing_columns: list[str],
    model_schema: dict[str, dict[str, Any]],
) -> int:
    """Добавляет недостающие колонки в таблицу."""
    if not missing_columns:
        return 0

    table = Base.metadata.tables.get(table_name)
    if table is None:
        logger.warning(f"Таблица {table_name} не найдена в моделях")
        return 0

    added = 0

    async with engine.begin() as conn:
        for col_name in missing_columns:
            column = table.columns.get(col_name)
            if column is None:
                continue

            pg_type = get_pg_type_for_column(column)
            default_clause = get_default_clause(column)
            nullable = "" if column.nullable else " NOT NULL"

            # Для NOT NULL без дефолта нужен дефолт
            if not column.nullable and not default_clause:
                if "INT" in pg_type:
                    default_clause = " DEFAULT 0"
                elif "BOOL" in pg_type:
                    default_clause = " DEFAULT FALSE"
                elif "VARCHAR" in pg_type or pg_type == "TEXT":
                    default_clause = " DEFAULT ''"
                elif "TIMESTAMP" in pg_type or "DATE" in pg_type:
                    default_clause = " DEFAULT NOW()"

            sql = f"""
                ALTER TABLE {table_name}
                ADD COLUMN IF NOT EXISTS {col_name} {pg_type}{nullable}{default_clause}
            """

            try:
                await conn.execute(text(sql))
                logger.info(
                    f"Добавлена колонка {col_name} в таблицу {table_name}",
                    column=col_name,
                    table=table_name,
                    type=pg_type,
                )
                added += 1
            except Exception as e:
                logger.error(
                    f"Ошибка добавления колонки {col_name} в {table_name}: {e}",
                    column=col_name,
                    table=table_name,
                    error=str(e),
                )

    return added


async def get_db_indexes(table_name: str) -> set[str]:
    """Получает существующие индексы таблицы из PostgreSQL."""
    query = text(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = :table_name
        """
    )

    async with engine.begin() as conn:
        result = await conn.execute(query, {"table_name": table_name})
        rows = result.fetchall()

    return {row[0] for row in rows}


def get_model_indexes() -> dict[str, list[dict[str, Any]]]:
    """Извлекает ожидаемые индексы из SQLAlchemy моделей."""
    indexes_by_table: dict[str, list[dict[str, Any]]] = {}

    for table_name, table in Base.metadata.tables.items():
        table_indexes = []

        for index in table.indexes:
            index_columns = [col.name for col in index.columns]
            table_indexes.append(
                {
                    "name": index.name,
                    "columns": index_columns,
                    "unique": index.unique,
                }
            )

        if table_indexes:
            indexes_by_table[table_name] = table_indexes

    return indexes_by_table


async def add_missing_indexes() -> int:
    """Добавляет недостающие индексы в базу данных."""
    model_indexes = get_model_indexes()
    total_created = 0

    async with engine.begin() as conn:
        for table_name, indexes in model_indexes.items():
            existing_indexes = await get_db_indexes(table_name)

            for index_info in indexes:
                index_name = index_info["name"]

                if index_name in existing_indexes:
                    continue

                columns = ", ".join(index_info["columns"])
                unique = "UNIQUE " if index_info.get("unique") else ""

                sql = (
                    f'CREATE {unique}INDEX IF NOT EXISTS "{index_name}" '
                    f"ON {table_name} ({columns})"
                )

                try:
                    await conn.execute(text(sql))
                    logger.info(
                        f"Создан индекс {index_name} на таблице {table_name}",
                        index=index_name,
                        table=table_name,
                        columns=index_info["columns"],
                    )
                    total_created += 1
                except Exception as e:
                    logger.error(
                        f"Ошибка создания индекса {index_name} на {table_name}: {e}",
                        index=index_name,
                        table=table_name,
                        error=str(e),
                    )

    return total_created


async def sync_enum_values() -> int:
    """
    Универсальная синхронизация PostgreSQL enum-типов с Python-моделями.

    SQLAlchemy ``create_all()`` создаёт enum-типы, используя ``.name``
    (UPPERCASE) каждого Python-элемента. Эта функция проверяет все
    enum-колонки в моделях и добавляет недостающие значения в
    соответствующие PostgreSQL enum-типы.

    Returns:
        Количество добавленных значений
    """
    from sqlalchemy import Enum as SAEnum

    added = 0

    # Собираем уникальные пары (pg_type_name, python_enum_class)
    enum_types: dict[str, type] = {}

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, SAEnum) and column.type.enum_class is not None:
                pg_type_name = column.type.name
                if pg_type_name and pg_type_name not in enum_types:
                    enum_types[pg_type_name] = column.type.enum_class

    if not enum_types:
        return 0

    async with engine.begin() as conn:
        for pg_type_name, py_enum in enum_types.items():
            check_type = text("SELECT 1 FROM pg_type WHERE typname = :tname")
            result = await conn.execute(check_type, {"tname": pg_type_name})
            if not result.fetchone():
                logger.debug(
                    "Enum-тип ещё не создан, пропускаем (create_all создаст)",
                    pg_type=pg_type_name,
                )
                continue

            labels_sql = text(
                "SELECT enumlabel FROM pg_enum "
                "WHERE enumtypid = CAST(:tname AS regtype) ORDER BY enumsortorder"
            )
            result = await conn.execute(labels_sql, {"tname": pg_type_name})
            existing_labels = {row[0] for row in result.fetchall()}

            expected_labels = {member.name for member in py_enum}

            missing = expected_labels - existing_labels
            if not missing:
                continue

            for label in sorted(missing):
                try:
                    await conn.execute(
                        text(f"ALTER TYPE {pg_type_name} ADD VALUE IF NOT EXISTS '{label}'")
                    )
                    added += 1
                    logger.info(
                        "Добавлено значение в enum",
                        pg_type=pg_type_name,
                        label=label,
                    )
                except Exception as e:
                    logger.warning(
                        "Не удалось добавить значение в enum",
                        pg_type=pg_type_name,
                        label=label,
                        error=str(e),
                    )

    return added


async def run_auto_migrations() -> None:
    """
    Главная функция автомиграции.

    Сравнивает модели SQLAlchemy с реальной схемой БД и безопасно
    добавляет недостающие колонки, индексы и enum-значения.

    ВАЖНО: Вызывается ТОЛЬКО при старте бота из main.py.
    Не удаляет колонки/таблицы/индексы — только добавляет.

    При появлении специфичных одноразовых миграций (бэкфиллы данных,
    создание sequence, спец-индексов и т.п.) — добавляй их в этот файл
    отдельными функциями и вызывай в самом конце run_auto_migrations().
    """
    logger.info("Запуск автоматической миграции...")

    try:
        model_schema = get_model_schema()
        db_schema = await get_db_schema()

        total_columns_added = 0
        warnings = []

        for table_name, model_columns in model_schema.items():
            if table_name not in db_schema:
                logger.debug(
                    f"Таблица {table_name} новая, будет создана через create_all"
                )
                continue

            db_columns = db_schema[table_name]

            missing_columns = [col for col in model_columns if col not in db_columns]

            if missing_columns:
                logger.info(
                    f"Найдены недостающие колонки в {table_name}: {missing_columns}",
                    table=table_name,
                    columns=missing_columns,
                )
                added = await add_missing_columns(table_name, missing_columns, model_schema)
                total_columns_added += added

            extra_columns = [col for col in db_columns if col not in model_columns]

            if extra_columns:
                warnings.append(
                    f"Таблица {table_name} содержит лишние колонки: {extra_columns}"
                )

        for warning in warnings:
            logger.warning(warning)

        enums_added = await sync_enum_values()
        total_indexes_added = await add_missing_indexes()

        if total_columns_added or total_indexes_added or enums_added:
            logger.info(
                "Автомиграция завершена: добавлено "
                f"{total_columns_added} колонок, "
                f"{total_indexes_added} индексов, "
                f"{enums_added} enum-значений"
            )
        else:
            logger.info("Автомиграция завершена: изменений не требуется")

    except Exception as e:
        logger.error(f"Ошибка автомиграции: {e}", error=str(e))
        # Не прерываем запуск бота при ошибке миграции —
        # бот может работать с существующей схемой
