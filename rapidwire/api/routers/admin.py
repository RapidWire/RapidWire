"""Operator endpoints: backup listing and download, each recorded as an execution initiated by the operator."""

from __future__ import annotations

import base64
import functools
import logging
from datetime import datetime
from typing import Annotated, Any, Final

import anyio
from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.auth import Principal, PrincipalDep, Writer
from rapidwire.api.backups import BackupPage, BackupStream, OpenedBackup, checksum_cache, list_backup_page, open_backup
from rapidwire.api.deps import EngineDep, SettingsDep
from rapidwire.api.formats import IntOut
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.core.errors import NotFoundError, PermissionDenied
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.paging import BACKUPS_PER_PAGE_DEFAULT, BACKUPS_PER_PAGE_MAX
from rapidwire.core.types import ExecutionType

__all__ = ["CHECKSUM_HEADER", "router"]

log = logging.getLogger("rapidwire.api.backups")

CHECKSUM_HEADER: Final = "X-Checksum-SHA256"
_NO_STORE: Final = {"Cache-Control": "no-store"}
_BACKUP_NOT_FOUND: Final = "The backup was not found."

router = APIRouter(prefix="/v1", tags=["Admin backups"])


class BackupItem(BaseModel):
    name: str = Field(description="The file name, which is also the {name} of the download path.")
    created_at: datetime = Field(description="When the backup was completed (the file's modification time), in UTC.")
    size: int = Field(description="The file size in bytes.")
    sha256: str = Field(description="The SHA-256 of the file as 64 lowercase hex digits, computed by the API from the file itself.")


class BackupList(Page[BackupItem]):
    """One page of the backups, newest first, with the backup_list execution that records the listing."""

    execution_id: IntOut = Field(description="The backup_list execution that records this listing.")


BackupPageParams = Annotated[PageParams, Depends(page_params(BACKUPS_PER_PAGE_DEFAULT, BACKUPS_PER_PAGE_MAX))]


async def require_operator(principal: PrincipalDep, settings: SettingsDep) -> Principal:
    """The caller, when the token's account is one of RAPIDWIRE_OPERATOR_ACCOUNT_IDS; PermissionDenied otherwise."""
    if principal.account_id not in settings.operator_account_ids:
        raise PermissionDenied()
    return principal


OperatorDep = Annotated[Principal, Depends(require_operator)]
BackupName = Annotated[str, Path(description="The backup's file name; the route takes the rest of the path, so a name with a slash is refused after the operator check rather than by the router.")]


async def _record(engine: AsyncEngine, principal: Principal, response: Response, type: ExecutionType, input: dict[str, Any], output: dict[str, Any]) -> int:
    """Record one successful execution of type by the operator; the token is re-checked in its transaction, so a token revoked meanwhile records nothing and raises Unauthorized."""

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return output

    async with Writer(principal, None, response):
        result = await run_operation(engine, type=type, initiator_id=principal.account_id, input=input, body=body)
    return result.execution_id


@router.get("/admin/backups", response_model=BackupList, summary="List backups newest first (operators only)")
async def get_backups(request: Request, response: Response, principal: OperatorDep, params: BackupPageParams, settings: SettingsDep, engine: EngineDep) -> BackupList:
    cache = checksum_cache(request.app.state)
    listing = await anyio.to_thread.run_sync(functools.partial(list_backup_page, settings.backup_dir, cache, offset=params.offset, limit=params.per_page))
    if listing is None:
        if not cache.directory_missing_logged:
            cache.directory_missing_logged = True
            log.warning("backup directory does not exist; listing no backups", extra={"backup_dir": str(settings.backup_dir)})
        listing = BackupPage(total=0, files=[])
    else:
        cache.directory_missing_logged = False
    files = listing.files
    recorded_input = {"page": params.page, "per_page": params.per_page}
    execution_id = await _record(engine, principal, response, ExecutionType.BACKUP_LIST, recorded_input, {"count": len(files), "total": listing.total})
    response.headers.update(_NO_STORE)
    return BackupList(
        execution_id=execution_id,
        items=[BackupItem(name=file.name, created_at=file.created_at, size=file.size, sha256=file.sha256) for file in files],
        page=params.page,
        per_page=params.per_page,
        total=listing.total,
        total_is_lower_bound=False,
    )


def _download_headers(backup: OpenedBackup, execution_id: int) -> dict[str, str]:
    digest = base64.b64encode(bytes.fromhex(backup.sha256)).decode("ascii")
    return {
        **_NO_STORE,
        "Content-Disposition": f'attachment; filename="{backup.name}"',
        CHECKSUM_HEADER: backup.sha256,
        "Repr-Digest": f"sha-256=:{digest}:",
        "X-Content-Type-Options": "nosniff",
        "X-Execution-Id": str(execution_id),
    }


@router.get(
    "/admin/backups/{name:path}",
    summary="Download a backup (operators only)",
    response_class=Response,
    responses={200: {"description": "The backup file.", "content": {"application/gzip": {}, "application/octet-stream": {}}}},
)
async def get_backup(name: BackupName, request: Request, response: Response, principal: OperatorDep, settings: SettingsDep, engine: EngineDep) -> Response:
    backup = await anyio.to_thread.run_sync(open_backup, settings.backup_dir, name, checksum_cache(request.app.state))
    if backup is None:
        raise NotFoundError(_BACKUP_NOT_FOUND)
    try:
        execution_id = await _record(engine, principal, response, ExecutionType.BACKUP_DOWNLOAD, {"name": name}, {"name": name, "size": backup.size, "sha256": backup.sha256})
        return BackupStream(backup, _download_headers(backup, execution_id))
    except BaseException:
        backup.close()
        raise
