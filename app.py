from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI()

@app.get("/")
def home():
    return HTMLResponse("<h3>Trading bot service is up ✅</h3>")

@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
