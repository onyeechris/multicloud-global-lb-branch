# MultiCloud VPN Service

Standalone FastAPI + Pulumi Automation API microservice that provisions an
AWS <-> Azure site-to-site VPN. Assumes an existing VPC (AWS) and VNet
(Azure) with route tables already provisioned by other services — this
service does not create either.

Each resource is its own Pulumi stack with full CRUD endpoints. Routes
resolve dependency stack outputs automatically — you only pass stack names,
not raw IDs.

## Build order

```
1  POST /azure/vpn/gateway-subnet
2  POST /azure/vpn/vnet-gateway
3  POST /aws/vpn/vgw
4  POST /aws/vpn/customer-gateway        (reads #2's public_ip)
5  POST /aws/vpn/vpn-connection          (reads #3 + #4)
6  PATCH /azure/vpn/vnet-gateway/bgp-config   (reads #5's tunnel CIDRs)
7  POST /azure/vpn/local-network-gateway x2   (tunnel_number 1, 2)
8  POST /azure/vpn/connection x2               (tunnel_number 1, 2)
9  POST /aws/vpn/route-propagation        (existing route table)
```

Steps 3 and 1-2 can run in parallel. Steps 7 and 8 must each run twice
(`tunnel_number: 1` then `tunnel_number: 2`).

## Global load balancer (AWS-side only)

Once the VPN is up and healthy, put an NLB in front — its target groups can
mix AWS private IPs and Azure private IPs (reachable over the tunnel):

```
10  POST /aws/lb/target-group          (one per backend pool, e.g. aws-only, azure-only)
11  POST /aws/lb/network-load-balancer (public subnets, internet-facing)
12  POST /aws/lb/listener              (forwards to one or more target groups, weighted)
```

`PATCH /aws/lb/target-group/{stack}` replaces the full target list on an
existing target group — useful for adding/removing instances without
recreating the group. Target `port: 80` only works if targets are actually
serving on that port and their SG/NSG allows inbound from `0.0.0.0/0` (not
just the VPC CIDR) — health checks originate from inside the VPC, but real
client traffic through an NLB keeps the original source IP.

## Example sequence

```bash
BASE=http://localhost:8000

curl -X POST $BASE/azure/vpn/gateway-subnet -d '{
  "stack": "azure-gw-subnet-prod",
  "resource_group": "aws-azure-rg",
  "vnet_name": "aws-azure-vnet",
  "location": "eastus",
  "address_prefix": "10.11.255.0/27"
}'

curl -X POST $BASE/azure/vpn/vnet-gateway -d '{
  "stack": "azure-vnet-gw-prod",
  "resource_group": "aws-azure-rg",
  "location": "eastus",
  "gateway_subnet_stack": "azure-gw-subnet-prod"
}'

curl -X POST $BASE/aws/vpn/vgw -d '{
  "stack": "aws-vgw-prod",
  "vpc_id": "vpc-084f644a6da365191"
}'

curl -X POST $BASE/aws/vpn/customer-gateway -d '{
  "stack": "aws-cgw-prod",
  "azure_vnet_gateway_stack": "azure-vnet-gw-prod"
}'

curl -X POST $BASE/aws/vpn/vpn-connection -d '{
  "stack": "aws-vpn-conn-prod",
  "vgw_stack": "aws-vgw-prod",
  "cgw_stack": "aws-cgw-prod"
}'

curl -X PATCH $BASE/azure/vpn/vnet-gateway/bgp-config -d '{
  "vnet_gateway_stack": "azure-vnet-gw-prod",
  "aws_vpn_connection_stack": "aws-vpn-conn-prod"
}'

curl -X POST $BASE/azure/vpn/local-network-gateway -d '{
  "stack": "azure-lng-t1-prod",
  "resource_group": "aws-azure-rg",
  "location": "eastus",
  "aws_vpn_connection_stack": "aws-vpn-conn-prod",
  "tunnel_number": 1,
  "address_spaces": ["10.10.0.0/16"]
}'

curl -X POST $BASE/azure/vpn/local-network-gateway -d '{
  "stack": "azure-lng-t2-prod",
  "resource_group": "aws-azure-rg",
  "location": "eastus",
  "aws_vpn_connection_stack": "aws-vpn-conn-prod",
  "tunnel_number": 2,
  "address_spaces": ["10.10.0.0/16"]
}'

curl -X POST $BASE/azure/vpn/connection -d '{
  "stack": "azure-conn-t1-prod",
  "resource_group": "aws-azure-rg",
  "location": "eastus",
  "vnet_gateway_stack": "azure-vnet-gw-prod",
  "local_network_gateway_stack": "azure-lng-t1-prod",
  "aws_vpn_connection_stack": "aws-vpn-conn-prod",
  "tunnel_number": 1
}'

curl -X POST $BASE/azure/vpn/connection -d '{
  "stack": "azure-conn-t2-prod",
  "resource_group": "aws-azure-rg",
  "location": "eastus",
  "vnet_gateway_stack": "azure-vnet-gw-prod",
  "local_network_gateway_stack": "azure-lng-t2-prod",
  "aws_vpn_connection_stack": "aws-vpn-conn-prod",
  "tunnel_number": 2
}'

curl -X POST $BASE/aws/vpn/route-propagation -d '{
  "stack": "aws-route-prop-prod",
  "vgw_stack": "aws-vgw-prod",
  "route_table_id": "rtb-xxxxxxxx"
}'

curl -X POST $BASE/aws/lb/target-group -d '{
  "stack": "aws-tg-prod",
  "vpc_id": "vpc-084f644a6da365191",
  "port": 80,
  "targets": [
    {"ip": "10.10.25.64", "port": 80},
    {"ip": "10.11.1.4", "port": 80, "availability_zone": "all"}
  ]
}'

curl -X POST $BASE/aws/lb/network-load-balancer -d '{
  "stack": "aws-nlb-prod",
  "name": "aws-azure-lb",
  "subnet_ids": ["subnet-06ac9c0823245ea83", "subnet-05d50fe235a97c1dc"],
  "security_group_ids": ["sg-0ffe47feb17218c01"]
}'

curl -X POST $BASE/aws/lb/listener -d '{
  "stack": "aws-listener-prod",
  "nlb_stack": "aws-nlb-prod",
  "port": 80,
  "target_groups": [
    {"target_group_stack": "aws-tg-prod", "weight": 1}
  ]
}'
```

## Notes

- Tunnel inside CIDRs default to `169.254.21.0/30` / `169.254.22.0/30` —
  required because Azure only accepts Custom APIPA BGP addresses in the
  `169.254.21.0/24` / `169.254.22.0/24` ranges.
- `use_local_azure_ip_address` is always `False` on the Connection resource;
  leaving it unset/True without `EnablePrivateIpAddress` on the gateway
  causes stack creation to fail (`CannotSetUseLocalAzureIpAddressForVirtualNetworkGatewayWithoutRequiredProperty`).
- `pulumi_azure_native`'s exact class/field names for custom BGP peering
  addresses vary by SDK version — verify `IpConfigurationBgpPeeringAddressArgs`
  against your installed `pulumi-azure-native` version before running; the
  ARM property path is `bgpSettings.bgpPeeringAddresses[].customBgpIpAddresses`.
- Every route validates its dependency stacks exist (`422`) before running
  `pulumi up`, so a caller gets a clear error instead of a Pulumi state file
  half-provisioned against missing inputs.