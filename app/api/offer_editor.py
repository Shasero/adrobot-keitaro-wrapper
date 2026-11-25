from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.campaigns import Connection
from app.integrations.keitaro import KeitaroError
from app.services.campaigns import MAX_ID, MESSAGES, CampaignError
from app.services.offer_editor import Editor, EditorView, error_code
from app.services.offer_shares import Action, EditorError, PositiveID, StrictModel
from app.services.offer_store import Store
from app.services.offer_streams import Choice, campaigns, streams

ERRORS = dict(MESSAGES,
    forbidden=(403, "Кампания или поток вне выбранного проекта."),
    unsupported_stream=(422, "Редактор поддерживает потоки landings типов regular и default."),
    unsupported_bindings=(422, "Состав, состояния или доли привязок не поддерживаются."),
    invalid_action=(422, "Действие недоступно для этой строки."),
    share_budget=(422, "Закреплённые доли не оставляют допустимого распределения."),
    empty_target=(422, "Публикация пустого состава landings недоступна."),
    conflict=(409, "Удалённое состояние изменилось. Сверьте изменения."),
    stale_revision=(409, "Версия устарела. Перечитайте черновик."),
    busy=(409, "Идёт публикация или требуется явная сверка."),
    invalid_input=(422, "Проверьте параметры запроса."),
    not_found=(404, "Сначала откройте редактор."),
    storage=(503, "Не удалось подтвердить локальное состояние. Перечитайте черновик и выполните сверку."),
)


class Version(StrictModel):
    expected_revision: PositiveID


class Command(Version):
    action: Action
    offer_id: PositiveID


class Problem(StrictModel):
    code: str
    message: str
    editor: EditorView | None = None


def editor_error(request: Request, exc: EditorError) -> JSONResponse:
    status, message = ERRORS[exc.code]
    problem = Problem(code=exc.code, message=message, editor=getattr(exc, "editor", None))
    return JSONResponse(problem.model_dump(), status_code=status)


def translate() -> Iterator[None]:
    try:
        yield
    except (KeitaroError, CampaignError) as exc:
        raise EditorError(error_code(exc)) from None
    except SQLAlchemyError:
        raise EditorError("storage") from None


router = APIRouter(prefix="/api", dependencies=[Depends(translate)], responses={
    code: {"model": Problem} for code in (403, 404, 409, 422, 502, 503, 504)
})
# Use canonical decimal path IDs; e.g. 2.0 is not an ID.
PathID = Annotated[str, Path(pattern=r"^[1-9][0-9]{0,18}$")]


def key(value: str) -> int:
    result = int(value)
    if result > MAX_ID:
        raise EditorError("invalid_input")
    return result


def editor(campaign_id: PathID, stream_id: PathID, request: Request, connection: Connection) -> Editor:
    return Editor(Store(request.app.state.session_factory, key(campaign_id), key(stream_id)), connection())


Selected = Annotated[Editor, Depends(editor)]
ROOT = "/campaigns/{campaign_id}/streams/{stream_id}/editor"


@router.get("/campaigns")
def list_campaigns(request: Request, connection: Connection) -> list[Choice]:
    return campaigns(connection(), request.app.state.settings.campaign_name_prefix)


@router.get("/campaigns/{campaign_id}/streams")
def list_streams(campaign_id: PathID, connection: Connection) -> list[Choice]:
    return streams(connection(), key(campaign_id))


@router.get(ROOT)
def open_editor(selected: Selected) -> EditorView:
    return selected.open()


@router.post(ROOT + "/actions")
def action(selected: Selected, data: Command) -> EditorView:
    return selected.action(data.expected_revision, data.action, data.offer_id)


@router.post(ROOT + "/cancel")
def cancel(selected: Selected, data: Version) -> EditorView:
    return selected.cancel(data.expected_revision)


@router.post(ROOT + "/refresh", responses={409: {"model": Problem}})
def refresh(selected: Selected, data: Version) -> EditorView:
    result = selected.refresh(data.expected_revision)
    if result.status in ("conflict", "reconciliation_conflict"):
        error = EditorError("conflict")
        error.editor = result
        raise error
    return result


@router.post(ROOT + "/push")
def push(selected: Selected, data: Version) -> EditorView:
    return selected.push(data.expected_revision)
