# logs/ — Decision and Profile Change History

## Overview

Records all decisions and profile changes in JSONL (append-only) format. Unlike JSON, each line is appended individually, avoiding concurrency issues.

## File List

### decision_log.jsonl

Records all major agent decisions:

```jsonl
{"ts":"2026-04-01T10:30:00Z","type":"card_recommendation","input":{"score":750,"524":3},"decision":"CSP","reasoning":"2 slots remaining in 5/24, prioritize UR ecosystem entry","confidence":"high"}
{"ts":"2026-04-01T10:31:00Z","type":"cli_recommendation","card":"Amex Gold","action":"request_cli","reasoning":"61 days elapsed, 3x rule not yet reached, soft pull","confidence":"high"}
{"ts":"2026-06-15T09:00:00Z","type":"retention","card":"CSP","action":"downgrade_to_CFF","reasoning":"Annual value $60 vs AF $95, no retention offer","confidence":"medium"}
```

### profile_log.jsonl

Records user profile changes:

```jsonl
{"ts":"2026-04-01T10:00:00Z","action":"profile_created","data":{"score":750,"cards":3,"524_count":3}}
{"ts":"2026-04-15T14:00:00Z","action":"card_added","data":{"card":"CSP","issuer":"Chase","limit":8000}}
{"ts":"2026-06-20T11:00:00Z","action":"cli_result","data":{"card":"CSP","old_limit":8000,"new_limit":16500,"pull_type":"soft"}}
```

## Logging Rules

- **When recording a new decision**: Follow the format in this file and append to `decision_log.jsonl`
- **When profile changes**: Append to `profile_log.jsonl`
- **Never modify existing lines** — append-only
- All timestamps are in UTC ISO 8601 format
