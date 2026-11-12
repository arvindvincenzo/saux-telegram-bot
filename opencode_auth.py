import asyncio
import logging
import httpx

logger = logging.getLogger("opencode_auth")

OPENCODE_CONSOLE_URL = "https://opencode.ai/console"
CLIENT_ID = "opencode-cli"

async def request_device_code() -> dict:
    """
    Initiates the OpenCode OAuth Device Authorization flow.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{OPENCODE_CONSOLE_URL}/auth/device/code",
            json={"client_id": CLIENT_ID}
        )
        resp.raise_for_status()
        data = resp.json()
        
        user_code = data["user_code"]
        verif_url = f"https://opencode.ai/console/device?user_code={user_code}&client_id={CLIENT_ID}"
        
        return {
            "device_code": data["device_code"],
            "user_code": user_code,
            "verification_url": verif_url,
            "expires_in": data.get("expires_in", 600),
            "interval": data.get("interval", 5)
        }

async def fetch_user_org_id(access_token: str) -> str | None:
    """Fetches the user's default organization ID from OpenCode."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{OPENCODE_CONSOLE_URL}/api/orgs",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code == 200:
                orgs = resp.json()
                if orgs and isinstance(orgs, list) and len(orgs) > 0:
                    return orgs[0].get("id")
    except Exception as e:
        logger.error(f"Failed to fetch OpenCode orgs: {e}")
    return None

async def poll_for_token(device_code: str, interval: int = 5, max_attempts: int = 120) -> dict | None:
    """
    Polls OpenCode token endpoint until user approves or it expires.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(max_attempts):
            await asyncio.sleep(interval)
            try:
                resp = await client.post(
                    f"{OPENCODE_CONSOLE_URL}/auth/device/token",
                    json={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": CLIENT_ID
                    }
                )
                data = resp.json()
                
                if "access_token" in data:
                    logger.info("OpenCode token obtained! Fetching org ID...")
                    org_id = await fetch_user_org_id(data["access_token"])
                    data["org_id"] = org_id
                    return data
                
                err = data.get("error", "")
                if err == "authorization_pending":
                    continue
                elif err == "slow_down":
                    await asyncio.sleep(5)
                    continue
                else:
                    logger.warning(f"Device authorization error: {err}")
                    return None
            except Exception as e:
                logger.error(f"Error polling OpenCode token: {e}")
                
    return None
