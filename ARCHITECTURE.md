# System Architecture — Surplus Router (Good Neighbor Agent)

Surplus Router is an autonomous coordination agent built on the **Strands Agents SDK** and deployed on **Amazon Bedrock AgentCore**. It is architected for the **Good Neighbor Agents** track to eliminate the daily operational busywork of community food recovery by autonomously matching surplus food from commercial donors to community recipient organizations (shelters, soup kitchens, food pantries) and dispatching transit volunteers.

---

## 1. High-Level Architecture

The system operates as an autonomous event-driven state machine where human intervention is reserved exclusively for unrecoverable exceptions and safety-critical threshold breaches.

```mermaid
flowchart TB
    subgraph Clients["Zero-IDOR Client Portals"]
        D[("📦 Commercial Donor<br/>(Restaurant/Bakery)")]
        R[("🏢 Recipient Partner<br/>(Shelter/Soup Kitchen)")]
        V[("🚲 Transit Volunteer<br/>(Vehicle/Bicycle)")]
        C[("🛡️ City Coordinator<br/>(Human-in-the-Loop)")]
    end

    subgraph Gateway["Security Gateway & Edge Routing"]
        APIGW["REST API / HTTP Gateway<br/>(Rate Limiting & Security Headers)"]
        AUTH["Zero-IDOR Capability Token Engine<br/>(Salted SHA-256 Constant-Time)"]
    end

    subgraph Core["Strands AgentCore Orchestrator"]
        ORCH["Strands Orchestrator<br/>(agent/orchestrator.py)"]
        CLASS["Tool: classify_donation<br/>(Urgency & Perishability)"]
        CAP["Tool: get_recipient_capacity<br/>(Live Intake Capacity)"]
        MATCH["Tool: find_best_match<br/>(Multi-Factor Deterministic Matcher)"]
        VOL["Tool: assign_volunteer<br/>(Idempotent Fleet Dispatch)"]
        ESCAL["Tool: flag_for_human<br/>(Transactional Escalation Queue)"]
    end

    subgraph Data["AWS Cloud Persistence Layer"]
        DDB_DON[("DynamoDB: frca-donations-dev<br/>PK: donation_id")]
        DDB_REC[("DynamoDB: frca-recipients-dev<br/>PK: recipient_id")]
        DDB_VOL[("DynamoDB: frca-volunteers-dev<br/>PK: volunteer_id")]
        DDB_AUD[("DynamoDB: frca-matches-audit-dev<br/>PK: event_id")]
        DDB_MEM[("DynamoDB: frca-sessions-memory-dev<br/>PK: session_id")]
    end

    subgraph External["AWS Managed Services"]
        ALS["Amazon Location Service<br/>(Matrix Distance & Routing)"]
        BEDROCK["Amazon Bedrock AgentCore<br/>(Foundation Model Runtime)"]
        CW["Amazon CloudWatch<br/>(Structured JSON Logs & Audit Alarms)"]
    end

    %% Wiring
    D -->|"POST /api/donations"| APIGW
    R -->|"POST /api/recipients/{id}/capacity"| APIGW
    V -->|"GET /api/volunteers/{id}/assignments"| APIGW
    C -->|"POST /api/coordinator/login"| APIGW

    APIGW --> AUTH
    AUTH --> ORCH

    ORCH --> CLASS
    ORCH --> CAP
    ORCH --> MATCH
    ORCH --> VOL
    ORCH --> ESCAL

    MATCH --> ALS
    ORCH -.-> BEDROCK

    CAP --> DDB_REC
    MATCH --> DDB_DON
    MATCH --> DDB_REC
    VOL --> DDB_VOL
    ORCH --> DDB_AUD
    ORCH --> DDB_MEM

    ORCH -.-> CW
    ESCAL ==>|"Alerts on Exception"| C
```

---

## 2. End-to-End Coordination Lifecycle

When a commercial donor reports surplus food, the agent coordinates the entire match within milliseconds using atomic database transactions.

