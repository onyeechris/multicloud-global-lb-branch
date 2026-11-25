import ipaddress
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from typing import Annotated

from app.utils.stack import stack_exists, stack_destroy, stack_outputs, stack_list, is_locked
from app.models.vpn_orchestrator import VpnOrchestrationRequest
from app.utils.auth import get_current_user

user_dependency = Annotated[dict, Depends(get_current_user)]

from app.models.aws_vpn import (
    VgwRequest,
    CgwRequest,
    VpnConnectionRequest as AwsVpnConnectionRequest,
    RoutePropagationRequest,
)
from app.models.azure_vpn import (
    GatewaySubnetRequest,
    VnetGatewayRequest,
    VnetGatewayBgpConfigRequest,
    LocalNetworkGatewayRequest,
    VpnGatewayConnectionRequest,
)

from app.routes.aws_vpn import (
    create_vgw,
    create_customer_gateway,
    create_vpn_connection as create_aws_vpn_connection,
    create_route_propagation,
    _resolve_vpc_cidr,
)
from app.routes.azure_vpn import (
    create_gateway_subnet,
    create_vnet_gateway,
    update_vnet_gateway_bgp_config,
    create_local_network_gateway,
    create_vpn_connection as create_azure_vpn_connection,
)

router = APIRouter(prefix="/vpn", tags=["vpn-orchestrator"])

_VNET_ADDRESS_SPACE_KEY = "address_spaces"


def _stacks(name: str) -> dict[str, str]:
    return {
        "gateway_subnet": f"azure-gw-subnet-{name}",
        "vnet_gateway": f"azure-vnet-gw-{name}",
        "vgw": f"aws-vgw-{name}",
        "cgw": f"aws-cgw-{name}",
        "vpn_connection": f"aws-vpn-conn-{name}",
        "lng_t1": f"azure-lng-t1-{name}",
        "lng_t2": f"azure-lng-t2-{name}",
        "conn_t1": f"azure-conn-t1-{name}",
        "conn_t2": f"azure-conn-t2-{name}",
    }


def _route_prop_prefix(name: str) -> str:
    return f"aws-route-prop-{name}-"


_TEARDOWN_ORDER = [
    "conn_t2", "conn_t1", "lng_t2", "lng_t1",
    "vpn_connection", "cgw", "vgw", "vnet_gateway", "gateway_subnet",
]

_STACK_PREFIXES = {
    "gateway_subnet": "azure-gw-subnet-",
    "vnet_gateway": "azure-vnet-gw-",
    "vgw": "aws-vgw-",
    "cgw": "aws-cgw-",
    "vpn_connection": "aws-vpn-conn-",
    "lng_t1": "azure-lng-t1-",
    "lng_t2": "azure-lng-t2-",
    "conn_t1": "azure-conn-t1-",
    "conn_t2": "azure-conn-t2-",
}


def _rollback(created: list[str]) -> None:
    for stack_name in reversed(created):
        try:
            if stack_exists(stack_name):
                stack_destroy(stack_name)
        except Exception as exc:
            print(f"rollback: failed to destroy {stack_name}: {exc}")


# --- pre-flight validation — everything here runs synchronously, before the
# job is marked running and before any Pulumi call happens ---

def _validate_vnet_stack(vnet_stack: str) -> dict:
    if not stack_exists(vnet_stack):
        raise HTTPException(status_code=422, detail=f"VNet stack not found: {vnet_stack}")
    outputs = stack_outputs(vnet_stack)
    required = ("resource_group_name", "vnet_name", "vnet_location", _VNET_ADDRESS_SPACE_KEY)
    missing = [k for k in required if not outputs.get(k)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"VNet stack '{vnet_stack}' is missing required output(s): {missing}",
        )
    return outputs


def _validate_vpc_stack(vpc_stack: str) -> dict:
    if not stack_exists(vpc_stack):
        raise HTTPException(status_code=422, detail=f"VPC stack not found: {vpc_stack}")
    outputs = stack_outputs(vpc_stack)
    required = ("vpc_id", "vpc_cidr")
    missing = [k for k in required if not outputs.get(k)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"VPC stack '{vpc_stack}' is missing required output(s): {missing}",
        )
    return outputs


