import os
import json
import boto3
from botocore.exceptions import ClientError
from typing import Callable
from importlib.metadata import version
from pulumi import automation as auto
import logging

logger = logging.getLogger(__name__)

# Construct PulumiCommand once, explicitly, at import time — fails loudly here
# with a real error if the CLI can't be found/run, instead of surfacing as a
# cryptic None deep inside subprocess.Popen later.
_pulumi_command = auto.PulumiCommand()

_s3 = boto3.client("s3", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
_BUCKET = os.getenv("PULUMI_BACKEND_URL", "").replace("s3://", "")

PROJECT_NAME = "multicloud-networking"
_AWS_PLUGIN_VERSION = f"v{version('pulumi-aws')}"
_AZURE_PLUGIN_VERSION = f"v{version('pulumi-azure-native')}"

_INPUT_ERRORS = (
    "InvalidSubnet",
    "does not exist",
    "InvalidVpcID",
    "InvalidParameterValue",
    "InvalidVpnGatewayID",
    "InvalidCustomerGatewayID",
    "InvalidVpnConnectionID",
    "ResourceGroupNotFound",
    "InvalidResourceLocation",
    "SubnetNotFound",
    "VirtualNetworkNotFound",
)


def _stack_key(stack_name: str) -> str:
    return f".pulumi/stacks/{PROJECT_NAME}/{stack_name}.json"


_SPECIAL_SIG_KEY = "4dabf18193072939515e22adb298388d"
_SECRET_SIG = "1b47061264138c4ac30d75fd1eb44270"


def _unwrap_secrets(value):
    """Recursively unwrap Pulumi's secret-sig wrapper objects so stack_outputs
    returns plain values, not {"4dabf...": "1b470...", "value": <actual>}."""
    if isinstance(value, dict):
        if value.get(_SPECIAL_SIG_KEY) == _SECRET_SIG:
            return _unwrap_secrets(value.get("value"))
        return {k: _unwrap_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_secrets(v) for v in value]
    return value


def _get_stack_state_from_s3(stack_name: str) -> dict:
    key = _stack_key(stack_name)
    try:
        obj = _s3.get_object(Bucket=_BUCKET, Key=key)
        state = json.loads(obj["Body"].read())
        outputs = {}
        for resource in state.get("checkpoint", {}).get("latest", {}).get("resources", []):
            if resource.get("type") == "pulumi:pulumi:Stack":
                outputs = resource.get("outputs", {})
        return _unwrap_secrets(outputs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            raise KeyError(f"Stack {stack_name} not found")
        raise


def stack_exists(stack_name: str) -> bool:
    try:
        _s3.head_object(Bucket=_BUCKET, Key=_stack_key(stack_name))
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def _plugin_present(stack: auto.Stack, name: str, version: str, kind: str = "resource") -> bool:
    installed = stack.workspace.list_plugins()
    return any(p.name == name and p.kind == kind and p.version == version for p in installed)

def is_locked(stack_name: str) -> bool:
    """Check for a live Pulumi lock on this stack — the same lock file
    responsible for ConcurrentUpdateError when two operations collide."""
    prefix = f".pulumi/locks/organization/{PROJECT_NAME}/{stack_name}/"
    response = _s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix, MaxKeys=1)
    return response.get("KeyCount", 0) > 0


def stack_up(stack_name: str, program: Callable, plugin: str = "aws", config: dict | None = None) -> dict:
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=PROJECT_NAME,
        program=program,
        opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
    )
    logger.info(f"Workspace pulumi_command: {stack.workspace.pulumi_command.command!r}")

    if plugin == "aws":
        plugin_name, plugin_version = "aws", _AWS_PLUGIN_VERSION
        region = (config.get("region") if config else None) or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        stack.set_config("aws:region", auto.ConfigValue(value=region))
    else:
        plugin_name, plugin_version = "azure-native", _AZURE_PLUGIN_VERSION
        location = (config.get("location") if config else None) or "eastus"
        stack.set_config("azure-native:location", auto.ConfigValue(value=location))

    if _plugin_present(stack, plugin_name, plugin_version):
        logger.info(f"Plugin {plugin_name}@{plugin_version} already installed, skipping install")
    else:
        stack.workspace.install_plugin(name=plugin_name, version=plugin_version)

    if config:
        for k, v in config.items():
            if v is None:
                continue
            stack.set_config(k, auto.ConfigValue(value=v))

    result = stack.up(on_output=print)
    return {k: v.value for k, v in result.outputs.items()}

