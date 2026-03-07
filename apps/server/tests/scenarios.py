from __future__ import annotations

COMPLEX_DIALOGUE = [
    {"speaker": "interviewer", "text": "Please introduce the backend project you worked on most recently."},
    {"speaker": "candidate", "text": "I worked on an internal data service platform and owned query APIs, cache stability, and SQL tuning."},
    {"speaker": "interviewer", "text": "How are SQL indexes implemented?"},
    {"speaker": "candidate", "text": "I would start with B plus tree structure and then explain why it fits databases."},
    {"speaker": "interviewer", "text": "Why do databases usually use B plus trees instead of hash indexes?"},
    {"speaker": "candidate", "text": "Because range queries, ordering, and disk access patterns fit B plus trees better."},
    {"speaker": "interviewer", "text": "What is the difference between clustered and secondary indexes?"},
    {"speaker": "candidate", "text": "Clustered indexes usually keep row data at the leaf level, while secondary indexes need another lookup."},
    {"speaker": "interviewer", "text": "If write pressure is high, would you still add many indexes, and why?"},
    {"speaker": "candidate", "text": "No, I would balance query gains against write amplification and maintenance costs."},
    {"speaker": "interviewer", "text": "Now explain Redis persistence and how you choose between RDB and AOF."},
    {"speaker": "candidate", "text": "It depends on restart speed, disk overhead, and the acceptable data loss window."},
    {"speaker": "interviewer", "text": "If I ask a follow up about AOF rewrite, how would you answer?"},
]
