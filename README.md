# Debugging Agent

An LLM-powered DevOps incident debugging agent that investigates PagerDuty alerts by querying AWS services (CloudWatch, Athena, CloudTrail, ALB, SQS, Kinesis, Firehose), correlates findings, and produces structured investigation reports.

## Adding Your Service to `config.yaml`

The service catalog lives in `config.yaml` under the `services:` key. Each entry tells the agent **which AWS resources belong to your service** so it can automatically query the right logs, metrics, and databases during an investigation.

### How to submit changes

1. Create a feature branch: `git checkout -b add-<your-service-name>`
2. Edit `config.yaml` — add or update your service entry under `services:`.
3. Push and raise a **Merge Request** with reviewers:
   - Add your team's designated reviewers here.


### Full template

```yaml
services:
  my-service:
    description: My service description
    architecture: "Request -> ALB (k8s-myservice) -> TG -> K8s pod -> RDS (mydb-cluster)"
    tags: [app]
    email_recipients:
      - team-lead@example.com
      - team-members@example.com

    cloudwatch:
      log_groups:
        - /aws/lambda/my-lambda-function
      regions:
        us-east-1:
          log_groups:
            - /aws/lambda/my-lambda-us-east-1

    athena:
      integ:
        database: 111111111111-integ-us-east-1-app-logging-db
        table: xf-myservice
      prod:
        database: 222222222222-prod-us-east-1-app-logging-db
        table: xf-myservice
      prod-eu:
        database: 333333333333-prod-eu-central-1-app-logging-db
        table: xf-myservice
      prod-ap:
        database: 444444444444-prod-ap-northeast-1-app-logging-db
        table: xf-myservice

    metrics:
      lambda_functions: [my-lambda-function]
      rds_clusters: [mydb-cluster]
      regions:
        us-east-1:
          alb:
            load_balancer: app/k8s-myservice-abc123/deadbeef1234
            target_groups:
              blue: targetgroup/k8s-myservice-blue-xyz/aabbccdd1234
          sqs_queues: [my-processing-queue, my-dlq]
          kinesis_streams: [my-data-stream]
          firehose_streams: [my-firehose]
        eu-central-1:
          alb:
            load_balancer: app/k8s-myservice-abc123/cafebabe5678
```

### Field reference

| Field | Description |
|-------|-------------|
| `description` | What the service does |
| `architecture` | Request flow, e.g. `"ALB -> TG -> K8s pod -> RDS"` |
| `tags` | Categorization: `app`, `infra`, `identity`, `events`, etc. |
| `email_recipients` | Who gets the investigation report email |
| `cloudwatch.log_groups` | Default CloudWatch log groups (all regions) |
| `cloudwatch.regions.<region>.log_groups` | Region-specific log group overrides |
| `athena.<env>.database` / `table` | Athena database + table per environment |
| `metrics.lambda_functions` | Lambda functions (shared across regions) |
| `metrics.rds_clusters` / `docdb_clusters` | RDS / DocumentDB clusters |
| `metrics.regions.<region>.alb` | ALB `load_balancer` + `target_groups` per region |
| `metrics.regions.<region>.sqs_queues` | SQS queues per region |
| `metrics.regions.<region>.kinesis_streams` | Kinesis streams per region |
| `metrics.regions.<region>.firehose_streams` | Firehose streams per region |

### Athena environment keys

| Key | Environment |
|-----|-------------|
| `integ` | Integration (us-east-1) |
| `integ-eu` | Integration (eu-central-1) |
| `prod` | Production (us-east-1) |
| `prod-eu` | Production (eu-central-1) |
| `prod-ap` | Production (ap-northeast-1) |

### Metrics — shared vs region-specific

- **Shared fields** (`metrics.lambda_functions`, `metrics.rds_clusters`, etc.) apply to all regions.
- **Region overrides** (`metrics.regions.<region>.*`) take precedence when present. ALBs always differ by region.

## How It Works

```
User Query / PagerDuty Alert
            │
            ▼
      Agent Core (ReAct loop)
            │
  ┌─────────┼─────────┐
  ▼         ▼         ▼
LLM       Tools     Config
          (AWS)    (config.yaml)
```

1. Receives a query with service name, environment, region, and time window.
2. Resolves matching AWS resources from `config.yaml`.
3. Investigates outside-in: ALB → logs → metrics → change history.
4. Produces a root cause analysis with evidence.

### Tools

| Tool | Description |
|------|-------------|
| `athena` | Query application logs via Athena SQL |
| `cloudwatch_logs` | Tail CloudWatch log groups |
| `cloudwatch_metrics` | Fetch CloudWatch metrics (ALB, Lambda, RDS, etc.) |
| `cloudtrail` | Search recent API activity / deployments |
| `describe_alb` | Inspect ALB target group health |
| `describe_sqs` | Check SQS queue attributes and DLQ counts |
| `describe_kinesis` | Check Kinesis stream status |
| `describe_firehose` | Check Firehose delivery stream status |
| `list_services` | List all configured services |
| `publish_confluence` | Publish report to Confluence |
| `send_email` | Send email notifications |


## Installation & Local Run

```bash
git clone https://github.com/humblepoc/cloud-services-debugging-agent.git
cd cloud-services-debugging-agent
pip install -e ".[dev]"
```

### Running

```bash
agent                          # Interactive mode
agent -q "Investigate ..."     # One-shot mode
```

### Credentials

- **AWS**: loaded from default profile.
- **LLM**: add your AGENT_API_KEY, AGENT_BASE_URL, AGENT_LLM_PROVIDER, AGENT_LLM_MODEL in .env file.

---

## Testing

```bash
pytest
```