```mermaid
sequenceDiagram
    autonumber
    actor Donor as Commercial Donor
    participant API as Security Gateway
    participant Orch as Strands Orchestrator
    participant Matcher as Multi-Factor Matcher
    participant DDB as AWS DynamoDB
    actor Volunteer as Transit Volunteer
    actor Shelter as Recipient Shelter
    actor Coordinator as Human Coordinator

    Donor->>API: POST /api/donations (Food details, ready_by, GPS lat/lon)
    API->>API: Generate Cryptographic Capability Token (Salted SHA-256)
    API->>DDB: PutItem (frca-donations-dev, Status: REPORTED)
    API->>Orch: coordinate_donation(donation_id)
    
    rect rgb(240, 249, 255)
        Note over Orch,Matcher: Step 1: Autonomous Classification
        Orch->>Orch: classify_donation (Compute perishability & urgency window)
    end

    rect rgb(236, 253, 245)
        Note over Orch,Matcher: Step 2: Intelligent Multi-Factor Matching
        Orch->>DDB: Query Active Recipients in Region (frca-recipients-dev)
        Orch->>Matcher: find_best_match(Donation, Candidates)
        Matcher->>Matcher: Evaluate: Distance (35%), Capacity (25%), Dietary (20%), Urgency (20%)
        Matcher-->>Orch: Ranked Top Candidate (Score: 0.92, Reason: "Closest verified shelter with 150kg intake")
    end

    alt Match Found & Capacity Available
        rect rgb(245, 243, 255)
            Note over Orch,DDB: Step 3: ACID Atomic Allocation
            Orch->>DDB: TransactWriteItems: Deduct Recipient Capacity & Lock Donation
            Orch->>DDB: assign_volunteer (Idempotent Transit Allocation)
            Orch->>DDB: PutItem (frca-matches-audit-dev, Action: MATCH_CONFIRMED)
        end
        Orch-->>API: Status: MATCHED & ASSIGNED
        API-->>Donor: HTTP 201 Created + Private Capability Token
        Orch-->>Volunteer: Mission Assigned (Pickup at Bakery -> Deliver to Shelter)
        Orch-->>Shelter: Delivery Incoming Notification (25 kg hot meals arriving)
    else Safety Threshold Breach or No Match Within Safe Window
        rect rgb(254, 242, 242)
            Note over Orch,Coordinator: Step 4: Human-in-the-Loop Escalation
            Orch->>DDB: UpdateItem (Status: ESCALATED, Reason: NO_MATCH_WITHIN_WINDOW)
            Orch->>DDB: PutItem (frca-matches-audit-dev, Action: ESCALATION_TRIGGERED)
            Orch->>Coordinator: Trigger Pager / Command Center Alert
            Coordinator->>API: POST /api/coordinator/escalations/{id}/resolve (Manual Override)
        end
    end
```

---

## 3. Multi-Factor Matching Algorithm

The agent uses a mathematically rigorous, deterministic scoring formula that prevents arbitrary decisions and guarantees auditable outcomes:

$$\text{Total Score} = w_{\text{dist}} \cdot S_{\text{dist}} + w_{\text{cap}} \cdot S_{\text{cap}} + w_{\text{diet}} \cdot S_{\text{diet}} + w_{\text{urg}} \cdot S_{\text{urg}}$$

### Scoring Parameters & Weights:

| Factor | Weight ($w$) | Objective | Metric / Function |
| :--- | :--- | :--- | :--- |
| **Distance ($S_{\text{dist}}$)** | **0.35** | Minimize transit time & fuel | Linear inverse decay over service radius ($1.0 - \frac{d}{d_{\text{max}}}$) |
| **Capacity ($S_{\text{cap}}$)** | **0.25** | Prevent over-allocation / waste | $\frac{\text{capacity\_remaining}}{\text{capacity\_total}}$, penalized if donation $> \text{remaining}$ |
| **Dietary Fit ($S_{\text{diet}}$)** | **0.20** | Respect shelter food constraints | 1.0 (Exact match), 0.5 (Compatible), 0.0 (Excluded category) |
| **Urgency ($S_{\text{urg}}$)** | **0.20** | Prioritize highly perishable food | Scaled by remaining hours before food safety boundary |

### Deterministic Tie-Breaking:
1. Higher Composite Score wins.
2. Equal score $\rightarrow$ Lower distance wins.
3. Equal distance $\rightarrow$ Lexicographical `recipient_id` (guarantees 100% deterministic test reproducibility).

---

## 4. DynamoDB Schema & ACID Concurrency Model

All operational tables utilize single-digit millisecond latency access patterns:

