from fastapi import FastAPI

app = FastAPI(title="Basic API")

@app.get("/")
def root():
    return {"message": "hello group 11 :)"}

@app.get("/health")
def health():
    return {"status": "ok"}