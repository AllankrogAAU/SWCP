# Epic: Hybrid AI-Assisted C Programming Learning Platform
 
## Description
Develop a web application that enables students to write C code, complete programming assignments, and receive AI-generated feedback. The system will employ a hybrid AI architecture where a local LLM handles routine feedback and a cloud-hosted LLM is reserved for more advanced assistance. The objective is to investigate whether this architecture can reduce token usage and operational costs without significantly affecting feedback quality.
 
## Goal


Investigate whether a hybrid AI architecture consisting of a local LLM and a cloud-hosted LLM can reduce token consumption and operational costs while still providing high-quality programming assistance.
 
The platform will allow students to write C code and complete programming assignments. A local LLM will handle simple feedback and common programming issues, while a cloud-hosted LLM will be used only for more complex analysis and guidance. The project aims to evaluate whether this approach can maintain a good user experience while optimizing resource usage.
 
---
 
# User Story 1: Write C Code
 
1. As a student i would like to improve my C programming language understanding and knowledge by solving tasks and get feedback on my solutions.

1. I want to write C code in an online editor
So that I can complete programming assignments within the platform
 
## Acceptance Criteria
- The editor supports C syntax highlighting.
- The user can create and edit code files.
- The editor is accessible through a web browser.
- Changes are reflected immediately in the editor.??
 
---
 
# User Story 2: View Assignments (out of our scope)
 
As a student I want to view available programming assignments
So that I know what tasks I need to complete
 
## Acceptance Criteria
- Available assignments are displayed in a list.
- Each assignment includes a title and description.
- Assignment requirements are clearly visible.
- Students can select an assignment to begin working on it.
 
---
 
# User Story 3: Submit Code/compile code
 
As a student would like to learn how to compile my C code and then run it for usage

As a student i want to submit my C code so it can be evaluated
 
## Acceptance Criteria
- Students can submit code for an assignment.
- The platform confirms successful submission.
- The platform compiles the C code
- Submitted code is stored for later access.??
- Multiple submissions are supported.
 
---
 
# User Story 4: Receive Real-Time AI Feedback
 
As a student I want to receive AI feedback while coding
So that I can learn to identify mistakes and improve my solution
 
## Acceptance Criteria
- Feedback is generated based on the current code.
- The AI highlights potential errors and warnings.
- The AI provides suggestions for improvement.
- Feedback is displayed within the interface without requiring the user to leave the editor.
 
---
 
# User Story 5: Review Previous Work
 
As a student 
I want to access previous submissions and feedback
So that I can track my progress and learning
 
## Acceptance Criteria
- Previous submissions are stored.
- Associated AI feedback can be viewed.
- Submissions are organized by assignment.
- Students can compare updated solutions with previous attempts.
 
---
 
# User Story 6: Ask the AI for Help??
 
As a student I want to ask questions about my code and assignment
So that I can better understand programming concepts
 
## Acceptance Criteria
- Students can submit questions to the AI.
- The AI responds with relevant explanations.
- Responses are displayed directly in the platform.
- The AI references the student's current code when generating feedback.





# Technical Story 1
 
As a system I want to route simple requests to a local LLM
So that cloud token usage is reduced
 
# Technical Story 2
 
As a system I want to route complex requests to a cloud-hosted LLM
So that high-quality feedback can still be provided
 
# Technical Story 3
 
As a researcher
I want to measure token consumption for different routing strategies
So that the effectiveness of the hybrid architecture can be evaluated
 
# Technical Story 4
 
As a researcher I want to compare feedback quality between local and cloud models.. So that the trade-off between cost and quality can be assessed