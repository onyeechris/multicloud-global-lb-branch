from typing import List

from fastapi import APIRouter, HTTPException

# from app.utils.stack import stack_up_safe, stack_exists, stack_outputs, stack_destroy, stack_list
from app.utils.stack import stack_up_safe, stack_exists, stack_outputs, stack_outputs_secure, stack_destroy, stack_list
from app.utils.location_state import get_location
from app.pulumi_programs.azure_vpn import (
    create_gateway_subnet_program,
    create_vnet_gateway_program,
    create_local_network_gateway_program,
    create_vpn_gateway_connection_program,
)
from app.models.common import StackResponse
from app.models.azure_vpn import (
    GatewaySubnetRequest,
    VnetGatewayRequest,
    VnetGatewayBgpConfigRequest,
    LocalNetworkGatewayRequest,
    VpnGatewayConnectionRequest,
)

router = APIRouter(prefix="/azure/vpn", tags=["azure-vpn"])

_RESOURCE_TYPES = {
    "gateway-subnet": "azure_gateway_subnet",
    "vnet-gateway": "azure_vnet_gateway",
    "local-network-gateway": "azure_local_network_gateway",
    "connection": "azure_vpn_connection",
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


# @router.get("/gateway-subnet", response_model=List[StackResponse])
def list_gateway_subnets():
    """List all GatewaySubnet stacks."""
    return _list_by_type(_RESOURCE_TYPES["gateway-subnet"])


# @router.get("/gateway-subnet/{stack}", response_model=StackResponse)
def get_gateway_subnet(stack: str):
    """Fetch a single GatewaySubnet stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["gateway-subnet"])


# @router.post("/gateway-subnet", response_model=StackResponse, status_code=201)
def create_gateway_subnet(req: GatewaySubnetRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if not stack_exists(req.vnet_stack):
        raise HTTPException(status_code=422, detail="VNet stack not found")

    vnet_outputs = stack_outputs(req.vnet_stack)
    resource_group = vnet_outputs.get("resource_group_name")
    vnet_name = vnet_outputs.get("vnet_name")
    location = vnet_outputs.get("vnet_location")
    if not all([resource_group, vnet_name, location]):
        raise HTTPException(
            status_code=422,
            detail=f"VNet stack {req.vnet_stack!r} is missing resource_group_name/vnet_name/vnet_location outputs",
        )

    try:
        program = create_gateway_subnet_program(resource_group, vnet_name, req.address_prefix)
        outputs = stack_up_safe(
            req.stack, program, plugin="azure", config={"location": location}
        )
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


# @router.get("/vnet-gateway", response_model=List[StackResponse])
def list_vnet_gateways():
    """List all VNet Gateway stacks."""
    return _list_by_type(_RESOURCE_TYPES["vnet-gateway"])


# @router.get("/vnet-gateway/{stack}", response_model=StackResponse)
def get_vnet_gateway(stack: str):
    """Fetch a single VNet Gateway stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["vnet-gateway"])


