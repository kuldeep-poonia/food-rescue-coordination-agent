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
   - `RecordAuditEvent`: Conditional PutItem on `attribute_not_exists(idempotency_key)` on PK `idempotency_key`. Guarantees table-wide deduplication against replayed requests or Lambda retries.
   - `QueryDonationAuditTrail`: Query GSI `donation-audit-index` (`donation_id = :id` ordered by `timestamp` ASC). Used by coordinators to inspect the immutable chronological lifecycle.

---

## Deterministic Idempotency Key Strategy

To eliminate accidental collisions and prevent false-positive deduplication across different donations or operational steps, all idempotency keys are deterministically generated via `build_idempotency_key(donation_id, action, attempt_number)`:

```text
{donation_id}:{action}:{attempt_number}
```
- **`donation_id`**: Scopes the operation strictly to the target donation.
- **`action`**: Scopes the key to the specific state mutation (e.g. `ASSIGN_VOLUNTEER`, `MATCH_RECIPIENT`).
- **`attempt_number`**: Incremented when a fresh attempt is explicitly desired, ensuring retries of the same attempt deduplicate while deliberate re-runs succeed.

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
```
