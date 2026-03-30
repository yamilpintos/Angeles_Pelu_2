from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.core.utils import safe_phone
from app.flows.message_processor import process_consolidated_message

router = APIRouter()

MESSAGE_BATCH_WINDOW_SECONDS = 1

_MESSAGE_BUFFERS: dict[str, dict] = {}
_BATCH_TASKS: dict[str, asyncio.Task] = {}


async def _run_deferred_batch(phone: str) -> None:
    try:
        while True:
            await asyncio.sleep(MESSAGE_BATCH_WINDOW_SECONDS)

            payload = _MESSAGE_BUFFERS.get(phone)
            if not payload:
                return

            silence = time.time() - float(payload.get("last_at") or 0.0)
            if silence < MESSAGE_BATCH_WINDOW_SECONDS:
                continue

            texts = payload.get("texts") or []
            text = " ".join(str(x).strip() for x in texts if str(x).strip()).strip()

            _MESSAGE_BUFFERS.pop(phone, None)
            _BATCH_TASKS.pop(phone, None)

            if text:
                await process_consolidated_message(phone, text)
            return
    except Exception as e:
        print("[ERR DEFERRED BATCH]", type(e).__name__, str(e))
        _MESSAGE_BUFFERS.pop(phone, None)
        _BATCH_TASKS.pop(phone, None)


@router.get("/meta/whatsapp")
async def meta_whatsapp_verify(request: Request):
    mode = str(request.query_params.get("hub.mode") or "").strip()
    token = str(request.query_params.get("hub.verify_token") or "").strip()
    challenge = str(request.query_params.get("hub.challenge") or "")

    expected_token = str(getattr(settings, "WHATSAPP_VERIFY_TOKEN", "") or "").strip()

    print("[DBG VERIFY] mode=", repr(mode))
    print("[DBG VERIFY] token recibido=", repr(token))
    print("[DBG VERIFY] token settings=", repr(expected_token))
    print("[DBG VERIFY] challenge=", repr(challenge))

    if mode == "subscribe" and token == expected_token:
        return PlainTextResponse(content=challenge, status_code=200)

    raise HTTPException(status_code=403, detail="verify token inválido")


@router.post("/meta/whatsapp")
async def meta_whatsapp(request: Request):
    body = await request.json()

    print("[META RAW] object=", body.get("object"))

    TEST_TARGET_PHONE = ""

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})

            print("[META CHANGE] field=", repr(field))
            print("[META CHANGE] value keys=", list(value.keys()))
            print(
                "[META INBOUND META]",
                {
                    "display_phone_number": value.get("metadata", {}).get("display_phone_number"),
                    "phone_number_id": value.get("metadata", {}).get("phone_number_id"),
                },
            )

            statuses = value.get("statuses", [])
            if statuses:
                print("[META STATUSES]", statuses)

            messages = value.get("messages", [])
            if not messages:
                continue

            for msg in messages:
                source_phone_raw = str(msg.get("from") or "").strip()
                source_phone = safe_phone(source_phone_raw)

                target_phone = "".join(ch for ch in source_phone_raw if ch.isdigit())
                if target_phone.startswith("549"):
                    target_phone = "54" + target_phone[3:]

                print("[DBG PHONE FORMAT] raw=", source_phone_raw, "target=", target_phone)

                if TEST_TARGET_PHONE and source_phone_raw != TEST_TARGET_PHONE:
                    print("[META SKIP] remitente fuera de prueba:", source_phone_raw)
                    continue

                msg_type = str(msg.get("type") or "").strip()
                incoming_text = ""

                if msg_type == "text":
                    incoming_text = str(msg.get("text", {}).get("body") or "").strip()

                elif msg_type == "button":
                    incoming_text = str(msg.get("button", {}).get("text") or "").strip()

                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    interactive_type = str(interactive.get("type") or "").strip()

                    if interactive_type == "button_reply":
                        incoming_text = str(interactive.get("button_reply", {}).get("title") or "").strip()
                    elif interactive_type == "list_reply":
                        incoming_text = str(interactive.get("list_reply", {}).get("title") or "").strip()

                print(
                    "[META MSG]",
                    {
                        "from": source_phone_raw,
                        "source_phone": source_phone,
                        "target_phone": target_phone,
                        "type": msg_type,
                        "text": incoming_text,
                    },
                )

                if not incoming_text:
                    print("[META SKIP] mensaje sin texto util")
                    continue

                payload = _MESSAGE_BUFFERS.get(
                    source_phone,
                    {
                        "texts": [],
                        "last_at": 0.0,
                        "target_phone": target_phone,
                    },
                )
                payload["texts"].append(incoming_text)
                payload["last_at"] = time.time()
                payload["target_phone"] = target_phone
                _MESSAGE_BUFFERS[source_phone] = payload

                task = _BATCH_TASKS.get(source_phone)
                print(
                    "[META TASK] existing=",
                    task,
                    "done=",
                    None if task is None else task.done(),
                )

                if task is None or task.done():
                    print("[META TASK] creando deferred batch para", source_phone)
                    _BATCH_TASKS[source_phone] = asyncio.create_task(
                        _run_deferred_batch(source_phone)
                    )

    return Response(status_code=200)