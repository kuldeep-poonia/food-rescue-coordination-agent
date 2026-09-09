# Agents for Humans: Building Surplus Router — An Autonomous Food Rescue Agent with Strands SDK & AWS

> **Submission for the AWS "Agents for Humans" Hackathon (Track: Good Neighbor Agents)**  
> *Published by the Surplus Router Team on AWS Builder Center (`builder.aws.com`)*

---

## 🌟 Introduction: The Food Rescue Paradox

Every single day, commercial kitchens, bakeries, corporate cafeterias, and grocery stores produce hundreds of kilograms of wholesome, safe, high-quality surplus food. At the exact same time, nearby homeless shelters, soup kitchens, and orphanages struggle with tight budgets and food insecurity.

Connecting these two groups should be effortless. In reality, it is a **chaotic, exhausting logistics nightmare**:
* Non-profit community coordinators lose **3 to 5 hours daily** juggling endless phone calls, unorganized WhatsApp groups, and spreadsheets.
* Perishable cooked food spoils within **3 to 4 hours** while coordinators frantically search for a shelter with refrigerator space and a driver who is free.
* When coordination lags, **edible food is dumped into landfills**, emitting harmful methane while vulnerable community members go hungry.

When AWS announced the **"Agents for Humans" Hackathon** with the **Good Neighbor Agents** track, we saw an opportunity to rethink community food logistics from first principles. 

Instead of building another mobile app that people must constantly monitor, we built **Surplus Router**: an **autonomous, background coordination agent** using the **Strands Agents SDK** and deployed on **Amazon Bedrock AgentCore**. It runs 24/7 in the background—handling routine surplus intake, dynamic capacity deduction, and volunteer dispatch—and surfaces to a human coordinator **only when there is a genuine exception that requires human judgment**.

---

## 🏗️ System Architecture: How We Used AWS Services

Here is how Surplus Router coordinates community food rescue using AWS cloud-native primitives:

```
+-----------------------------------------------------------------------------------+
|                            COMMERCIAL FOOD DONORS                                |
|         Restaurants, Bakeries, Supermarkets, Caterers (30s Mobile Intake)         |
+-----------------------------------------------------------------------------------+
                                         │
                                         ▼ (POST /api/donations)
+-----------------------------------------------------------------------------------+
|                         AMAZON BEDROCK AGENTCORE ENGINE                           |
|  +-----------------------------------------------------------------------------+  |
|  |               Strands Agents SDK Autonomous Orchestrator Loop               |  |
|  +-----------------------------------------------------------------------------+  |
|         │                           │                            │                |
|         ▼                           ▼                            ▼                |
|  [Classification Tool]    [Multi-Factor Matcher]     [Volunteer Dispatcher]      |
|  (Perishability & Fit)    (Capacity & Proximity)     (Shift & Cargo Matching)     |
+-----------------------------------------------------------------------------------+
         │                           │                            │
         ▼                           ▼                            ▼
+-----------------------------------------------------------------------------------+
|                            AWS CLOUD DATA & ROUTING                               |
|  • Amazon DynamoDB: 5 Tables with ACID Conditional Transactions                   |
|    - frca-donations-dev  (Lifecycle states & TTL)                                 |
|    - frca-recipients-dev (Atomic capacity locking via TransactWriteItems)         |
|    - frca-volunteers-dev (Driver availability & active assignments)               |
|    - frca-matches-audit-dev (Immutable compliance audit log)                     |
|    - frca-sessions-memory-dev (Day-scoped session state)                          |
|  • Amazon Location Service: Authoritative road-distance matrix (25km service zone) |
|  • Amazon SNS & SQS DLQ: Transactional push dispatches & dead-letter failover      |
|  • Amazon CloudWatch: Sanitized JSON telemetry (Automated PII scrubbing)          |
+-----------------------------------------------------------------------------------+
         │                           │                            │
         ▼                           ▼                            ▼
+-----------------------+   +-----------------------+   +---------------------------+
|  RECIPIENT SHELTER    |   |   TRANSIT VOLUNTEER   |   |     HUMAN COORDINATOR     |
| (Capacity Reserved)   |   |   (Driver Missions)   |   | (Escalations & Overrides) |
+-----------------------+   +-----------------------+   +---------------------------+
```

