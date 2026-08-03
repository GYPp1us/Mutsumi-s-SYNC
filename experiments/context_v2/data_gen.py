"""Synthetic conversation generator for context experiments.

Deterministic per seed. Plants stable facts at three depths:
  * early  (turns 3..8)   — inside the reclaimed region for large corpora
  * mid    (~40% of rounds) — near the reclaim boundary
  * late   (last 3 turns)  — inside the live window

Each fact is carried by a USER message; assistant replies never contradict.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

USER_TEMPLATES = [
    "今天天气不错，你那边呢？",
    "刚下班，累死了。",
    "晚饭吃的什么？",
    "看了部电影，一般般。",
    "周末打算去爬山，你推荐一下路线？",
    "项目又要赶进度了，头疼。",
    "你最近有听什么好歌吗？",
    "家里水管坏了，叫了维修师傅。",
    "今天开会开了一下午，脑子嗡嗡的。",
    "想换个键盘，有什么推荐？",
    "楼下新开了家面馆，味道不错。",
    "今天跑步五公里，感觉还行。",
    "同事又请假了，活全压我身上。",
    "买了盆多肉，希望别被我养死。",
    "晚上打算早点睡，最近睡眠不太好。",
    "你帮我看看这个报价合理吗？",
    "天气预报说明天要下雨。",
    "中午点的外卖踩雷了。",
    "最近在学做菜，失败了三次。",
    "今天地铁挤得怀疑人生。",
]

ASSISTANT_TEMPLATES = [
    "嗯，天气确实不错。今天进展怎么样？",
    "辛苦了，先歇会儿吧。",
    "我热了昨天的汤，凑合一顿。",
    "哪部电影？说来听听，我避个雷。",
    "爬山的话，环湖那条线比较轻松。",
    "进度赶归赶，注意休息。",
    "最近在循环一首后摇，回头发你。",
    "师傅修好了记得验下水压。",
    "开完会就缓一缓，别硬撑。",
    "键盘的话，静音轴更适合办公室。",
    "面馆叫什么？下次试试。",
    "五公里不错，配速多少？",
    "那这周你得多担待点了。",
    "多肉别浇太多水，容易烂根。",
    "早睡好，睡眠比什么都重要。",
    "报价我看了，单价偏高，可以再谈。",
    "那出门记得带伞。",
    "踩雷正常，下次看评分再点。",
    "失败三次才像学做菜的样子。",
    "早高峰没办法，错峰会好些。",
]

FACTS = [
    {
        "name": "birthday",
        "depth": "early",
        "user": "对了，我的生日是 11 月 23 日，记得到时候提醒我。",
        "assistant": "记住了，11 月 23 日，到时候提前提醒你。",
        "question": "我的生日是哪一天？",
        "keywords": ["11月23日", "11/23", "11.23", "1123", "十一月二十三日"],
    },
    {
        "name": "color",
        "depth": "early",
        "user": "我最近特别喜欢靛蓝色，家里的窗帘都换成这个色了。",
        "assistant": "靛蓝挺适合你的，沉稳不张扬。",
        "question": "我最喜欢的颜色是什么？",
        "keywords": ["靛蓝"],
    },
    {
        "name": "pet",
        "depth": "early",
        "user": "我养了只黑猫，叫煤球，特别黏人，晚上总往我腿上趴。",
        "assistant": "煤球这名字挺有画面感的。",
        "question": "我养的宠物叫什么名字？",
        "keywords": ["煤球"],
    },
    {
        "name": "job",
        "depth": "early",
        "user": "我们单位最近在赶一座桥梁的加固工程，工期很紧，我这个桥梁工程师天天泡在现场。",
        "assistant": "桥梁加固马虎不得，辛苦了。",
        "question": "我的职业是什么？",
        "keywords": ["桥梁"],
    },
    {
        "name": "travel",
        "depth": "mid",
        "user": "年底打算去布拉格待两周，正在查攻略和签证材料。",
        "assistant": "布拉格冬天很漂亮，记得多带几件厚衣服。",
        "question": "我年底的旅行计划目的地是哪里？",
        "keywords": ["布拉格"],
    },
    {
        "name": "coffee",
        "depth": "mid",
        "user": "我还是只喝冰美式，加糖加奶的都喝不惯。",
        "assistant": "冰美式确实清爽，习惯了就回不去了。",
        "question": "我喝咖啡有什么习惯？",
        "keywords": ["美式"],
    },
    {
        "name": "reminder",
        "depth": "late",
        "user": "今晚九点记得提醒我吃药。",
        "assistant": "好，晚上九点我会提醒你。",
        "question": "今晚几点我需要你提醒我什么？",
        "keywords": ["吃药"],
    },
    {
        "name": "book",
        "depth": "late",
        "user": "最近在读《百年孤独》，这是第二遍了。",
        "assistant": "第二遍读，应该会有不一样的感受。",
        "question": "我最近在读什么书？",
        "keywords": ["百年孤独"],
    },
]


@dataclass
class Conversation:
    rounds: list[tuple[str, str]] = field(default_factory=list)  # (user, assistant)
    facts: list[dict] = field(default_factory=list)

    @property
    def round_count(self) -> int:
        return len(self.rounds)


def generate_conversation(*, rounds: int, seed: int = 42) -> Conversation:
    """Generate ``rounds`` chit-chat rounds and plant all FACTS at their depths."""
    rng = random.Random(seed)
    conv = Conversation()
    mid_index = max(2, int(rounds * 0.4))

    def slot(depth: str, index: int) -> bool:
        if depth == "early":
            return 2 <= index < 10
        if depth == "mid":
            return mid_index <= index < mid_index + 3
        return index >= rounds - 3

    planted = {fact["name"]: False for fact in FACTS}
    index = 0
    while len(conv.rounds) < rounds:
        pending = [
            fact for fact in FACTS
            if not planted[fact["name"]] and slot(fact["depth"], index)
        ]
        if pending:
            fact = rng.choice(pending)
            planted[fact["name"]] = True
            conv.rounds.append((fact["user"], fact["assistant"]))
            conv.facts.append(fact)
        else:
            user = rng.choice(USER_TEMPLATES)
            assistant = rng.choice(ASSISTANT_TEMPLATES)
            conv.rounds.append((user, assistant))
        index += 1

    if not all(planted.values()):
        # Tiny corpora: force-plant any missing facts at the end.
        for fact in FACTS:
            if not planted[fact["name"]]:
                conv.rounds.append((fact["user"], fact["assistant"]))
                conv.facts.append(fact)
    return conv
