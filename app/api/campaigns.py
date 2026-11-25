from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.integrations.keitaro import KeitaroClient, KeitaroError
from app.services import campaigns as service


def get_connection(request: Request) -> Iterator[Callable[[], KeitaroClient]]:
    with ExitStack() as stack:
        yield lambda: stack.enter_context(KeitaroClient(request.app.state.settings))


Connection = Annotated[Callable[[], KeitaroClient], Depends(get_connection)]


class Problem(BaseModel):
    code: str
    message: str
    operation_id: UUID | None
    operation: service.Operation | None
    observed_ids: dict[str, int] | None = None


router = APIRouter(
    prefix="/api",
    responses={code: {"model": Problem} for code in (404, 409, 422, 502, 503, 504)},
)


def campaign_error(request: Request, exc: service.CampaignError) -> JSONResponse:
    status, message = service.MESSAGES[exc.code]
    if exc.operation and exc.operation.reconciliation_required:
        message += " Сверьте объекты по alias и известным ID до новой попытки."
    problem = Problem(code=exc.code, message=message, operation_id=exc.operation_id,
                      operation=exc.operation, observed_ids=exc.observed_ids)
    return JSONResponse(problem.model_dump(mode="json"), status_code=status)


def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    key = None
    if isinstance(exc.body, dict) and isinstance(exc.body.get("operation_id"), str):
        try:
            key = UUID(exc.body["operation_id"])
        except ValueError:
            pass
    return campaign_error(request, service.CampaignError("invalid_input", key))


@router.get("/offers")
def offers(connection: Connection, q: str = Query("", max_length=120),
           limit: int = Query(20, ge=1, le=100)) -> list[service.Offer]:
    try:
        return service.search_offers(connection(), q, limit)
    except KeitaroError as exc:
        raise service.CampaignError(service.upstream_code(exc)) from None


@router.post("/campaigns", status_code=201, responses={200: {"model": service.Operation}})
def create_campaign(data: service.CreateCampaign, request: Request, response: Response,
                    connection: Connection) -> service.Operation:
    state = request.app.state
    operation, created = service.create(state.session_factory, connection, state.settings, data)
    response.status_code = 201 if created else 200
    return operation


@router.get("/campaigns/operations/{operation_id}")
def operation_status(operation_id: UUID, request: Request) -> service.Operation:
    return service.read_operation(request.app.state.session_factory, operation_id)
