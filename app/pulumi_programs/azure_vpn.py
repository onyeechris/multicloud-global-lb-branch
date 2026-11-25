import pulumi
import pulumi_azure_native.network as network
from app.utils.location_state import get_location


def create_gateway_subnet_program(resource_group: str, vnet_name: str, address_prefix: str):
    def program():
        subnet = network.Subnet(
            "gateway-subnet",
            resource_group_name=resource_group,
            virtual_network_name=vnet_name,
            subnet_name="GatewaySubnet",
            address_prefix=address_prefix,
        )
        pulumi.export("subnet_id", subnet.id)
        pulumi.export("address_prefix", address_prefix)
        pulumi.export("resource_type", "azure_gateway_subnet")
    return program


def create_vnet_gateway_program(
    resource_group: str,
    location: str,
    gateway_subnet_id: str,
    sku: str,
    asn: int,
    custom_bgp_addresses: list[str] | None = None,
    vnet_gateway_id: str | None = None,
):
    resolved_location = location or get_location()
    if not resolved_location:
        raise ValueError("No location provided and no active location selected.")
    def program():
        pip = network.PublicIPAddress(
            "vpn-gw-pip",
            resource_group_name=resource_group,
            location=resolved_location,
            sku=network.PublicIPAddressSkuArgs(name="Standard"),
            public_ip_allocation_method="Static",
            zones=["1", "2", "3"]
        )

        bgp_settings_args = {"asn": asn}
        if custom_bgp_addresses:
            if not vnet_gateway_id:
                raise ValueError(
                    "vnet_gateway_id is required when setting custom_bgp_addresses "
                    "on an existing gateway"
                )
            ipconfiguration_id = f"{vnet_gateway_id}/ipConfigurations/vnetGatewayConfig"
            bgp_settings_args["bgp_peering_addresses"] = [
                network.IPConfigurationBgpPeeringAddressArgs(
                    ipconfiguration_id=ipconfiguration_id,
                    custom_bgp_ip_addresses=custom_bgp_addresses,
                )
            ]

        gateway = network.VirtualNetworkGateway(
            "vnet-gateway",
            resource_group_name=resource_group,
            location=resolved_location,
            gateway_type="Vpn",
            vpn_type="RouteBased",
            sku=network.VirtualNetworkGatewaySkuArgs(name=sku, tier=sku),
            ip_configurations=[
                network.VirtualNetworkGatewayIPConfigurationArgs(
                    name="vnetGatewayConfig",
                    public_ip_address=network.SubResourceArgs(id=pip.id),
                    subnet=network.SubResourceArgs(id=gateway_subnet_id),
                )
            ],
            bgp_settings=network.BgpSettingsArgs(**bgp_settings_args),
            enable_bgp=True,
        )

        pulumi.export("vnet_gateway_id", gateway.id)
        pulumi.export("vnet_gateway_name", gateway.name)
        pulumi.export("public_ip", pip.ip_address)
        pulumi.export("asn", asn)
        pulumi.export("custom_bgp_addresses", custom_bgp_addresses or [])
        pulumi.export("resource_group", resource_group)
        pulumi.export("location", resolved_location)
        pulumi.export("gateway_subnet_id", gateway_subnet_id)
        pulumi.export("resource_type", "azure_vnet_gateway")
        pulumi.export("sku", sku)
    return program


def create_local_network_gateway_program(
    resource_group: str,
    location: str,
    aws_tunnel_outside_ip: str,
    aws_tunnel_vgw_inside_ip: str,
    address_spaces: list[str],
    asn: int,
):
    resolved_location = location or get_location()
    if not resolved_location:
        raise ValueError("No location provided and no active location selected.")
    def program():
        lng = network.LocalNetworkGateway(
            "local-network-gateway",
            resource_group_name=resource_group,
            location=resolved_location,
            gateway_ip_address=aws_tunnel_outside_ip,
            local_network_address_space=network.AddressSpaceArgs(
                address_prefixes=address_spaces,
            ),
            bgp_settings=network.BgpSettingsArgs(
                asn=asn,
                bgp_peering_address=aws_tunnel_vgw_inside_ip,
            ),
        )
        pulumi.export("local_network_gateway_id", lng.id)
        pulumi.export("local_network_gateway_name", lng.name)
        pulumi.export("resource_type", "azure_local_network_gateway")
    return program


def create_vpn_gateway_connection_program(
    resource_group: str,
    location: str,
    vnet_gateway_id: str,
    local_network_gateway_id: str,
    shared_key: str,
):
    resolved_location = location or get_location()
    if not resolved_location:
        raise ValueError("No location provided and no active location selected.")
    def program():
        conn = network.VirtualNetworkGatewayConnection(
            "vpn-connection",
            resource_group_name=resource_group,
            location=resolved_location,
            virtual_network_gateway1=network.SubResourceArgs(id=vnet_gateway_id),
            local_network_gateway2=network.SubResourceArgs(id=local_network_gateway_id),
            connection_type="IPsec",
            enable_bgp=True,
            use_local_azure_ip_address=False,
            shared_key=shared_key,
        )
        pulumi.export("connection_id", conn.id)
        pulumi.export("connection_name", conn.name)
        pulumi.export("resource_type", "azure_vpn_connection")
    return program

































