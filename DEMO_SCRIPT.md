# 🎬 Official 5-Minute Demo Video Script & Walkthrough

> **AWS "Agents for Humans" Hackathon Submission**  
> **Track:** **Good Neighbor Agents**  
> **Project:** **Surplus Router — Autonomous Food Rescue Coordination Agent**  
> **Maximum Length:** **5:00 minutes** (Strict Hackathon Rule)  
> **Recording Recommendation:** Open `demo_deck.html` in one browser tab and `http://localhost:8080` in another tab. Use Loom, OBS Studio, or Windows Snipping Tool Screen Recorder.

---

## ⏱️ Video Breakdown at a Glance

| Time Window | Section | Screen Display | Key Objective |
| :--- | :--- | :--- | :--- |
| **0:00 – 1:00** | **The Pitch & Problem** | `demo_deck.html` (Slides 1–3) | Cover: (1) Problem, (2) Who it's for, (3) Why it matters |
| **1:00 – 1:45** | **Architecture & Tech Stack** | `demo_deck.html` (Slide 4) & `ARCHITECTURE.md` | Strands SDK, Bedrock AgentCore, DynamoDB ACID |
| **1:45 – 3:15** | **Live System Demo: Intake & Auto-Match** | `http://localhost:8080` (Donor & Volunteer) | Submit live donation, show capability token & match |
| **3:15 – 4:15** | **Human-in-the-Loop & Audit** | `http://localhost:8080` (Coordinator Portal) | Show Escalation Queue, resolve ticket, show DynamoDB |
| **4:15 – 5:00** | **Conclusion & Community Impact** | `demo_deck.html` (Slides 6–7) | Measurable impact, meals served, hackathon closing |

---

## 🎙️ Word-for-Word Voiceover Narration Script

---

### Part 1: The Pitch (0:00 – 1:00) — *[Display: `demo_deck.html` Slide 1 to 3]*

**[0:00 – 0:20 | Slide 1]**  
> *"Hello judges! Welcome to **Surplus Router**, an autonomous coordination agent built with the **Strands Agents SDK** and deployed for **Amazon Bedrock AgentCore** for the **Good Neighbor Agents** track.*  
> *Every day, millions of tons of safe, edible food are thrown away by restaurants, caterers, and supermarkets. At the exact same time, local homeless shelters and community soup kitchens struggle with food shortages."*

**[0:20 – 0:40 | Slide 2]**  
> *"Why does this food waste paradox exist? Because connecting donors to shelters is a **crushing manual busywork nightmare**.  
> Community coordinators lose **three to five hours every single day** playing frantic dispatchers on WhatsApp and phone calls—checking shelter fridge capacity, calculating driving distances, verifying dietary constraints, and calling drivers before the three-hour food safety window expires."*

**[0:40 – 1:00 | Slide 3]**  
> *"Surplus Router solves this end-to-end.  
> **Who is it for?** It serves an entire community network: commercial food donors, non-profit recipient shelters, and transit volunteers.  
> **Why does it matter?** It runs quietly and autonomously in the background, matching and routing surplus food in seconds, and **only surfaces to a human coordinator when there is a genuine exception** or safety boundary."*

---

### Part 2: Technical Architecture (1:00 – 1:45) — *[Display: `demo_deck.html` Slide 4]*

**[1:00 – 1:25 | Slide 4]**  
> *"Under the hood, Surplus Router leverages the **Strands Agents SDK** and AWS serverless primitives:  
> • The **Strands Orchestrator** drives autonomous tool execution: donation classification, live capacity lookups, multi-factor deterministic matching, and volunteer dispatch.  
> • We utilize **five AWS DynamoDB tables** with **ACID transactions (`TransactWriteItems`)** to guarantee that shelters are never over-allocated or double-booked.*  
> • To protect kitchen and shelter locations, we built a **Zero-IDOR Security Architecture** using 256-bit cryptographic capability tokens verified with constant-time SHA-256 hashing."*

**[1:25 – 1:45 | Slide 5]**  
> *"Our matching algorithm is mathematically deterministic: it balances **Distance (35%)**, **Remaining Capacity (25%)**, **Dietary Fit (20%)**, and **Perishability Urgency (20%)**.  
> Now, let’s see the live system in action!"*

---

### Part 3: Live System Demonstration (1:45 – 3:15) — *[Display: `http://localhost:8080`]*