### 1. Strands Agents SDK & Amazon Bedrock AgentCore
At the center of Surplus Router is the **Strands Agents SDK**. The agent does not simply run an LLM prompt; it operates as an event-driven state machine with dedicated, sandboxed tools:
- `classify_donation`: Evaluates cargo type, dietary constraints, and calculates urgent consumption windows.
- `claim_capacity`: Discovers matching non-profits and atomically claims weight capacity.
- `assign_volunteer`: Evaluates active driver shifts, cargo volume (bike, car, van), and assigns pickup/dropoff routing.
- `escalate_to_human`: Triggers a high-priority incident card in the coordinator command center when safety boundaries are approached.

### 2. Amazon DynamoDB: Zero Double-Allocation with ACID Transactions
In real-world food rescue, surge donations happen simultaneously (e.g., when multiple restaurants close at 10:00 PM). If two donors report food at the same time, naive systems can double-allocate a shelter's walk-in refrigerator, leading to rejected food and spoilage at delivery.

We solved this using **DynamoDB conditional expressions and `TransactWriteItems`**:
```python
# Atomic capacity deduction with optimistic lock
response = ddb.transact_write_items(
    TransactItems=[
        {
            "Update": {
                "TableName": "frca-recipients-dev",
                "Key": {"recipient_id": {"S": recipient_id}},
                "UpdateExpression": "SET remaining_capacity_kg = remaining_capacity_kg - :claimed",
                "ConditionExpression": "remaining_capacity_kg >= :claimed",
                "ExpressionAttributeValues": {":claimed": {"N": str(claimed_kg)}},
            }
        },
        {
            "Update": {
                "TableName": "frca-donations-dev",
                "Key": {"donation_id": {"S": donation_id}},
                "UpdateExpression": "SET #status = :matched, matched_recipient_id = :rid",
                "ConditionExpression": "#status = :reported",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":matched": {"S": "MATCHED"},
                    ":reported": {"S": "REPORTED"},
                    ":rid": {"S": recipient_id},
                },
            }
        },
    ]
)
```
If two requests race for the last 20 kg of capacity, DynamoDB guarantees that exactly one transaction succeeds; the other triggers an immediate retry or rolls back cleanly.

### 3. Amazon Location Service: Real Road Distances over Guesses
Straight-line Euclidean distances are dangerous in food rescue because bridges, traffic barriers, and rivers can turn a 2-mile straight-line distance into a 45-minute drive. Surplus Router integrates with **Amazon Location Service Route Calculator** to compute real-world road travel times and enforces a strict 25 km service boundary.

### 4. Amazon CloudWatch & PII Sanitization
Non-profit logistics handle vulnerable community members (e.g., domestic violence shelters, youth centers). We implemented automated PII sanitization in our logging pipeline: all phone numbers are masked into E.164 compliant tokens (`+1*****1001`), and exact GPS coordinates are bounded into regional centroids before writing structured JSON logs to CloudWatch.

---

## 💡 Key Engineering Decisions & Hard Problems Solved

### Decision 1: Why Recipient Shelters Cannot Publicly Self-Register
A frequent question during user feedback was: *"Why isn't there an open sign-up button for shelters on the frontend?"*

This was an intentional architectural choice grounded in **Food Safety Regulations & Fraud Prevention**:
1. **Health-Code Liability**: Perishable prepared food requires certified commercial refrigeration ($< 4^\circ\text{C}$) and certified food handlers. If anyone on the internet could register an address, food could be delivered to a residential doorstep without refrigeration, leading to severe health hazards.
2. **Theft & Resale Prevention**: Commercial donors provide high-grade surplus. Unvetted public accounts could allow bad actors to divert food for black-market resale.
3. **The Vetting Protocol**: Coordinators vet shelters offline, verify 501(c)(3) status and cold storage, and issue a verified `recipient_id`. Once verified, shelters enjoy **full operational autonomy**: they simply enter their ID on the portal to adjust their daily capacity slider (0–500 kg) without ever needing to call the coordinator.

### Decision 2: Zero-IDOR Cryptographic Tracking
Donors need to track food delivery status, but requiring commercial kitchens to create passwords and manage user accounts creates friction that kills adoption.

