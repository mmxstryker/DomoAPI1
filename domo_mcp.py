"""domo_mcp — a lightweight, personal MCP server that bridges Claude to Domo.

Exposes read-only tools for discovering and querying datasets across your
whole Domo instance, not just Toolkit + Leads:
  - domo_list_datasets:        find datasets by name
  - domo_get_dataset_schema:   see a dataset's columns/types without querying data
  - domo_query_dataset:        run a filtered SELECT against ANY dataset
  - domo_query_toolkit_leads:  convenience wrapper for the Fall Promo 2026 use case

Auth: OAuth 2.0 client credentials flow against api.domo.com, using
DOMO_CLIENT_ID / DOMO_CLIENT_SECRET from the environment (set these in your
Claude Desktop / Claude Code MCP config — never hardcode them here).
"""

import json
import os
import time
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = MCPServer("domo_mcp")

DOMO_OAUTH_URL = "https://api.domo.com/oauth/token"
DOMO_DATASETS_URL = "https://api.domo.com/v1/datasets"
DOMO_DATASET_URL = "https://api.domo.com/v1/datasets/{dataset_id}"
DOMO_QUERY_URL = "https://api.domo.com/v1/datasets/query/execute/{dataset_id}"
DOMO_SCOPE = "data"

# Defaults for the winter promo work — override per-call if you query something else.
DEFAULT_DATASET_ID = "d6aa8458-daa6-4bcd-aa0c-210808b25a7f"  # Toolkit + Leads
DEFAULT_PROMOTION_COLUMN = "Promotion Name (Toolkit Only)"
DEFAULT_PROMOTION_NAME = "Fall Promo 2026"

_token_cache: dict = {"access_token": None, "expires_at": 0}


