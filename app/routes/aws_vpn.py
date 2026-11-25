from typing import List

from fastapi import APIRouter, HTTPException

from app.utils.stack import stack_up_safe, stack_exists, stack_outputs, stack_destroy, stack_list
from app.pulumi_programs.aws_vpn import (
    create_vgw_program,
    create_cgw_program,
    create_vpn_connection_program,
    create_route_propagation_program,
)
from app.models.common import StackResponse
from app.models.aws_vpn import (
    VgwRequest,
    CgwRequest,
    VpnConnectionRequest,
    RoutePropagationRequest,
)

router = APIRouter(prefix="/aws/vpn", tags=["aws-vpn"])

_RESOURCE_TYPES = {
    "vgw": "aws_vgw",
    "customer-gateway": "aws_cgw",
    "vpn-connection": "aws_vpn_connection",
    "route-propagation": "aws_route_propagation",
}

def _resolve_vpc_id(vpc_stack: str) -> str:
    try:
        return stack_outputs(vpc_stack)["vpc_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{vpc_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{vpc_stack}': {exc}")

def _resolve_vpc_cidr(vpc_stack: str) -> str:
    try:
        return stack_outputs(vpc_stack)["vpc_cidr"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_cidr not found in stack '{vpc_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{vpc_stack}': {exc}")


def _resolve_vgw_id(vgw_stack: str) -> str:
    try:
        return stack_outputs(vgw_stack)["vgw_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{vgw_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{vgw_stack}': {exc}")

def _resolve_cgw_id(cgw_stack: str) -> str:
    try:
        return stack_outputs(cgw_stack)["cgw_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{cgw_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{cgw_stack}': {exc}")

def _resolve_route_table_id(route_table_stack: str) -> str:
    try:
        return stack_outputs(route_table_stack)["route_table_id"]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vpc_id not found in stack '{route_table_stack}'")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Could not resolve vpc stack '{route_table_stack}': {exc}")

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


# @router.get("/vgw", response_model=List[StackResponse])
def list_vgws():
    """List all Virtual Private Gateway stacks."""
    return _list_by_type(_RESOURCE_TYPES["vgw"])


# @router.post("/vgw", response_model=StackResponse, status_code=201)
def create_vgw(req: VgwRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    vpc_id = _resolve_vpc_id(req.vpc_stack)
    try:
        program = create_vgw_program(vpc_id, req.amazon_side_asn, req.tags)
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


# @router.get("/vgw/{stack}", response_model=StackResponse)
def get_vgw(stack: str):
    """Fetch a single vgw stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["vgw"])


# @router.get("/customer-gateway", response_model=List[StackResponse])
def list_customer_gateways():
    """List all Customer Gateway stacks."""
    return _list_by_type(_RESOURCE_TYPES["customer-gateway"])


# @router.post("/customer-gateway", response_model=StackResponse, status_code=201)
def create_customer_gateway(req: CgwRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if not stack_exists(req.azure_vnet_gateway_stack):
        raise HTTPException(status_code=422, detail="Azure VNet Gateway stack not found — create it first")

    azure_outputs = stack_outputs(req.azure_vnet_gateway_stack)
    azure_public_ip = azure_outputs.get("public_ip")
    if not azure_public_ip:
        raise HTTPException(status_code=422, detail="Azure VNet Gateway has no public_ip output yet")

    try:
        program = create_cgw_program(azure_public_ip, req.bgp_asn, req.tags)
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

# @router.get("/customer-gateway/{stack}", response_model=StackResponse)
def get_customer_gateways(stack: str):
    """Fetch a single GatewaySubnet stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["customer-gateway"])


# @router.get("/vpn-connection", response_model=List[StackResponse])
def list_vpn_connections():
    """List all AWS-side VPN Connection stacks."""
    return _list_by_type(_RESOURCE_TYPES["vpn-connection"])


# @router.post("/vpn-connection", response_model=StackResponse, status_code=201)
def create_vpn_connection(req: VpnConnectionRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    for dep in (req.vgw_stack, req.cgw_stack):
        if not stack_exists(dep):
            raise HTTPException(status_code=422, detail=f"Dependency stack not found: {dep}")

    vgw_id = _resolve_vgw_id(req.vgw_stack)
    cgw_id = _resolve_cgw_id(req.cgw_stack)

    try:
        program = create_vpn_connection_program(
            vgw_id, cgw_id, req.tunnel1_inside_cidr, req.tunnel2_inside_cidr, req.tags
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


# @router.get("/vpn-connection/{stack}", response_model=StackResponse)
def get_vpn_connection(stack: str):
    """Fetch a single GatewaySubnet stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["vpn-connection"])


# @router.get("/route-propagation", response_model=List[StackResponse])
def list_route_propagations():
    """List all route propagation stacks."""
    return _list_by_type(_RESOURCE_TYPES["route-propagation"])


# @router.post("/route-propagation", response_model=StackResponse, status_code=201)
def create_route_propagation(req: RoutePropagationRequest):
    if not stack_exists(req.vgw_stack):
        raise HTTPException(status_code=422, detail="VGW stack not found")
    vgw_id = _resolve_vgw_id(req.vgw_stack)
    route_table_id = _resolve_route_table_id(req.route_table_stack)

    try:
        program = create_route_propagation_program(vgw_id, route_table_id)
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


# @router.get("/route-propagation/{stack}", response_model=StackResponse)
def get_route_propagation(stack: str):
    """Fetch a single GatewaySubnet stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["route-propagation"])



# @router.get("/{stack}", response_model=StackResponse)
def get_stack(stack: str):
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")
    return StackResponse(status="ok", stack=stack, outputs=stack_outputs(stack))


# @router.delete("/{stack}")
def destroy_stack(stack: str):
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")
    stack_destroy(stack)
    return {"status": "destroyed", "stack": stack}