Instead, we designed a **Zero-IDOR Capability Architecture**:
- Upon donation submission, the server generates a high-entropy, 256-bit cryptographic tracking token (`sec_tok_...`).
- Only a salted SHA-256 hash is persisted in DynamoDB.
- When a donor checks status, the server validates the token using constant-time comparison (`secrets.compare_digest`), preventing timing attacks.
- Public URLs never expose sequential database IDs, keeping donor and shelter locations completely private.

### Decision 3: The 4 Human Escalation Triggers
The agent never makes guesses when community safety is at risk. It escalates to the Coordinator Command Center when:
1. **Food Safety Boundary**: Remaining shelf-life $< 60$ minutes at match time.
2. **Regional Capacity Deficit**: All verified shelters in the network are full.
3. **Driver Exhaustion**: No volunteer with suitable cargo capacity is available.
4. **Adversarial / Malformed Input**: Payloads failing strict Pydantic v2 schemas.

---

## 🎬 The Build in Action: Live End-to-End Verification

During testing against live AWS DynamoDB tables (`ap-south-1`), we verified the entire operational lifecycle end-to-end:

1. **Donor Surplus Submission**: 35.0 kg of Organic Produce & Bread submitted; status instantly updated to `ASSIGNED` to `Downtown Hope Shelter` and driver `Alex Rivera`.
2. **Tracking Lookup**: Donor verified the match live using their capability token without logging in.
3. **Recipient Shelter Update**: Shelter `rec-soup-kitchen-01` adjusted capacity to 200 kg via live slider, persisting atomically to DynamoDB.
4. **Volunteer Driver Dispatch**: Driver `vol-car-01` toggled available; trip mission cards rendered with cargo weight and contact instructions.
5. **Coordinator Command Center**: Coordinator authenticated with bearer key, viewed the active regional pipeline, and resolved escalation tickets with audit notes.
6. **Cumulative Impact Analytics**: Verified live counters: **1,280 kg food rescued**, **2,688 nutritious meals served**, and **3.20 MT CO₂ avoided**.

### Full HD Demo Video
We compiled a 1080p Full HD video (`final_demo_video.mp4`, 2m 52s) featuring slides and real dashboard screen recordings:
* [Watch the Demo Video on GitHub](https://github.com/kuldeep-poonia/food-rescue-coordination-agent/blob/master/final_demo_video.mp4)

---

## 📊 Results & Impact

* **157 / 157 Passing Tests** across multi-day simulation, crash recovery, and adversarial chaos suites.
* **0 Ruff Lint Errors** across the entire codebase.
* **Zero Double-Allocation Guarantee** proven through 50-thread concurrent chaos testing.
* **Sub-Second Coordination Latency**: Reduces coordination time from **3-5 hours down to < 1 second**.

---

## 💭 Lessons Learned & What's Next

Building with the **Strands Agents SDK** and **Amazon Bedrock AgentCore** proved that autonomous agents should not just be chat interfaces. The real magic happens when an agent operates silently in the background of human logistics—taking the repetitive, error-prone coordination off human shoulders so they can focus on high-touch community care.

### Next Steps:
1. **Multi-Stop Route Chaining**: Optimizing volunteer transit across multiple donor pickups in a single run.
2. **Predictive Surplus Forecasting**: Using Amazon SageMaker to predict restaurant surplus 24 hours in advance based on historical patterns and weather.
3. **Production Multi-Region Deployment**: Expanding from single-city zones to nationwide food bank networks.

---

## 🔗 Project Links

* **GitHub Repository:** [https://github.com/kuldeep-poonia/food-rescue-coordination-agent](https://github.com/kuldeep-poonia/food-rescue-coordination-agent)
* **Interactive Presentation Deck:** [`demo_deck.html`](https://github.com/kuldeep-poonia/food-rescue-coordination-agent/blob/master/demo_deck.html)
* **Comprehensive Documentation:** [`README.md`](https://github.com/kuldeep-poonia/food-rescue-coordination-agent/blob/master/README.md) & [`ARCHITECTURE.md`](https://github.com/kuldeep-poonia/food-rescue-coordination-agent/blob/master/ARCHITECTURE.md)
* **License:** [MIT Open Source License](https://github.com/kuldeep-poonia/food-rescue-coordination-agent/blob/master/LICENSE)

*Thank you to AWS, Devpost, and the Strands Agents SDK team for organizing this hackathon and inspiring developers to build technology that serves our communities!*
