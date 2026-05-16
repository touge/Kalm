from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security, HTTPException
from src.logic.yaml_config_loader import yaml_config_loader

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
):
    tokens = yaml_config_loader.get("api_server.tokens", [])
    if not tokens:
        return True
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    if credentials.credentials not in tokens:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return True
