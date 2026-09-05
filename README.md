# Food Rescue Coordination Agent

Food Rescue Coordination Agent is an autonomous coordination system designed for the Good Neighbor Agents ecosystem. Built on the Strands Agents SDK and deployed on Amazon Bedrock AgentCore, it coordinates surplus food routing end-to-end: matching surplus food from donors (restaurants, bakeries, grocery stores) to recipient organizations (food banks, shelters, community kitchens) and assigning volunteers for transport, escalating only critical edge cases to human coordinators.

## Architecture Summary

```text
+---------------+      +------------------------------------------+      +-----------------------+
|  Donation In  | ---> |   Food Rescue Coordination Agent         | ---> | Notification Dispatch |
|  (Donor API/  |      |   (Strands Orchestrator on AgentCore)    |      | (Donor, Recipient,    |
|   Check-in)   |      +------------------------------------------+      |  Volunteer via SNS)   |
+---------------+              |             |            |              +-----------------------+
                               v             v            v
                        +------------+ +------------+ +------------+
                        | DynamoDB   | | Location   | | Human      |
                        | State Store| | Service    | | Escalation |
                        +------------+ +------------+ +------------+
```

1. **Intake & Classification**: Captures donation quantity, perishable category, and ready-by timeframe.
2. **State & Capacity Check**: Queries active recipients' real-time remaining capacity and dietary needs.
3. **Multi-Factor Scoring**: Ranks recipient matches based on distance, expiry urgency, dietary requirements, and remaining capacity with explicit reasoning.
4. **Volunteer Assignment**: Assigns an available volunteer idempotently within the delivery window.
5. **Human-in-the-Loop Escalation**: Enforces explicit boundaries, escalating conflicts, safety threshold violations, and unmatchable donations to human coordinators.
6. **Immutable Audit Trail**: Records state transitions from reporting through delivery.

## Project Structure

- `agent/` — Strands agent orchestrator, decision boundary logic, and prompts
- `tools/` — Modular Lambda tool implementations backing Strands agent tools
- `infra/` — Infrastructure as Code (IaC) templates for AWS resources
- `frontend/` — Donor reporting, recipient capacity, volunteer check-in, and coordinator views
- `tests/` — Unit, integration, property-based, adversarial, and chaos test suites
- `docs/` — Architecture specifications, threat models, and data schemas

## Setup Instructions

### Prerequisites

- Python 3.10 or higher
- Git

### Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd frca
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. Install pinned dependencies and development tools:
   ```bash
   pip install -r requirements.txt
   ```

### Quality & Testing

Run code quality checks and tests:
```bash
ruff check .
mypy --strict .
pytest
```

## Demo Video

- Live demonstration recording: *To be added*

## License

This project is licensed under the [MIT License](LICENSE).
