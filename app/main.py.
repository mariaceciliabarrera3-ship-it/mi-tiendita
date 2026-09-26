from fastapi import FastAPI

app = FastAPI(
    title="Mi Tiendita",
    description="Asistente de IA para comercios sobre WhatsApp",
    version="1.0.0"
)

@app.get("/healthz")
async def health_check():
    return {"status": "ok", "app": "Mi Tiendita"}
