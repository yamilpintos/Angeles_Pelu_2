from fastapi import FastAPI
from app.api.twilio_webhook import router as whatsapp_router

app = FastAPI(title="SalonBot (Conversacional)")
app.include_router(whatsapp_router)