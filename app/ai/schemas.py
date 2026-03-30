AI_REPLY_SCHEMA = {
    "name": "ai_reply",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["book", "cancel", "reschedule", "info", "late", "unknown"],
            },
            "draft_patch": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "customer_name": {"type": ["string", "null"]},
                    "barber": {"type": ["string", "null"]},
                    "day_text": {"type": ["string", "null"]},
                    "time_hhmm": {"type": ["string", "null"]},
                    "service_name": {"type": ["string", "null"]},
                    "service_key": {"type": ["string", "null"]},
                    "age": {"type": ["integer", "null"]},
                    "latest_finish_hhmm": {"type": ["string", "null"]},
                },
            },
            "action": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "none",
                            "check_day_availability",
                            "find_offers",
                            "resolve_pending_choice",
                            "cancel_booking",
                            "handle_late_arrival",
                        ],
                    },
                    "booking_id": {"type": ["integer", "null"]},
                    "late_minutes": {"type": ["integer", "null"]},
                },
                "required": ["type", "booking_id", "late_minutes"],
            },
            "confirmation_state": {
                "type": "string",
                "enum": ["none", "confirm", "reject"],
            },
            "pending_resolution": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["none", "pending_option"],
                    },
                    "option_id": {"type": ["string", "null"]},
                },
                "required": ["type", "option_id"],
            },
            "selected_time_hhmm": {"type": ["string", "null"]},
            "reply_text": {"type": "string"},
        },
        "required": [
            "intent",
            "draft_patch",
            "action",
            "confirmation_state",
            "pending_resolution",
            "selected_time_hhmm",
            "reply_text",
        ],
    },
}