def _validate_resource_group_matches_vnet(resource_group: str, vnet_stack: str, vnet_outputs: dict) -> None:
    vnet_rg = vnet_outputs.get("resource_group_name")
    if resource_group != vnet_rg:
        raise HTTPException(
            status_code=422,
            detail=(
                f"resource_group '{resource_group}' does not match VNet stack "
                f"'{vnet_stack}''s own resource_group_name '{vnet_rg}'. The gateway "
                f"subnet always lives in the VNet's resource group, and every Azure "
                f"resource this orchestrator creates is meant to land in that same "
                f"group — a mismatch here is almost always a copy-paste error."
            ),
        )


def _validate_gateway_subnet_prefix(vnet_outputs: dict, prefix: str) -> None:
    vnet_cidrs = vnet_outputs.get(_VNET_ADDRESS_SPACE_KEY)
    if not vnet_cidrs:
        raise HTTPException(status_code=422, detail=f"VNet stack has no '{_VNET_ADDRESS_SPACE_KEY}' output")
    if isinstance(vnet_cidrs, str):
        vnet_cidrs = [vnet_cidrs]

    try:
        subnet = ipaddress.ip_network(prefix, strict=False)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"gateway_subnet_address_prefix '{prefix}' is not a valid CIDR: {exc}",
        )

    for cidr in vnet_cidrs:
        try:
            parent = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if subnet.version == parent.version and subnet.subnet_of(parent):
            return

    raise HTTPException(
        status_code=422,
        detail=f"gateway_subnet_address_prefix '{prefix}' is not within VNet address space {vnet_cidrs}",
    )


def _validate_route_table_stacks(route_table_stacks: list[str]) -> None:
    # Fail fast here — without this, a missing route table only surfaces after
    # ~9 other stacks (and 30-40+ minutes) have already been built, triggering
    # a full rollback for something checkable in milliseconds up front.
    for rt_stack in route_table_stacks:
        if not stack_exists(rt_stack):
            raise HTTPException(status_code=422, detail=f"Route table stack not found: {rt_stack}")
        outputs = stack_outputs(rt_stack)
        if not outputs.get("route_table_id"):
            raise HTTPException(
                status_code=422,
                detail=f"Route table stack '{rt_stack}' has no route_table_id output",
            )


# --- listing / detail (reads real Pulumi state) ---

@router.get("/orchestrate")
def list_orchestrations(user: user_dependency):
    all_stacks = stack_list()
    by_name: dict[str, dict] = {}

    for entry in all_stacks:
        for step_key, prefix in _STACK_PREFIXES.items():
            if entry["name"].startswith(prefix):
                name = entry["name"][len(prefix):]
                by_name.setdefault(name, {"stacks": {}, "route_prop_stacks": []})
                by_name[name]["stacks"][step_key] = entry["name"]
                break

    for entry in all_stacks:
        for name in by_name:
            if entry["name"].startswith(_route_prop_prefix(name)):
                by_name[name]["route_prop_stacks"].append(entry["name"])

    return {"orchestrations": [{"name": n, **d} for n, d in by_name.items()]}


@router.get("/orchestrate/{name}")
def get_orchestration(user: user_dependency, name: str):
    s = _stacks(name)
    found = {
        step_key: {"stack": stack_name, "outputs": stack_outputs(stack_name)}
        for step_key, stack_name in s.items() if stack_exists(stack_name)
    }

    prefix = _route_prop_prefix(name)
    route_props = [
        {"stack": entry["name"], "outputs": entry["outputs"]}
        for entry in stack_list() if entry["name"].startswith(prefix)
    ]

    if not found and not route_props:
        raise HTTPException(status_code=404, detail=f"No stacks found for name={name!r}")

    return {"name": name, "stacks": found, "route_prop": route_props}


# --- in-memory job tracking (fast layer, reset on process restart by design) ---

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_job(name: str, **fields) -> None:
    with _JOBS_LOCK:
        _JOBS.setdefault(name, {}).update(fields)


