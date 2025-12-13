# -*- coding: utf-8 -*-
"""UltraMsg WhatsApp API client wrapper."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, cast

import httpx


class UltraMsgError(RuntimeError):
    """Raised when UltraMsg responds with an error payload."""


@dataclass(slots=True)
class UltraMsgCredentials:
    instance_id: str
    token: str
    base_url: str = "https://api.ultramsg.com"

    def validate(self) -> None:
        if not self.instance_id or not self.token:
            raise UltraMsgError("UltraMsg credentials are incomplete; check instance ID and token.")


class UltraMsgClient:
    """Thin async wrapper around UltraMsg REST endpoints."""

    def __init__(
        self,
        credentials: UltraMsgCredentials,
        *,
        timeout: float = 15.0,
        session: Optional[httpx.AsyncClient] = None,
    ) -> None:
        credentials.validate()
        self._creds = credentials
        self._timeout = timeout
        self._session = session

    @property
    def base_url(self) -> str:
        return self._creds.base_url.rstrip("/")

    @property
    def instance_id(self) -> str:
        return self._creds.instance_id

    async def send_text(self, to: str, body: str, **extra: Any) -> Dict[str, Any]:
        payload = {"to": to, "body": body}
        payload.update({k: v for k, v in extra.items() if v is not None})
        return await self._post("/messages/chat", payload)

    async def send_image(
        self,
        to: str,
        *,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not image_url and not image_base64:
            raise UltraMsgError("Either image_url or image_base64 must be provided.")
        payload: Dict[str, Any] = {"to": to}
        if image_url:
            payload["image"] = image_url
        if image_base64:
            payload["imageBase64"] = image_base64
        if caption:
            payload["caption"] = caption
        if filename:
            payload["filename"] = filename
        return await self._post("/messages/image", payload)

    async def send_document(
        self,
        to: str,
        *,
        document_url: Optional[str] = None,
        document_base64: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not document_url and not document_base64:
            raise UltraMsgError("Either document_url or document_base64 must be provided.")
        payload: Dict[str, Any] = {"to": to}
        if document_url:
            payload["document"] = document_url
        if document_base64:
            payload["document"] = document_base64
        if caption:
            payload["caption"] = caption
        if filename:
            payload["filename"] = filename
        
        # If base64 is very large, try to send as JSON to avoid form-data limits if possible,
        # but UltraMsg usually expects form-data for base64.
        # If we hit 413, we might need to increase client timeout or check server limits.
        # However, since we can't change server limits, we rely on base64.
        return await self._post("/messages/document", payload)

    async def send_buttons(
        self,
        to: str,
        body: str,
        buttons: Sequence[Mapping[str, object]],
        *,
        title: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send native WhatsApp interactive buttons via UltraMsg.
        
        :param buttons: List of dicts, e.g. [{"id": "btn1", "label": "Click Me"}]
        """
        if not buttons:
            raise UltraMsgError("Buttons list cannot be empty.")

        # UltraMsg Interactive Message Structure
        # https://docs.ultramsg.com/api/post/messages/interactive
        
        formatted_buttons: list[Dict[str, Any]] = []
        for btn in buttons:
            btn_map: Dict[str, object] = dict(btn)
            reply_raw = btn_map.get("reply")
            reply_map: Dict[str, object] = dict(cast(Mapping[str, object], reply_raw)) if isinstance(reply_raw, Mapping) else {}
            btn_id = btn_map.get("id") or reply_map.get("id")
            btn_title = btn_map.get("label") or btn_map.get("title") or btn_map.get("body") or reply_map.get("title")

            if not btn_id or not btn_title:
                continue

            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": str(btn_id),
                    "title": str(btn_title)[:20],  # WhatsApp limit for button title is ~20 chars
                },
            })

        payload: Dict[str, Any] = {
            "to": to,
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": formatted_buttons},
        }
        
        if title:
            payload["header"] = {"type": "text", "text": title}
        if footer:
            payload["footer"] = {"text": footer}

        return await self._post("/messages/interactive", payload, is_json=True)

    async def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict[str, Any]],
        *,
        title: Optional[str] = None,
        footer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send native WhatsApp interactive list via UltraMsg.
        
        :param sections: List of dicts defining sections and rows.
        Example:
        [
            {
                "title": "Section 1",
                "rows": [
                    {"id": "row1", "title": "Row 1", "description": "Desc 1"},
                    {"id": "row2", "title": "Row 2"}
                ]
            }
        ]
        """
        payload: Dict[str, Any] = {
            "to": to,
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text,
                "sections": sections
            }
        }
        
        if title:
            payload["header"] = {"type": "text", "text": title}
        if footer:
            payload["footer"] = {"text": footer}

        return await self._post("/messages/interactive", payload, is_json=True)

    async def _post(self, endpoint: str, payload: Dict[str, Any], is_json: bool = False) -> Dict[str, Any]:
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/{self.instance_id}/{endpoint}"
        params = {"token": self._creds.token}
        client = self._session or httpx.AsyncClient(
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
            http2=True,
        )
        close_client = self._session is None
        try:
            if is_json:
                response = await client.post(url, params=params, json=payload)
            else:
                response = await client.post(url, params=params, data=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise UltraMsgError(f"UltraMsg HTTP error: {exc.response.status_code} {exc.response.text}") from exc
        except httpx.HTTPError as exc:
            raise UltraMsgError(f"UltraMsg network error: {exc}") from exc
        finally:
            if close_client:
                await client.aclose()
        if not isinstance(data, dict):
            raise UltraMsgError("UltraMsg response was not a JSON object.")

        data_dict: Dict[str, Any] = cast(Dict[str, Any], data)
        if data_dict.get("error"):
            raise UltraMsgError(str(data_dict.get("error")))
        return data_dict
