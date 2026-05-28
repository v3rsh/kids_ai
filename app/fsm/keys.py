"""
Ключи FSM data (Redis hash field ``data``).

Значения строк **не менять** — они могут уже лежать в активных сессиях
(``FSM_TTL_DAYS``). При добавлении нового ключа — обновить
``docs/architecture.md`` → «FSM data keys».
"""

# --- moderator queue (moderator_queue.py) ---
FSM_KEY_TRACKS = "moderator_queue_tracks"
FSM_KEY_AGES = "moderator_queue_ages"
FSM_KEY_STATUSES = "moderator_queue_statuses"
FSM_KEY_DATE_FROM = "moderator_queue_date_from"
FSM_KEY_DATE_TO = "moderator_queue_date_to"
FSM_KEY_QUEUE_PAGE = "moderator_queue_page"
FSM_KEY_BROWSE_INDEX = "moderator_browse_index"

# --- moderator multi_subs ---
FSM_KEY_MULTI_SUBS_PAGE = "moderator:multi_subs:page"

# --- moderator dialog navigation cache ---
FSM_KEY_MOD_NAV_ORIGIN_KIND = "moderator_nav_origin_kind"
FSM_KEY_MOD_NAV_ORIGIN_DATA = "moderator_nav_origin_data"
FSM_KEY_MOD_NAV_ORIGIN_BR_ID = "moderator_nav_origin_br_id"
FSM_KEY_MODERATOR_TARGET_BR_ID = "moderator_target_br_id"

# --- user applications ---
FSM_KEY_MY_APPS_PAGE = "user:my_apps:page"

# --- user intake files ---
FSM_KEY_FILE_UPLOAD_ALLOWED = "file_upload_allowed"

# --- jury task carousel ---
FSM_KEY_JURY_TASK_ROUND_ID = "jury_task_round_id"
FSM_KEY_JURY_TASK_INDEX = "jury_task_index"
FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID = "jury_task_anchor_sync_id"

# --- admin roles ---
FSM_KEY_ADMIN_ADD_ROLE = "admin_add_role"

__all__ = [
    "FSM_KEY_TRACKS",
    "FSM_KEY_AGES",
    "FSM_KEY_STATUSES",
    "FSM_KEY_DATE_FROM",
    "FSM_KEY_DATE_TO",
    "FSM_KEY_QUEUE_PAGE",
    "FSM_KEY_BROWSE_INDEX",
    "FSM_KEY_MULTI_SUBS_PAGE",
    "FSM_KEY_MOD_NAV_ORIGIN_KIND",
    "FSM_KEY_MOD_NAV_ORIGIN_DATA",
    "FSM_KEY_MOD_NAV_ORIGIN_BR_ID",
    "FSM_KEY_MODERATOR_TARGET_BR_ID",
    "FSM_KEY_MY_APPS_PAGE",
    "FSM_KEY_FILE_UPLOAD_ALLOWED",
    "FSM_KEY_JURY_TASK_ROUND_ID",
    "FSM_KEY_JURY_TASK_INDEX",
    "FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID",
    "FSM_KEY_ADMIN_ADD_ROLE",
]