# def stack_up(stack_name: str, program: Callable, plugin: str = "aws", config: dict | None = None) -> dict:
#     stack = auto.create_or_select_stack(
#         stack_name=stack_name,
#         project_name=PROJECT_NAME,
#         program=program,
#         opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
#     )
#     logger.info(f"Workspace pulumi_command: {stack.workspace.pulumi_command.command!r}")
#
#     if plugin == "aws":
#         stack.workspace.install_plugin(name="aws", version=_AWS_PLUGIN_VERSION)
#         region = (config.get("region") if config else None) or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
#         stack.set_config("aws:region", auto.ConfigValue(value=region))
#     else:
#         stack.workspace.install_plugin(name="azure-native", version=_AZURE_PLUGIN_VERSION)
#         location = (config.get("location") if config else None) or "eastus"
#         stack.set_config("azure-native:location", auto.ConfigValue(value=location))
#
#     if config:
#         for k, v in config.items():
#             if v is None:
#                 continue
#             stack.set_config(k, auto.ConfigValue(value=v))
#
#     result = stack.up(on_output=print)
#     return {k: v.value for k, v in result.outputs.items()}


def stack_up_safe(stack_name: str, program: Callable, plugin: str = "aws", config: dict | None = None) -> dict:
    try:
        return stack_up(stack_name, program, plugin, config)
    except Exception as exc:
        msg = str(exc)
        if any(k in msg for k in _INPUT_ERRORS):
            raise ValueError(f"Invalid input: {msg}")
        raise


def stack_outputs(stack_name: str) -> dict:
    return _get_stack_state_from_s3(stack_name)


def stack_outputs_secure(stack_name: str) -> dict:
    """Fetch stack outputs via the Automation API so secret values (e.g. VPN
    PSKs) are properly decrypted using PULUMI_CONFIG_PASSPHRASE. Slower than
    stack_outputs() — spawns a real pulumi CLI call — so only use this when
    you specifically need a secret-tagged output. Raw S3 reads (stack_outputs)
    only ever see ciphertext for secret outputs, never plaintext."""
    try:
        stack = auto.select_stack(
            stack_name=stack_name,
            project_name=PROJECT_NAME,
            program=lambda: None,
            opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
        )
        outputs = stack.outputs()
        return {k: v.value for k, v in outputs.items()}
    except auto.StackNotFoundError:
        raise KeyError(f"Stack {stack_name} not found")


def stack_list() -> list[dict]:
    """List all stacks in the project by reading S3 directly — fast, no Pulumi API calls."""
    key_prefix = f".pulumi/stacks/{PROJECT_NAME}/"
    try:
        response = _s3.list_objects_v2(Bucket=_BUCKET, Prefix=key_prefix)
        result = []
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            stack_name = key[len(key_prefix):-len(".json")]
            if stack_name.startswith("."):
                continue
            try:
                outputs = _get_stack_state_from_s3(stack_name)
            except (ClientError, KeyError) as e:
                logger.warning(f"Failed to get stack outputs for {stack_name!r}: {e}")
                outputs = {}
            result.append({"name": stack_name, "outputs": outputs})
        return result
    except Exception as exc:
        raise RuntimeError(f"Failed to list stacks from S3: {exc}")


def stack_destroy(stack_name: str) -> None:
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=PROJECT_NAME,
        program=lambda: None,
        opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
    )
    stack.destroy(on_output=print, show_secrets=False)
    stack.workspace.remove_stack(stack_name)










































