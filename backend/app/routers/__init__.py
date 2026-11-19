from fastapi import APIRouter

from app.modules.single_open_cap import api as single_open_cap_api
from app.routers import core

api = APIRouter()
api.include_router(core.router)
api.include_router(single_open_cap_api.router)