```mermaid
erDiagram
    DONATIONS {
        string donation_id PK "e.g. don-7ca4a8a358bb"
        string donor_id "Restaurant identifier"
        string donor_name "Commercial entity name"
        string donor_phone "E.164 sanitized phone"
        string status "REPORTED | MATCHED | ASSIGNED | DELIVERED | ESCALATED"
        decimal quantity_kg "Weight of surplus food"
        string food_category "prepared_meals | produce | bakery | dairy | meat | packaged"
        string service_region "metro-core"
        string tracking_token_hash "SHA-256 capability digest"
        string matched_recipient_id FK "Matched shelter ID"
        string assigned_volunteer_id FK "Assigned transit driver ID"
        timestamp ready_by "ISO-8601 Future timestamp"
    }

    RECIPIENTS {
        string recipient_id PK "e.g. rec-soup-kitchen-01"
        string organization_name "Nonprofit shelter title"
        decimal capacity_kg_remaining "Current real-time capacity"
        string status "ACTIVE | INACTIVE"
        list dietary_requirements "Approved food categories"
        list dietary_exclusions "Forbidden food types"
        map coordinates "Lat & Long decimal values"
        string service_region "metro-core"
    }

    VOLUNTEERS {
        string volunteer_id PK "e.g. vol-car-01"
        string volunteer_name "Transit operator"
        string status "AVAILABLE | BUSY | OFFLINE"
        decimal max_capacity_kg "Vehicle limit"
        string vehicle_type "car | van | bicycle"
        string service_region "metro-core"
    }

    AUDIT_LOG {
        string event_id PK "UUID v4"
        string donation_id FK "Associated donation"
        string action "MATCH_CONFIRMED | ESCALATED | RESOLVED"
        string actor "AGENT | COORDINATOR"
        string idempotency_key "Unique execution digest"
        map details "Input snapshot & matching rationale"
        timestamp timestamp "UTC timestamp"
    }

    DONATIONS ||--o| RECIPIENTS : "matched to"
    DONATIONS ||--o| VOLUNTEERS : "transported by"
    DONATIONS ||--|{ AUDIT_LOG : "audited by"
```

### Double-Allocation Prevention (DynamoDB `TransactWriteItems`):
To prevent race conditions where two simultaneous donations claim the same shelter beyond its capacity:
```python
# Atomic Transaction:
# 1. Update Donation with matched_recipient_id (Condition: matched_recipient_id is NULL)
# 2. Deduct Recipient remaining capacity (Condition: capacity_kg_remaining >= donation_qty)
client.transact_write_items(
    TransactItems=[
        {
            "Update": {
                "TableName": "frca-donations-dev",
                "Key": {"donation_id": {"S": donation_id}},
                "UpdateExpression": "SET #st = :m_st, #rec = :rec_id",
                "ConditionExpression": "attribute_not_exists(matched_recipient_id) OR matched_recipient_id = :null_val",
                ...
            }
        },
        {
            "Update": {
                "TableName": "frca-recipients-dev",
                "Key": {"recipient_id": {"S": recipient_id}},
                "UpdateExpression": "SET #cap = #cap - :qty",
                "ConditionExpression": "#cap >= :qty AND #st = :active",
                ...
            }
        }
    ]
)
```

---

## 5. Security Architecture (Zero-IDOR & PII Redaction)

### Cryptographic Capability Tokens
* **No Sequential IDs:** Public endpoints never expose auto-incrementing integers.
* **Token Issuance:** Upon submission, donors receive an unguessable 256-bit entropy token (`token_urlsafe(32)`).
* **Salted SHA-256 Storage:** Only the cryptographic digest is stored in DynamoDB. Incoming lookup tokens are validated using **constant-time equality** (`secrets.compare_digest`) to completely prevent timing attacks.

### Strict PII Masking:
* Donor phone numbers and exact addresses are scrubbed from system logs using regex-based structured redaction (`redaction.py`).
* Volatile coordinates are restricted to verified transit drivers and the coordinator console.

---

## 6. Human-in-the-Loop Escalation Boundaries

The autonomous agent is bounded by strict guardrails and deliberately surfaces exceptions to the human coordinator when automated safety cannot be guaranteed:

```mermaid
stateDiagram-v2
    [*] --> REPORTED: Donor reports surplus

    REPORTED --> MATCHED: Best match found (Score >= 0.70 & Capacity Valid)
    REPORTED --> ESCALATED: No match within safe time window
    REPORTED --> ESCALATED: Remaining shelf-life < 60 minutes (Safety Breach)
    REPORTED --> ESCALATED: Concurrent claim conflict detected

    MATCHED --> ASSIGNED: Volunteer vehicle capacity confirmed
    ASSIGNED --> PICKED_UP: Volunteer checks in at kitchen
    PICKED_UP --> DELIVERED: Drop-off confirmed at shelter
    DELIVERED --> CLOSED: Audit log sealed

    ESCALATED --> ASSIGNED: Coordinator Manual Override
    ESCALATED --> CLOSED: Coordinator Cancels / Reroutes
```

1. **Safety Threshold Breach:** If remaining shelf-life is below 60 minutes at match time, the agent halts auto-dispatch and pings the coordinator.
2. **Exhausted Regional Capacity:** If no active shelter has remaining kg capacity, the donation is placed on the priority triage queue.
3. **Audit Immutability:** Every decision—whether automated or manual—is committed to `frca-matches-audit-dev` for post-event analytics.
