"""
EKS node failure detector (Lambda).

Flow: EventBridge schedule -> Lambda -> EKS API (node conditions)
      + EC2 instance status checks -> DynamoDB (dedupe) -> SNS alert.
No third-party dependencies: boto3/botocore/urllib3 ship with the Lambda runtime.
"""
import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

import boto3
import urllib3
from botocore.signers import RequestSigner

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLUSTER = os.environ["CLUSTER_NAME"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
TABLE = os.environ["STATE_TABLE"]
HEARTBEAT_STALE_SECONDS = int(os.environ.get("HEARTBEAT_STALE_SECONDS", "120"))

PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

eks = boto3.client("eks", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

_http = None  # cached across warm invocations


# --------------------------------------------------------------------------- #
# EKS access
# --------------------------------------------------------------------------- #
def _eks_token(cluster_name: str) -> str:
    """Build a short-lived bearer token (same mechanism as `aws eks get-token`)."""
    session = boto3.session.Session()
    sts = session.client("sts", region_name=REGION,
                         endpoint_url=f"https://sts.{REGION}.amazonaws.com")
    signer = RequestSigner(sts.meta.service_model.service_id, REGION, "sts", "v4",
                           session.get_credentials(), session.events)
    params = {
        "method": "GET",
        "url": f"https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    url = signer.generate_presigned_url(params, region_name=REGION,
                                        expires_in=60, operation_name="")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _get_cluster_client():
    global _http
    info = eks.describe_cluster(name=CLUSTER)["cluster"]
    ca_file = os.path.join(tempfile.gettempdir(), "eks-ca.crt")
    with open(ca_file, "wb") as f:
        f.write(base64.b64decode(info["certificateAuthority"]["data"]))
    if _http is None:
        _http = urllib3.PoolManager(ca_certs=ca_file, timeout=urllib3.Timeout(connect=5, read=15))
    return _http, info["endpoint"]


def list_nodes():
    http, endpoint = _get_cluster_client()
    resp = http.request("GET", f"{endpoint}/api/v1/nodes",
                        headers={"Authorization": f"Bearer {_eks_token(CLUSTER)}"})
    if resp.status != 200:
        raise RuntimeError(f"K8s API returned {resp.status}: {resp.data[:300]!r}")
    return json.loads(resp.data)["items"]


# --------------------------------------------------------------------------- #
# Health evaluation
# --------------------------------------------------------------------------- #
def _parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def evaluate_node(node: dict) -> list:
    """Return a list of human-readable problems; empty list == healthy."""
    problems = []
    conditions = {c["type"]: c for c in node.get("status", {}).get("conditions", [])}

    ready = conditions.get("Ready")
    if ready is None:
        problems.append("No Ready condition reported")
    else:
        if ready["status"] != "True":
            problems.append(f"Ready={ready['status']} ({ready.get('reason', '')}: {ready.get('message', '')})")
        hb = _parse_ts(ready.get("lastHeartbeatTime"))
        if hb:
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            if age > HEARTBEAT_STALE_SECONDS:
                problems.append(f"Kubelet heartbeat stale ({int(age)}s)")

    for name in PRESSURE_CONDITIONS:
        c = conditions.get(name)
        if c and c["status"] == "True":
            problems.append(f"{name}=True ({c.get('reason', '')})")

    for taint in node.get("spec", {}).get("taints", []) or []:
        if taint.get("key") in ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready"):
            problems.append(f"Taint {taint['key']}:{taint.get('effect')}")

    return problems


def _instance_id(node: dict):
    pid = node.get("spec", {}).get("providerID", "")  # aws:///us-east-1a/i-0abc123
    return pid.rsplit("/", 1)[-1] if pid.startswith("aws:///") else None


def ec2_status(instance_ids: list) -> dict:
    """EC2 system/instance status checks, so we can tell infra faults from k8s faults."""
    result = {}
    if not instance_ids:
        return result
    for i in range(0, len(instance_ids), 100):
        resp = ec2.describe_instance_status(InstanceIds=instance_ids[i:i + 100],
                                            IncludeAllInstances=True)
        for s in resp["InstanceStatuses"]:
            result[s["InstanceId"]] = {
                "state": s["InstanceState"]["Name"],
                "system": s["SystemStatus"]["Status"],
                "instance": s["InstanceStatus"]["Status"],
            }
    return result


def ec2_problems(info):
    if not info:
        return []
    out = []
    if info["state"] != "running":
        out.append(f"EC2 state={info['state']}")
    if info["system"] not in ("ok", "initializing", "not-applicable"):
        out.append(f"EC2 system check={info['system']}")
    if info["instance"] not in ("ok", "initializing", "not-applicable"):
        out.append(f"EC2 instance check={info['instance']}")
    return out


# --------------------------------------------------------------------------- #
# State + notifications
# --------------------------------------------------------------------------- #
def _known_unhealthy() -> dict:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return {i["node"]: i for i in items}


def _notify(subject: str, body: str):
    sns.publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=body)


def lambda_handler(event, context):
    nodes = list_nodes()
    instance_ids = [i for i in (_instance_id(n) for n in nodes) if i]
    ec2_info = ec2_status(instance_ids)
    known = _known_unhealthy()

    unhealthy, seen = {}, set()
    for node in nodes:
        name = node["metadata"]["name"]
        seen.add(name)
        iid = _instance_id(node)
        problems = evaluate_node(node) + ec2_problems(ec2_info.get(iid))
        if problems:
            unhealthy[name] = {"instance_id": iid, "problems": problems,
                               "zone": node["metadata"]["labels"].get("topology.kubernetes.io/zone", "n/a")}

    # New or changed failures
    for name, d in unhealthy.items():
        signature = "|".join(sorted(d["problems"]))
        prev = known.get(name)
        if prev is None or prev.get("signature") != signature:
            _notify(f"[{CLUSTER}] Node FAILURE: {name}",
                    f"Cluster: {CLUSTER}\nNode: {name}\nInstance: {d['instance_id']}\n"
                    f"AZ: {d['zone']}\nProblems:\n  - " + "\n  - ".join(d["problems"]))
            table.put_item(Item={"node": name, "signature": signature,
                                 "instance_id": d["instance_id"] or "n/a",
                                 "since": prev["since"] if prev else datetime.now(timezone.utc).isoformat()})

    # Recovered or removed nodes
    for name, prev in known.items():
        if name not in unhealthy:
            state = "recovered" if name in seen else "no longer in the cluster (removed/replaced)"
            _notify(f"[{CLUSTER}] Node {name} {state}",
                    f"Node {name} ({prev.get('instance_id')}) is {state}. Unhealthy since {prev.get('since')}.")
            table.delete_item(Key={"node": name})

    summary = {"total_nodes": len(nodes), "unhealthy": list(unhealthy)}
    logger.info(json.dumps(summary))
    return summary
