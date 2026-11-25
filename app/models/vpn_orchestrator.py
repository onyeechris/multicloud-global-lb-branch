from pydantic import BaseModel, Field, field_validator


class VpnOrchestrationRequest(BaseModel):
    name: str = Field(..., description="Suffix applied to every stack name, e.g. 'prod'")
    resource_group: str = Field(
        ..., min_length=1, description="Azure resource group hosting all Azure resources — "
        "must match vnet_stack's own resource_group_name output"
    )
    vnet_stack: str = Field(..., min_length=1, description="Existing Azure VNet stack")
    gateway_subnet_address_prefix: str = Field(
        ..., description="GatewaySubnet CIDR, min /27 — must fall within vnet_stack's address space"
    )
    vpc_stack: str = Field(
        ..., min_length=1, description="Existing AWS VPC stack — its vpc_cidr output is used as the "
        "Local Network Gateway address_spaces, not user-supplied"
    )
    route_table_stacks: list[str] = Field(
        ..., min_length=1, description="Existing route table stacks to enable propagation on — "
        "one Pulumi stack per route table, each must export route_table_id"
    )
    vnet_gateway_sku: str = Field("VpnGw1AZ")
    azure_asn: int = Field(65515, description="Azure VNet Gateway BGP ASN, also used as the AWS Customer Gateway's bgp_asn")
    amazon_side_asn: int = Field(64512, description="AWS VGW BGP ASN, also used as the Local Network Gateways' asn")
    tunnel1_inside_cidr: str = Field("169.254.21.0/30")
    tunnel2_inside_cidr: str = Field("169.254.22.0/30")
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("route_table_stacks")
    @classmethod
    def _no_duplicate_route_tables(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("route_table_stacks contains duplicate entries")
        return v






















# class VpnOrchestrationRequest(BaseModel):
#     name: str = Field(..., description="Suffix applied to every stack name, e.g. 'prod'")
#     resource_group: str = Field(..., description="Azure resource group hosting all Azure resources")
#     vnet_stack: str = Field(..., description="Existing Azure VNet stack")
#     address_prefix: str = Field(..., description="GatewaySubnet CIDR, min /27")
#     vpc_stack: str = Field(..., description="Existing AWS VPC stack")
#     route_table_stacks: list[str] = Field(
#         ..., description="Existing route table stacks to enable propagation on — "
#         "one Pulumi stack per route table, each must export route_table_id"
#     )
#     address_spaces: list[str] = Field(..., description="AWS-side VPC CIDR(s), used on both Local Network Gateways")
#     vnet_gateway_sku: str = Field("VpnGw1AZ")
#     azure_asn: int = Field(65515, description="Azure VNet Gateway BGP ASN, also used as the AWS Customer Gateway's bgp_asn")
#     amazon_side_asn: int = Field(64512, description="AWS VGW BGP ASN, also used as the Local Network Gateways' asn")
#     tunnel1_inside_cidr: str = Field("169.254.21.0/30")
#     tunnel2_inside_cidr: str = Field("169.254.22.0/30")
#     tags: dict[str, str] = Field(default_factory=dict)