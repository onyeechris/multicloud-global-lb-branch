from pydantic import BaseModel, Field
from typing import Optional, Dict


class VgwRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    vpc_stack: str = Field(..., description="Existing VPC to attach the VGW to")
    amazon_side_asn: int = Field(64512, description="BGP ASN for the VGW")
    tags: Dict[str, str] = {}


class CgwRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    azure_vnet_gateway_stack: str = Field(..., description="Stack name of the Azure VNet Gateway; its public_ip output is used as the Customer Gateway IP")
    bgp_asn: int = Field(65515, description="Must match the Azure VNet Gateway's ASN")
    tags: Dict[str, str] = {}


class VpnConnectionRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    vgw_stack: str = Field(..., description="Stack name of the Virtual Private Gateway")
    cgw_stack: str = Field(..., description="Stack name of the Customer Gateway")
    tunnel1_inside_cidr: str = Field("169.254.21.0/30", description="Must fall in 169.254.21.0/24 (Azure APIPA range)")
    tunnel2_inside_cidr: str = Field("169.254.22.0/30", description="Must fall in 169.254.22.0/24 (Azure APIPA range)")
    tags: Dict[str, str] = {}


class RoutePropagationRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    vgw_stack: str
    route_table_stack: str = Field(..., description="Existing route table to enable propagation on")


class StackResponse(BaseModel):
    outputs: dict