# @router.post("/vnet-gateway", response_model=StackResponse, status_code=201)
def create_vnet_gateway(req: VnetGatewayRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if not stack_exists(req.gateway_subnet_stack):
        raise HTTPException(status_code=422, detail="GatewaySubnet stack not found — create it first")

    subnet_id = stack_outputs(req.gateway_subnet_stack)["subnet_id"]
    location = get_location()
    try:
        program = create_vnet_gateway_program(
            req.resource_group, location, subnet_id, req.sku, req.asn
        )
        outputs = stack_up_safe(
            req.stack, program, plugin="azure", config={"location": location}
        )
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


# @router.patch("/vnet-gateway/bgp-config", response_model=StackResponse)
def update_vnet_gateway_bgp_config(req: VnetGatewayBgpConfigRequest):
    """Re-runs the VNet Gateway stack with Custom Azure APIPA BGP IP addresses,
    derived from the AWS VPN connection's tunnel inside CIDRs (the CGW/Azure side
    of each /30). Must run after the AWS VPN connection exists."""
    for dep in (req.vnet_gateway_stack, req.aws_vpn_connection_stack):
        if not stack_exists(dep):
            raise HTTPException(status_code=422, detail=f"Dependency stack not found: {dep}")

    current = stack_outputs(req.vnet_gateway_stack)
    aws_outputs = stack_outputs(req.aws_vpn_connection_stack)

    custom_bgp_addresses = [
        aws_outputs["tunnel1_cgw_inside_ip"],
        aws_outputs["tunnel2_cgw_inside_ip"],
    ]

    try:
        program = create_vnet_gateway_program(
            resource_group=current.get("resource_group") or _require_rg(req.vnet_gateway_stack),
            location=current.get("location") or _require_location(req.vnet_gateway_stack),
            gateway_subnet_id=current["gateway_subnet_id"],
            sku=current.get("sku", "VpnGw1AZ"),
            asn=current["asn"],
            custom_bgp_addresses=custom_bgp_addresses,
            vnet_gateway_id=current["vnet_gateway_id"]
        )

        outputs = stack_up_safe(req.vnet_gateway_stack, program, plugin="azure")
        return StackResponse(status="updated", stack=req.vnet_gateway_stack, outputs=outputs)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _require_rg(stack: str) -> str:
    raise HTTPException(status_code=500, detail=f"resource_group missing from {stack} outputs — export it in the pulumi program")


def _require_location(stack: str) -> str:
    raise HTTPException(status_code=500, detail=f"location missing from {stack} outputs — export it in the pulumi program")


# @router.get("/local-network-gateway", response_model=List[StackResponse])
def list_local_network_gateways():
    """List all Local Network Gateway stacks."""
    return _list_by_type(_RESOURCE_TYPES["local-network-gateway"])


# @router.get("/local-network-gateway/{stack}", response_model=StackResponse)
def get_local_network_gateway(stack: str):
    """Fetch a single Local Network Gateway stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["local-network-gateway"])


# @router.post("/local-network-gateway", response_model=StackResponse, status_code=201)
def create_local_network_gateway(req: LocalNetworkGatewayRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    if not stack_exists(req.aws_vpn_connection_stack):
        raise HTTPException(status_code=422, detail="AWS VPN connection stack not found")

    aws_outputs = stack_outputs(req.aws_vpn_connection_stack)
    n = req.tunnel_number
    outside_ip = aws_outputs.get(f"tunnel{n}_outside_ip")
    vgw_inside_ip = aws_outputs.get(f"tunnel{n}_vgw_inside_ip")
    if not outside_ip or not vgw_inside_ip:
        raise HTTPException(status_code=422, detail=f"tunnel{n} outputs not found on AWS VPN connection stack")

    location = get_location()

    try:
        program = create_local_network_gateway_program(
            req.resource_group, location, outside_ip, vgw_inside_ip, req.address_spaces, req.asn
        )
        outputs = stack_up_safe(
            req.stack, program, plugin="azure", config={"location": location}
        )
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


# @router.get("/connection", response_model=List[StackResponse])
def list_vpn_connections():
    """List all Azure-side VPN Connection stacks."""
    return _list_by_type(_RESOURCE_TYPES["connection"])


# @router.get("/connection/{stack}", response_model=StackResponse)
def get_vpn_connection(stack: str):
    """Fetch a single Azure-side VPN Connection stack by name."""
    return _get_by_type(stack, _RESOURCE_TYPES["connection"])


# @router.post("/connection", response_model=StackResponse, status_code=201)
def create_vpn_connection(req: VpnGatewayConnectionRequest):
    if stack_exists(req.stack):
        raise HTTPException(status_code=409, detail="Stack already exists")
    for dep in (req.vnet_gateway_stack, req.local_network_gateway_stack, req.aws_vpn_connection_stack):
        if not stack_exists(dep):
            raise HTTPException(status_code=422, detail=f"Dependency stack not found: {dep}")

    vnet_gateway_id = stack_outputs(req.vnet_gateway_stack)["vnet_gateway_id"]
    lng_id = stack_outputs(req.local_network_gateway_stack)["local_network_gateway_id"]
    psk = stack_outputs_secure(req.aws_vpn_connection_stack)[f"tunnel{req.tunnel_number}_psk"]

    try:
        location = get_location()
        program = create_vpn_gateway_connection_program(
            req.resource_group, location, vnet_gateway_id, lng_id, psk
        )
        outputs = stack_up_safe(
            req.stack, program, plugin="azure", config={"location": location}
        )
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



# @router.delete("/{stack}")
def destroy_stack(stack: str):
    if not stack_exists(stack):
        raise HTTPException(status_code=404, detail="Stack not found")
    stack_destroy(stack)
    return {"status": "destroyed", "stack": stack}