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
  - **Sort key**: `is_active` (Number: `1` for active, `0` for inactive)
  - **Attribute projection**: All attributes

### Table 3: Volunteers
- **Table name**: `frca-volunteers-dev`
- **Partition key**: `volunteer_id` (String)
- **Capacity**: On-demand
- **Global Secondary Index (GSI)**:
  - **Index name**: `region-status-index`
  - **Partition key**: `service_region` (String)
  - **Sort key**: `is_available` (Number: `1` for available, `0` for unavailable)
  - **Attribute projection**: All attributes

### Table 4: Matches & Audit Log
- **Table name**: `frca-matches-audit-dev`
- **Partition key**: `donation_id` (String)
- **Sort key**: `event_id` (String)
- **Capacity**: On-demand

---

## Local Development Configuration

When running against the deployed AWS tables, configure the following environment variables:
```bash
export AWS_REGION=us-east-1
export DONATIONS_TABLE_NAME=frca-donations-dev
export RECIPIENTS_TABLE_NAME=frca-recipients-dev
export VOLUNTEERS_TABLE_NAME=frca-volunteers-dev
export MATCHES_AUDIT_TABLE_NAME=frca-matches-audit-dev
```
