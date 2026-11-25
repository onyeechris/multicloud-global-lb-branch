from pydantic import BaseModel, Field
from typing import Optional


class GatewaySubnetRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    resource_group: str
    vnet_stack: str = Field(..., description="Existing VNet to add the GatewaySubnet to")
    address_prefix: str = Field(..., description="Min /27, e.g. '10.11.255.0/27'")


class VnetGatewayRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    resource_group: str
    # location: Optional[str] = Field(None, description="Azure location — omit to use the pre-selected location")
    gateway_subnet_stack: str = Field(..., description="Stack name of the GatewaySubnet")
    sku: str = Field("VpnGw1AZ", description="AZ-redundant SKU required — Azure no longer allows VpnGw1-5 non-AZ SKUs for new gateways. Use VpnGw1AZ-VpnGw5AZ.")
    asn: int = Field(65515, description="Must match the AWS Customer Gateway's ASN")


class VnetGatewayBgpConfigRequest(BaseModel):
    vnet_gateway_stack: str = Field(..., description="Stack name of the VNet Gateway to update")
    aws_vpn_connection_stack: str = Field(..., description="Stack name of the AWS VPN connection; tunnel inside CIDRs are read from its outputs")


class LocalNetworkGatewayRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    resource_group: str
    # location: Optional[str] = Field(None, description="Azure location — omit to use the pre-selected location")
    aws_vpn_connection_stack: str = Field(..., description="Stack name of the AWS VPN connection")
    tunnel_number: int = Field(..., ge=1, le=2, description="1 or 2")
    address_spaces: list[str] = Field(..., description="AWS-side VPC CIDR(s)")
    asn: int = Field(64512, description="Must match the AWS VGW's amazon_side_asn")


class VpnGatewayConnectionRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    resource_group: str
    # location: Optional[str] = Field(None, description="Azure location — omit to use the pre-selected location")
    vnet_gateway_stack: str
    local_network_gateway_stack: str
    aws_vpn_connection_stack: str = Field(..., description="Source of the tunnel PSK")
    tunnel_number: int = Field(..., ge=1, le=2)


class StackResponse(BaseModel):
    outputs: dict