**[1:45 – 2:25 | Switch to `http://localhost:8080` (Donor Portal)]**  
> *(Action: Click on the **Donor Portal** tab)*  
> *"Here is our modern, clean white web interface. Let's act as a local bakery reporting surplus bread and prepared meals.*  
> *(Action: Fill the form)*  
> • Donor: **Green Bakery & Deli**  
> • Phone: `+12125550199`  
> • Food Category: **Prepared Meals (Hot/Chilled)**  
> • Quantity: **25.0 kg**  
> • Perishability: **4.0 Safe Hours**  
> *(Action: Click 'Report Surplus Food')*  
> *Notice that within milliseconds, the Strands agent receives the report, classifies the perishability window, queries regional shelters in AWS DynamoDB, executes the match, and returns a secure tracking receipt with our cryptographic token!  
> Status is immediately **MATCHED** to the nearest verified shelter!"*

**[2:25 – 2:50 | Switch to Recipient Partner Tab]**  
> *(Action: Click on **Recipient Partner** tab)*  
> *"Non-profit shelters don't have to manage an account or answer phone calls. They simply maintain their daily intake capacity here.  
> Notice that when our 25 kg donation was matched, DynamoDB's ACID transaction automatically deducted the capacity from the shelter, preventing any duplicate food deliveries."*

**[2:50 – 3:15 | Switch to Transit Volunteer Tab]**  
> *(Action: Click on **Transit Volunteer** tab)*  
> *"Now let’s look at the Transit Volunteer view. Pre-registered volunteer drivers can toggle their availability.  
> When the agent confirmed the match, it immediately dispatched the nearest available vehicle—in this case, driver `vol-car-01`—providing the pickup address and delivery destination with zero manual dispatcher intervention!"*

---

### Part 4: Human-in-the-Loop & Safety Boundaries (3:15 – 4:15) — *[Display: Coordinator Command Center]*

**[3:15 – 3:50 | Switch to Coordinator Tab]**  
> *(Action: Click on **Coordinator** tab)*  
> *"Now, what happens when an edge case occurs? Remember: the agent runs autonomously, but **never removes the human from safety-critical decisions**.  
> Let's authenticate to the **Coordinator Command Center** using our secure API key.*  
> *(Action: Enter key `dev-insecure-coordinator-key-for-local-testing-only-32chars` and click Authenticate)*  
> *Here is the live coordinator dashboard! In the **Active Donation Pipeline**, we see all autonomous lifecycles in real time.*  
> *And over here is our **Escalation Queue**. When a donation has less than 60 minutes of shelf-life or shelter capacity is exhausted, the agent flags it immediately as `NO_MATCH_WITHIN_WINDOW`.*  
> *(Action: Click 'Dismiss Ticket' or inspect details)*  
> *The coordinator can review the situation, override the dispatch, or re-route with a single click."*

**[3:50 – 4:15 | Highlight Audit Trail]**  
> *"Every single autonomous decision and coordinator action is immutably recorded to our DynamoDB audit log table with execution timestamps and idempotency keys, providing 100% compliance and transparency."*

---

### Part 5: Impact & Conclusion (4:15 – 5:00) — *[Display: `demo_deck.html` Slide 7]*

**[4:15 – 4:40 | Switch to Impact Metrics Tab or Slide 7]**  
> *"Finally, look at the **Impact Metrics**: Surplus Router tracks cumulative kilograms diverted from landfills, calculates USDA meal equivalents, and tallies non-profits fed.  
> Instead of spending hours on daily logistics, coordinators can now expand their donor network 10-fold and focus on what truly matters: community human care."*

**[4:40 – 5:00 | Wrap Up & Sign Off]**  
> *"Surplus Router brings autonomous AI micro-logistics to good neighbor organizations across the globe—powered by the **Strands Agents SDK** and **Amazon Bedrock AgentCore**.  
> Thank you to AWS and Devpost for hosting the Agents for Humans Hackathon!"*

---

## 💡 Top Video Recording Tips

1. **Resolution:** Record at 1080p (1920x1080) in fullscreen for sharp text.
2. **Audio:** Use a clean microphone or record in a quiet room.
3. **Pacing:** Keep mouse movements smooth and deliberate when switching tabs.
4. **Time Check:** Ensure your video is under **4 minutes 50 seconds** to avoid any Devpost cutoff penalties.
