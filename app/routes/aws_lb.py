from typing import List

from fastapi import APIRouter, HTTPException, Depends
from typing import Annotated


from app.utils.stack import stack_up_safe, stack_exists, stack_outputs, stack_destroy, stack_list
from app.pulumi_programs.aws_lb import (
    create_target_group_program,
    create_nlb_program,
    create_listener_program,
)
from app.models.common import StackResponse
from app.models.aws_lb import (
    TargetGroupRequest,
    TargetGroupPatchRequest,
    NlbRequest,
    ListenerRequest,
)
from app.utils.auth import get_current_user


user_dependency = Annotated[dict, Depends(get_current_user)]


router = APIRouter(prefix="/aws/lb", tags=["aws-lb"])

_RESOURCE_TYPES = {
    "target-group": "aws_target_group",
    "network-load-balancer": "aws_nlb",
    "listener": "aws_listener",
}


def _list_by_type(resource_type: str) -> List[StackResponse]:
    try:
        all_stacks = stack_list()
        matched = [s for s in all_stacks if s["outputs"].get("resource_type") == resource_type]
        return [StackResponse(status="ok", stack=s["name"], outputs=s["outputs"]) for s in matched]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

def _get_by_type(stack: str, resource_type: str) -> StackResponse:
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")
    outputs = stack_outputs(stack)
    if outputs.get("resource_type") != resource_type:
        raise HTTPException(
            status_code=404,
            detail=f"Stack {stack!r} exists but is not a {resource_type} stack",
        )
    return StackResponse(status="ok", stack=stack, outputs=outputs)


def _resolve_vpc_id(vpc_stack: str) -> str:
    try:
        return stack_outputs(vpc_stack)["vpc_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{vpc_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{vpc_stack}': {exc}")


def _resolve_sg_id(security_group_stack: str) -> str:
    try:
        return stack_outputs(security_group_stack)["security_group_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{sg_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{sg_stack}': {exc}")


@router.get("/target-group", response_model=List[StackResponse])
def list_target_groups(user: user_dependency):
    """List all target group stacks."""
    return _list_by_type(_RESOURCE_TYPES["target-group"])


