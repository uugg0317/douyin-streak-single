"""Compatibility discovery for the local edition; no login/session endpoints."""
from fastapi import APIRouter, HTTPException
router = APIRouter()

@router.get('/api/auth/mode')
def api_auth_mode() -> dict:
    return {'login_gate': False, 'password_public': False, 'token_entry': False, 'local_only': True}

@router.api_route('/api/auth/{legacy_path:path}', methods=['GET', 'POST'])
def retired_auth(legacy_path: str) -> None:
    raise HTTPException(status_code=410, detail='此本机版本没有后台密码门，请直接打开控制台')
