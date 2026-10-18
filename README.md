# SafeGuard — AI-Powered Emergency Response System

SafeGuard is a Python-based emergency response and safety monitoring prototype designed to assess potential incidents, organize response workflows, visualize relationships between entities, and support incident tracking.

The project uses Streamlit for its user interface and supports FalkorDB for graph-backed data, with a NetworkX-based fallback.

> **Disclaimer:** SafeGuard is a prototype for demonstration and testing. It is not a replacement for official emergency services. A planned dispatch action does not necessarily mean that emergency personnel or contacts have been reached.

## Key Features

- **SOS and Incident Management:** Demonstrate emergency alert workflows and incident handling.
- **Risk Assessment:** Display risk scores, risk levels, and confidence estimates.
- **Response Planning:** Organize proposed response actions and escalation workflows.
- **Graph-Based Reasoning:** Represent relationships between incidents, agents, and other entities.
- **FalkorDB Integration:** Support graph persistence through FalkorDB, with a NetworkX fallback.
- **Incident Tracking:** Provide interfaces for tracking and location-related workflows.
- **Agency Console:** Display agency-oriented incident workflows.
- **Context Graph:** Explore relationships and contextual information.
- **Audit and Reasoning Trace:** Inspect decision-related information produced by the application.
- **Reporting:** Generate incident-related reports.
- **What-If Analysis:** Explore alternative response scenarios where supported by the application.

## Technology Stack

- Python
- Streamlit
- FalkorDB
- NetworkX
- SQLite
- Pyvis
- Docker

## Project Structure

```text
SafeGuard/
├── app.py
├── 1_Agency_Console.py
├── 2_live_track.py
├── 3_context_graph.py
├── agent_workflow.py
├── crew.py
├── falkor_graph.py
├── falkor_memory.py
├── graph_store.py
├── learning.py
├── llm.py
├── memory_store.py
├── notifier.py
├── osm_data.py
├── report.py
├── routing.py
├── sos_server.py
├── tracking.py
├── walk_ui.py
├── walkmode.py
├── whatif.py
├── requirements.txt
└── README.md
```

*The structure above lists the main application modules. Additional local runtime data may be generated when the application runs.*

## Requirements

- Python installed on your system
- Git
- Docker Desktop, when using FalkorDB
- The Python packages required by the project

## Installation on Windows

### 1. Clone the repository

