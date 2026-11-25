from fastapi import APIRouter, HTTPException, Depends
from typing import Annotated


from app.utils.location_state import AzureLocation, get_location, set_location
from app.utils.auth import get_current_user


user_dependency = Annotated[dict, Depends(get_current_user)]

router = APIRouter(prefix="/location", tags=["Location"])


@router.post("/{location}", status_code=200)
async def select_location(user: user_dependency, location: AzureLocation):
    """Pre-select the active Azure location used by all Azure-side stack operations."""
    set_location(location)
    return {"active_location": location.value}


@router.get("")
async def current_location(user: user_dependency):
    """Return the currently active Azure location."""
    loc = get_location()
    if loc is None:
        raise HTTPException(status_code=404, detail="No location selected yet. Call POST /location/{location} first.")
    return {"active_location": loc}