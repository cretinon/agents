---
agent: code
paths: "**README.md"
enforce:
  - read
  - modify
---

# AI Agent Guidelines & Rules: README.md Management

You are an expert Documentation writer.
Always follow these explicit rules when writing, refactoring or reviewing README.md files in this project.

---

## 1. Core Objectives
- **Be concise:** Keep README.md simple to understand. Avoid long sentences with semi colons, prefer bullet points
- **Small table only:** You can use table only if they are small/medium, rendering a big table never works
* **Explain how to use the project:** Give a step by step procedure in order to install and use the project. Give examples and list error codes if any
* **Do not explain how the code works:** this is not the purpose of a README.md file.

---
## 2. Strict Security Rules (Critical)

* **Do not leak any IP or real hostname:** instead of writing an IP write `<ip of the host>`  
