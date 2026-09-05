# AWS Deployment & Setup Guide — Data Layer

This guide provides step-by-step instructions for deploying and verifying the DynamoDB tables and least-privilege IAM roles for the Food Rescue Coordination Agent.

---

## Method 1: Automated Deployment via AWS CLI (Recommended)

### 1. Prerequisites
Ensure the AWS CLI is installed and configured with appropriate administrative permissions:
```bash
aws sts get-caller-identity
```

### 2. Deploy CloudFormation Stack
Run the deployment command from the project root:
```bash
aws cloudformation deploy \
  --template-file infra/dynamodb_tables.yaml \
  --stack-name frca-data-layer-dev \
  --parameter-overrides EnvironmentName=dev \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

### 3. Retrieve Table Names & Role ARN
Query stack outputs to obtain resource names:
```bash
aws cloudformation describe-stacks \
  --stack-name frca-data-layer-dev \
  --query "Stacks[0].Outputs" \
  --output table
```

---

## Method 2: Manual Deployment via AWS Management Console

If you prefer using the AWS Web Console:

1. **Navigate to CloudFormation**:
   - Open [AWS CloudFormation Console](https://console.aws.amazon.com/cloudformation/home?region=us-east-1).
   - Ensure the desired Region is selected (e.g., `us-east-1`).

2. **Create Stack**:
   - Click the **Create stack** button and choose **With new resources (standard)**.
   - Under **Prerequisite - Prepare template**, select **Template is ready**.
   - Under **Specify template**, select **Upload a template file**.
   - Click **Choose file** and upload `infra/dynamodb_tables.yaml`.
   - Click **Next**.

3. **Specify Stack Details**:
   - **Stack name**: `frca-data-layer-dev`
   - **EnvironmentName**: `dev`
   - Click **Next**.

4. **Configure Stack Options**:
   - Leave defaults unchanged (Tags, Permissions, Rollback configuration).
   - Click **Next**.

5. **Review & Acknowledge**:
   - Scroll to the bottom **Capabilities** section.
   - Check the box: *"I acknowledge that AWS CloudFormation might create IAM resources with custom names."*
   - Click **Submit**.

6. **Verify Creation**:
   - The stack status will display `CREATE_IN_PROGRESS` and transition to `CREATE_COMPLETE` within 1–2 minutes.
   - Open the **Outputs** tab to view the generated table names.

---

## Method 3: Direct Manual Table Creation in DynamoDB Console

If CloudFormation is not permitted in your environment, create each table manually in the [Amazon DynamoDB Console](https://console.aws.amazon.com/dynamodbv2/):

### Table 1: Donations
- **Table name**: `frca-donations-dev`
- **Partition key**: `donation_id` (String)
- **Table class / Capacity**: On-demand (`PAY_PER_REQUEST`)
- **Encryption**: AWS-managed KMS (Default)
- **Global Secondary Index (GSI)**:
  - **Index name**: `status-ready_by-index`
  - **Partition key**: `status` (String)
  - **Sort key**: `ready_by` (String)
  - **Attribute projection**: All attributes

### Table 2: Recipients
- **Table name**: `frca-recipients-dev`
- **Partition key**: `recipient_id` (String)
- **Capacity**: On-demand
- **Global Secondary Index (GSI)**:
  - **Index name**: `region-status-index`
  - **Partition key**: `service_region` (String)
  - **Sort key**: `status` (String: `"ACTIVE"` for active, `"INACTIVE"` for inactive)
  - **Attribute projection**: All attributes

### Table 3: Volunteers
- **Table name**: `frca-volunteers-dev`
- **Partition key**: `volunteer_id` (String)
- **Capacity**: On-demand
- **Global Secondary Index (GSI)**:
  - **Index name**: `region-status-index`
  - **Partition key**: `service_region` (String)
  - **Sort key**: `status` (String: `"AVAILABLE"` for available, `"UNAVAILABLE"` for unavailable)
  - **Attribute projection**: All attributes

### Table 4: Matches & Audit Log
- **Table name**: `frca-matches-audit-dev`
- **Partition key**: `idempotency_key` (String)
- **Table class / Capacity**: On-demand (`PAY_PER_REQUEST`)
- **Encryption**: AWS-managed KMS (Default)
- **Point-in-Time Recovery**: Enabled
- **Global Secondary Index (GSI)**:
  - **Index name**: `donation-audit-index`
  - **Partition key**: `donation_id` (String)
  - **Sort key**: `timestamp` (String)
  - **Attribute projection**: All attributes

---

## Exact Access Patterns & Index Design

Every index in the data layer maps 1:1 to an operational access pattern:

1. **Donations Table**:
   - `GetDonation`: Strongly consistent read by `donation_id` (PK). Used for atomic claim state checks.
   - `QueryUnmatchedDonations`: Query GSI `status-ready_by-index` (`status = 'reported'` ordered by `ready_by` ASC). Used by background evaluation rules to discover donations approaching deadline.

2. **Recipients Table**:
   - `GetRecipient`: Strongly consistent read by `recipient_id` (PK). Used for real-time capacity and dietary checks.
   - `QueryActiveRecipients`: Query GSI `region-status-index` (`service_region = :region AND status = 'ACTIVE'`). Used by the matching algorithm to fetch all active candidates in the operational zone.

3. **Volunteers Table**:
   - `GetVolunteer`: Strongly consistent read by `volunteer_id` (PK). Used for atomic volunteer availability verification.
   - `QueryAvailableVolunteers`: Query GSI `region-status-index` (`service_region = :region AND status = 'AVAILABLE'`). Used during assignment to discover unassigned drivers in the operational zone.

4. **Matches & Audit Log Table**:
   - `RecordAuditEvent`: Conditional PutItem on `attribute_not_exists(idempotency_key)` on PK `idempotency_key`. Guarantees table-wide deduplication against replayed requests or Lambda retries. When a duplicate key is encountered, returns `False`, which callers must treat as a successful no-op (never retry or escalate).
   - `QueryDonationAuditTrail`: Query GSI `donation-audit-index` (`donation_id = :id` ordered by `timestamp` ASC). Used by coordinators to inspect the immutable chronological lifecycle.

---

## Deterministic Idempotency Key Strategy

To eliminate accidental collisions and prevent false-positive deduplication across different donations or operational steps, all idempotency keys are deterministically generated via `build_idempotency_key(donation_id, action)`:

```text
{donation_id}:{action}
```
- **`donation_id`**: Scopes the operation strictly to the target donation.
- **`action`**: Scopes the key to the specific state mutation (e.g. `ASSIGN_VOLUNTEER`, `MATCH_RECIPIENT`).

This format ensures that automatic Lambda retries, network timeouts, or crash replays for the exact same logical operation always produce the exact same key, allowing DynamoDB's conditional check `attribute_not_exists(idempotency_key)` to reliably deduplicate side effects without spurious re-execution.

---

## Read Consistency Policy

To prevent concurrency flakiness and race conditions:
- **Matching-Critical Reads**: All operational queries (`get_donation`, `get_recipient`, `get_volunteer`) default strictly to **Strongly Consistent Reads** (`ConsistentRead=True`). This guarantees that concurrent claims or rapid capacity deductions never read stale replica state.
- **Reporting & Telemetry Reads**: Non-critical reads (e.g. coordinator overview dashboard statistics) can explicitly pass `consistent_read=False` to minimize read capacity unit consumption.

---

## Environment Configuration & Table Name Resolution

Per project security rules, table names, topic ARNs, and AWS regions are **never hardcoded as string literals** in repositories or business logic. All repository classes (`DonationsRepository`, `RecipientsRepository`, etc.) initialize via `config.py` (`load_app_configuration()`):

```bash
export AWS_REGION=us-east-1
export DONATIONS_TABLE_NAME=frca-donations-dev
export RECIPIENTS_TABLE_NAME=frca-recipients-dev
export VOLUNTEERS_TABLE_NAME=frca-volunteers-dev
export MATCHES_AUDIT_TABLE_NAME=frca-matches-audit-dev
export SESSIONS_MEMORY_TABLE_NAME=frca-sessions-memory-dev
export COORDINATOR_DLQ_URL=https://sqs.us-east-1.amazonaws.com/123456789012/frca-coordinator-dlq-dev
```

---

## AgentCore Runtime: Container Reuse & Bedrock Schema Compliance

### 1. Module-Level Singleton Caching (Stateless Guarantee)

To guarantee sub-second cold starts and optimal warm-container reuse in AWS Lambda:
- **Cached Objects (Stateless Only)**:
  - `_CONFIG`: Immutable application configuration (`AppConfig`).
  - `_SQS_CLIENT`: Boto3 SQS client connection for dead-letter queuing.
  - `_SESSION_MANAGER` & `_MEMORY_STORE`: Class instances maintaining only boto3 Table resources (`self._table`).
  - `_ORCHESTRATOR`: Orchestrator instance holding stateless repository client handles.
- **Strictly Excluded from In-Memory Caching (Zero Business State)**:
  - **No `SessionContext`**, no `Donation`, and no recipient capacity/workload data is ever cached at the Lambda container level.
  - Every invocation performs a **fresh, strongly consistent read** from DynamoDB (`ConsistentRead=True`).
  - Session counters are mutated exclusively via DynamoDB atomic operations (`ADD total_donations_processed :one...`), eliminating the possibility of lost updates or container-level staleness across concurrent warm invocations.

### 2. Bedrock Agent Action Group Response Schema Compliance

The Lambda runtime (`agent/runtime.py`) formats all handler responses—including graceful throttling degradations—strictly according to the AWS Bedrock Agent Action Group response specification:

```json
{
  "messageVersion": "1.0",
  "response": {
    "actionGroup": "<actionGroup>",
    "apiPath": "<apiPath>",
    "httpMethod": "POST",
    "httpStatusCode": 429,
    "responseBody": {
      "application/json": {
        "body": "{\"status\": \"QUEUED_FOR_COORDINATOR\", \"reason\": \"THROTTLING_DEGRADATION\", \"message\": \"...\", \"error\": \"ThrottlingException\"}"
      }
    }
  }
}
```

- **No Raw HTTP Wrappers**: Bedrock AgentCore action groups reject raw HTTP responses (e.g. `{statusCode: 429, body: ...}`). All responses in FRCA are strongly typed via Pydantic model `AgentCoreRuntimeResponse` and wrapped inside the `response` dictionary.
- **Upstream Compatibility**: Bedrock Runtime parses the response cleanly without schema errors, enabling the agent loop to gracefully acknowledge the queued status and forward the payload to coordinator dashboards.

