"""
Uazapi v2 WhatsApp API Client.

Endpoints from OpenAPI spec v2.1.0:
- POST /send/text       → Send text message
- POST /send/media      → Send media (image, video, audio, document, ptt, sticker)
- POST /message/download → Download media + optional audio transcription
- Auth: header "token"

Baseado no liriel-agent (doi010520/liriel-agent).
"""

import httpx
from loguru import logger
from app.core.config import get_settings

settings = get_settings()


class UazapiClient:
    def __init__(self):
        self.base_url = settings.uazapi_base_url.rstrip("/")
        self.token = settings.uazapi_token
        self.timeout = httpx.Timeout(60.0)

    @property
    def headers(self) -> dict:
        return {"Content-Type": "application/json", "token": self.token}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ── Send ──────────────────────────────────────────────────

    async def send_text(self, phone: str, text: str, delay: int = 0, reply_id: str | None = None) -> dict | None:
        payload: dict = {"number": phone, "text": text}
        if delay:
            payload["delay"] = delay
        if reply_id:
            payload["replyid"] = reply_id
        return await self._post("/send/text", payload)

    async def send_media(self, phone: str, media_type: str, file: str, caption: str = "", doc_name: str = "") -> dict | None:
        payload: dict = {"number": phone, "type": media_type, "file": file}
        if caption:
            payload["text"] = caption
        if doc_name:
            payload["docName"] = doc_name
        return await self._post("/send/media", payload)

    # ── Presence / Read (UX) ─────────────────────────────────

    async def mark_read(self, message_id: str) -> dict | None:
        return await self._post("/message/markread", {"id": message_id})

    async def send_presence(self, phone: str, presence: str = "composing") -> dict | None:
        """presence: composing | recording | paused"""
        return await self._post("/message/presence", {"number": phone, "presence": presence})

    # ── Profile / Instance ────────────────────────────────────

    async def update_profile_image(self, image: str) -> dict | None:
        return await self._post("/profile/image", {"image": image})

    async def update_profile_name(self, name: str) -> dict | None:
        return await self._post("/profile/name", {"name": name[:25]})

    async def get_instance_status(self) -> dict | None:
        url = self._url("/instance/status")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Uazapi /instance/status HTTP {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Uazapi /instance/status error: {e}")
            return None

    # ── Download / Transcribe ────────────────────────────────

    async def download_media(
        self,
        message_id: str,
        return_base64: bool = False,
        return_link: bool = True,
        transcribe: bool = False,
        generate_mp3: bool = True,
    ) -> dict | None:
        payload: dict = {
            "id": message_id,
            "return_base64": return_base64,
            "return_link": return_link,
            "transcribe": transcribe,
            "generate_mp3": generate_mp3,
        }
        return await self._post("/message/download", payload)

    async def transcribe_audio(self, message_id: str) -> str | None:
        result = await self.download_media(
            message_id=message_id, transcribe=True, return_link=False, return_base64=False
        )
        if result and result.get("transcription"):
            return result["transcription"]
        return None

    async def get_media_base64(self, message_id: str) -> tuple[str, str] | None:
        result = await self.download_media(message_id=message_id, return_base64=True, return_link=False)
        if result and result.get("base64Data"):
            return result["base64Data"], result.get("mimetype", "")
        return None

    async def get_media_url(self, message_id: str) -> str | None:
        result = await self.download_media(message_id=message_id, return_base64=False, return_link=True)
        if result and result.get("fileURL"):
            return result["fileURL"]
        return None

    # ── Internal ──────────────────────────────────────────────

    async def _post(self, path: str, payload: dict) -> dict | None:
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                logger.debug(f"Uazapi {path} → {response.status_code}")
                return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Uazapi {path} HTTP {e.response.status_code}: {e.response.text[:300]}")
            return None
        except Exception as e:
            logger.error(f"Uazapi {path} error: {e}")
            return None

    # ── Webhook Parser ───────────────────────────────────────

    @staticmethod
    def parse_webhook(payload: dict) -> dict | None:
        """Parse Uazapi v2 webhook → normalized dict.

        Aceita mensagens diretas (ignora fromMe e isGroup).
        """
        try:
            event = payload.get("event") or payload.get("EventType", "")
            if event not in ("message", "messages"):
                logger.debug(f"Ignoring webhook event: {event}")
                return None

            msg = payload.get("message") or payload.get("data", {})

            if msg.get("fromMe", False):
                return None
            if msg.get("isGroup", False):
                return None

            chat_id = msg.get("chatid") or msg.get("sender_pn") or msg.get("sender", "")
            phone = chat_id.split("@")[0] if chat_id else ""

            if not phone or not phone.isdigit():
                sender_pn = msg.get("sender_pn", "")
                phone = sender_pn.split("@")[0] if sender_pn else ""

            if not phone or not phone.isdigit():
                chat_data = payload.get("chat", {})
                raw_phone = chat_data.get("phone", "")
                phone = "".join(c for c in raw_phone if c.isdigit())

            if not phone or not phone.isdigit():
                logger.warning(f"Invalid phone from webhook: {chat_id}")
                return None

            msg_type_raw = msg.get("messageType") or msg.get("type", "conversation")
            msg_type = _normalize_message_type(msg_type_raw)
            message_id = msg.get("messageid") or msg.get("id", "")
            text = msg.get("text", "")

            return {
                "phone": phone,
                "body": str(text).strip() if text else "",
                "type": msg_type,
                "type_raw": msg_type_raw,
                "push_name": msg.get("senderName", ""),
                "message_id": message_id,
                "quoted_id": msg.get("quoted", ""),
                "timestamp": msg.get("messageTimestamp", 0),
                "content": msg.get("content", {}),
            }
        except Exception as e:
            logger.error(f"Error parsing webhook: {e}")
            return None


def _normalize_message_type(raw_type: str) -> str:
    mapping = {
        "conversation": "text",
        "Conversation": "text",
        "extendedTextMessage": "text",
        "imageMessage": "image",
        "videoMessage": "video",
        "audioMessage": "audio",
        "pttMessage": "audio",
        "documentMessage": "document",
        "documentWithCaptionMessage": "document",
        "stickerMessage": "sticker",
        "contactMessage": "contact",
        "locationMessage": "location",
        "reactionMessage": "reaction",
    }
    return mapping.get(raw_type, raw_type)


uazapi = UazapiClient()