def _get_job(name: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        return dict(job) if job is not None else None


def _try_start_job(name: str, action: str) -> bool:
    with _JOBS_LOCK:
        existing = _JOBS.get(name)
        if existing and existing.get("status") == "running":
            return False
        _JOBS[name] = {
            "action": action, "status": "running", "current_step": None,
            "error": None, "started_at": _now(), "finished_at": None,
        }
        return True


def _locked_stacks(name: str) -> list[str]:
    return [stack_name for stack_name in _stacks(name).values() if is_locked(stack_name)]


# --- background workers (no HTTP context — record outcome in _JOBS, never raise) ---

def _run_orchestration(req: VpnOrchestrationRequest) -> None:
    name = req.name
    s = _stacks(name)
    created: list[str] = []
    outputs: dict[str, object] = {}

    try:
        address_spaces = [_resolve_vpc_cidr(req.vpc_stack)]

        r = create_gateway_subnet(GatewaySubnetRequest(
            stack=s["gateway_subnet"], resource_group=req.resource_group,
            vnet_stack=req.vnet_stack, address_prefix=req.gateway_subnet_address_prefix,
        ))
        created.append(s["gateway_subnet"]); outputs["gateway_subnet"] = r.outputs
        _set_job(name, current_step="gateway_subnet")

        r = create_vnet_gateway(VnetGatewayRequest(
            stack=s["vnet_gateway"], resource_group=req.resource_group,
            gateway_subnet_stack=s["gateway_subnet"], sku=req.vnet_gateway_sku, asn=req.azure_asn,
        ))
        created.append(s["vnet_gateway"]); outputs["vnet_gateway"] = r.outputs
        _set_job(name, current_step="vnet_gateway")

        r = create_vgw(VgwRequest(
            stack=s["vgw"], vpc_stack=req.vpc_stack,
            amazon_side_asn=req.amazon_side_asn, tags=req.tags,
        ))
        created.append(s["vgw"]); outputs["vgw"] = r.outputs
        _set_job(name, current_step="vgw")

        r = create_customer_gateway(CgwRequest(
            stack=s["cgw"], azure_vnet_gateway_stack=s["vnet_gateway"],
            bgp_asn=req.azure_asn, tags=req.tags,
        ))
        created.append(s["cgw"]); outputs["cgw"] = r.outputs
        _set_job(name, current_step="cgw")

        r = create_aws_vpn_connection(AwsVpnConnectionRequest(
            stack=s["vpn_connection"], vgw_stack=s["vgw"], cgw_stack=s["cgw"],
            tunnel1_inside_cidr=req.tunnel1_inside_cidr, tunnel2_inside_cidr=req.tunnel2_inside_cidr,
            tags=req.tags,
        ))
        created.append(s["vpn_connection"]); outputs["vpn_connection"] = r.outputs
        _set_job(name, current_step="vpn_connection")

        r = update_vnet_gateway_bgp_config(VnetGatewayBgpConfigRequest(
            vnet_gateway_stack=s["vnet_gateway"], aws_vpn_connection_stack=s["vpn_connection"],
        ))
        outputs["vnet_gateway"] = r.outputs
        _set_job(name, current_step="vnet_gateway_bgp_config")

        r = create_local_network_gateway(LocalNetworkGatewayRequest(
            stack=s["lng_t1"], resource_group=req.resource_group,
            aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=1,
            address_spaces=address_spaces, asn=req.amazon_side_asn,
        ))
        created.append(s["lng_t1"]); outputs["lng_t1"] = r.outputs
        _set_job(name, current_step="lng_t1")

        r = create_local_network_gateway(LocalNetworkGatewayRequest(
            stack=s["lng_t2"], resource_group=req.resource_group,
            aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=2,
            address_spaces=address_spaces, asn=req.amazon_side_asn,
        ))
        created.append(s["lng_t2"]); outputs["lng_t2"] = r.outputs
        _set_job(name, current_step="lng_t2")

        r = create_azure_vpn_connection(VpnGatewayConnectionRequest(
            stack=s["conn_t1"], resource_group=req.resource_group,
            vnet_gateway_stack=s["vnet_gateway"], local_network_gateway_stack=s["lng_t1"],
            aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=1,
        ))
        created.append(s["conn_t1"]); outputs["conn_t1"] = r.outputs
        _set_job(name, current_step="conn_t1")

        r = create_azure_vpn_connection(VpnGatewayConnectionRequest(
            stack=s["conn_t2"], resource_group=req.resource_group,
            vnet_gateway_stack=s["vnet_gateway"], local_network_gateway_stack=s["lng_t2"],
            aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=2,
        ))
        created.append(s["conn_t2"]); outputs["conn_t2"] = r.outputs
        _set_job(name, current_step="conn_t2")

        outputs["route_prop"] = []
        for rt_stack in req.route_table_stacks:
            prop_stack = f"{_route_prop_prefix(name)}{rt_stack}"
            r = create_route_propagation(RoutePropagationRequest(
                stack=prop_stack, vgw_stack=s["vgw"], route_table_stack=rt_stack,
            ))
            created.append(prop_stack)
            outputs["route_prop"].append({
                "stack": prop_stack, "route_table_stack": rt_stack, "outputs": r.outputs,
            })
            _set_job(name, current_step=f"route_prop:{rt_stack}")

    except HTTPException as exc:
        _rollback(created)
        error = f"Orchestration failed, rolled back {len(created)} stack(s): {exc.detail}"
        print(f"orchestrate[{name}]: {error}")
        _set_job(name, status="failed", error=error, finished_at=_now())
        return
    except Exception as exc:
        _rollback(created)
        error = f"Orchestration failed, rolled back {len(created)} stack(s): {exc}"
        print(f"orchestrate[{name}]: {error}")
        _set_job(name, status="failed", error=error, finished_at=_now())
        return

    _set_job(name, status="succeeded", current_step=None, outputs=outputs, finished_at=_now())


def _run_teardown(name: str) -> None:
    s = _stacks(name)
    destroyed: list[str] = []

    try:
        prefix = _route_prop_prefix(name)
        for entry in stack_list():
            if entry["name"].startswith(prefix):
                stack_destroy(entry["name"])
                destroyed.append(entry["name"])
                _set_job(name, current_step=entry["name"])

        for step_key in _TEARDOWN_ORDER:
            stack_name = s[step_key]
            if stack_exists(stack_name):
                stack_destroy(stack_name)
                destroyed.append(stack_name)
                _set_job(name, current_step=step_key)
    except Exception as exc:
        error = f"Teardown failed after destroying {len(destroyed)} stack(s): {exc}"
        print(f"teardown[{name}]: {error}")
        _set_job(name, status="failed", error=error, finished_at=_now(), destroyed=destroyed)
        return

    if not destroyed:
        _set_job(name, status="failed", error=f"No stacks found for name={name!r}", finished_at=_now())
        return

    _set_job(name, status="succeeded", current_step=None, destroyed=destroyed, finished_at=_now())


# --- endpoints: validate synchronously, then kick off in background ---

@router.post("/orchestrate", status_code=202)
def orchestrate_vpn(user: user_dependency, req: VpnOrchestrationRequest, background_tasks: BackgroundTasks):
    vnet_outputs = _validate_vnet_stack(req.vnet_stack)
    _validate_resource_group_matches_vnet(req.resource_group, req.vnet_stack, vnet_outputs)
    _validate_vpc_stack(req.vpc_stack)
    _validate_gateway_subnet_prefix(vnet_outputs, req.gateway_subnet_address_prefix)
    _validate_route_table_stacks(req.route_table_stacks)

    locked = _locked_stacks(req.name)
    if locked:
        raise HTTPException(
            status_code=409,
            detail=f"Pulumi lock(s) held on {locked} — a prior operation may still be in flight",
        )

    if not _try_start_job(req.name, "orchestrate"):
        raise HTTPException(status_code=409, detail=f"An operation is already running for name={req.name!r}")

    background_tasks.add_task(_run_orchestration, req)

    return {
        "status": "accepted",
        "name": req.name,
        "check_status_at": f"/vpn/orchestrate/{req.name}/status",
    }


@router.delete("/orchestrate/{name}", status_code=202)
def teardown_vpn(user: user_dependency, name: str, background_tasks: BackgroundTasks):
    locked = _locked_stacks(name)
    if locked:
        raise HTTPException(
            status_code=409,
            detail=f"Pulumi lock(s) held on {locked} — a prior operation may still be in flight",
        )

    if not _try_start_job(name, "teardown"):
        raise HTTPException(status_code=409, detail=f"An operation is already running for name={name!r}")

    background_tasks.add_task(_run_teardown, name)

    return {
        "status": "accepted",
        "name": name,
        "check_status_at": f"/vpn/orchestrate/{name}/status",
    }


@router.get("/orchestrate/{name}/status")
def get_orchestration_status(user: user_dependency, name: str):
    job = _get_job(name)
    locked = _locked_stacks(name)

    if job is None:
        if locked:
            return {
                "name": name,
                "status": "unknown_but_locked",
                "detail": (
                    "No in-memory record — likely a process restart — but a Pulumi "
                    "lock is still held, so an operation may still be running."
                ),
                "locked_stacks": locked,
            }
        raise HTTPException(
            status_code=404,
            detail=f"No in-memory job record for name={name!r}. "
                   f"Check GET /vpn/orchestrate/{name} for actual resource state.",
        )

    return {**job, "name": name, "locked_stacks": locked}






























# import threading
# from datetime import datetime, timezone
#
# from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
# from typing import Annotated
#
# from app.utils.stack import stack_exists, stack_destroy, stack_outputs, stack_list, is_locked
# from app.models.vpn_orchestrator import VpnOrchestrationRequest
# from app.utils.auth import get_current_user
#
# user_dependency = Annotated[dict, Depends(get_current_user)]
#
# from app.models.aws_vpn import (
#     VgwRequest,
#     CgwRequest,
#     VpnConnectionRequest as AwsVpnConnectionRequest,
#     RoutePropagationRequest,
# )
# from app.models.azure_vpn import (
#     GatewaySubnetRequest,
#     VnetGatewayRequest,
#     VnetGatewayBgpConfigRequest,
#     LocalNetworkGatewayRequest,
#     VpnGatewayConnectionRequest,
# )
#
# from app.routes.aws_vpn import (
#     create_vgw,
#     create_customer_gateway,
#     create_vpn_connection as create_aws_vpn_connection,
#     create_route_propagation,
# )
# from app.routes.azure_vpn import (
#     create_gateway_subnet,
#     create_vnet_gateway,
#     update_vnet_gateway_bgp_config,
#     create_local_network_gateway,
#     create_vpn_connection as create_azure_vpn_connection,
# )
#
# router = APIRouter(prefix="/vpn", tags=["vpn-orchestrator"])
#
#
# def _stacks(name: str) -> dict[str, str]:
#     return {
#         "gateway_subnet": f"azure-gw-subnet-{name}",
#         "vnet_gateway": f"azure-vnet-gw-{name}",
#         "vgw": f"aws-vgw-{name}",
#         "cgw": f"aws-cgw-{name}",
#         "vpn_connection": f"aws-vpn-conn-{name}",
#         "lng_t1": f"azure-lng-t1-{name}",
#         "lng_t2": f"azure-lng-t2-{name}",
#         "conn_t1": f"azure-conn-t1-{name}",
#         "conn_t2": f"azure-conn-t2-{name}",
#     }
#
#
# def _route_prop_prefix(name: str) -> str:
#     return f"aws-route-prop-{name}-"
#
#
# _TEARDOWN_ORDER = [
#     "conn_t2", "conn_t1", "lng_t2", "lng_t1",
#     "vpn_connection", "cgw", "vgw", "vnet_gateway", "gateway_subnet",
# ]
#
# _STACK_PREFIXES = {
#     "gateway_subnet": "azure-gw-subnet-",
#     "vnet_gateway": "azure-vnet-gw-",
#     "vgw": "aws-vgw-",
#     "cgw": "aws-cgw-",
#     "vpn_connection": "aws-vpn-conn-",
#     "lng_t1": "azure-lng-t1-",
#     "lng_t2": "azure-lng-t2-",
#     "conn_t1": "azure-conn-t1-",
#     "conn_t2": "azure-conn-t2-",
# }
#
#
# def _rollback(created: list[str]) -> None:
#     for stack_name in reversed(created):
#         try:
#             if stack_exists(stack_name):
#                 stack_destroy(stack_name)
#         except Exception as exc:
#             print(f"rollback: failed to destroy {stack_name}: {exc}")
#
#
# # --- listing / detail (reads real Pulumi state) ---
#
# @router.get("/orchestrate")
# def list_orchestrations(user: user_dependency):
#     all_stacks = stack_list()
#     by_name: dict[str, dict] = {}
#
#     for entry in all_stacks:
#         for step_key, prefix in _STACK_PREFIXES.items():
#             if entry["name"].startswith(prefix):
#                 name = entry["name"][len(prefix):]
#                 by_name.setdefault(name, {"stacks": {}, "route_prop_stacks": []})
#                 by_name[name]["stacks"][step_key] = entry["name"]
#                 break
#
#     for entry in all_stacks:
#         for name in by_name:
#             if entry["name"].startswith(_route_prop_prefix(name)):
#                 by_name[name]["route_prop_stacks"].append(entry["name"])
#
#     return {"orchestrations": [{"name": n, **d} for n, d in by_name.items()]}
#
#
# @router.get("/orchestrate/{name}")
# def get_orchestration(user: user_dependency, name: str):
#     s = _stacks(name)
#     found = {
#         step_key: {"stack": stack_name, "outputs": stack_outputs(stack_name)}
#         for step_key, stack_name in s.items() if stack_exists(stack_name)
#     }
#
#     prefix = _route_prop_prefix(name)
#     route_props = [
#         {"stack": entry["name"], "outputs": entry["outputs"]}
#         for entry in stack_list() if entry["name"].startswith(prefix)
#     ]
#
#     if not found and not route_props:
#         raise HTTPException(status_code=404, detail=f"No stacks found for name={name!r}")
#
#     return {"name": name, "stacks": found, "route_prop": route_props}
#
#
# # --- in-memory job tracking (fast layer, reset on process restart by design) ---
#
# _JOBS: dict[str, dict] = {}
# _JOBS_LOCK = threading.Lock()
#
#
# def _now() -> str:
#     return datetime.now(timezone.utc).isoformat()
#
#
# def _set_job(name: str, **fields) -> None:
#     with _JOBS_LOCK:
#         _JOBS.setdefault(name, {}).update(fields)
#
#
# def _get_job(name: str) -> dict | None:
#     with _JOBS_LOCK:
#         job = _JOBS.get(name)
#         return dict(job) if job is not None else None
#
#
# def _locked_stacks(name: str) -> list[str]:
#     return [stack_name for stack_name in _stacks(name).values() if is_locked(stack_name)]
#
#
# # --- background workers (no HTTP context — record outcome in _JOBS, never raise) ---
#
# def _run_orchestration(req: VpnOrchestrationRequest) -> None:
#     name = req.name
#     s = _stacks(name)
#     created: list[str] = []
#     outputs: dict[str, object] = {}
#
#     try:
#         r = create_gateway_subnet(GatewaySubnetRequest(
#             stack=s["gateway_subnet"], resource_group=req.resource_group,
#             vnet_stack=req.vnet_stack, address_prefix=req.address_prefix,
#         ))
#         created.append(s["gateway_subnet"]); outputs["gateway_subnet"] = r.outputs
#         _set_job(name, current_step="gateway_subnet")
#
#         r = create_vnet_gateway(VnetGatewayRequest(
#             stack=s["vnet_gateway"], resource_group=req.resource_group,
#             gateway_subnet_stack=s["gateway_subnet"], sku=req.vnet_gateway_sku, asn=req.azure_asn,
#         ))
#         created.append(s["vnet_gateway"]); outputs["vnet_gateway"] = r.outputs
#         _set_job(name, current_step="vnet_gateway")
#
#         r = create_vgw(VgwRequest(
#             stack=s["vgw"], vpc_stack=req.vpc_stack,
#             amazon_side_asn=req.amazon_side_asn, tags=req.tags,
#         ))
#         created.append(s["vgw"]); outputs["vgw"] = r.outputs
#         _set_job(name, current_step="vgw")
#
#         r = create_customer_gateway(CgwRequest(
#             stack=s["cgw"], azure_vnet_gateway_stack=s["vnet_gateway"],
#             bgp_asn=req.azure_asn, tags=req.tags,
#         ))
#         created.append(s["cgw"]); outputs["cgw"] = r.outputs
#         _set_job(name, current_step="cgw")
#
#         r = create_aws_vpn_connection(AwsVpnConnectionRequest(
#             stack=s["vpn_connection"], vgw_stack=s["vgw"], cgw_stack=s["cgw"],
#             tunnel1_inside_cidr=req.tunnel1_inside_cidr, tunnel2_inside_cidr=req.tunnel2_inside_cidr,
#             tags=req.tags,
#         ))
#         created.append(s["vpn_connection"]); outputs["vpn_connection"] = r.outputs
#         _set_job(name, current_step="vpn_connection")
#
#         r = update_vnet_gateway_bgp_config(VnetGatewayBgpConfigRequest(
#             vnet_gateway_stack=s["vnet_gateway"], aws_vpn_connection_stack=s["vpn_connection"],
#         ))
#         outputs["vnet_gateway"] = r.outputs
#         _set_job(name, current_step="vnet_gateway_bgp_config")
#
#         r = create_local_network_gateway(LocalNetworkGatewayRequest(
#             stack=s["lng_t1"], resource_group=req.resource_group,
#             aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=1,
#             address_spaces=req.address_spaces, asn=req.amazon_side_asn,
#         ))
#         created.append(s["lng_t1"]); outputs["lng_t1"] = r.outputs
#         _set_job(name, current_step="lng_t1")
#
#         r = create_local_network_gateway(LocalNetworkGatewayRequest(
#             stack=s["lng_t2"], resource_group=req.resource_group,
#             aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=2,
#             address_spaces=req.address_spaces, asn=req.amazon_side_asn,
#         ))
#         created.append(s["lng_t2"]); outputs["lng_t2"] = r.outputs
#         _set_job(name, current_step="lng_t2")
#
#         r = create_azure_vpn_connection(VpnGatewayConnectionRequest(
#             stack=s["conn_t1"], resource_group=req.resource_group,
#             vnet_gateway_stack=s["vnet_gateway"], local_network_gateway_stack=s["lng_t1"],
#             aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=1,
#         ))
#         created.append(s["conn_t1"]); outputs["conn_t1"] = r.outputs
#         _set_job(name, current_step="conn_t1")
#
#         r = create_azure_vpn_connection(VpnGatewayConnectionRequest(
#             stack=s["conn_t2"], resource_group=req.resource_group,
#             vnet_gateway_stack=s["vnet_gateway"], local_network_gateway_stack=s["lng_t2"],
#             aws_vpn_connection_stack=s["vpn_connection"], tunnel_number=2,
#         ))
#         created.append(s["conn_t2"]); outputs["conn_t2"] = r.outputs
#         _set_job(name, current_step="conn_t2")
#
#         outputs["route_prop"] = []
#         for rt_stack in req.route_table_stacks:
#             prop_stack = f"{_route_prop_prefix(name)}{rt_stack}"
#             r = create_route_propagation(RoutePropagationRequest(
#                 stack=prop_stack, vgw_stack=s["vgw"], route_table_stack=rt_stack,
#             ))
#             created.append(prop_stack)
#             outputs["route_prop"].append({
#                 "stack": prop_stack, "route_table_stack": rt_stack, "outputs": r.outputs,
#             })
#             _set_job(name, current_step=f"route_prop:{rt_stack}")
#
#     except HTTPException as exc:
#         _rollback(created)
#         error = f"Orchestration failed, rolled back {len(created)} stack(s): {exc.detail}"
#         print(f"orchestrate[{name}]: {error}")
#         _set_job(name, status="failed", error=error, finished_at=_now())
#         return
#     except Exception as exc:
#         _rollback(created)
#         error = f"Orchestration failed, rolled back {len(created)} stack(s): {exc}"
#         print(f"orchestrate[{name}]: {error}")
#         _set_job(name, status="failed", error=error, finished_at=_now())
#         return
#
#     _set_job(name, status="succeeded", current_step=None, outputs=outputs, finished_at=_now())
#
#
# def _run_teardown(name: str) -> None:
#     s = _stacks(name)
#     destroyed: list[str] = []
#
#     try:
#         prefix = _route_prop_prefix(name)
#         for entry in stack_list():
#             if entry["name"].startswith(prefix):
#                 stack_destroy(entry["name"])
#                 destroyed.append(entry["name"])
#                 _set_job(name, current_step=entry["name"])
#
#         for step_key in _TEARDOWN_ORDER:
#             stack_name = s[step_key]
#             if stack_exists(stack_name):
#                 stack_destroy(stack_name)
#                 destroyed.append(stack_name)
#                 _set_job(name, current_step=step_key)
#     except Exception as exc:
#         error = f"Teardown failed after destroying {len(destroyed)} stack(s): {exc}"
#         print(f"teardown[{name}]: {error}")
#         _set_job(name, status="failed", error=error, finished_at=_now(), destroyed=destroyed)
#         return
#
#     if not destroyed:
#         _set_job(name, status="failed", error=f"No stacks found for name={name!r}", finished_at=_now())
#         return
#
#     _set_job(name, status="succeeded", current_step=None, destroyed=destroyed, finished_at=_now())
#
#
# # --- endpoints: kick off in background, return immediately ---
#
# @router.post("/orchestrate", status_code=202)
# def orchestrate_vpn(user: user_dependency, req: VpnOrchestrationRequest, background_tasks: BackgroundTasks):
#     existing = _get_job(req.name)
#     if existing and existing.get("status") == "running":
#         raise HTTPException(status_code=409, detail=f"An operation is already running for name={req.name!r}")
#
#     locked = _locked_stacks(req.name)
#     if locked:
#         raise HTTPException(
#             status_code=409,
#             detail=f"Pulumi lock(s) held on {locked} — a prior operation may still be in flight",
#         )
#
#     _set_job(req.name, action="orchestrate", status="running", current_step=None,
#               error=None, started_at=_now(), finished_at=None)
#     background_tasks.add_task(_run_orchestration, req)
#
#     return {
#         "status": "accepted",
#         "name": req.name,
#         "check_status_at": f"/vpn/orchestrate/{req.name}/status",
#     }
#
#
# @router.delete("/orchestrate/{name}", status_code=202)
# def teardown_vpn(user: user_dependency, name: str, background_tasks: BackgroundTasks):
#     existing = _get_job(name)
#     if existing and existing.get("status") == "running":
#         raise HTTPException(status_code=409, detail=f"An operation is already running for name={name!r}")
#
#     locked = _locked_stacks(name)
#     if locked:
#         raise HTTPException(
#             status_code=409,
#             detail=f"Pulumi lock(s) held on {locked} — a prior operation may still be in flight",
#         )
#
#     _set_job(name, action="teardown", status="running", current_step=None,
#               error=None, started_at=_now(), finished_at=None)
#     background_tasks.add_task(_run_teardown, name)
#
#     return {
#         "status": "accepted",
#         "name": name,
#         "check_status_at": f"/vpn/orchestrate/{name}/status",
#     }
#
#
# @router.get("/orchestrate/{name}/status")
# def get_orchestration_status(user: user_dependency, name: str):
#     job = _get_job(name)
#     locked = _locked_stacks(name)
#
#     if job is None:
#         if locked:
#             return {
#                 "name": name,
#                 "status": "unknown_but_locked",
#                 "detail": (
#                     "No in-memory record — likely a process restart — but a Pulumi "
#                     "lock is still held, so an operation may still be running."
#                 ),
#                 "locked_stacks": locked,
#             }
#         raise HTTPException(
#             status_code=404,
#             detail=f"No in-memory job record for name={name!r}. "
#                    f"Check GET /vpn/orchestrate/{name} for actual resource state.",
#         )
#
#     return {**job, "name": name, "locked_stacks": locked}













