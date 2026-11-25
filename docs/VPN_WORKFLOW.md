# VPN orchestration and load balancer guide

> Every request needs `Authorization: Bearer <JWT>`. See the top-level README.

Examples use `$BASE=http://localhost:8000` and `$TOKEN` for a valid JWT.

## Prerequisites

The orchestrator **does not create** a VPC or a VNet. Create these first, as stacks, using the sibling services:

| Needed | Created by | Required stack outputs |
|---|---|---|
| AWS VPC stack | [multicloud-vpc-feat-branch](https://github.com/onyeechris/multicloud-vpc-feat-branch) | `vpc_id`, `vpc_cidr` |
| AWS route table stack(s) | same | `route_table_id` |
| Azure VNet stack | [multicloud-vnet-branch](https://github.com/onyeechris/multicloud-vnet-branch) | `resource_group_name`, `vnet_name`, `vnet_location`, `address_spaces` |

Request validation checks all of these first and returns `422` with a specific message when something is missing, before any cloud resource is created.

## What `POST /vpn/orchestrate` builds

One call builds the whole site-to-site VPN as nine-plus independent stacks. The `name` you pass becomes the suffix of every stack name:

| # | Stack | Cloud | Notes |
|---|---|---|---|
| 1 | `azure-gw-subnet-<name>` | Azure | `GatewaySubnet` inside your VNet (min `/27`, must fall inside the VNet address space) |
| 2 | `azure-vnet-gw-<name>` | Azure | VPN gateway, ASN `azure_asn` (default 65515) |
| 3 | `aws-vgw-<name>` | AWS | Virtual private gateway attached to the VPC, ASN `amazon_side_asn` (default 64512) |
| 4 | `aws-cgw-<name>` | AWS | Customer gateway pointing at the Azure gateway's public IP |
| 5 | `aws-vpn-conn-<name>` | AWS | VPN connection (two tunnels) |
| 6 | (update of #2) | Azure | BGP configuration taken from the AWS tunnel details |
| 7 | `azure-lng-t1-<name>`, `azure-lng-t2-<name>` | Azure | One local network gateway per tunnel, using the VPC CIDR as address space |
| 8 | `azure-conn-t1-<name>`, `azure-conn-t2-<name>` | Azure | One connection per tunnel |
| 9 | `aws-route-prop-<name>-<route-table-stack>` | AWS | Route propagation, one per route table you list |

The request model (`VpnOrchestrationRequest`):

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Suffix for every stack name, e.g. `prod` |
| `resource_group` | yes | Azure resource group. Must equal the VNet stack's `resource_group_name` |
| `vnet_stack` | yes | Existing Azure VNet stack |
| `gateway_subnet_address_prefix` | yes | CIDR for the GatewaySubnet, e.g. `10.11.255.0/27` |
| `vpc_stack` | yes | Existing AWS VPC stack |
| `route_table_stacks` | yes | One or more existing route table stacks (no duplicates) |
| `vnet_gateway_sku` | no | Default `VpnGw1AZ` |
| `azure_asn` / `amazon_side_asn` | no | BGP ASNs (defaults 65515 / 64512) |
| `tunnel1_inside_cidr` / `tunnel2_inside_cidr` | no | Defaults `169.254.21.0/30` and `169.254.22.0/30` |
| `tags` | no | Tags applied to AWS resources |

```bash
curl -X POST $BASE/vpn/orchestrate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "demo",
    "resource_group": "demo-rg",
    "vnet_stack": "demo-vnet",
    "gateway_subnet_address_prefix": "10.11.255.0/27",
    "vpc_stack": "demo-vpc",
    "route_table_stacks": ["demo-private-rt"]
  }'
# 202 {"status": "accepted", "name": "demo", "check_status_at": "/vpn/orchestrate/demo/status"}
```

## Jobs, status and rollback

- Orchestration and teardown run as **background jobs**. The POST and DELETE return `202` immediately, and the work can take a long time because Azure VPN gateways are slow to create.
- Poll `GET /vpn/orchestrate/{name}/status`. It reports `status` (`running`, `succeeded`, `failed`), the `current_step`, any `error`, timestamps and, on success, the outputs of every stack.
- If any step fails, every stack created so far in that run is **destroyed in reverse order** (rolled back) and the job ends as `failed` with the reason.
- Only one operation can run per `name` at a time. A second request returns `409`, as does a request while a Pulumi lock is held on any of the stacks.
- Job records are kept **in memory**. After a process restart the status endpoint can only report `unknown_but_locked` (if a lock is still held) or `404`. Use `GET /vpn/orchestrate/{name}` to see the real state of the stacks.
- `GET /vpn/orchestrate` lists all orchestrations found in stack state.

## Teardown

```bash
curl -X DELETE $BASE/vpn/orchestrate/demo -H "Authorization: Bearer $TOKEN"
```

Destroys route propagations first, then connections, local network gateways, the AWS VPN connection, customer gateway, virtual private gateway, the Azure VNet gateway and finally the gateway subnet.

## Load balancer

Once the VPN is up, an AWS Network Load Balancer can front backends in **both clouds**. Target groups can mix AWS private IPs and Azure private IPs that are reachable over the tunnel.

```
1  POST /aws/lb/target-group           one per backend pool (e.g. aws-only, azure-only)
2  POST /aws/lb/network-load-balancer  internet-facing or internal, in public subnets
3  POST /aws/lb/listener               forwards to one or more target groups with weights
```

```bash
curl -X POST $BASE/aws/lb/target-group \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "stack": "demo-tg",
    "vpc_stack": "demo-vpc",
    "port": 80,
    "targets": [
      {"ip": "10.0.1.10", "port": 80},
      {"ip": "10.1.1.4", "port": 80, "availability_zone": "all"}
    ]
  }'
```

- `PATCH /aws/lb/target-group/{stack}` replaces the **entire** target list. Use it to add or remove backends without recreating the group.
- Targets outside the VPC (for example Azure IPs) need `"availability_zone": "all"`.
- `DELETE /aws/lb/{stack}` destroys any load balancer stack (target group, NLB or listener).
- Real client traffic through an NLB keeps its original source IP, so the target's security group or NSG must allow inbound from `0.0.0.0/0` on the target port (or from your client ranges). Health checks come from inside the VPC, which is why a group can look healthy and still reject clients.

## Notes and gotchas

- Tunnel inside CIDRs must come from `169.254.21.0/24` and `169.254.22.0/24`, because Azure only accepts custom APIPA BGP addresses in those ranges.
- `use_local_azure_ip_address` is always `False` on the Azure connection. Leaving it on without `EnablePrivateIpAddress` on the gateway fails with `CannotSetUseLocalAzureIpAddressForVirtualNetworkGatewayWithoutRequiredProperty`.
- Class and field names for custom BGP peering addresses vary between `pulumi-azure-native` versions. If you change the pinned SDK version, verify `IpConfigurationBgpPeeringAddressArgs` first. The ARM path is `bgpSettings.bgpPeeringAddresses[].customBgpIpAddresses`.
- The individual resource endpoints for the AWS VPN pieces (`/aws/vpn/*`) and Azure VPN pieces (`/azure/vpn/*`) exist as code but are not mounted as routes. The orchestrator calls them internally.