#
#
# import os
# import json
# import boto3
# from botocore.exceptions import ClientError
# from typing import Callable
# from importlib.metadata import version
# from pulumi import automation as auto
# import logging
#
# logger = logging.getLogger(__name__)
#
# # Construct PulumiCommand once, explicitly, at import time — fails loudly here
# # with a real error if the CLI can't be found/run, instead of surfacing as a
# # cryptic None deep inside subprocess.Popen later.
# _pulumi_command = auto.PulumiCommand()
#
# _s3 = boto3.client("s3", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
# _BUCKET = os.getenv("PULUMI_BACKEND_URL", "").replace("s3://", "")
#
# PROJECT_NAME = "multicloud-networking"
# _AWS_PLUGIN_VERSION = f"v{version('pulumi-aws')}"
# _AZURE_PLUGIN_VERSION = f"v{version('pulumi-azure-native')}"
#
# _INPUT_ERRORS = (
#     "InvalidSubnet",
#     "does not exist",
#     "InvalidVpcID",
#     "InvalidParameterValue",
#     "InvalidVpnGatewayID",
#     "InvalidCustomerGatewayID",
#     "InvalidVpnConnectionID",
#     "ResourceGroupNotFound",
#     "InvalidResourceLocation",
#     "SubnetNotFound",
#     "VirtualNetworkNotFound",
# )
#
#
# def _stack_key(stack_name: str) -> str:
#     return f".pulumi/stacks/{PROJECT_NAME}/{stack_name}.json"
#
#
# def _get_stack_state_from_s3(stack_name: str) -> dict:
#     key = _stack_key(stack_name)
#     try:
#         obj = _s3.get_object(Bucket=_BUCKET, Key=key)
#         state = json.loads(obj["Body"].read())
#         outputs = {}
#         for resource in state.get("checkpoint", {}).get("latest", {}).get("resources", []):
#             if resource.get("type") == "pulumi:pulumi:Stack":
#                 outputs = resource.get("outputs", {})
#         return outputs
#     except ClientError as e:
#         if e.response["Error"]["Code"] == "NoSuchKey":
#             raise KeyError(f"Stack {stack_name} not found")
#         raise
#
#
# def stack_exists(stack_name: str) -> bool:
#     try:
#         _s3.head_object(Bucket=_BUCKET, Key=_stack_key(stack_name))
#         return True
#     except ClientError as e:
#         if e.response["Error"]["Code"] == "404":
#             return False
#         raise
#
#
# def stack_up(stack_name: str, program: Callable, plugin: str = "aws", config: dict | None = None) -> dict:
#     stack = auto.create_or_select_stack(
#         stack_name=stack_name,
#         project_name=PROJECT_NAME,
#         program=program,
#         opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
#     )
#     logger.info(f"Workspace pulumi_command: {stack.workspace.pulumi_command.command!r}")
#
#     if plugin == "aws":
#         stack.workspace.install_plugin(name="aws", version=_AWS_PLUGIN_VERSION)
#         region = (config.get("region") if config else None) or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
#         stack.set_config("aws:region", auto.ConfigValue(value=region))
#     else:
#         stack.workspace.install_plugin(name="azure-native", version=_AZURE_PLUGIN_VERSION)
#         location = (config.get("location") if config else None) or "eastus"
#         stack.set_config("azure-native:location", auto.ConfigValue(value=location))
#
#     if config:
#         for k, v in config.items():
#             if v is None:
#                 continue
#             stack.set_config(k, auto.ConfigValue(value=v))
#
#     result = stack.up(on_output=print)
#     return {k: v.value for k, v in result.outputs.items()}
#
#
# def stack_up_safe(stack_name: str, program: Callable, plugin: str = "aws", config: dict | None = None) -> dict:
#     try:
#         return stack_up(stack_name, program, plugin, config)
#     except Exception as exc:
#         msg = str(exc)
#         if any(k in msg for k in _INPUT_ERRORS):
#             raise ValueError(f"Invalid input: {msg}")
#         raise
#
#
# def stack_outputs(stack_name: str) -> dict:
#     return _get_stack_state_from_s3(stack_name)
#
#
# def stack_list() -> list[dict]:
#     """List all stacks in the project by reading S3 directly — fast, no Pulumi API calls."""
#     key_prefix = f".pulumi/stacks/{PROJECT_NAME}/"
#     try:
#         response = _s3.list_objects_v2(Bucket=_BUCKET, Prefix=key_prefix)
#         result = []
#         for obj in response.get("Contents", []):
#             key = obj["Key"]
#             if not key.endswith(".json"):
#                 continue
#             stack_name = key[len(key_prefix):-len(".json")]
#             if stack_name.startswith("."):
#                 continue
#             try:
#                 outputs = _get_stack_state_from_s3(stack_name)
#             except (ClientError, KeyError) as e:
#                 logger.warning(f"Failed to get stack outputs for {stack_name!r}: {e}")
#                 outputs = {}
#             result.append({"name": stack_name, "outputs": outputs})
#         return result
#     except Exception as exc:
#         raise RuntimeError(f"Failed to list stacks from S3: {exc}")
#
#
# def stack_destroy(stack_name: str) -> None:
#     stack = auto.create_or_select_stack(
#         stack_name=stack_name,
#         project_name=PROJECT_NAME,
#         program=lambda: None,
#         opts=auto.LocalWorkspaceOptions(pulumi_command=_pulumi_command),
#     )
#     stack.destroy(on_output=print)
#     stack.workspace.remove_stack(stack_name)