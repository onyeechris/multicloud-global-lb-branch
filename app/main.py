import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

_backend_url = os.getenv("PULUMI_BACKEND_URL")
_pulumi_token = os.getenv("PULUMI_ACCESS_TOKEN")
if _backend_url:
    os.environ["PULUMI_BACKEND_URL"] = _backend_url
elif _pulumi_token:
    os.environ["PULUMI_ACCESS_TOKEN"] = _pulumi_token
else:
    raise RuntimeError(
        "No Pulumi backend configured. Set either PULUMI_BACKEND_URL (S3) "
        "or PULUMI_ACCESS_TOKEN (Pulumi Cloud) in your .env file."
    )

_passphrase = os.getenv("PULUMI_CONFIG_PASSPHRASE")
if _passphrase is None:
    raise RuntimeError(
        "PULUMI_CONFIG_PASSPHRASE is not set. Add it to your .env file. "
        "Set to empty string to disable passphrase encryption."
    )
os.environ["PULUMI_CONFIG_PASSPHRASE"] = _passphrase

from app.routes.aws_vpn import router as aws_vpn_router
from app.routes.azure_vpn import router as azure_vpn_router
from app.routes.aws_lb import router as aws_lb_router
from app.routes.region import router as region_router
from app.routes.location import router as location_router
from app.routes.vpn_orchestrator import router as vpn_orchestrator_router

app = FastAPI(
    title="MultiCloud VPN Service",
    description=(
        "Provisions and manages an AWS <-> Azure site-to-site VPN via the "
        "Pulumi Automation API. Each resource type is an independent stack "
        "with full CRUD endpoints; endpoints resolve dependency stack "
        "outputs automatically."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(region_router)
app.include_router(aws_vpn_router)
app.include_router(location_router)
app.include_router(azure_vpn_router)
app.include_router(aws_lb_router)
app.include_router(vpn_orchestrator_router)



@app.get("/", tags=["Root"])
async def root():
    return {"message": "SERVER IS RUNNING"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "multicloud-vpn"}