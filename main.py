#!/usr/bin/env python3
"""
Main production-ready API entry for BrittonMethod-auto
"""
from fastapi import FastAPI

app = FastAPI(title="BrittonMethod-auto API")

@app.get("/")
async def root():
    return {"message": "BrittonMethod-auto is live!"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
