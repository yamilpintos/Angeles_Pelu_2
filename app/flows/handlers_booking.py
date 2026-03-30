from __future__ import annotations

from app.actions.booking import find_offers, recheck_slot_live, reserve_slot, rgb_from_draft
from app.core.catalog import format_price, get_service, price_for
from app.core.config import settings
from app.core.utils import merge_draft
from app.flows.booking_flow import (
    draft_has_all,
    handle_booking_progress,
    offer_to_draft_patch,
    offers_sys_event,
    prompt_missing_fields,
    safe_service_blocks,
    should_release_choose_slot_for_new_query,
    validate_requested_slot_for_chat,
)
from app.flows.common import ai_confirmed, ai_rejected, maybe_resolve_any_barber, resolve_pending_option
from app.flows.day_context import pending_choose_slot_options
from app.flows.pending_helpers import set_pending
from app.flows.post_success import enter_soft_post_success_context, reset_session_after_success
from app.flows.replying import reply_async, reply_with_event, safe_reply_text


def handle_pending_booking(phone: str, session, ai, text: str, prev_draft: dict) -> bool:
    pending_type = session.pending.type if session.pending else "none"

    if pending_type == "confirm_booking":
        if ai_confirmed(ai):
            blocks = safe_service_blocks(session.draft)
            rgb = rgb_from_draft(session.draft)
            res = reserve_slot(
                draft=session.draft,
                phone=phone,
                provider="meta",
                blocks=blocks,
                rgb=rgb,
                extra_metadata={"age": session.draft.age},
            )
            if res.ok:
                svc = get_service(session.draft.service_key)
                effective_price = price_for(session.draft.service_key, age=session.draft.age)
                base_price = svc.price if svc else None
                senior_price = svc.price_senior if svc else None
                has_senior_discount = bool(
                    svc
                    and session.draft.age is not None
                    and session.draft.age >= 65
                    and svc.price_senior < svc.price
                )

                reserve_payload = {
                    "booking_id": res.booking_id,
                    "customer_name": session.draft.customer_name,
                    "barber": session.draft.barber,
                    "day_text": session.draft.day_text,
                    "time_hhmm": session.draft.time_hhmm,
                    "service_name": session.draft.service_name or (svc.display_name if svc else ""),
                    "service_key": session.draft.service_key,
                    "price": effective_price,
                    "base_price": base_price,
                    "price_senior": senior_price,
                    "age": session.draft.age,
                    "has_senior_discount": has_senior_discount,
                    "formatted_price": format_price(effective_price) if effective_price is not None else "",
                    "formatted_base_price": format_price(base_price) if base_price is not None else "",
                    "formatted_price_senior": format_price(senior_price) if senior_price is not None else "",
                }
                enter_soft_post_success_context(session, "booking")
                reply_with_event(phone, session, f"SISTEMA_RESERVE_OK: {reserve_payload}", text)
                return True

            if res.reason == "slot_taken":
                offers_result = find_offers(
                    session.draft,
                    blocks=blocks,
                    barbers=settings.BARBERS,
                    max_offers=3,
                )
                offers = offers_result.offers or []
                if offers:
                    set_pending(
                        session,
                        "choose_slot",
                        pending_choose_slot_options(
                            offers_result,
                            session,
                            all_barbers=settings.BARBERS,
                        ),
                    )
                    reply_with_event(
                        phone,
                        session,
                        offers_sys_event(
                            offers_result,
                            session,
                            event_name="SISTEMA_SLOT_TAKEN_OFFERS",
                            all_barbers=settings.BARBERS,
                        ),
                        text,
                    )
                    return True

                set_pending(session, "none", [])
                reply_with_event(phone, session, "SISTEMA_SLOT_TAKEN_NO_NEAR_OFFERS", text)
                return True

            set_pending(session, "none", [])
            reply_with_event(phone, session, f"SISTEMA_RESERVE_ERROR: {res.error}", text)
            return True

        if ai_rejected(ai):
            reset_session_after_success(session)
            reply_with_event(phone, session, "SISTEMA_CONFIRMATION_CANCELLED", text)
            return True

        if not draft_has_all(session):
            prompt_missing_fields(phone, session, text)
            return True

        invalid_reason = validate_requested_slot_for_chat(session)
        if invalid_reason:
            from app.flows.booking_flow import validation_error_sys_event

            set_pending(session, "collect_booking_data", [])
            reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
            return True

        set_pending(session, "confirm_booking", [])
        reply_with_event(phone, session, "SISTEMA_CONFIRM_BOOKING", text)
        return True

    if pending_type == "choose_slot":
        chosen = resolve_pending_option(session, ai)
        if chosen:
            patch = offer_to_draft_patch(session, chosen)
            session.draft = merge_draft(session.draft, patch)

            invalid_reason = validate_requested_slot_for_chat(session)
            if invalid_reason:
                from app.flows.booking_flow import validation_error_sys_event

                reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
                return True

            if not draft_has_all(session):
                prompt_missing_fields(phone, session, text)
                return True

            set_pending(session, "confirm_booking", [])
            reply_with_event(phone, session, "SISTEMA_CONFIRM_BOOKING", text)
            return True

        if should_release_choose_slot_for_new_query(prev_draft, session, ai):
            print("[DBG CHOOSE_SLOT RELEASE] nueva consulta detectada -> limpio pending viejo y recalculo")
            set_pending(session, "none", [])

            if handle_booking_progress(phone, session, ai, text, all_barbers=settings.BARBERS):
                return True

        reply_async(phone, safe_reply_text(ai.reply_text, session, text))
        return True

    return False


def handle_booking_main(phone: str, session, ai, text: str) -> bool:
    if handle_booking_progress(phone, session, ai, text, all_barbers=settings.BARBERS):
        return True

    pending_type = session.pending.type if session.pending else "none"

    if session.intent == "book" and not draft_has_all(session) and pending_type == "none":
        set_pending(session, "collect_booking_data", [])

    if session.intent == "book" and draft_has_all(session) and (
        session.pending is None or session.pending.type in {"none", "collect_booking_data"}
    ):
        invalid_reason = validate_requested_slot_for_chat(session)
        if invalid_reason:
            from app.flows.booking_flow import validation_error_sys_event

            set_pending(session, "collect_booking_data", [])
            reply_with_event(phone, session, validation_error_sys_event(invalid_reason, session), text)
            return True

        blocks = safe_service_blocks(session.draft)
        maybe_resolve_any_barber(session)

        ok = recheck_slot_live(session.draft, time_hhmm=session.draft.time_hhmm, blocks=blocks)
        if ok:
            set_pending(session, "confirm_booking", [])
            reply_with_event(phone, session, "SISTEMA_CONFIRM_BOOKING", text)
            return True

        offers_result = find_offers(session.draft, blocks=blocks, barbers=settings.BARBERS, max_offers=3)
        offers = offers_result.offers or []
        if offers:
            set_pending(
                session,
                "choose_slot",
                pending_choose_slot_options(
                    offers_result,
                    session,
                    all_barbers=settings.BARBERS,
                ),
            )
            reply_with_event(phone, session, offers_sys_event(offers_result, session, all_barbers=settings.BARBERS), text)
            return True

        set_pending(session, "none", [])
        reply_with_event(phone, session, "SISTEMA_OFFERS_EMPTY", text)
        return True

    return False