@router.post("/target-group", response_model=StackResponse, status_code=201)
def create_target_group(user: user_dependency, req: TargetGroupRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")

    targets = [t.model_dump() for t in req.targets]
    vpc_id = _resolve_vpc_id(req.vpc_stack)
    try:
        program = create_target_group_program(
            vpc_id,
            req.port,
            req.protocol,
            req.health_check_port,
            req.health_check_protocol,
            req.health_check_interval,
            req.health_check_timeout,
            req.healthy_threshold,
            req.unhealthy_threshold,
            targets,
            req.tags,
        )
        outputs = stack_up_safe(req.stack, program, plugin="aws")
        return StackResponse(status="created", stack=req.stack, outputs=outputs)
    except Exception as exc:
        if stack_exists(req.stack):
            try:
                stack_destroy(req.stack)
            except Exception as exc2:
                raise HTTPException(
                    status_code=500,
                    detail=f"Original error: {exc}. Also failed to cleanup: {exc2}. Manually check stack {req.stack}"
                )
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/target-group/{stack}", response_model=StackResponse)
def get_tg(user: user_dependency, stack: str):
    """Fetch a single "target-group" stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["target-group"])


@router.patch("/target-group/{stack}", response_model=StackResponse)
def update_target_group_targets(user: user_dependency, stack: str, req: TargetGroupPatchRequest):
    """Replaces the full target list on an existing target group — Pulumi
    diffs old vs new TargetGroupAttachments and adds/removes accordingly."""
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")

    current = stack_outputs(stack)
    targets = [t.model_dump() for t in req.targets]

    program = create_target_group_program(
        vpc_id=current["vpc_id"],
        port=current["port"],
        protocol=current.get("protocol", "TCP"),
        health_check_port=current.get("health_check_port", "traffic-port"),
        health_check_protocol=current.get("health_check_protocol", "TCP"),
        health_check_interval=current.get("health_check_interval", 30),
        health_check_timeout=current.get("health_check_timeout", 10),
        healthy_threshold=current.get("healthy_threshold", 3),
        unhealthy_threshold=current.get("unhealthy_threshold", 3),
        targets=targets,
        tags=current.get("tags", {}),
    )
    outputs = stack_up_safe(stack, program, plugin="aws")
    return StackResponse(status="updated", stack=stack, outputs=outputs)



@router.get("/network-load-balancer", response_model=List[StackResponse])
def list_nlbs(user: user_dependency):
    """List all Network Load Balancer stacks."""
    return _list_by_type(_RESOURCE_TYPES["network-load-balancer"])


@router.post("/network-load-balancer", response_model=StackResponse, status_code=201)
def create_nlb(user: user_dependency, req: NlbRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if len(req.subnet_ids) < 1:
        raise HTTPException(status_code=422, detail="At least 2 subnets across different AZs are recommended")

    try:
        security_group_id = _resolve_sg_id(req.security_group_stack)
        program = create_nlb_program(
            req.name, req.subnet_ids, req.scheme, security_group_id, req.tags
        )
        outputs = stack_up_safe(req.stack, program, plugin="aws")
        return StackResponse(status="created", stack=req.stack, outputs=outputs)
    except Exception as exc:
        if stack_exists(req.stack):
            try:
                stack_destroy(req.stack)
            except Exception as exc2:
                raise HTTPException(
                    status_code=500,
                    detail=f"Original error: {exc}. Also failed to cleanup: {exc2}. Manually check stack {req.stack}"
                )
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/network-load-balancer/{stack}", response_model=StackResponse)
def get_nlb(user: user_dependency, stack: str):
    """Fetch a single "network-load-balancer" stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["network-load-balancer"])


@router.get("/listener", response_model=List[StackResponse])
def list_listeners(user: user_dependency):
    """List all listener stacks."""
    return _list_by_type(_RESOURCE_TYPES["listener"])


@router.post("/listener", response_model=StackResponse, status_code=201)
def create_listener(user: user_dependency, req: ListenerRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if not stack_exists(req.nlb_stack):
        raise HTTPException(status_code=422, detail="NLB stack not found")

    nlb_arn = stack_outputs(req.nlb_stack)["nlb_arn"]

    target_group_weights = []
    for tgw in req.target_groups:
        if not stack_exists(tgw.target_group_stack):
            raise HTTPException(status_code=422, detail=f"Target group stack not found: {tgw.target_group_stack}")
        arn = stack_outputs(tgw.target_group_stack)["target_group_arn"]
        target_group_weights.append({"arn": arn, "weight": tgw.weight})

    try:
        program = create_listener_program(
            nlb_arn, req.port, req.protocol, target_group_weights, req.certificate_arn
        )
        outputs = stack_up_safe(req.stack, program, plugin="aws")
        return StackResponse(status="created", stack=req.stack, outputs=outputs)
    except Exception as exc:
        if stack_exists(req.stack):
            try:
                stack_destroy(req.stack)
            except Exception as exc2:
                raise HTTPException(
                    status_code=500,
                    detail=f"Original error: {exc}. Also failed to cleanup: {exc2}. Manually check stack {req.stack}"
                )
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/listener/{stack}", response_model=StackResponse)
def get_listener(user: user_dependency, stack: str):
    """Fetch a single "listener" stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["listener"])


# @router.get("/{stack}", response_model=StackResponse)
# def get_stack(stack: str):
#     if not stack_exists(stack):
#         raise HTTPException(status_code=404, detail="Stack not found")
#     return StackResponse(status="ok", stack=stack, outputs=stack_outputs(stack))


@router.delete("/{stack}")
def destroy_stack(user: user_dependency, stack: str):
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")
    stack_destroy(stack)
    return {"status": "destroyed", "stack": stack}