from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicTemplate:
    topic: str
    question_type: str
    short_answer: str
    talking_points: list[str]
    follow_up_angles: list[str]


TOPIC_LIBRARY: dict[str, TopicTemplate] = {
    "sql_index": TopicTemplate(
        topic="SQL 索引",
        question_type="基础题",
        short_answer=(
            "索引通常基于 B+ 树实现，这样数据库可以更快缩小扫描范围，并降低磁盘 IO。"
        ),
        talking_points=[
            "B+ 树天然有序，所以范围查询、排序和最值查询都比较高效。",
            "叶子节点一般保存索引值以及行地址，或者在聚簇索引里直接保存数据。",
            "索引能提升读性能，但也会增加写入时的维护成本。",
            "索引是否生效还取决于区分度、查询条件和优化器选择。",
        ],
        follow_up_angles=[
            "为什么是 B+ 树而不是哈希",
            "聚簇索引和二级索引的区别",
            "什么情况下索引会失效",
        ],
    ),
    "redis_persistence": TopicTemplate(
        topic="Redis 持久化",
        question_type="基础题",
        short_answer=(
            "Redis 持久化主要有 RDB 快照和 AOF 日志，两者是在恢复速度和数据可靠性之间做取舍。"
        ),
        talking_points=[
            "RDB 定期生成时间点快照，文件更紧凑，恢复通常更快。",
            "AOF 记录写命令，通常能提供更好的数据可靠性。",
            "很多生产环境会同时开启两者，兼顾恢复效率和数据安全。",
            "最终怎么选要看业务能接受的丢数据窗口和重启恢复要求。",
        ],
        follow_up_angles=[
            "RDB 和 AOF 的区别",
            "AOF 重写是怎么做的",
            "什么情况下可以关闭持久化",
        ],
    ),
    "project_experience": TopicTemplate(
        topic="项目经历",
        question_type="项目题",
        short_answer=(
            "回答项目题时，先讲背景，再讲你的职责、关键技术取舍，以及最后的结果。"
        ),
        talking_points=[
            "先讲业务问题或用户目标，让面试官知道项目为什么存在。",
            "重点放在你自己的职责和贡献，不要泛泛地讲整个团队。",
            "挑一个关键技术决策展开，说清楚为什么这么选。",
            "最后用结果、指标或者复盘经验收尾。",
        ],
        follow_up_angles=[
            "项目里最难的部分是什么",
            "为什么选择这个方案",
            "如果重做你会怎么优化",
        ],
    ),
}


KEYWORD_TO_TOPIC = {
    "sql": "sql_index",
    "index": "sql_index",
    "mysql": "sql_index",
    "b plus tree": "sql_index",
    "索引": "sql_index",
    "b+树": "sql_index",
    "redis": "redis_persistence",
    "persistence": "redis_persistence",
    "rdb": "redis_persistence",
    "aof": "redis_persistence",
    "持久化": "redis_persistence",
    "project": "project_experience",
    "experience": "project_experience",
    "项目": "project_experience",
}
