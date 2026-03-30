from openai import OpenAI
from pydantic import BaseModel
from app.core.config import settings

# ✅ QUIRÚRGICO:
# timeout explícito para que no quede colgado demasiado tiempo
# y menos reintentos para que falle rápido y vuelva al flujo normal
_client = OpenAI(
    api_key=settings.OPENAI_API_KEY,
    timeout=25.0,
    max_retries=1,
)

def responses_parse(*, model: str, system: str, user: str, text_format: type[BaseModel]) -> BaseModel:
    try:
        resp = _client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=text_format,
        )
        parsed = getattr(resp, "output_parsed", None)
        if parsed is None:
            print("[DBG OPENAI PARSE] output_parsed vacío")
            return text_format()  # type: ignore

        return parsed

    except Exception as e:
        print("[ERR OPENAI RESPONSES_PARSE]", type(e).__name__, str(e))
        return text_format()  # type: ignore