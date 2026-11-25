# MultiCloud VPN & Global Load Balancer API

FastAPI + Pulumi Automation API service that connects an **AWS VPC** and an **Azure VNet** with a redundant site-to-site VPN (BGP, two tunnels) in a single API call, then puts an **AWS Network Load Balancer** in front of backends that live in either cloud.

Every piece of infrastructure is a separate **Pulumi stack** that the service creates, tracks and destroys for you. No `pulumi up` is run by hand.

> This is one of four related services. See [Related repositories](#related-repositories).

## Contents

- [How it works](#how-it-works) · [Authentication](#authentication) · [Quick start](#quick-start) · [Configuration](#configuration)
- [API reference](#api-reference) · [VPN orchestration](#vpn-orchestration) · [Global load balancer](#global-load-balancer)
- [Docker](#docker) · [Project layout](#project-layout) · [Security notes](#security-notes) · [Troubleshooting](#troubleshooting)

## How it works

```
                 +------------------- FastAPI (JWT-protected) -------------------+
client --------> |  /vpn/orchestrate        /aws/lb/*        /region, /location  |
                 +----------------+-----------------------------+----------------+
                                  | Pulumi Automation API       |
                    +-------------v-------------+     +---------v----------+
                    | AWS: VGW, CGW, VPN conn,  |     | AWS: NLB, target   |
                    | route propagation         |     | groups, listeners  |
                    | Azure: GatewaySubnet, VPN |     +--------------------+
                    | gateway, LNGs, connections|
                    +---------------------------+
                          stack state in an S3 bucket
```

- **Prerequisite stacks:** the service assumes an existing VPC (AWS) and VNet (Azure), plus route tables, already provisioned as stacks by the sibling services. It validates them up front and returns `422` with a precise message if anything is missing.
- **One stack per resource.** Dependent stacks refer to earlier ones **by name**, and the service resolves their outputs. You never paste raw cloud IDs between calls.
- **Long-running work is asynchronous.** VPN creation and teardown run as background jobs that you poll. A failed run rolls back everything it created.
- Stack state lives in S3 at `.pulumi/stacks/multicloud-networking/<stack>.json`.

## Authentication

Every endpoint except `/` and `/health` requires a JWT bearer token:

```
Authorization: Bearer <token>
```

The service **only validates** tokens. It does not issue them, so they come from whatever identity service you run. Requirements:

| Item | Value |
|---|---|
| Algorithm | `HS256` |
| Signing key | the `jwt_secret_key` environment variable, shared with the issuer |
| Required claims | `sub` (an integer user id) and `role` |

A missing, malformed, wrongly signed or incomplete token gets `401`. If `jwt_secret_key` is not set, protected routes return `500` rather than silently allowing access.

Mint a token for local testing:

```bash
python -c "from jose import jwt; print(jwt.encode({'sub': '1', 'role': 'admin'}, 'your-jwt-secret', algorithm='HS256'))"
export TOKEN=<paste the output>
```

Auth is applied where routers are mounted in `app/main.py`, so any router added there is protected automatically. The `role` claim is read but not yet used for authorization, so every valid token has full access.

## Quick start

Prerequisites: Python 3.12+, the [Pulumi CLI](https://www.pulumi.com/docs/install/), an AWS account and an Azure subscription.

```bash
git clone <this repo> && cd <this repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit the values
uvicorn main:app --reload
```

- Interactive docs: http://localhost:8000/docs (use **Authorize** to paste your token)
- Health check: `curl http://localhost:8000/health`

Create the S3 bucket for Pulumi state first and set it in `PULUMI_BACKEND_URL`. The app refuses to start if neither `PULUMI_BACKEND_URL` nor `PULUMI_ACCESS_TOKEN` is set, or if `PULUMI_CONFIG_PASSPHRASE` is missing.

## Configuration

All settings are environment variables, loaded from `.env`. Never commit `.env`.

| Variable | Required | Purpose |
|---|---|---|
| `PULUMI_BACKEND_URL` | one of these two | S3 state backend, e.g. `s3://my-pulumi-state-bucket` |
| `PULUMI_ACCESS_TOKEN` | one of these two | Pulumi Cloud token, used if no backend URL is set |
| `PULUMI_CONFIG_PASSPHRASE` | yes | Encrypts secrets in stack state. Set once and do not change it for existing stacks |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | yes | Provision AWS resources and read the state bucket |
| `AWS_DEFAULT_REGION` | yes | Default AWS region |
| `ARM_CLIENT_ID` / `ARM_CLIENT_SECRET` / `ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID` | yes | Azure service principal with Contributor rights |
| `jwt_secret_key` | yes | HS256 key used to validate API tokens. Use a long random value |
| `SLACK_WEBHOOK_URL` | no | Incoming webhook for notifications |

## API reference

All paths require the bearer token unless marked otherwise. Live documentation is served at `/docs` and `/redoc`.

| Method | Path | Description |
|---|---|---|
| POST / GET | `/region/{region}`, `/region` | Set / read the active AWS region |
| POST / GET | `/location/{location}`, `/location` | Set / read the active Azure location |
| POST | `/vpn/orchestrate` | Build the full AWS-Azure VPN (async, returns `202`) |
| GET | `/vpn/orchestrate` | List orchestrations found in stack state |
| GET | `/vpn/orchestrate/{name}` | Outputs of every stack in one orchestration |
| GET | `/vpn/orchestrate/{name}/status` | Background job status, current step, errors |
| DELETE | `/vpn/orchestrate/{name}` | Tear the VPN down (async, returns `202`) |
| GET, POST | `/aws/lb/target-group` | List / create target groups (may mix AWS and Azure IPs) |
| GET, PATCH | `/aws/lb/target-group/{stack}` | Read / replace the full target list |
| GET, POST | `/aws/lb/network-load-balancer` | List / create an NLB |
| GET | `/aws/lb/network-load-balancer/{stack}` | Read an NLB stack |
| GET, POST | `/aws/lb/listener` | List / create a listener with weighted target groups |
| GET | `/aws/lb/listener/{stack}` | Read a listener stack |
| DELETE | `/aws/lb/{stack}` | Destroy any load balancer stack |
| GET | `/`, `/health` | Root and liveness checks (no auth) |

Errors follow FastAPI's `{"detail": ...}` shape. Typical codes: `401` bad token, `404` unknown stack or job, `409` already exists or an operation is in progress, `422` a prerequisite is missing or invalid, `502` the cloud provider rejected a request.

## VPN orchestration

`POST /vpn/orchestrate` validates your existing VPC, VNet and route-table stacks, then creates the Azure GatewaySubnet and VPN gateway, the AWS virtual private gateway, customer gateway and VPN connection, wires BGP between them, adds a local network gateway and connection per tunnel, and enables route propagation on your route tables.

```bash
curl -X POST http://localhost:8000/vpn/orchestrate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "demo",
    "resource_group": "demo-rg",
    "vnet_stack": "demo-vnet",
    "gateway_subnet_address_prefix": "10.11.255.0/27",
    "vpc_stack": "demo-vpc",
    "route_table_stacks": ["demo-private-rt"]
  }'

curl http://localhost:8000/vpn/orchestrate/demo/status -H "Authorization: Bearer $TOKEN"
```

Creation is slow because Azure VPN gateways take a long time to provision, so poll the status endpoint. Full step list, request fields, rollback rules and teardown order: [`docs/VPN_WORKFLOW.md`](docs/VPN_WORKFLOW.md).

## Global load balancer

After the VPN is healthy, an internet-facing NLB can distribute traffic across backends in both clouds. Create a target group (Azure IPs need `"availability_zone": "all"`), then an NLB, then a listener that forwards to one or more target groups with relative weights. Details and gotchas: [`docs/VPN_WORKFLOW.md`](docs/VPN_WORKFLOW.md#load-balancer).

## Docker

```bash
docker build -t multicloud-global-lb .
docker run -p 8000:8000 --env-file .env multicloud-global-lb
```

The image is based on `pulumi/pulumi-python-3.12` and listens on port 8000. `entrypoint.sh` sets public DNS resolvers and starts uvicorn with a long keep-alive timeout, because stack operations can take minutes.

The included workflow `.github/workflows/cicd.yaml` is an example that builds the image and pushes it to Amazon ECR. It runs only on manual dispatch and needs the repository secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` and `ECR_REPOSITORY`.

## Project layout

```
main.py                  entry point (uvicorn main:app)
Pulumi.yaml              Pulumi project definition
app/
  main.py                FastAPI app, CORS, auth wiring, backend checks
  routes/
    vpn_orchestrator.py  /vpn/orchestrate (async jobs, validation, rollback)
    aws_lb.py            /aws/lb/* target groups, NLB, listeners
    aws_vpn.py           AWS VPN building blocks (called by the orchestrator)
    azure_vpn.py         Azure VPN building blocks (called by the orchestrator)
    region.py, location.py
  models/                Pydantic request and response models
  pulumi_programs/       Pulumi programs that define the cloud resources
  utils/
    auth.py              JWT bearer validation
    stack.py             create / destroy / list stacks, lock detection
    region_state.py, location_state.py, slack.py
docs/VPN_WORKFLOW.md     detailed orchestration and load balancer guide
```

## Security notes

- Keep `jwt_secret_key`, cloud credentials and `PULUMI_CONFIG_PASSPHRASE` in a secret manager or in environment variables. Never put them in the repo, and rotate any that were ever committed.
- The service holds credentials that can create and destroy network infrastructure in two clouds. Run it on a private network, put it behind TLS, and restrict who can obtain tokens.
- CORS currently allows only `http://localhost:3000`. Change `allow_origins` in `app/main.py` for your front end.
- Stack state is encrypted with `PULUMI_CONFIG_PASSPHRASE`, so protect that passphrase and the state bucket. Secret-tagged outputs are read from state as ciphertext, but any authenticated caller can still read the non-secret outputs of every stack.
- An internet-facing NLB passes client IPs through to targets, so review the target security groups and NSGs carefully.
- Authorization is all-or-nothing today. Enforce the `role` claim if you need least-privilege access.

## Troubleshooting

- **App exits at startup:** `PULUMI_BACKEND_URL` or `PULUMI_ACCESS_TOKEN`, or `PULUMI_CONFIG_PASSPHRASE`, is missing.
- **401 on every call:** the token is signed with a different key than `jwt_secret_key`, or lacks `sub` / `role`.
- **500 "Auth is not configured":** `jwt_secret_key` is not set in the service's environment.
- **422 on `/vpn/orchestrate`:** a prerequisite stack is missing or lacks an expected output. The message names it.
- **409 "Pulumi lock(s) held":** an earlier operation is still running or was interrupted. Wait, or clear the stale lock.
- **Status returns 404 after a restart:** job records are in memory. Use `GET /vpn/orchestrate/{name}` for real resource state.

## Related repositories

[multicloud-vpc-feat-branch](https://github.com/onyeechris/multicloud-vpc-feat-branch) (AWS networking, provides the VPC and route tables), [multicloud-vnet-branch](https://github.com/onyeechris/multicloud-vnet-branch) (Azure networking, provides the VNet), [multicloud-eks-ec2-branch](https://github.com/onyeechris/multicloud-eks-ec2-branch) (EC2 compute).