```powershell
git clone https://github.com/Danishrashid07/SafeGuard.git
cd SafeGuard
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use Command Prompt instead:

```cmd
.venv\Scripts\activate
```

### 3. Install dependencies

If `requirements.txt` exists:

```powershell
python -m pip install -r requirements.txt
```

If it does not exist yet, create it from the working development environment before following this step.

## Running SafeGuard

### Option A: Run with FalkorDB

Start Docker Desktop, then start a FalkorDB container:

```powershell
docker run -d --name falkordb -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest
```

If a container named `falkordb` already exists, start it instead:

```powershell
docker start falkordb
```

Set the graph backend and launch the main application:

```powershell
$env:GRAPH_BACKEND = "falkordb"
python -m streamlit run app.py
```

### Option B: Run with the fallback backend

In PowerShell:

```powershell
$env:GRAPH_BACKEND = "networkx"
python -m streamlit run app.py
```

Open the local URL printed by Streamlit in your browser.

> Actual available features depend on the project's installed dependencies and configuration. Configure optional language-model and notification integrations separately if required.

## Configuration and Security

- Never commit API keys, passwords, access tokens, or `.env` files.
- Keep private incident records, real contact information, and sensitive location data out of public repositories.
- Use synthetic data when demonstrating the application publicly.
- Review notification settings before testing, because configured integrations may send real notifications.
- Do not publish personal information contained in generated reports or local database files.

## Demo

**GitHub Repository:**  
https://github.com/Danishrashid07/SafeGuard

**Demo Video:**  
Add your demo video link here after recording and uploading it.

**Screenshots:**  
Add screenshots of the dashboard, risk assessment, response plan, graph visualization, and tracking interface.

## Current Limitations

- This is a demonstration prototype and has not been established as a production-grade emergency dispatch system.
- Actual SMS, push notification, or external service delivery depends on the relevant integration and configuration.
- Language-model explanations may use fallback behavior when an LLM provider is unavailable.
- Risk scores and response suggestions should not be treated as verified real-world assessments.

## Future Improvements

- Improve incident acknowledgement and escalation tracking.
- Expand automated testing and failure handling.
- Improve database persistence verification and auditability.
- Add more comprehensive access control and security protections.
- Improve monitoring, deployment, and documentation.

## Author

**Danish Rashid**

GitHub: https://github.com/Danishrashid07

## License

No license has been specified yet. All rights remain with the copyright holder unless a license is added to this repository.
## Graph Data Model

SafeGuard uses FalkorDB as its graph data store and maintains an in-process NetworkX mirror for UI rendering, geometry, and fallback operations.

### Main Node Types

| Node label | Purpose |
|---|---|
| `User` | Represents a user who can trigger an SOS alert. |
| `Location` | Stores user location information. |
| `Zone` | Represents a geographic safety zone with risk attributes. |
| `Station` | Represents a police or response station. |
| `Patrol` | Represents a patrol unit and its availability. |
| `Hospital` | Represents a hospital, including trauma capability where configured. |
| `Contact` | Represents an emergency contact associated with a user. |
| `Alert` | Stores alert information used in incident-context assessment. |
| `Place` | Represents other relevant places. |
| `Incident` | Records incident status, type, risk, confidence, and timestamp. |
| `Decision` | Stores the risk decision, score, confidence, and rationale. |
| `Action` | Represents a planned response action and its status. |
| `Step` | Records a reasoning or workflow step in the audit trail. |
| `Agent` | Represents a workflow agent. |
| `Task` | Tracks work assigned to agents. |
| `Episode` | Supports incident-related memory and contextual retrieval. |
| `Walk` | Represents a walking session that may be escalated. |

### Important Relationships

- `HAS_CONTACT`: connects a user to an emergency contact.
- `IN_ZONE`: associates a location with a geographic zone.
- `JURISDICTION_OVER`: connects a station to its responsible zone.
- `ASSIGNED_TO`: connects a patrol to a station.
- `RAISED`: connects a user to an incident.
- `OCCURRED_IN`: connects an incident to a zone.
- `CREATED_ALERT`: connects an incident to an alert.
- `DECIDED`: connects an incident to its risk decision.
- `BASED_ON`: connects a decision to supporting evidence.
- `LED_TO`: connects a decision to response actions.
- `SENT_TO`: connects an action to its intended recipient.
- `HAS_STEP`: connects an incident to its audit steps.
- `PERFORMED`: connects an agent to a workflow step.
- `NEXT`: links consecutive workflow steps.
- `RETRIEVED`: links a workflow step to retrieved graph context.
- `HANDS_OFF_TO`: represents handoffs between workflow agents.
- `CREATED`, `CLAIMED`, `COMPLETED`, and `PRODUCED`: represent task lifecycle relationships.

### Graph-Based Response Selection

SafeGuard uses Cypher queries to identify relevant response resources:

1. Identify the zone containing the reported coordinates.
2. Find stations connected to that zone through `JURISDICTION_OVER`.
3. Rank candidate stations by geographic distance.
4. Find available patrols assigned to the selected station through `ASSIGNED_TO`.
5. Fall back to the nearest available patrol if no assigned patrol is available.
6. Find a nearby hospital and prefer a suitable trauma-capable hospital for configured medical, assault, or accident incidents.
7. Retrieve the user's emergency contacts.

Geographic distance and estimated travel time are calculated in application code. The resource-selection process combines graph traversal with geographic ranking.

### Example Cypher Queries

**Find stations responsible for a zone:**

```cypher
MATCH (s:Station)-[:JURISDICTION_OVER]->(z:Zone {id: $z})
RETURN s
```

**Find available patrols assigned to a station:**

```cypher
MATCH (p:Patrol {status: 'AVAILABLE'})
      -[:ASSIGNED_TO]->
      (s:Station {id: $s})
RETURN p
```

**Retrieve contacts for a user, ordered by priority:**

```cypher
MATCH (u:User {id: $u})-[h:HAS_CONTACT]->(c:Contact)
RETURN c, h.priority
ORDER BY h.priority
```

**Retrieve the risk decision for an incident:**

```cypher
MATCH (i:Incident {id: $i})-[:DECIDED]->(d:Decision)
RETURN d.level, d.score, d.confidence, d.rationale
```

These queries are parameterized in the Python implementation to separate query structure from supplied values.

### Incident Audit Graph

For valid incidents, SafeGuard records a graph-based audit trail containing the incident, its decision, supporting evidence, workflow steps, and planned actions. This allows the application to explain why a risk decision was made and which actions were proposed.

The graph is intended to improve traceability and contextual resource selection. Risk scores and suggested actions remain prototype outputs and should not be treated as verified emergency assessments.
