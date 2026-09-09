# Agents for Humans: Building Surplus Router with Strands SDK & AWS

> **AWS "Agents for Humans" Hackathon | Track: Good Neighbor Agents**

### 🌟 The Problem: Food Rescue Coordination Grind
Every day, commercial kitchens produce hundreds of kg of surplus food while local shelters face severe shortages. Connecting them is an exhausting manual grind: non-profit coordinators lose 3–5 hours daily on phone calls, WhatsApp groups, and spreadsheets. Perishable meals spoil in 3–4 hours while coordinators frantically search for open shelters with fridge space and available drivers.

### 🤖 The Solution: Autonomous Background Agent
We built **Surplus Router**: an autonomous background coordination agent powered by the **Strands Agents SDK** and **Amazon Bedrock AgentCore**. Rather than an app users must constantly manage, Surplus Router runs silently 24/7—evaluating shelter constraints, atomically locking capacity, and dispatching volunteers. It surfaces to a human coordinator *only when genuine exceptions occur*.

### 🏗️ How We Used AWS Cloud Primitives
1. **Strands Agents SDK & Amazon Bedrock**: Event-driven agent loop with dedicated tools for classification, multi-factor matching, driver dispatching, and safety escalations.
2. **Amazon DynamoDB (ACID)**: 5 tables (`donations`, `recipients`, `volunteers`, `audit`, `memory`). `TransactWriteItems` and conditional writes guarantee **zero double-allocations** during peak donation hours.
3. **Amazon Location Service**: Authoritative road-distance calculations enforcing a 25 km service boundary.
4. **Amazon CloudWatch**: High-granularity JSON telemetry with automated PII masking (phones and GPS scrubbed).

### 💡 Key Engineering Decisions
* **Zero-IDOR Security**: Donors track status via unguessable 256-bit cryptographic capability tokens validated via constant-time SHA-256 checks—no passwords needed.
* **Why Shelters Are Vetted Offline**: Food safety laws require commercial cold storage (< 4°C). To prevent fraud and spoilage, shelters are vetted by coordinators, then given full autonomy via their Organization ID to adjust daily capacity sliders (0–500 kg).
* **Human-in-the-Loop Guardrail**: Automatically escalates if shelf-life is < 60 minutes, regional capacity is full, or conflicts occur.

### 📊 Results & Impact
* **157 / 157 Passing Tests** across simulation and adversarial chaos suites.
* **0 Ruff Lint Errors** across the entire codebase.
* **Live Verified Metrics**: Diverted **1,280 kg of food**, provided **2,688 nutritious meals**, and avoided **3.20 MT of CO2 emissions**.
* **Coordination Latency**: Reduced from **3–5 hours to under 1 second**.

🔗 **GitHub**: https://github.com/kuldeep-poonia/food-rescue-coordination-agent  
🎬 **Demo Video**: `final_demo_video.mp4` in repo (2m 52s Full HD)
