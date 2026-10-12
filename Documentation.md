
## Architecture Overview


<img src="Architecture.jpg" alt="AI Task Worker UI" width="700">


The system uses Python, LangGraph, LangChain, and an LLM accessed through OpenRouter. LangGraph provides the orchestration layer, while LangChain supplies the model and tool-calling interfaces.
The architecture consists of the following components:
1. Task Input: Accepts a natural-language instruction from the user and initializes the task state.
2. Agent / Planning Node: Uses the LLM to interpret the task, determine the next action, and select an appropriate tool.
3. Tool Execution Layer: Executes registered functions, such as listing workspace files, reading text documents, extracting PDF content, and reading spreadsheets. Browser automation functions using Playwright have also been developed independently.
4. State and Observation Management: Maintains information such as the task goal, conversation messages, observations, completed steps, and execution attempts.
5. Verification Layer: Evaluates whether the observed results provide sufficient evidence that the original task has been completed.
6. Recovery Mechanism: Supports bounded retries when a task requires another attempt, subject to the implemented verification and routing logic.
7. Execution Logging: Records task information, tool calls, execution duration, status, and verification details in a JSON Lines log.

The intended execution flow is:
User Task → Agent → Tool Execution → Observation → Agent → Verification → Completion or Recovery
LangGraph's conditional edges allow the system to select the next node based on whether the agent has requested a tool call and what the verification stage determines.

## Important Technical and Design Decisions

### 3.1 LangGraph for Orchestration
LangGraph was selected to represent execution as an explicit graph of nodes and transitions rather than relying entirely on an opaque agent abstraction.
This approach makes the execution flow easier to inspect and extend. Separate nodes can handle agent reasoning, tool execution, result recording, verification, and recovery. Conditional routing also provides a foundation for bounded execution and more controlled task management.

### 3.2 OpenRouter for Model Access
The system uses langchain-openai library to access models via OpenRouter.
This separates the orchestration logic from the model provider and allows compatible models to be changed without redesigning the overall architecture. Free model access was chosen to reduce development costs during development.
However, free-model availability, rate limits, and model capabilities can affect execution reliability.

### 3.3 Modular Tool Design
Tools are implemented as Python functions and registered with the agent. This separates the actual execution of an operation from the LLM's decision to invoke it.
The existing file tools provide controlled access to workspace documents, including text files, PDFs, spreadsheets, Browser and Desktop access tools.
This modular design allows new capabilities to be added without rewriting the entire execution pipeline.

### 3.4 Workspace-Constrained File Access
File operations are restricted to a designated workspace using path validation. This reduces the risk of the agent unintentionally accessing or modifying arbitrary files on the host machine. And simulates an actual work computer.
Where applicable, file-writing operations can also verify the saved result by reading it back and comparing it with the requested content.
### 3.5 Explicit Memory/State Management
The graph maintains structured task state rather than relying exclusively on conversational history. Relevant state fields include the task goal, messages, observations, completed steps, retry count, and verification evidence.
Structured state supports debugging, execution tracking, and future improvements such as per-attempt summaries and more sophisticated recovery policies.
### 3.6 Verification and Bounded Recovery
A successful tool call does not necessarily mean that the user's task has been completed. For example, a file operation may execute successfully but produce incorrect content, or a browser click may succeed without navigating to the intended destination.
The design therefore includes a verification stage that evaluates the available evidence against the original goal. A bounded recovery mechanism is intended to allow another attempt when appropriate, while preventing unlimited retry loops.
Programmatic checks, such as checking a file's existence or comparing its saved contents, are preferred for properties that can be tested deterministically. LLM-based verification is useful for semantic judgments but cannot guarantee correctness.
### 3.7 Execution Logging
Task execution is designed to produce structured logs containing the original task, tool calls, observations, status, duration, and verification information.
JSON Lines is suitable for this purpose because each execution can be stored as an individual JSON record. These logs provide a foundation for debugging, evaluating success rates, and identifying recurring failure patterns.

