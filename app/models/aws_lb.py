from pydantic import BaseModel, Field
from typing import Optional, List, Dict


class Target(BaseModel):
    ip: str = Field(..., description="Target IP — can be AWS private IP or, via VPN, an Azure private IP")
    port: int = 80
    availability_zone: Optional[str] = Field(
        None, description="Set to 'all' for cross-cloud/on-prem IP targets not tied to a VPC AZ"
    )


class TargetGroupRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    vpc_stack: str
    port: int = 80
    protocol: str = Field("TCP", description="TCP, UDP, or TCP_UDP")
    health_check_port: str = Field("traffic-port")
    health_check_protocol: str = Field("TCP")
    health_check_interval: int = 30
    health_check_timeout: int = 10
    healthy_threshold: int = 3
    unhealthy_threshold: int = 3
    targets: List[Target] = []
    tags: Dict[str, str] = {}


class TargetGroupPatchRequest(BaseModel):
    targets: List[Target] = Field(..., description="Full replacement list of registered targets")


class NlbRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    name: str = Field(..., description="Load balancer name, max 32 chars")
    subnet_ids: List[str] = Field(..., description="Public subnets, one per AZ, min 2 recommended")
    scheme: str = Field("internet-facing", description="internet-facing or internal")
    security_group_stack: str = None
    tags: Dict[str, str] = {}


class ListenerTargetGroupWeight(BaseModel):
    target_group_stack: str
    weight: int = 1


class ListenerRequest(BaseModel):
    stack: str = Field(..., description="Pulumi stack name")
    nlb_stack: str
    port: int = 80
    protocol: str = Field("TCP", description="TCP, UDP, TCP_UDP, or TLS")
    target_groups: List[ListenerTargetGroupWeight] = Field(
        ..., description="One or more target group stacks with relative weights for weighted forwarding"
    )
    certificate_arn: Optional[str] = Field(None, description="Required if protocol is TLS")


class StackResponse(BaseModel):
    outputs: dict