"""API routers; ROUTERS lists every one in the order the app includes them."""

from __future__ import annotations

from fastapi import APIRouter

from rapidwire.api.routers import accounts, admin, contracts, dex, ledger_writes, meta

__all__ = ["ROUTERS"]

ROUTERS: tuple[APIRouter, ...] = (meta.router, accounts.router, ledger_writes.router, dex.router, contracts.router, admin.router)
