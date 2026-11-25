import pulumi
import pulumi_aws as aws


def create_vgw_program(vpc_id: str, amazon_side_asn: int, tags: dict):
    def program():
        vgw = aws.ec2.VpnGateway(
            "vgw",
            vpc_id=vpc_id,
            amazon_side_asn=amazon_side_asn,
            tags={"ManagedBy": "Pulumi", **tags},
        )
        pulumi.export("vgw_id", vgw.id)
        pulumi.export("amazon_side_asn", amazon_side_asn)
        pulumi.export("resource_type", "aws_vgw")
    return program


def create_cgw_program(azure_gateway_ip: str, bgp_asn: int, tags: dict):
    def program():
        cgw = aws.ec2.CustomerGateway(
            "cgw",
            bgp_asn=bgp_asn,
            ip_address=azure_gateway_ip,
            type="ipsec.1",
            tags={"ManagedBy": "Pulumi", **tags},
        )
        pulumi.export("cgw_id", cgw.id)
        pulumi.export("bgp_asn", bgp_asn)
        pulumi.export("resource_type", "aws_cgw")
    return program


def create_vpn_connection_program(
    vgw_id: str,
    cgw_id: str,
    tunnel1_inside_cidr: str,
    tunnel2_inside_cidr: str,
    tags: dict,
):
    def program():
        conn = aws.ec2.VpnConnection(
            "vpn-connection",
            vpn_gateway_id=vgw_id,
            customer_gateway_id=cgw_id,
            type="ipsec.1",
            static_routes_only=False,
            tunnel1_inside_cidr=tunnel1_inside_cidr,
            tunnel2_inside_cidr=tunnel2_inside_cidr,
            tags={"ManagedBy": "Pulumi", **tags},
        )
        pulumi.export("vpn_connection_id", conn.id)
        pulumi.export("resource_type", "aws_vpn_connection")

        pulumi.export("tunnel1_outside_ip", conn.tunnel1_address)
        pulumi.export("tunnel1_psk", conn.tunnel1_preshared_key)
        pulumi.export("tunnel1_inside_cidr", tunnel1_inside_cidr)
        pulumi.export("tunnel1_vgw_inside_ip", conn.tunnel1_vgw_inside_address)
        pulumi.export("tunnel1_cgw_inside_ip", conn.tunnel1_cgw_inside_address)

        pulumi.export("tunnel2_outside_ip", conn.tunnel2_address)
        pulumi.export("tunnel2_psk", conn.tunnel2_preshared_key)
        pulumi.export("tunnel2_inside_cidr", tunnel2_inside_cidr)
        pulumi.export("tunnel2_vgw_inside_ip", conn.tunnel2_vgw_inside_address)
        pulumi.export("tunnel2_cgw_inside_ip", conn.tunnel2_cgw_inside_address)
    return program


def create_route_propagation_program(vgw_id: str, route_table_id: str):
    def program():
        prop = aws.ec2.VpnGatewayRoutePropagation(
            "route-propagation",
            vpn_gateway_id=vgw_id,
            route_table_id=route_table_id,
        )
        pulumi.export("route_table_id", route_table_id)
        pulumi.export("vgw_id", vgw_id)
        pulumi.export("resource_type", "aws_route_propagation")
    return program