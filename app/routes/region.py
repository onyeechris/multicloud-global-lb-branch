from fastapi import APIRouter, HTTPException, Depends
from typing import Annotated

from app.utils.region_state import AwsRegion, get_region, set_region
from app.utils.auth import get_current_user


user_dependency = Annotated[dict, Depends(get_current_user)]

router = APIRouter(prefix="/region", tags=["Region"])


@router.post("/{region}", status_code=200)
async def select_region(user: user_dependency, region: AwsRegion):
    """Pre-select the active AWS region used by all AWS-side stack operations."""
    set_region(region)
    return {"active_region": region.value}


@router.get("")
async def current_region(user: user_dependency):
    """Return the currently active AWS region."""
    r = get_region()
    if r is None:
        raise HTTPException(status_code=404, detail="No region selected yet. Call POST /region/{region} first.")
    return {"active_region": r}