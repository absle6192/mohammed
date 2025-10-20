from from
fastapi import FastAPI
fastapi.responses import JSONResponse, F
app = FastAPI()
@app.get("/")
def home():
return HTMLResponse("<h3>Trading bot serv
@app.get ("/health")
def health():
return
JSONResponse ({"status": "ok"})
