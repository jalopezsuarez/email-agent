"""Microsoft Graph wrapper for a personal Microsoft 365 account.

Authentication: MSAL public-client device-code flow. Tokens are cached on disk
so the user only completes the browser step once.

Safety: Every outgoing HTTP request is guarded by :func:`safety.assert_safe_path`,
which raises ``SendingForbiddenError`` if the URL targets a mail-send endpoint.
The ``Mail.Send`` scope is never requested, so even bypassing the guard would
result in a server-side 403.
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Any, Iterable

import httpx
import msal

from .safety import assert_safe_path

log = logging.getLogger(__name__)
_GRAPH = "https://graph.microsoft.com/v1.0"
_RESERVED_SCOPES = {"openid", "profile", "offline_access"}


class GraphAuthError(RuntimeError):
    pass


class GraphClient:
    def __init__(
        self,
        client_id: str,
        tenant: str,
        scopes: Iterable[str],
        token_cache_path: str,
        http_timeout: float = 30.0,
    ):
        if not client_id:
            raise GraphAuthError("MS_CLIENT_ID is not configured.")
        # Defensive: block Mail.Send and OIDC reserved scopes even if mis-configured.
        # MSAL manages reserved scopes itself and rejects them in initiate_device_flow().
        scopes = [
            s for s in scopes
            if s.lower() != "mail.send" and s.lower() not in _RESERVED_SCOPES
        ]
        self._client_id = client_id
        self._authority = f"https://login.microsoftonline.com/{tenant}"
        self._scopes = list(scopes)
        self._cache = self._build_cache(token_cache_path)
        self._app = msal.PublicClientApplication(
            client_id=client_id,
            authority=self._authority,
            token_cache=self._cache,
        )
        self._http = httpx.Client(timeout=http_timeout)
        self._token: str | None = None

    # ---------------------------------------------------------- auth
    def _build_cache(self, path: str) -> msal.SerializableTokenCache:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cache = msal.SerializableTokenCache()
        if os.path.exists(path):
            try:
                cache.deserialize(Path(path).read_text())
            except Exception:
                log.warning("Token cache corrupt; starting fresh")

        def _persist() -> None:
            if cache.has_state_changed:
                Path(path).write_text(cache.serialize())

        atexit.register(_persist)
        self._persist_cache = _persist
        return cache

    def ensure_token(self, *, interactive: bool = True) -> str:
        try:
            return self.acquire_cached_token()
        except GraphAuthError:
            if not interactive:
                raise
        flow = self.initiate_device_flow()
        print("\n=== Microsoft 365 login ===")
        print(flow["message"])
        print("===========================\n")
        return self.complete_device_flow(flow)

    def acquire_cached_token(self) -> str:
        accounts = self._app.get_accounts()
        result: dict | None = None
        if accounts:
            result = self._app.acquire_token_silent(self._scopes, account=accounts[0])
        if not result:
            raise GraphAuthError("No cached token; start Microsoft login.")
        return self._consume_token_result(result)

    def initiate_device_flow(self) -> dict:
        flow = self._app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise GraphAuthError(self._format_device_flow_error(flow))
        return flow

    def complete_device_flow(self, flow: dict) -> str:
        result = self._app.acquire_token_by_device_flow(flow)
        return self._consume_token_result(result)

    def _consume_token_result(self, result: dict) -> str:
        if "access_token" not in result:
            raise GraphAuthError(f"Auth failed: {result.get('error_description')}")
        self._persist_cache()
        self._token = result["access_token"]
        return self._token

    def _format_device_flow_error(self, flow: dict) -> str:
        code = str(flow.get("error_codes", [""])[0] or "")
        desc = flow.get("error_description", "Unknown device-flow error.")
        if code == "700016" and self._authority.endswith("/consumers"):
            return (
                "MS_CLIENT_ID is not valid for personal Microsoft accounts. "
                "Use an app registration whose Supported account types include "
                "'Personal Microsoft accounts', or switch graph.tenant to a work/school "
                "tenant if this mailbox is not personal."
            )
        if code == "50059":
            return (
                "The configured authority does not identify a valid tenant for device-code "
                "login. Use 'consumers' for personal accounts, or 'organizations' / a tenant "
                "ID/domain for work and school accounts."
            )
        return f"Device flow failed: {desc}"

    def is_authenticated(self) -> bool:
        try:
            accounts = self._app.get_accounts()
            return bool(accounts)
        except Exception:
            return False

    # ---------------------------------------------------------- HTTP core
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        # SAFETY: refuse to issue any outbound-send request, regardless of scope.
        assert_safe_path(method, path)
        if not self._token:
            self.ensure_token(interactive=False)
        url = path if path.startswith("http") else f"{_GRAPH}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        r = self._http.request(method, url, headers=headers, params=params, json=json)
        if r.status_code == 401:
            # refresh once
            self.ensure_token(interactive=False)
            headers = {"Authorization": f"Bearer {self._token}"}
            r = self._http.request(method, url, headers=headers, params=params, json=json)
        return r

    def _ok(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise RuntimeError(f"Graph {r.request.method} {r.request.url}: {r.status_code} {r.text[:400]}")
        return r.json() if r.content else {}

    def _iter_messages(
        self,
        path: str,
        *,
        select: str,
        orderby: str,
        filter_field: str,
        since_iso: str | None = None,
        before_iso: str | None = None,
        page_size: int = 50,
    ):
        url: str | None = path
        params: dict[str, Any] | None = {
            "$top": page_size,
            "$orderby": orderby,
            "$select": select,
        }
        filters: list[str] = []
        if since_iso:
            filters.append(f"{filter_field} ge {since_iso}")
        if before_iso:
            filters.append(f"{filter_field} lt {before_iso}")
        if filters:
            params["$filter"] = " and ".join(filters)
        while url:
            data = self._ok(self._request("GET", url, params=params))
            for message in data.get("value", []):
                yield message
            url = data.get("@odata.nextLink")
            params = None

    # ---------------------------------------------------------- folders
    def list_folders(self) -> list[dict]:
        """Flat list of all mail folders (including nested)."""
        out: list[dict] = []

        def recurse(prefix: str, parent_id: str | None):
            path = (
                "/me/mailFolders"
                if parent_id is None
                else f"/me/mailFolders/{parent_id}/childFolders"
            )
            url: str | None = path
            params: dict | None = {"$top": 100}
            while url:
                r = self._request("GET", url, params=params)
                data = self._ok(r)
                for f in data.get("value", []):
                    name = f["displayName"]
                    full = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
                    out.append(
                        {
                            "id": f["id"],
                            "display_name": name,
                            "full_name": full,
                            "child_folder_count": f.get("childFolderCount", 0),
                            "total_item_count": f.get("totalItemCount", 0),
                            "well_known_name": f.get("wellKnownName"),
                        }
                    )
                    if f.get("childFolderCount", 0):
                        recurse(full, f["id"])
                url = data.get("@odata.nextLink")
                params = None  # nextLink carries query

        recurse("", None)
        return out

    def ensure_folder(self, name: str) -> dict:
        for f in self.list_folders():
            if f["display_name"] == name and "/" not in f["full_name"]:
                return f
        r = self._request("POST", "/me/mailFolders", json={"displayName": name})
        created = self._ok(r)
        return {
            "id": created["id"],
            "display_name": created["displayName"],
            "full_name": created["displayName"],
            "child_folder_count": 0,
            "total_item_count": 0,
            "well_known_name": created.get("wellKnownName"),
        }

    # ---------------------------------------------------------- inbox
    def list_inbox(self, since_iso: str | None = None, top: int = 25) -> list[dict]:
        params: dict[str, Any] = {
            "$top": min(top, 50),
            "$orderby": "receivedDateTime desc",
            "$select": (
                "id,subject,from,toRecipients,receivedDateTime,bodyPreview,"
                "parentFolderId,isRead,conversationId"
            ),
        }
        if since_iso:
            params["$filter"] = f"receivedDateTime gt {since_iso}"
        r = self._request("GET", "/me/mailFolders/Inbox/messages", params=params)
        return self._ok(r).get("value", [])

    def iter_inbox(self, since_iso: str | None = None, page_size: int = 50):
        yield from self._iter_messages(
            "/me/mailFolders/Inbox/messages",
            select=(
                "id,subject,from,toRecipients,receivedDateTime,bodyPreview,"
                "parentFolderId,isRead,conversationId"
            ),
            orderby="receivedDateTime desc",
            filter_field="receivedDateTime",
            since_iso=since_iso,
            page_size=min(page_size, 50),
        )

    def list_sent(self, top: int = 100) -> list[dict]:
        params = {
            "$top": min(top, 100),
            "$orderby": "sentDateTime desc",
            "$select": (
                "id,subject,toRecipients,sentDateTime,bodyPreview,body,conversationId"
            ),
        }
        r = self._request("GET", "/me/mailFolders/SentItems/messages", params=params)
        return self._ok(r).get("value", [])

    def iter_sent(
        self,
        *,
        since_iso: str | None = None,
        before_iso: str | None = None,
        page_size: int = 100,
    ):
        yield from self._iter_messages(
            "/me/mailFolders/SentItems/messages",
            select="id,subject,toRecipients,sentDateTime,bodyPreview,body,conversationId",
            orderby="sentDateTime desc",
            filter_field="sentDateTime",
            since_iso=since_iso,
            before_iso=before_iso,
            page_size=min(page_size, 100),
        )

    def get_message(self, message_id: str) -> dict:
        r = self._request("GET", f"/me/messages/{message_id}")
        return self._ok(r)

    def move_message(self, message_id: str, destination_folder_id: str) -> dict:
        r = self._request(
            "POST",
            f"/me/messages/{message_id}/move",
            json={"destinationId": destination_folder_id},
        )
        return self._ok(r)

    # ---------------------------------------------------------- drafts
    def create_reply_draft(self, message_id: str, body_html: str) -> dict:
        """Create a reply draft (NO send). Returns the new draft message."""
        # createReply produces a draft message under Drafts folder.
        r = self._request("POST", f"/me/messages/{message_id}/createReply")
        draft = self._ok(r)
        draft_id = draft["id"]
        # Update the draft body.
        patch = {"body": {"contentType": "HTML", "content": body_html}}
        r2 = self._request("PATCH", f"/me/messages/{draft_id}", json=patch)
        return self._ok(r2)

    def update_draft_body(self, draft_id: str, body_html: str) -> dict:
        patch = {"body": {"contentType": "HTML", "content": body_html}}
        r = self._request("PATCH", f"/me/messages/{draft_id}", json=patch)
        return self._ok(r)

    def move_draft(self, draft_id: str, destination_folder_id: str) -> dict:
        r = self._request(
            "POST",
            f"/me/messages/{draft_id}/move",
            json={"destinationId": destination_folder_id},
        )
        return self._ok(r)
