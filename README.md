# eks-node-watch

Lambda-based Kubernetes (EKS) node failure detector.

## How it works
1. EventBridge invokes the Lambda every minute.
2. Lambda gets a token via STS and calls `GET /api/v1/nodes` on the EKS API.
3. Each node is checked for: `Ready != True`, stale kubelet heartbeat, Memory/Disk/PID/Network
   pressure, and `unreachable` / `not-ready` taints.
4. EC2 system/instance status checks are added, which separates AWS host faults from kubelet faults.
5. DynamoDB stores the last alert per node, so you get one alert per failure, one on change, and one on recovery.
6. Alerts go to SNS (email by default; wire it to Slack/PagerDuty/Chatbot as needed).

## Deploy
    cd terraform
    terraform init
    terraform apply -var cluster_name=my-cluster -var alert_email=you@example.com -var region=ap-south-1

Confirm the SNS email subscription afterwards.

## Notes
- Private-only API endpoint: set `subnet_ids` / `security_group_ids` (SG must reach the
  cluster SG on 443, and the subnets need NAT or VPC endpoints for STS, EKS, EC2, SNS, DynamoDB).
- Cluster auth mode must be `API` or `API_AND_CONFIG_MAP` for access entries.
- Cadence: `rate(1 minute)` costs well under $1/month in Lambda.
- Testing: cordon-drain a node and stop kubelet (`systemctl stop kubelet`) on a test node, then wait ~1-2 minutes.
