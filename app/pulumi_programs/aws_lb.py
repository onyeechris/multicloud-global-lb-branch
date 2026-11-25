import pulumi
import pulumi_aws as aws


def create_target_group_program(
    vpc_id: str,
    port: int,
    protocol: str,
    health_check_port: str,
    health_check_protocol: str,
    health_check_interval: int,
    health_check_timeout: int,
    healthy_threshold: int,
    unhealthy_threshold: int,
    targets: list[dict],
    tags: dict,
):
    def program():
        tg = aws.lb.TargetGroup(
            "target-group",
            port=port,
            protocol=protocol,
            vpc_id=vpc_id,
            target_type="ip",
            health_check=aws.lb.TargetGroupHealthCheckArgs(
                protocol=health_check_protocol,
                port=health_check_port,
                interval=health_check_interval,
                timeout=health_check_timeout,
                healthy_threshold=healthy_threshold,
                unhealthy_threshold=unhealthy_threshold,
            ),
            tags={"ManagedBy": "Pulumi", **tags},
        )

        for i, t in enumerate(targets):
            aws.lb.TargetGroupAttachment(
                f"tg-attach-{i}",
                target_group_arn=tg.arn,
                target_id=t["ip"],
                port=t.get("port", port),
                availability_zone=t.get("availability_zone"),
            )

        pulumi.export("target_group_arn", tg.arn)
        pulumi.export("target_group_name", tg.name)
        pulumi.export("vpc_id", vpc_id)
        pulumi.export("port", port)
        pulumi.export("targets", targets)
        pulumi.export("resource_type", "aws_target_group")
    return program


def create_nlb_program(
    name: str,
    subnet_ids: list[str],
    scheme: str,
    security_group_id: str,
    tags: dict,
):
    def program():
        nlb = aws.lb.LoadBalancer(
            "nlb",
            name=name,
            load_balancer_type="network",
            internal=(scheme == "internal"),
            subnets=subnet_ids,
            security_groups=security_group_id or None,
            tags={"ManagedBy": "Pulumi", **tags},
        )
        pulumi.export("nlb_arn", nlb.arn)
        pulumi.export("nlb_dns_name", nlb.dns_name)
        pulumi.export("nlb_zone_id", nlb.zone_id)
        pulumi.export("nlb_name", nlb.name)
        pulumi.export("resource_type", "aws_nlb")
    return program


def create_listener_program(
    nlb_arn: str,
    port: int,
    protocol: str,
    target_group_weights: list[dict],
    certificate_arn: str | None,
):
    def program():
        forward_config = aws.lb.ListenerDefaultActionForwardArgs(
            target_groups=[
                aws.lb.ListenerDefaultActionForwardTargetGroupArgs(
                    arn=tgw["arn"], weight=tgw["weight"]
                )
                for tgw in target_group_weights
            ]
        )

        listener_args = dict(
            load_balancer_arn=nlb_arn,
            port=port,
            protocol=protocol,
            default_actions=[
                aws.lb.ListenerDefaultActionArgs(type="forward", forward=forward_config)
            ],
        )
        if protocol == "TLS":
            if not certificate_arn:
                raise ValueError("certificate_arn is required for TLS listeners")
            listener_args["certificate_arn"] = certificate_arn
            listener_args["ssl_policy"] = "ELBSecurityPolicy-TLS13-1-2-2021-06"

        listener = aws.lb.Listener("listener", **listener_args)

        pulumi.export("listener_arn", listener.arn)
        pulumi.export("port", port)
        pulumi.export("protocol", protocol)
        pulumi.export("resource_type", "aws_listener")
    return program