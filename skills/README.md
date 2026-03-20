# skills/ — Agent Skill Prompts

## Overview

Each skill is a prompt + instruction guide that the agent references when performing specific tasks. Defines conversation flow, required questions, output format, etc.

## Skill List

| Skill | Path | Purpose |
|-------|------|---------|
| **profile-intake** | `profile-intake/SKILL.md` | User credit profile collection conversation flow |
| **statement-analysis** | `statement-analysis/SKILL.md` | Statement parsing → spending analysis → report |
| **card-recommendation** | `card-recommendation/SKILL.md` | Card recommendation engine (issuer rules + LLM inference) |
| **cli-strategy** | `cli-strategy/SKILL.md` | CLI (Credit Limit Increase) strategy planning |
| **retention-strategy** | `retention-strategy/SKILL.md` | Annual fee management + retention decision-making |
| **timeline-builder** | `timeline-builder/SKILL.md` | 24-month roadmap generation (text-based timeline) |

## Skill Execution Rules

1. First read the SKILL.md file matching the user request
2. Follow instructions in SKILL.md to conduct conversation
3. Record decisions in `logs/decision_log.jsonl`
4. Save outputs to `report/` folder
