from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Report that the HTTP process can respond."""
    return {"status": "ok"}
