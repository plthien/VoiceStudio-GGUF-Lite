"""Allowlisted same-origin bundled UI. No static directory mount or arbitrary file serving."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import Response, RedirectResponse
from .config import ROOT

router=APIRouter(include_in_schema=False)
UI=ROOT/"lite"/"ui"
ASSETS={
    "assets/app.css":"text/css; charset=utf-8",
    "assets/app.js":"text/javascript; charset=utf-8",
    "assets/api.js":"text/javascript; charset=utf-8",
}
HEADERS={
 "Content-Security-Policy":"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
 "X-Content-Type-Options":"nosniff",
 "Referrer-Policy":"no-referrer",
 "Cache-Control":"no-store"
}
@router.get("/")
def home():
    return RedirectResponse(url="/ui",status_code=307,headers=HEADERS)

@router.get("/ui")
def index():
    return Response((UI/"index.html").read_bytes(),media_type="text/html",headers=HEADERS)

@router.get("/ui/assets/{asset_path:path}")
def asset(asset_path:str):
    if asset_path not in ASSETS:
        return Response(status_code=404,headers=HEADERS)
    return Response((UI/asset_path).read_bytes(),media_type=ASSETS[asset_path],headers=HEADERS)