# import pulumi
# import pulumi_azure_native.network as network
# from app.utils.location_state import get_location
#
#
# def create_gateway_subnet_program(resource_group: str, vnet_name: str, address_prefix: str):
#     def program():
#         subnet = network.Subnet(
#             "gateway-subnet",
#             resource_group_name=resource_group,
#             virtual_network_name=vnet_name,
#             subnet_name="GatewaySubnet",
#             address_prefix=address_prefix,
#         )
#         pulumi.export("subnet_id", subnet.id)
#         pulumi.export("address_prefix", address_prefix)
#         pulumi.export("resource_type", "azure_gateway_subnet")
#     return program
#
#
# def create_vnet_gateway_program(
#     resource_group: str,
#     location: str,
#     gateway_subnet_id: str,
#     sku: str,
#     asn: int,
#     custom_bgp_addresses: list[str] | None = None,
# ):
#     resolved_location = location or get_location()
#     if not resolved_location:
#         raise ValueError("No location provided and no active location selected.")
#     def program():
#         pip = network.PublicIPAddress(
#             "vpn-gw-pip",
#             resource_group_name=resource_group,
#             location=resolved_location,
#             sku=network.PublicIPAddressSkuArgs(name="Standard"),
#             public_ip_allocation_method="Static",
#             zones=["1", "2", "3"]
#         )
#
#         bgp_settings_args = {"asn": asn}
#         if custom_bgp_addresses:
#             bgp_settings_args["bgp_peering_addresses"] = [
#                 network.IpConfigurationBgpPeeringAddressArgs(
#                     ip_config_id=pulumi.Output.concat(
#                         "vnetGatewayConfig"
#                     ),
#                     custom_bgp_ip_addresses=custom_bgp_addresses,
#                 )
#             ]
#
#         gateway = network.VirtualNetworkGateway(
#             "vnet-gateway",
#             resource_group_name=resource_group,
#             location=resolved_location,
#             gateway_type="Vpn",
#             vpn_type="RouteBased",
#             sku=network.VirtualNetworkGatewaySkuArgs(name=sku, tier=sku),
#             ip_configurations=[
#                 network.VirtualNetworkGatewayIPConfigurationArgs(
#                     name="vnetGatewayConfig",
#                     public_ip_address=network.SubResourceArgs(id=pip.id),
#                     subnet=network.SubResourceArgs(id=gateway_subnet_id),
#                 )
#             ],
#             bgp_settings=network.BgpSettingsArgs(**bgp_settings_args),
#             enable_bgp=True,
#         )
#
#         pulumi.export("vnet_gateway_id", gateway.id)
#         pulumi.export("vnet_gateway_name", gateway.name)
#         pulumi.export("public_ip", pip.ip_address)
#         pulumi.export("asn", asn)
#         pulumi.export("custom_bgp_addresses", custom_bgp_addresses or [])
#         pulumi.export("resource_group", resource_group)
#         pulumi.export("location", location)
#         pulumi.export("gateway_subnet_id", gateway_subnet_id)
#         pulumi.export("resource_type", "azure_vnet_gateway")
#         pulumi.export("sku", sku)
#     return program
#
#
# def create_local_network_gateway_program(
#     resource_group: str,
#     location: str,
#     aws_tunnel_outside_ip: str,
#     aws_tunnel_vgw_inside_ip: str,
#     address_spaces: list[str],
#     asn: int,
# ):
#     resolved_location = location or get_location()
#     if not resolved_location:
#         raise ValueError("No location provided and no active location selected.")
#     def program():
#         lng = network.LocalNetworkGateway(
#             "local-network-gateway",
#             resource_group_name=resource_group,
#             location=resolved_location,
#             gateway_ip_address=aws_tunnel_outside_ip,
#             local_network_address_space=network.AddressSpaceArgs(
#                 address_prefixes=address_spaces,
#             ),
#             bgp_settings=network.BgpSettingsArgs(
#                 asn=asn,
#                 bgp_peering_address=aws_tunnel_vgw_inside_ip,
#             ),
#         )
#         pulumi.export("local_network_gateway_id", lng.id)
#         pulumi.export("local_network_gateway_name", lng.name)
#     return program
#
#
# def create_vpn_gateway_connection_program(
#     resource_group: str,
#     location: str,
#     vnet_gateway_id: str,
#     local_network_gateway_id: str,
#     shared_key: str,
# ):
#     resolved_location = location or get_location()
#     if not resolved_location:
#         raise ValueError("No location provided and no active location selected.")
#     def program():
#         conn = network.VirtualNetworkGatewayConnection(
#             "vpn-connection",
#             resource_group_name=resource_group,
#             location=resolved_location,
#             virtual_network_gateway1=network.SubResourceArgs(id=vnet_gateway_id),
#             local_network_gateway2=network.SubResourceArgs(id=local_network_gateway_id),
#             connection_type="IPsec",
#             enable_bgp=True,
#             use_local_azure_ip_address=False,
#             shared_key=shared_key,
#         )
#         pulumi.export("connection_id", conn.id)
#         pulumi.export("connection_name", conn.name)
#     return program