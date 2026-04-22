from __future__ import annotations
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


async def forward_request(request: Request, target_url: str, method: str) -> JSONResponse:
    try:
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("authorization", "host", "content-length")
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method.upper(),
                url=target_url,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )

        try:
            return JSONResponse(
                content=response.json(),
                status_code=response.status_code,
            )
        except Exception:
            return JSONResponse(
                content={"response": response.text},
                status_code=response.status_code,
            )

    except httpx.ConnectError:
        return JSONResponse(
            content={"error": "Could not reach workflow. Is it running?"},
            status_code=502,
        )
    except httpx.TimeoutException:
        return JSONResponse(
            content={"error": "Workflow timed out after 30 seconds."},
            status_code=504,
        )
    except Exception as e:
        return JSONResponse(
            content={"error": f"Proxy error: {str(e)}"},
            status_code=500,
        )
