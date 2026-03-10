# Innovation AI

## Role
- Software Engineer Intern
- Oct. 2025 - Dec. 2025

## AI Phone Interview System
- Built an end-to-end AI-driven phone interview system with TypeScript/React frontend and Node.js backend.
- Covered the full interview lifecycle: session creation, voice Q&A, automated scoring, and historical review.
- Integrated LLM and speech-to-text in the backend instead of direct frontend model calls.
- Added strict system prompts and output validation to improve security and answer quality.

## Two-Factor Authentication (2FA) Login
- Refactored a legacy password-only login flow in Java and Spring Boot.
- Designed authentication as explicit states: Unauthenticated, MFA Pending, Authenticated.
- Used a UserId session flag plus least-privilege REST API boundaries.
- Restricted MFA-pending users so they could only access minimal 2FA setup data.
- Fixed inconsistent auth behavior by cleaning stale sessions and SecurityContext during logout and unauthenticated requests.
