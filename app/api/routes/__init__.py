"""Сборка HTTP-контракта: единственное место, где перечислены все роутеры."""

from fastapi import APIRouter

from . import attachments, health, mailboxes, messages, uploads

api_router = APIRouter(prefix="/api/v1")

for module in (messages, mailboxes, uploads, attachments, health):
    api_router.include_router(module.router)

__all__ = ["api_router"]
