# 🕵️ Drupal Intent Testing

> **"Does this actually do what we meant?"**

This repository contains **agent skills** for UI-first verification of Drupal applications. It allows an AI agent to drive a real browser, performing the kind of semi-random, exploratory verification a human QA tester would do—but automated.

## 🧠 The Philosophy

This is **intentionally not a replacement** for Drupal’s standard PHPUnit, Kernel, or FunctionalJavascript tests. It complements them by validating **UX**, **Integration Glue**, and **End-to-End Intent**.

| Traditional Testing (PHPUnit) | Intent Testing (This Repo) |
| :--- | :--- |
| Checks if the code executes without error. | Checks if the user experience makes sense. |
| Validates internal logic. | Validates "Integration Glue" between modules. |
| Rigid and specific. | Exploratory, semantic, and flexible. |

**The Goal:** Verify that when we build a feature (like a new content type or AI integration), the actual workflow functions as intended in the browser.

## ⚡ Why `agent-browser`?

These skills are built for the **`agent-browser`** runtime, making them uniquely suited for AI workflows:

*   **CLI-First:** Works directly inside CLI agents (Claude Code, GitHub Copilot, Cursor) because it operates as a shell tool.
*   **No Vision Model Needed:** Uses the browser's **Accessibility Tree** and deterministic element references. It "reads" the page like a screen reader rather than relying on expensive or flaky computer vision.
*   **Visual Evidence:** While it navigates via text/code, it captures **real rendering screenshots** to provide an audit trail of what the user actually saw.

## 📂 Included Patterns

The repository includes templates for common "Intent" workflows.

### 1. The "Writer" Intent (`basic_content.agent`)
*   **The Intent:** "I want to verify that a basic user can log in and publish content without errors."
*   **What it validates:**
    *   Authentication flows.
    *   Database write permissions.
    *   The "Add Content" form UI stability.
    *   Success feedback loops (e.g., seeing the "Node has been created" message).

### 2. The "Builder" Intent (`canvas_ai_context.agent`)
*   **The Intent:** "I want to verify that backend configuration changes actually propagate to the frontend editor interface."
*   **What it validates:**
    *   **Integration Glue:** Checks the connection between Admin Configuration and the Content Editor UI.
    *   **Propagation:** Proves that complex modules (like *Canvas AI*, *Views*, or *Webform*) are correctly exposing their settings to end-users.

## 🚀 Usage

### Prerequisites
*   A running Drupal instance (Local, Dev, or Sandbox).
*   The `agent-browser` tool installed.
*   User credentials (provide via CLI args or `DRUPAL_TEST_USER` / `DRUPAL_TEST_PASS`).

### Running a Skill
Pass the skill file to your agent runner:

```bash
# Verify basic system health
agent-run skills/basic_content.agent

# Verify module configuration logic
agent-run skills/canvas_ai_context.agent
```

## ⚠️ Safety & Developer Notes

Exploratory testing is destructive by nature.

*   **Destructive Actions:** The agent will create content, change configurations, and click buttons. Do not run on Production without strict guardrails.
*   **Idempotency:** The integration script creates a label named "Legal Tone." If run twice, Drupal may throw a "Label already exists" error. You may need to reset your database between runs.
*   **CKEditor / Body Fields:** Standard Drupal replaces the "Body" textarea with a Rich Text Editor. If the agent cannot find label "Body", you may need to instruct it to click the "Source" button in the editor toolbar first.