async def _get_access_token() -> str:
    """Fetch (and cache) an OAuth access token via client credentials."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    client_id = os.environ.get("DOMO_CLIENT_ID")
    client_secret = os.environ.get("DOMO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "DOMO_CLIENT_ID / DOMO_CLIENT_SECRET are not set in the environment. "
            "Set them in your MCP server config, not in this file."
        )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            DOMO_OAUTH_URL,
            params={"grant_type": "client_credentials", "scope": DOMO_SCOPE},
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    return _token_cache["access_token"]


async def _domo_get(url: str, params: Optional[dict] = None) -> dict:
    """GET against the Domo API with a fresh bearer token."""
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
        if resp.status_code == 401:
            _token_cache["access_token"] = None
            raise RuntimeError("Domo rejected the access token (401). Try again — it will refresh.")
        if resp.status_code == 404:
            raise RuntimeError(f"Not found (404): {url}")
        resp.raise_for_status()
        return resp.json()


async def _run_sql(dataset_id: str, sql: str) -> dict:
    """Execute a read-only SELECT against a Domo dataset."""
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            DOMO_QUERY_URL.format(dataset_id=dataset_id),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"sql": sql},
        )
        if resp.status_code == 401:
            _token_cache["access_token"] = None
            raise RuntimeError("Domo rejected the access token (401). Try again — it will refresh.")
        if resp.status_code == 400:
            raise RuntimeError(f"Domo rejected the query (400): {resp.text}")
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


class ListDatasetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name_like: Optional[str] = Field(
        default=None, description="Optional substring to filter dataset names by, e.g. 'Leads'."
    )
    limit: int = Field(default=50, ge=1, le=500, description="Max datasets to return.")


@mcp.tool(
    name="domo_list_datasets",
    annotations={
        "title": "List Domo datasets",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def domo_list_datasets(params: ListDatasetsInput) -> str:
    """Find datasets in the Domo instance by (optional) name substring.

    Use this first when you don't already know a dataset's ID — then pass the
    ID into domo_get_dataset_schema or domo_query_dataset.

    Args:
        params (ListDatasetsInput): name_like (optional filter), limit.

    Returns:
        str: JSON list of {id, name, rowCount, columnCount, owner}.
    """
    query_params = {"limit": params.limit, "offset": 0}
    if params.name_like:
        query_params["nameLike"] = params.name_like

    data = await _domo_get(DOMO_DATASETS_URL, query_params)
    simplified = [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "rowCount": d.get("rows"),
            "columnCount": d.get("columns"),
            "owner": d.get("owner", {}).get("name") if isinstance(d.get("owner"), dict) else d.get("owner"),
        }
        for d in data
    ]
    return json.dumps(simplified, indent=2)


class GetDatasetSchemaInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    dataset_id: str = Field(..., description="Domo dataset ID to inspect.")


@mcp.tool(
    name="domo_get_dataset_schema",
    annotations={
        "title": "Get Domo dataset schema",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def domo_get_dataset_schema(params: GetDatasetSchemaInput) -> str:
    """Get a dataset's name, size, and column schema without querying its data.

    Args:
        params (GetDatasetSchemaInput): dataset_id (str).

    Returns:
        str: JSON with "name", "rows", "columns" (int), and "schema":
             list[{"name": str, "type": str}].
    """
    data = await _domo_get(DOMO_DATASET_URL.format(dataset_id=params.dataset_id))
    schema_columns = data.get("schema", {}).get("columns", [])
    return json.dumps(
        {
            "name": data.get("name"),
            "rows": data.get("rows"),
            "columns": data.get("columns"),
            "schema": [{"name": c.get("name"), "type": c.get("type")} for c in schema_columns],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------


class QueryDatasetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    dataset_id: str = Field(..., description="Domo dataset ID to query.")
    columns: str = Field(default="*", description="Comma-separated columns to select, or '*'.")
    where: Optional[str] = Field(
        default=None,
        description="Optional SQL WHERE clause (without the word WHERE), e.g. \"`Status` = 'Active'\".",
    )
    limit: int = Field(default=200, ge=1, le=5000, description="Max rows to return.")


@mcp.tool(
    name="domo_query_dataset",
    annotations={
        "title": "Query any Domo dataset",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def domo_query_dataset(params: QueryDatasetInput) -> str:
    """Run a read-only SELECT against any Domo dataset by ID.

    Use domo_list_datasets and domo_get_dataset_schema first if you don't
    already know the dataset ID and column names.

    Builds and runs:
        SELECT <columns> FROM `<dataset_id>` [WHERE <where>] LIMIT <limit>

    Args:
        params (QueryDatasetInput): dataset_id, columns, where, limit.

    Returns:
        str: JSON with "columns": list[str], "rows": list[list], "num_rows": int.
    """
    sql = f"SELECT {params.columns} FROM `{params.dataset_id}`"
    if params.where:
        sql += f" WHERE {params.where}"
    sql += f" LIMIT {params.limit}"

    result = await _run_sql(params.dataset_id, sql)
    return json.dumps(
        {
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "num_rows": result.get("numRows", len(result.get("rows", []))),
        },
        indent=2,
    )


class QueryToolkitLeadsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    dataset_id: str = Field(
        default=DEFAULT_DATASET_ID,
        description="Domo dataset ID to query. Defaults to Toolkit + Leads.",
    )
    promotion_name: str = Field(
        default=DEFAULT_PROMOTION_NAME,
        description="Value to filter the promotion column on, e.g. 'Fall Promo 2026'.",
    )
    extra_where: Optional[str] = Field(
        default=None,
        description=(
            "Optional additional SQL WHERE clause (without the word WHERE), "
            "e.g. \"`Lead Date` >= '2026-10-01'\". Combined with AND."
        ),
    )
    limit: int = Field(default=200, ge=1, le=5000, description="Max rows to return.")


@mcp.tool(
    name="domo_query_toolkit_leads",
    annotations={
        "title": "Query Toolkit + Leads dataset",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def domo_query_toolkit_leads(params: QueryToolkitLeadsInput) -> str:
    """Convenience wrapper: raw rows from Toolkit + Leads, filtered by promotion.

    Equivalent to calling domo_query_dataset with the promotion filter
    pre-built for you. For any other dataset, use domo_query_dataset directly.

    Args:
        params (QueryToolkitLeadsInput): dataset_id, promotion_name, extra_where, limit.

    Returns:
        str: JSON with "columns": list[str], "rows": list[list], "num_rows": int.
    """
    where = f"`{DEFAULT_PROMOTION_COLUMN}` = '{params.promotion_name}'"
    if params.extra_where:
        where += f" AND {params.extra_where}"

    sql = f"SELECT * FROM `{params.dataset_id}` WHERE {where} LIMIT {params.limit}"
    result = await _run_sql(params.dataset_id, sql)
    return json.dumps(
        {
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "num_rows": result.get("numRows", len(result.get("rows", []))),
        },
        indent=2,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request that doesn't present MCP_AUTH_TOKEN as a bearer token.

    This is a separate secret from your Domo credentials — it's what stops
    anyone who finds this server's URL from calling it at all. Required once
    the server is reachable over the network (not needed for local stdio use).
    """

    async def dispatch(self, request: Request, call_next):
        expected = os.environ.get("MCP_AUTH_TOKEN")
        if not expected:
            raise RuntimeError(
                "MCP_AUTH_TOKEN is not set. Refusing to start a network-reachable "
                "server with no auth check — set it in your host's environment/secrets."
            )
        if request.headers.get("authorization") != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    # host="0.0.0.0" here is only about the SDK's DNS-rebinding auto-protection
    # (it only auto-enables for 127.0.0.1/localhost); the actual bind address/port
    # for the container is set on uvicorn.run() below.
    app = mcp.streamable_http_app(host="0.0.0.0")
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
