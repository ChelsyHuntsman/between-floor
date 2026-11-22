# -*- coding: utf-8 -*-
"""《楼层之间》（Between Floors）引擎。

两个人被困在停电的电梯里。玩家用自由的中文说话，对方是一个由规则驱动的人：
有自己的性格、心事、底线和判断。游戏在 12~24 轮对话后，电梯恢复运行，按两人的实际相处形成结局。

对外接口：
  cmd("指令或直接说的话")            返回文字
  new_game(seed=None, force=False, **设定)
  set_save_path(path)
  export_code() / import_code(code)  存档码（适合会话环境撑不久的 AI 玩家）
  set_llm(fn)                         可选：接一个大模型来改写对方的台词（默认关闭，完全离线可玩）
"""
import base64
import copy
import json
import os
import re
import time
import zlib

try:  # 打包成单文件时由 build.py 注入
    from _bf_people import *  # noqa: F401,F403
    from _bf_lines import *  # noqa: F401,F403
except ImportError:  # 开发版
    from people import *  # noqa: F401,F403
    from lines import *  # noqa: F401,F403

VERSION = 1
SAVE_ENV = "BETWEEN_FLOORS_SAVE"
_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_save_path = os.environ.get(SAVE_ENV) or os.path.join(_HERE, "between_floors_save.json")
_llm = None
STATE_KEYS = ("trust", "comfort", "tension", "curiosity", "contact")


# ════════════════════ 随机数 ════════════════════
class Rng:
    """Mulberry32：状态是一个整数，可以存进存档。"""

    def __init__(self, state):
        self.state = state & 0xFFFFFFFF

    def random(self):
        self.state = (self.state + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    def randint(self, a, b):
        return a + int(self.random() * (b - a + 1))

    def choice(self, seq):
        return seq[int(self.random() * len(seq))]

    def chance(self, p):
        return self.random() < p


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


# ════════════════════ 开局 ════════════════════
GENDER_ALIAS = {"女": "女", "f": "女", "female": "女", "男": "男", "m": "男", "male": "男"}
TRAIT_ALIAS = {v: k for k, v in TRAITS.items()}
TRAIT_ALIAS.update({k: k for k in TRAITS})


def make_setup(seed, me=None, name=None, npc=None, rel=None, traits=None, job=None, bg=None):
    r = Rng(seed)
    me = GENDER_ALIAS.get(str(me).lower(), None) if me else None
    me = me or r.choice(["女", "男"])
    npc_g = GENDER_ALIAS.get(str(npc).lower(), None) if npc else None
    npc_g = npc_g or r.choice(["女", "男"])
    rel = REL_ALIAS.get(rel, rel) if rel else None
    if rel not in RELATIONS:
        rel = r.choice(list(RELATIONS))
    # 名字
    msur = r.choice(SURNAMES)
    myname = (name or (msur + r.choice(GIVEN[me])))[:8]
    msur = myname[0]
    while True:
        nsur = r.choice(SURNAMES)
        if nsur != msur:
            break
    ngiven = r.choice(GIVEN[npc_g])
    # 性格
    tr = []
    if traits:
        for x in re.split(r"[,，、+ ]+", str(traits)):
            k = TRAIT_ALIAS.get(x.strip())
            if k and k not in tr:
                tr.append(k)
    while len(tr) < 2:
        k = r.choice(list(TRAITS))
        if k not in tr and (not tr or (tr[0], k) not in TRAIT_CLASH):
            tr.append(k)
    tr = tr[:2]
    contrasts = [c for c, ok in CONTRAST_OK.items() if tr[0] in ok]
    contrast = r.choice(contrasts)
    # 素材
    pick = lambda pool: r.randint(0, len(pool) - 1)  # noqa: E731
    npc_d = {
        "name": nsur + ngiven, "sur": nsur, "given": ngiven, "gender": npc_g, "ta": "她" if npc_g == "女" else "他",
        "traits": tr, "contrast": contrast,
        "job": pick(JOBS), "reason": r.choice([i for i, x in enumerate(REASONS) if not x.get("only")]), "worry": pick(WORRIES), "regret": pick(REGRETS),
        "fear": pick(FEARS), "hobby": pick(HOBBIES),
    }
    # 怕密闭空间的人在电梯里更难受；不让它太常见
    if FEARS[npc_d["fear"]].get("claustro") and r.chance(0.4):
        npc_d["fear"] = (npc_d["fear"] + 1) % len(FEARS)
    pack = {"stranger": None, "coworker": COWORK, "oldfriend": OLDFRIEND, "quarrel": QUARREL, "estranged": ESTRANGED}[rel]
    npc_d["pack"] = pick(pack) if pack else None
    if rel == "quarrel" and REASONS[npc_d["reason"]]["id"] in ("lawyer", "checkup", "keys"):  # 私事太重 / 和吵架的事对不上，换成普通的
        npc_d["reason"] = next(i for i, x in enumerate(REASONS) if x["id"] == "contract")
    office = next(i for i, x in enumerate(REASONS) if x["id"] == "office")
    if rel == "coworker" or (rel == "quarrel" and "同事" in QUARREL[npc_d["pack"]]["who"]):
        npc_d["reason"] = office  # 同事今天在楼里，当然是来上班的
    meinfo = {"name": myname, "sur": msur, "gender": me}
    if job:
        meinfo["job"] = str(job)[:16]
    if bg:
        meinfo["bg"] = str(bg)[:60]
    return {"me": meinfo, "npc": npc_d, "rel": rel, "seed_used": r.state}


def fresh_state(seed, **kw):
    setup = make_setup(seed, **kw)
    rel = setup["rel"]
    st = dict(REL_START[rel])
    tr = setup["npc"]["traits"]
    if "cautious" in tr:
        st["trust"] -= 5
    if "talkative" in tr:
        st["comfort"] += 5
        st["curiosity"] += 5
    if "quiet" in tr:
        st["curiosity"] -= 5
    if "sensitive" in tr:
        st["tension"] += 5
    claustro = bool(FEARS[setup["npc"]["fear"]].get("claustro"))
    if claustro:
        st["tension"] += 10
    s0 = {
        "v": VERSION, "seed": seed, "rng": (seed * 2654435761 + 12345) & 0xFFFFFFFF,
        "me": setup["me"], "npc": setup["npc"], "rel": rel,
        "st": st, "st0": dict(st),
        "turn": 0, "progress": 0.0, "stage": 1, "phase": "play", "closing_left": 0,
        "name_known": rel != "stranger", "my_name_known": rel != "stranger",
        "anxious": 2 if claustro else 0,
        "topics": {}, "depth": {}, "npc_refused": {}, "my_boundary": [],
        "mem": [], "recalled": [], "npc_facts": [],
        "used": {}, "inits": [], "ambient": [], "beats": [], "last_ambient": 0,
        "pending": None, "quiet_until": -1, "silence_run": 0, "short_run": 0,
        "inputs": [], "insults": 0, "pushy": 0, "care_turns": [], "jokes": 0, "deep": 0,
        "quarrel": 0, "estr": 0, "unsaid": "none", "npc_asked_last": False,
        "contact": {"player_asked": 0, "npc_declined": False, "player_declined": False, "exchanged": False, "npc_asked": False,
                    "invite": False},
        "flags": [], "history": [], "ending": None, "autocode": False, "llm": False,
        "pose": "far" if rel in ("quarrel", "estranged") else "stand", "hostile": 0, "q_steps": [], "offered": {}, "light_on": False,
        "progress_lines": [], "mem_used": [],
        "started": time.strftime("%Y-%m-%d %H:%M"),
    }
    if setup["me"].get("job"):
        s0["mem"].append({"kind": "job", "x": setup["me"]["job"], "turn": 0, "given": True})
    return s0


# ════════════════════ 游戏对象 ════════════════════
class Game:
    def __init__(self, s):
        self.s = s
        self.rng = Rng(s["rng"])
        self.out = []

    # ── 小工具 ──
    @property
    def npc(self):
        return self.s["npc"]

    @property
    def tr(self):
        return self.s["npc"]["traits"]

    @property
    def main(self):
        return self.s["npc"]["traits"][0]

    def has(self, *t):
        return any(x in self.tr for x in t)

    def flag(self, f):
        return f in self.s["flags"]

    def setflag(self, f):
        if f not in self.s["flags"]:
            self.s["flags"].append(f)

    def add(self, **kw):
        for k, v in kw.items():
            self.s["st"][k] = clamp(self.s["st"][k] + v)

    def v(self, k):
        return self.s["st"][k]

    def job(self):
        return JOBS[self.npc["job"]]

    def reason(self):
        return REASONS[self.npc["reason"]]

    def worry(self):
        return WORRIES[self.npc["worry"]]

    def regret(self):
        return REGRETS[self.npc["regret"]]

    def fear(self):
        return FEARS[self.npc["fear"]]

    def hobby(self):
        return HOBBIES[self.npc["hobby"]]

    def pack(self):
        rel = self.s["rel"]
        pool = {"coworker": COWORK, "oldfriend": OLDFRIEND, "quarrel": QUARREL, "estranged": ESTRANGED}.get(rel)
        return pool[self.npc["pack"]] if pool else {}

    def claustro(self):
        return bool(self.fear().get("claustro"))

    # ── 文本 ──
    def label(self):
        if self.s["name_known"]:
            return self.npc["name"] if self.s["rel"] in ("stranger", "coworker") else self.npc["given"]
        return "对方"

    def mec(self):
        rel = self.s["rel"]
        me = self.s["me"]
        if not self.s["my_name_known"]:
            return ""
        if rel == "coworker":
            return f"小{me['sur']}，"
        if rel == "quarrel":
            return f"{me['name']}，"
        nick = self.pack().get("nick") if rel in ("oldfriend", "estranged") else ""
        return f"{nick or me['name'][1:] or me['name']}，"

    def fill(self, text, **extra):
        pk = self.pack()
        d = {
            "ta": self.npc["ta"], "n": self.label(), "mec": self.mec(), "myname": self.s["me"]["name"], "sur": self.npc["sur"],
            "sir": "女士" if self.s["me"]["gender"] == "女" else "先生",
            "gossip": pk.get("gossip", ""), "rumor": pk.get("rumor", ""), "npc_view": pk.get("npc_view", ""),
            "npc_sorry": pk.get("npc_sorry", ""), "my_dept": pk.get("my_dept", ""), "npc_dept": pk.get("npc_dept", ""),
            "how": pk.get("how", ""), "who": pk.get("who", ""), "was": pk.get("was", ""), "kw": "", "mutual": pk.get("mutual", ""), "mutual_news": pk.get("mutual_news", ""),
            "mem": self.next_memory() if pk.get("memories") and "{mem}" in text else "", "old_drift": pk.get("drift", ""),
            "drift_npc": pk.get("unsaid_npc", ""), "echo": "", "x": "",
        }
        d["mutual_q"] = (f"你还跟{d['mutual']}有联系吗？{d['mutual_news']}" if d["mutual"] else
                         "我们以前那帮人，你还跟谁有联系吗？")
        d.update(extra)

        class SafeDict(dict):
            def __missing__(self, k):
                return ""
        text = text.format_map(SafeDict(d))
        return text.replace("{ta}", d["ta"])

    def next_memory(self):
        mems = self.pack()["memories"]
        used = self.s.setdefault("mem_used", [])
        fresh = [i for i in range(len(mems)) if i not in used]
        i = fresh[0] if fresh else self.rng.randint(0, len(mems) - 1)
        if i not in used:
            used.append(i)
        return mems[i]

    def ok_line(self, it):
        if isinstance(it, str):
            return True
        if "rel" in it and self.s["rel"] not in it["rel"]:
            return False
        if "tr" in it and not any(t in self.tr for t in it["tr"]) and it.get("contrast") != self.npc["contrast"]:
            return False
        if "notr" in it and any(t in self.tr for t in it["notr"]):
            return False
        if "contrast" in it and "tr" not in it and self.npc["contrast"] != it["contrast"]:
            return False
        if "claustro" in it and it["claustro"] != (self.claustro() and self.s["anxious"] > 0):
            return False
        st = self.s["st"]
        for k, key, op in (("mint", "trust", 1), ("maxt", "trust", -1), ("minc", "comfort", 1), ("maxc", "comfort", -1),
                           ("minx", "tension", 1), ("maxx", "tension", -1)):
            if k in it and (st[key] - it[k]) * op < 0:
                return False
        if "stage" in it and self.s["stage"] not in it["stage"]:
            return False
        return True

    def pick(self, key, bank, once=False, **extra):
        """从台词库里挑一句：满足条件、尽量没用过；优先挑带条件的（更贴合人物）。
        once=True：用完就不再说（沉默时的小动作这类，重复了就像在循环）。"""
        cands = [(i, it) for i, it in enumerate(bank) if self.ok_line(it)]
        if not cands:
            return ""
        used = self.s["used"].setdefault(key, [])
        fresh = [c for c in cands if c[0] not in used]
        if not fresh and once:
            return ""
        if not fresh:  # 全用过了：清掉这一组的记录，但不立刻重复上一句
            last = used[-1] if used else None
            self.s["used"][key] = used = [last] if last is not None else []
            fresh = [c for c in cands if c[0] != last] or cands
        special = [c for c in fresh if isinstance(c[1], dict) and len(c[1]) > 1]
        pool = special if special and self.rng.chance(0.6) else fresh
        i, it = self.rng.choice(pool)
        used.append(i)
        return self.fill(it if isinstance(it, str) else it["t"], **extra)

    def npc_say(self, text):
        """把一句台词（可能带〔动作〕和 ‖ 停顿）渲染出来。"""
        if not text:
            return
        for seg in text.split("‖"):
            seg = seg.strip()
            if not seg:
                continue
            m = re.match(r"^〔(.+?)〕(.*)$", seg)
            if m:
                act = m.group(1).strip()
                self.out.append("  " + act + ("" if act.endswith(("。", "！", "？", "…", "」")) else "。"))
                seg = m.group(2).strip()
            if seg:
                self.out.append(f"{self.label()}：「{seg}」")
                self.s["history"].append(["npc", seg[:200]])

    def narrate(self, text):
        if text:
            t = self.fill(text).replace("〔", "").replace("〕", "")
            self.out.append("  " + t + ("" if t.endswith(("。", "！", "？", "…", "」")) else "。"))

    def blank(self):
        if self.out and self.out[-1] != "":
            self.out.append("")


# ════════════════════ 理解玩家说的话 ════════════════════
NEG_WORDS = ["舍不得", "想念", "怀念", "心疼", "孤单", "寂寞", "烦", "累", "难过", "讨厌", "糟", "倒霉", "不开心", "郁闷", "崩溃", "无语", "难受", "压力", "焦虑", "失眠", "怕", "惨", "完蛋"]
POS_WORDS = ["开心", "不错", "喜欢", "挺好", "好玩", "有意思", "舒服", "幸运", "高兴", "期待"]
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:~～…「」『』“”\"'（）()【】\[\]—\-]+")
SHORT_WORDS = {"嗯", "嗯嗯", "哦", "噢", "啊", "好", "好的", "行", "对", "是", "是啊", "对啊", "ok", "OK", "嗯呢", "唔", "哈", "呵"}


def norm(t):
    return PUNCT.sub("", t)


def has_kw(t, w):
    if w not in t:
        return False
    ex = KW_EXCLUDE.get(w)
    if not ex:
        return True
    tt = t
    for e in ex:
        tt = tt.replace(e, "")
    return w in tt


def has_any(t, words):
    return any(has_kw(t, w) for w in words)


NOT_Q = re.compile(r"(什么|怎么|谁|哪|多少|几)(也|都)|多少有|几乎|好几|十几|二十几|没几|有几|这几|那几|前几|过几|哪怕|不管怎么|怎么说呢|没什么|不知道为什么|之类的|什么的[。！!]?$")
Q_END = re.compile(r"(吗|呢|么|嘛|吧|没有|不|啥)[。！!…~～\s]*$")
Q_WORDS = ["什么", "怎么", "为什么", "为啥", "多久", "多少", "是不是", "有没有", "会不会", "能不能", "要不要", "可不可以", "哪", "谁", "咋", "啥", "几点", "几楼", "几年"]


def is_question(t):
    if "？" in t or "?" in t:
        return True
    tt = NOT_Q.sub("", t)
    if Q_END.search(tt) and not tt.endswith(("不错吧", "算了吧", "好吧", "行吧", "走吧", "来吧")):
        return True
    return any(w in tt for w in Q_WORDS)


def parse(text):
    t = str(text).strip()
    n = norm(t)
    p = {"raw": t, "norm": n, "intents": set(), "topics": [], "question": False, "about_you": False, "about_me": False,
         "self": [], "sent": 0, "claustro": False, "silence": False}
    if not n or set(n) <= set("…."):
        p["silence"] = True
        return p
    p["question"] = is_question(t)
    for k, words in INTENT_KW.items():
        if k in ("agree", "yes", "no", "disagree"):
            continue
        if has_any(t, words):
            p["intents"].add(k)
    # 「好」「对」「不是」这些字太常见，只有短句或句首才算
    short = len(n) <= 5
    head = re.split(r"[，,。！？!?]", t, maxsplit=1)
    rest = norm(head[1]) if len(head) > 1 else ""
    for k in ("agree", "yes", "no", "disagree"):
        for w in INTENT_KW[k]:
            if (short and w in n) or (n.startswith(w) and (len(rest) <= 3 or k in ("yes", "no"))):
                if k == "disagree" and n.startswith("不是") and ("是不是" in t):
                    continue
                p["intents"].add(k)
                break
    if "对不起" in t:
        p["intents"].discard("agree")
        p["intents"].discard("yes")
    if "boundary" in p["intents"]:
        p["intents"].discard("no")
    if n in SHORT_WORDS:
        p["intents"].add("ack")
    its = p["intents"]
    # 「我不想加微信」：说到了联系方式，但是在拒绝
    tq = re.sub(r"要不要|要不|能不能|可不可以|是不是|加不加|留不留|方不方便|愿不愿意|想不想|不(加|留|给)(吗|么|嘛|呀)", "", t)
    if "contact" in its and re.search(r"(不想|不要|不用|别|不|没必要|算了|先不).{0,4}(加|留|给|交换|要|存)", tq):
        its.discard("contact")
        its.add("no_contact")
    # 「等出去了一起吃个饭 / 回头一起再看看方案」：约以后；「要不要一起坐会儿」：眼前的事
    if re.search(r"(出去以后|出去后|出去了|等出去|回头|改天|下次|明天|今晚|晚上|周末|以后|有空|过几天|哪天)", t) and \
            re.search(r"(一起|请你|咱们|我们|聚|见面|见个|聊聊|谈谈|看看|吃|喝)", t) and "no_contact" not in its:
        its.add("invite")
    if "together_now" in its and "invite" in its and not re.search(r"(出去|回头|改天|下次|以后|明天|周末)", t):
        its.discard("invite")
    # 「你不想说也没关系」是给对方空间，不是你在拒绝
    if "give_space" in its or ("boundary" in its and re.search(r"你.{0,4}(不想|不愿|不方便|不用)", t) and not re.search(r"我.{0,3}(不想|不太想)", t)):
        its.discard("boundary")
        its.add("give_space")
    for topic, words in TOPIC_KW:
        if has_any(t, words):
            p["topics"].append(topic)
    p["about_you"] = "你" in t or "您" in t
    p["about_me"] = "我" in t
    neg = sum(1 for w in NEG_WORDS if w in t)
    pos = sum(1 for w in POS_WORDS if w in t)
    p["sent"] = (pos > neg) - (neg > pos)
    if p["about_me"] and has_any(t, CLAUSTRO_KW) and not ("你" in t and p["question"]):
        p["claustro"] = True
    if p["about_me"] and not p["question"]:
        for pat, kind in SELF_PATTERNS:
            m = re.search(pat, t)
            if m:
                x = m.group(1).strip("，。！？ 的了啊呀")[:14]
                if kind == "job_tail":
                    kind, x = "job", m.group(0)[2:].strip("个一名")
                if x and not x.startswith("你") and (kind, x) not in p["self"] and not any(k == kind for k, _ in p["self"]):
                    p["self"].append((kind, x))
    return p


# ════════════════════ 对话核心 ════════════════════
PRIVATE = {"worry", "fear", "regret", "family", "love"}
PERSONAL_ASK = {"recent", "regret", "family", "worry", "love", "fear", "shared"}  # 对方问你的私人问题
SMALL_BANKS = {"fault": FAULT, "signal": SIGNAL, "repair": REPAIR, "injury": INJURY, "weather": WEATHER, "bored": BORED,
               "food": FOOD, "city": CITY, "life": LIFE}
TIER_NEED = {  # 话题：(t1 信任, t2 信任, t3 信任)
    "work": (0, 30, 58), "why_here": (0, 35, 60), "hobby": (0, 25, 55),
    "worry": (32, 48, 62), "fear": (45, 52, 64), "regret": (99, 55, 66), "family": (40, 50, 62),
}
TRAIT_SHIFT = {"cautious": 8, "quiet": 5, "talkative": -8, "humor": -3, "sensitive": 0, "rational": 2}


class Talk(Game):
    """一轮对话：理解 → 更新关系 → 回应 → 对方主动 → 时间推进。"""

    # ── 阈值 ──
    def tier_ok(self, topic, tier):
        if tier > 3:
            return False
        need = TIER_NEED.get(topic, (0, 30, 60))[tier - 1]
        need += TRAIT_SHIFT.get(self.main, 0) + TRAIT_SHIFT.get(self.tr[1], 0) // 2
        if tier == 3:
            return self.v("trust") >= need and self.v("comfort") >= 45 and self.s["turn"] >= 5 and self.v("tension") < 60
        return self.v("trust") >= need and self.v("tension") < 75

    def contact_score(self):
        st = self.s["st"]
        sc = st["contact"] + st["trust"] * 0.35 + st["comfort"] * 0.3 + st["curiosity"] * 0.15 - st["tension"] * 0.45
        sc += {"talkative": 8, "humor": 5, "cautious": -10, "quiet": -6, "sensitive": 0, "rational": -4}[self.main]
        sc += {"oldfriend": 10, "estranged": 3, "coworker": 5, "stranger": 0, "quarrel": -5}[self.s["rel"]]
        if self.s["insults"]:
            sc -= 40
        if self.s["pushy"]:
            sc -= 12 * self.s["pushy"]
        return sc

    # ── 记忆 ──
    def remember(self, kind, x):
        x = re.sub(r"^(哈哈+|呵呵|嘿嘿|嗯+|啊|哦|噢|唉|哎)[，, ]*", "", str(x)).strip()
        if kind == "why_here":
            x = re.sub(r"^(我)?(今天)?(其实|本来)?(是)?(来)?", "", x).rstrip("的了啊呀")
        if len(x) < 2 or norm(x) in SHORT_WORDS:
            return
        if kind == "name":
            if 1 <= len(x) <= 4:
                self.s["me"]["name"] = x
                self.s["me"]["sur"] = x[0]
            self.s["my_name_known"] = True
            self.told_name = True
            return
        for m in self.s["mem"]:
            if m["kind"] == kind and m["x"] == x:
                return
        self.s["mem"].append({"kind": kind, "x": x, "turn": self.s["turn"]})

    def mem_of(self, kind):
        for m in reversed(self.s["mem"]):
            if m["kind"] == kind:
                return m
        return None

    def recall_line(self, force=False):
        """对方自然地提起你之前说过的话。"""
        cands = [m for m in self.s["mem"] if m["kind"] in RECALL and (m["kind"], m["x"]) not in
                 [tuple(r) for r in self.s["recalled"]] and self.s["turn"] - m["turn"] >= (1 if force else 3)]
        if not cands:
            return ""
        # 怕密闭空间的事最值得记住
        cands.sort(key=lambda m: (m["kind"] != "claustro", m["kind"] not in ("dislike", "why_here", "recent")))
        m = cands[0]
        self.s["recalled"].append([m["kind"], m["x"]])
        return self.pick("recall_" + m["kind"], RECALL[m["kind"]], x=m["x"])

    # ── 入口 ──
    def turn(self, text):
        s = self.s
        p = parse(text)
        if not p["silence"]:
            self.out.append(f"你：「{p['raw'][:300]}{'…' if len(p['raw']) > 300 else ''}」")
            s["history"].append(["me", p["raw"][:200]])
        self.npc_asked = False
        self.spoke = False
        self.heavy = False
        self.shared_now = False
        self.prev_npc_topic = s.pop("last_npc_topic", None)
        handled = self.respond(p)
        if not p["silence"]:
            s["inputs"] = (s["inputs"] + [p["norm"]])[-4:]
        s["npc_asked_last"] = self.npc_asked
        self.after_turn(p, handled)

    def say(self, text):
        self.spoke = True
        self.npc_say(text)

    def ask(self, text, topic):
        self.say(text)
        if topic:
            self.s["last_npc_topic"] = topic
            self.s.setdefault("npc_topics", []).append(topic)
        self.npc_asked = True
        self.s["pending"] = {"topic": topic, "turn": self.s["turn"]} if topic else None

    # ── 主分派 ──
    def respond(self, p):
        s = self.s
        pend = s["pending"]
        if p["silence"]:
            return self.on_silence(p)
        s["silence_run"] = 0
        s["turn"] += 1
        it = p["intents"]
        s["pending"] = None  # 对方的问题只等一句；你说别的，就算过去了
        self.pend = pend
        self.told_name = False
        for kind, x in p["self"]:
            self.remember(kind, x)
        if not any(k == "why_here" for k, _ in p["self"]) and (s.get("last_topic") == "why_here" or (pend or {}).get("topic") == "why_here") \
                and re.match(r"^(我)?(是)?来", p["raw"]):
            self.remember("why_here", self.snippet(p["raw"]))
        if p["claustro"]:
            self.remember("claustro", "密闭的地方")
        # 1. 对方问了要不要加联系方式，你在回答
        if pend and pend["topic"] == "contact_q":
            if "boundary" in it or "no" in it or "no_contact" in it or ("disagree" in it and "yes" not in it):
                return self.on_contact_answer(False)
            if "yes" in it or "agree" in it or "contact" in it:
                return self.on_contact_answer(True)
        # 2. 骂人
        if "insult" in it:
            return self.on_insult(p)
        # 2b. 被骂过以后，对方基本不想搭理你
        if s["insults"] >= 2 or (s["insults"] and self.v("tension") >= 85):
            if "apology" in it:
                self.add(tension=-6, trust=2)
                s["insults_forgiven"] = s.get("insults_forgiven", 0) + 1
                self.npc_say(self.pick("cold_ap", ["〔{ta}没回头〕……嗯。", "〔{ta}看了你一眼，又转回去了〕", "……知道了。"]))
            else:
                self.npc_say(self.pick("cold", COLD))
            s["pending"] = None
            return True
        # 2c. 话里带刺
        if "hostile" in it and "apology" not in it:
            return self.on_hostile(p)
        # 3. 重复同一句
        if len(p["norm"]) > 1 and p["norm"] in s["inputs"][-3:] and "ack" not in it:
            return self.on_repeat(p)
        # 4. 想安静
        if "quiet" in it:
            return self.on_quiet(p)
        # 4b. 你给对方留空间（「不想说也没关系」）
        if "give_space" in it:
            return self.on_give_space(p)
        # 5. 你拒绝谈
        if "boundary" in it:
            return self.on_boundary(p)
        # 6. 对方没说完的话
        if s["unsaid"] == "started" and "drift" in p["topics"] and s["rel"] in ("oldfriend", "estranged"):
            s["unsaid_what"] = "drift" if s["rel"] == "oldfriend" else "estr"
            return self.on_unsaid_ask(p)
        if s["unsaid"] == "started" and ("unsaid" in it or "unsaid" in p["topics"] or (
                p["question"] and p["about_you"] and set(p["topics"]) & {"drift", "shared", "worry", "quarrel", "recent"})):
            return self.on_unsaid_ask(p)
        # 7. 道歉
        if "apology" in it and not (p["question"] and "你" in p["raw"][:3]):
            return self.on_apology(p)
        # 8. 联系方式、约以后、表白、做回朋友、眼下一起做点什么
        if "no_contact" in it:
            return self.on_no_contact(p)
        if "friend_again" in it:
            return self.on_friend_again(p)
        if "confess" in it:
            return self.on_confess(p)
        if "contact" in it or "invite" in it:
            return self.on_contact_request(p)
        if "together_now" in it:
            return self.on_together(p)
        if "flirt" in it:
            return self.on_flirt(p)
        if "goodbye" in it and s["phase"] == "play" and len(p["norm"]) <= 8:
            self.say(self.pick("bye", GOODBYE_EARLY))
            return True
        # 9. 安慰 / 关心
        if "comfort" in it or ("concern" in it and p["about_you"]):
            return self.on_care(p)
        # 9b. 你报了名字：先接住名字，别把它当成别的回答
        if self.told_name and not p["question"]:
            self.add(trust=2, comfort=2)
            self.say(self.pick("name_recv", NAME_RECV))
            if s["rel"] == "stranger" and not s["name_known"] and self.rng.chance(0.5 if self.has("cautious", "quiet") else 0.85):
                s["name_known"] = True
                self.say(self.pick("name", NAME_GIVE))
            return True
        # 10. 回答对方刚才的问题
        if pend and pend["topic"] not in ("contact_q", "name") and not (p["question"] and (p["about_you"] or (
                p["topics"] and pend["topic"] not in p["topics"]))) and not (
                {"joke", "compliment", "thanks", "ask_back"} & it and pend["topic"] not in p["topics"]):
            s["pending"] = None
            return self.on_answer(p, pend["topic"])
        if pend and pend["topic"] == "name" and self.told_name:
            s["pending"] = None
            self.add(trust=2, comfort=2)
            self.say(self.pick("name_recv", NAME_RECV))
            return True
        s["pending"] = None
        if self.told_name and s["rel"] == "stranger" and not p["question"]:
            self.add(trust=2, comfort=2)
            self.say(self.pick("name_recv", NAME_RECV))
            if not s["name_known"] and self.rng.chance(0.5 if self.has("cautious", "quiet") else 0.85):
                s["name_known"] = True
                self.say(self.pick("name", NAME_GIVE))
            return True
        # 10b. 你问对方还记不记得你说过的话
        if re.search(r"(记得|记住)(我|了我)|我(刚才|之前|刚刚)?说(了|过)(什么|啥)|我说过的", p["raw"]) and p["question"]:
            m = [x for x in s["mem"] if x["kind"] in RECALL]
            if not m:
                self.say(self.pick("rc_none", ["你说了什么来着？……我刚才有点走神。", "嗯……你好像没说什么特别的？"]))
                return True
            self.add(trust=3, comfort=2)
            m = m[-1]
            self.say(self.pick("rc_yes", ["记得啊。", "〔{ta}想了一下〕记得。"]))
            self.say(self.pick("recall_" + m["kind"], RECALL[m["kind"]], x=m["x"]))
            return True
        # 11. 你提起对方说过的事
        if "recall" in it and p["about_you"] or ("recall" in it and not p["question"]):
            if self.on_player_recall(p):
                return True
        # 12. 玩笑 / 夸奖 / 感谢
        if "joke" in it:
            return self.on_joke(p)
        if "compliment" in it and not p["question"]:
            return self.on_compliment(p)
        if s["rel"] == "quarrel" and s["quarrel"] < 2 and re.search(r"我(应该|不该|该|也有不对|也有问题)|是我(的问题|没做好)|你也没错|你有你的道理", p["raw"]):
            return self.conciliate("concede")
        if "thanks" in it and len(p["norm"]) <= 10:
            self.add(comfort=2, trust=1)
            self.say(self.pick("thanks", THANKS_RECV))
            return True
        # 13. 问题
        if p["question"]:
            return self.on_question(p)
        if "ask_back" in it:
            return self.on_question(p, back=True)
        # 14. 说自己的事 / 聊话题
        if p["self"] or p["claustro"]:
            return self.on_self(p)
        if p["topics"]:
            return self.on_topic_statement(p)
        if "ack" in it or "agree" in it:
            return self.on_ack(p)
        if "disagree" in it:
            self.add(curiosity=3 if self.has("rational") else 0, comfort=-2 if self.has("sensitive") else 0)
            self.say(self.pick("disagree", DISAGREE_RECV))
            return True
        return self.on_fallback(p)

    # ── 各种情况 ──
    def on_silence(self, p):
        s = self.s
        s["silence_run"] += 1
        s["turn"] += 1
        if s["silence_run"] >= 2 and self.v("tension") < 50:
            s["quiet_shared"] = True
        s["pending"] = None
        if self.has("quiet", "cautious"):
            self.add(comfort=4, tension=-3)
        elif self.has("talkative"):
            self.add(comfort=-1)
        else:
            self.add(comfort=1, tension=-2)
        self.out.append("（你没有说话。）")
        if self.claustro() and s["anxious"] > 0 and self.rng.chance(0.5):
            line = self.pick("sil_cl", SILENCE_CLAUSTRO, once=True)
            if line:
                s["anxious"] = min(3, s["anxious"] + 1)
                self.narrate(line)
                return True
        quiet_mode = s["turn"] <= s["quiet_until"]
        # 安静了好一阵，对方自己开口说点心里的事（只一次）
        if s["silence_run"] >= 3 and not quiet_mode and not self.flag("silence_shared") and not self.blocked("worry") \
                and self.v("tension") < 60 and s["insults"] == 0:
            tier = s["depth"].get("worry", 0) + 1
            if tier <= 3 and self.tier_ok("worry", tier):
                self.setflag("silence_shared")
                self.heavy = True
                self.say(self.pick("sil_lead", ["……说出来你别笑。", "〔{ta}看着地面，忽然开口〕安静的时候，人老会想事情。",
                                                "我能说个事吗？你不用接话。"]))
                self.share("worry", tier)
                return True
        if self.has("talkative") and s["silence_run"] >= 2 and self.rng.chance(0.6 if not quiet_mode else 0.35):
            if quiet_mode:
                self.say(self.pick("qbreak", QUIET_BREAK))
                s["quiet_until"] = -1
            return "initiative"
        line = self.pick("sil_" + self.main, SILENCE[self.main], once=True)
        if line:
            self.npc_say(line)
        return True

    def on_insult(self, p):
        s = self.s
        s["insults"] += 1
        self.add(trust=-15, comfort=-15, tension=20, contact=-20)
        if s["insults"] >= 2:
            self.npc_say(self.pick("insult2", INSULT_AGAIN))
        else:
            self.npc_say(self.pick("insult_" + self.main, INSULT_RECV[self.main]))
        s["quiet_until"] = s["turn"] + 2
        return True

    def on_repeat(self, p):
        s = self.s
        s["rep"] = s.get("rep", 0) + 1
        self.add(comfort=-2)
        if s["rep"] >= 3:
            self.add(tension=3)
            self.npc_say(self.pick("rep2", REPEAT_MORE))
        else:
            self.npc_say(self.pick("rep", REPEAT))
        return True

    def on_quiet(self, p):
        s = self.s
        s["quiet_until"] = s["turn"] + 3
        s["quiet_shared"] = True
        if self.has("quiet", "cautious"):
            self.add(comfort=5, tension=-3)
        elif self.has("talkative"):
            self.add(comfort=-1)
        else:
            self.add(comfort=2)
        self.npc_say(self.pick("qack_" + self.main, QUIET_ACK[self.main]))
        s["pending"] = None
        return True

    def on_boundary(self, p):
        s = self.s
        topic = (getattr(self, "pend", None) or {}).get("topic") or next((t for t in p["topics"] if t not in ("contact", "unsaid")), None) \
            or s.get("last_topic") or "personal"
        if topic not in s["my_boundary"]:
            s["my_boundary"].append(topic)
        s["pending"] = None
        self.add(curiosity=-2, comfort=1 if self.has("cautious", "rational", "quiet") else 0)
        if "apology" in p["intents"]:
            self.add(tension=-2)
        self.npc_say(self.pick("respect_" + self.main, RESPECT[self.main]))
        s["respected"] = s.get("respected", 0) + 1
        return True

    def on_apology(self, p):
        s = self.s
        rel = s["rel"]
        self.add(tension=-8, trust=3)
        if rel == "quarrel" and s["quarrel"] < 2 and (not p["topics"] or "quarrel" in p["topics"] or s["quarrel"] == 0):
            return self.conciliate("apology")
        if rel == "estranged" and s["estr"] < 2 and ("shared" in p["topics"] or len(p["norm"]) > 6):
            self.add(trust=5, comfort=4, tension=-6)
            s["estr"] = max(s["estr"], 1)
            self.npc_say(self.pick("ap_est", ["〔{ta}看着你，过了一会儿才说〕……你没什么对不起我的。", "……嗯。我也是。"]))
            return True
        self.npc_say(self.pick("apol", APOLOGY_RECV))
        return True

    def on_hostile(self, p):
        s = self.s
        s["hostile"] = s.get("hostile", 0) + 1
        self.add(tension=10, trust=-6, comfort=-5, contact=-10)
        self.npc_say(self.pick("host_" + self.main, HOSTILE_RECV[self.main], once=True) or self.pick("cold", COLD))
        s["quiet_until"] = s["turn"] + 1
        return True

    def on_give_space(self, p):
        s = self.s
        self.add(comfort=4, trust=3, tension=-3, contact=2)
        s["gave_space"] = s.get("gave_space", 0) + 1
        if s["rel"] == "quarrel" and s["quarrel"] == 1:
            return self.conciliate("space")
        self.say(self.pick("space", GIVE_SPACE))
        # 不追问，对方反而愿意说一点
        topic = next((t for t in p["topics"] if t in ("worry", "family", "why_here", "work", "regret", "fear")), None) or s.get("last_topic")
        topic = {"family": "worry"}.get(topic, topic)
        if topic in ("worry", "why_here", "work", "regret", "fear") and self.rng.chance(0.5):
            tier = s["depth"].get(topic, 0) + 1
            if topic == "regret":
                tier = max(2, tier)
            if self.tier_ok(topic, tier):
                self.say(self.pick("space_lead", ["……其实也没什么不能说的。", "〔{ta}停了一会儿〕说一点也行。"]))
                self.share(topic, tier)
        return True

    def on_no_contact(self, p):
        c = self.s["contact"]
        c["player_declined"] = True
        self.add(contact=-10)
        self.say(self.pick("noc", NO_CONTACT_RECV))
        return True

    def on_friend_again(self, p):
        s = self.s
        rel = s["rel"]
        self.heavy = True
        if rel not in ("estranged", "oldfriend", "quarrel"):
            return self.on_contact_request(p)
        if rel == "quarrel" and s["quarrel"] < 2:
            return self.conciliate("friend")
        if self.contact_score() >= 45 and self.v("tension") < 65 and not s["contact"]["npc_declined"]:
            s["contact"]["exchanged"] = True
            self.setflag("contact")
            self.setflag("friend_again")
            self.add(trust=6, comfort=5, tension=-8, contact=10)
            if rel == "estranged":
                s["estr"] = max(s["estr"], 1)
            self.say(self.pick("fr_yes", FRIEND_YES))
        else:
            self.setflag("friend_pending")
            self.add(tension=2, curiosity=4)
            self.say(self.pick("fr_wait", FRIEND_WAIT))
        return True

    def on_confess(self, p):
        s = self.s
        if s["rel"] != "estranged":
            return self.on_flirt(p)
        self.heavy = True
        if self.v("trust") >= 50 and self.v("tension") < 60:
            self.setflag("confessed")
            self.add(tension=3, curiosity=6)
            self.say(self.pick("conf_ex", CONFESS_EX))
        else:
            self.add(tension=7, comfort=-4)
            s["pushy"] += 1 if self.flag("confessed_low") else 0
            self.setflag("confessed_low")
            self.say(self.pick("conf_low", CONFESS_LOW))
        return True

    def on_together(self, p):
        s = self.s
        if self.v("comfort") >= 22 and self.v("tension") < 70 and s["pose"] != "sit":
            s["pose"] = "sit"
            self.add(comfort=3)
            self.say(self.pick("tog", [TOGETHER_NOW[0], TOGETHER_NOW[2]]))
        else:
            self.say(TOGETHER_NOW[1] if s["pose"] != "sit" else self.fill("〔{ta}已经坐着了，往旁边挪了挪〕"))
        return True

    def on_contact_request(self, p):
        s = self.s
        c = s["contact"]
        invite = "invite" in p["intents"] and "contact" not in p["intents"]
        if c["exchanged"]:
            self.say(self.pick("c_done", ["已经加了啊。", "〔{ta}晃了晃手机〕加过了。"]))
            return True
        c["player_asked"] += 1
        if invite:
            c["invite"] = True
        if c["npc_declined"]:
            s["pushy"] += 1
            self.add(tension=6, comfort=-4)
            self.say(self.pick("c_again", CONTACT_AGAIN))
            return True
        if s["rel"] == "quarrel" and s["quarrel"] < 2 and invite:
            c["invite"] = s["quarrel"] == 1
            return self.conciliate("invite", reply=self.pick("inv_q", ["……行。出去了再说。", "〔{ta}看了你一眼〕你请。"]))
        sc = self.contact_score()
        # 关系太冷的时候，第一次开口通常会被婉拒；对方不会因为你热情就答应
        need = 52 if s["stage"] >= 3 or s["phase"] == "closing" else 60
        if sc >= need:
            c["exchanged"] = True
            self.add(contact=10, comfort=3)
            if invite:
                self.say(self.pick("inv_ok", INVITE_ACCEPT))
            else:
                bank = CONTACT_ACCEPT_OLD if s["rel"] in ("oldfriend", "estranged") else CONTACT_ACCEPT
                self.say(self.pick("c_ok", bank))
            self.setflag("contact")
        else:
            c["npc_declined"] = True
            self.add(tension=2 if sc < 30 else 0)
            self.say(self.pick("inv_no", INVITE_NO) if invite else self.pick("c_no", CONTACT_DECLINE))
        return True

    def on_contact_answer(self, yes):
        c = self.s["contact"]
        if yes:
            c["exchanged"] = True
            self.add(contact=10, comfort=4)
            self.setflag("contact")
            self.say(self.pick("c_yes", CONTACT_YES_REACT))
        else:
            c["player_declined"] = True
            self.add(contact=-15)
            self.say(self.pick("c_noreact", CONTACT_NO_REACT))
        return True

    def on_flirt(self, p):
        s = self.s
        if s["rel"] == "estranged":
            self.add(tension=8, comfort=-4)
            self.say(self.pick("flirt_e", FLIRT_ESTRANGED))
            return True
        if self.v("trust") >= 60 and self.v("comfort") >= 55:
            self.add(curiosity=3, tension=2)
            self.say(self.pick("flirt_ok", ["〔{ta}愣了一下，笑出声来〕你这人……", "〔{ta}低头笑了，没接话〕"]))
            return True
        s["flirt"] = s.get("flirt", 0) + 1
        self.add(comfort=-6, tension=6, trust=-3)
        if s["flirt"] >= 2:
            s["pushy"] += 1
        self.say(self.pick("flirt", FLIRT_RECV))
        return True

    def on_care(self, p):
        s = self.s
        s["care_turns"] = [t for t in s["care_turns"] if s["turn"] - t <= 4] + [s["turn"]]
        s["topics"]["injury"] = s["topics"].get("injury", 0) + 1
        anxious = self.claustro() and s["anxious"] > 0
        if anxious or ("dark" in self.fear() and s["stage"] >= 2 and self.rng.chance(0.3)):
            s["anxious"] = max(0, s["anxious"] - 1)
            self.add(trust=6, comfort=6, tension=-6, contact=4)
            self.setflag("helped_anxious")
            self.heavy = True
            if "comfort" in p["intents"]:
                self.say(self.pick("cf_anx", COMFORT_ANXIOUS))
            else:
                self.say(self.pick("cn_anx", CONCERN_ANXIOUS))
            if s["anxious"] <= 1 and not self.flag("fear_told") and self.claustro():
                self.setflag("fear_told")
                s["depth"]["fear"] = max(s["depth"].get("fear", 0), 2)
                self.say(self.fear()["t2"])
            return True
        if len(s["care_turns"]) >= 3 and not self.has("sensitive"):
            self.add(comfort=-4, tension=3)
            self.say(self.pick("cf_much", COMFORT_TOO_MUCH))
            return True
        if "concern" in p["intents"] and "comfort" not in p["intents"]:
            self.add(comfort=2, trust=1)
            if self.npc["contrast"] == "care_health" and self.rng.chance(0.6):
                self.say(self.pick("concern_cc", CONCERN_OK))
                self.ask(self.fill("你呢？你看起来比我还累。"), "recent")
                return True
            self.say(self.pick("concern", CONCERN_OK))
            return True
        self.add(comfort=-1 if self.has("rational", "cautious") else 1)
        self.say(self.pick("cf_calm", COMFORT_CALM))
        return True

    def on_answer(self, p, topic):
        """你在回答对方的问题。"""
        s = self.s
        snip = self.snippet(p["raw"])
        if topic in ("why_here", "work", "recent", "hobby", "city", "regret", "family", "animal", "shared", "rumor", "injury", "signal"):
            kind = {"work": "job", "hobby": "like"}.get(topic, topic)
            if topic in ("why_here", "work", "recent", "hobby", "regret", "family", "past"):
                self.remember(kind if kind in RECALL else "recent", snip)
        self.add(trust=2, comfort=3, curiosity=2, contact=2)
        if p["sent"] < 0 or topic in ("regret", "family", "recent") and len(p["norm"]) > 8:
            self.add(trust=2)
            bank = SELF_RECV["neg"] if p["sent"] < 0 else SELF_RECV["recent"]
            self.say(self.pick("ans_neg", bank))
        elif topic == "shared":
            self.add(comfort=5, trust=3, contact=3)
            self.say(self.pick("shared_reply", SHARED_REPLY))
        elif topic == "animal" and (self.npc["contrast"] == "soft_on_animals" or self.worry().get("animal")) and p["sent"] >= 0 \
                and not re.search(r"没养|没有|不养", p["raw"]):
            self.add(comfort=6, trust=4, contact=3)
            self.say(self.pick("animal_warm", ANIMAL_WARM))
        elif topic == "rumor":
            self.add(curiosity=4)
            self.say(self.pick("rumor_r", ["哦——原来是这样。", "〔{ta}点点头〕我就说嘛，传得跟真的一样。", "那我回去替你辟谣。"]))
        else:
            r = {"why_here": SELF_RECV["why_here"], "work": SELF_RECV["job"], "hobby": SELF_RECV["like"]}.get(topic)
            self.say(self.pick("ans_" + topic, r) if r else self.pick("ans_gen", ["嗯。", "这样啊。", "〔{ta}点点头〕", "哦——"]))
        if self.resonate(p):
            return True
        # 礼尚往来：对方也说说自己的
        if topic in ("why_here", "work", "hobby") and s["depth"].get(topic, 0) == 0 and self.rng.chance(0.7):
            self.share(topic, 1, lead=self.rng.choice(["我是", "我啊，", "我嘛……", ""]))
        return True

    def on_player_recall(self, p):
        """你提起对方说过的事。"""
        facts = self.s["npc_facts"]
        if not facts:
            return False
        hit = None
        for f in facts:
            if any(k in p["raw"] for k in f.get("kw", [])):
                hit = f
        if not hit:
            return False
        self.add(trust=4, comfort=3, contact=3)
        self.say(self.pick("recalled_me", ["〔{ta}有点意外〕你还记得啊。", "嗯，你记性挺好。", "〔{ta}笑了一下〕我随口一说的。"]))
        topic = hit["topic"]
        tier = self.s["depth"].get(topic, 0) + 1
        if tier <= 3 and self.tier_ok(topic, tier):
            self.share(topic, tier)
        return True

    def on_joke(self, p):
        s = self.s
        if self.prev_npc_topic == "shared" or (s.get("last_topic") == "shared" and s["rel"] in ("oldfriend", "estranged")):
            self.add(comfort=6, tension=-6, contact=3, trust=2)
            self.setflag("laughed")
            self.say(self.pick("sh_laugh", SHARED_LAUGH))
            return True
        bad = (self.v("tension") >= 60 or (self.claustro() and s["anxious"] >= 2) or
               (self.has("sensitive") and self.v("comfort") < 30) or (s["rel"] == "quarrel" and s["quarrel"] == 0 and self.v("tension") > 55))
        if self.npc["contrast"] == "joke_when_nervous":
            bad = bad and self.rng.chance(0.4)
        if bad:
            self.add(tension=5, comfort=-3)
            self.say(self.pick("joke_bad", JOKE_BAD))
            return True
        s["jokes"] += 1
        k = 2 if self.has("humor") else 1
        self.add(comfort=5 * k, tension=-6, contact=2 * k, trust=1)
        self.setflag("laughed")
        self.say(self.pick("joke_good", JOKE_GOOD))
        return True

    def on_compliment(self, p):
        if self.s["rel"] == "quarrel" and self.s["quarrel"] < 2:
            return self.conciliate("concede")
        if self.has("cautious") and self.v("trust") < 30:
            self.add(tension=1)
        else:
            self.add(trust=2, comfort=3, contact=2)
        self.say(self.pick("compl", COMPLIMENT_RECV))
        return True

    def on_ack(self, p):
        s = self.s
        s["short_run"] = s.get("short_run", 0) + 1
        self.add(comfort=1)
        line = self.pick("ack", ACK)
        if line:
            self.npc_say(line)
        return "initiative" if s["short_run"] >= 2 else True

    def on_self(self, p):
        s = self.s
        self.add(trust=3, curiosity=4, comfort=2, contact=2)
        if p["sent"] < 0 or len(p["norm"]) > 12:
            self.heavy = True
        if p["claustro"]:
            self.setflag("player_claustro")
            self.add(trust=2)
            self.say(self.pick("self_cl", SELF_RECV["claustro"]))
            return True
        kind = p["self"][0][0]
        if not any(m["x"] == p["self"][0][1] for m in s["mem"]):
            self.remember(kind, p["self"][0][1])
        bank = SELF_RECV.get(kind) or (SELF_RECV["neg"] if p["sent"] < 0 else SELF_RECV["pos"])
        if p["sent"] < 0 and kind in ("recent", "family", "past"):
            self.add(trust=2)
            if self.has("sensitive"):
                self.add(trust=2, comfort=2)
        ack = self.kw_ack(p)
        self.say(ack or self.pick("self_" + kind, bank))
        if self.resonate(p):
            return True
        # 你说了自己的事，对方有时会说说自己的
        tmap = {"job": "work", "why_here": "why_here", "like": "hobby", "recent": "worry"}
        topic = tmap.get(kind)
        if topic and self.rng.chance(0.55):
            tier = s["depth"].get(topic, 0) + 1
            if tier <= 3 and self.tier_ok(topic, tier):
                self.share(topic, tier, lead=self.rng.choice(["我也是……", "我的话，", "说起来，", ""]))
        return True

    def on_topic_statement(self, p):
        topic = p["topics"][0]
        self.s["last_topic"] = topic
        if topic in SMALL_BANKS:
            self.add(comfort=2, tension=-2)
            self.say(self.pick(topic, SMALL_BANKS[topic]))
            return True
        if topic in ("recognize", "drift", "us") or topic == "quarrel" or (topic == "shared" and (p["about_you"] or self.s["rel"] in ("oldfriend", "estranged"))):
            return self.on_question(p)
        if topic in PRIVATE or topic in ("work", "why_here", "hobby"):
            # 陈述里带了私人话题词，但不是问对方：当成你在说自己的事
            if p["about_me"]:
                kind = "recent" if re.search(r"最近|这阵子|这段时间|这几天", p["raw"]) else "said"
                return self.on_self({**p, "self": [(kind, self.snippet(p["raw"]))]})
            return self.on_fallback(p)
        return self.on_fallback(p)

    def kw_ack(self, p):
        """你话里提到的具体东西（考试、猫、房子……），先接住它。"""
        for kws, lines in KW_ACK:
            hit = next((k for k in kws if k in p["raw"]), None)
            if hit:
                return self.pick("kwack_" + kws[0], lines, kw=hit)
        return ""

    def resonate(self, p):
        """你说到的东西碰到了对方自己的事：对方会顺着说一点自己的。"""
        if getattr(self, "shared_now", False):
            return False
        s = self.s
        srcs = [("worry", self.worry()), ("work", self.job()), ("why_here", self.reason()), ("hobby", self.hobby()),
                ("regret", self.regret()), ("fear", self.fear())]
        for topic, d in srcs:
            hit = next((k for k in d.get("kw", []) if len(k) >= 1 and k in p["raw"]), None)
            if not hit:
                continue
            tier = s["depth"].get(topic, 0) + 1
            if topic == "regret":
                tier = max(2, tier)
            if tier > 3 or not self.tier_ok(topic, tier):
                continue
            if topic == "why_here" and d.get("private") and self.v("trust") < 50:
                continue
            self.shared_now = True
            return self.share(topic, tier, lead=self.rng.choice([f"说到{hit}……", f"{hit}啊。", "我也是……" if topic in ("worry", "regret") else "", ""]))
        return False

    def on_fallback(self, p):
        self.add(comfort=1 if len(p["norm"]) > 3 else 0)
        echo = self.snippet(p["raw"], 10)
        if p["question"]:
            self.say(self.pick("fb_q", FALLBACK_Q, echo=echo))
            return True  # 没答上来，就别紧接着换话题问别的
        ack = self.kw_ack(p)
        if ack:
            self.add(trust=1)
            self.say(ack)
            if self.resonate(p):
                self.heavy = True
            return True
        if self.resonate(p):
            self.heavy = True
            return True
        if p["sent"]:
            self.add(trust=1)
            self.say(self.pick("fb_s", SELF_RECV["pos" if p["sent"] > 0 else "neg"]))
        else:
            line = self.pick("fb", FALLBACK, echo=echo)
            self.npc_say(line)
        return "initiative" if self.rng.chance(0.5) else True

    # ── 问对方 ──
    def on_question(self, p, back=False):
        s = self.s
        topics = [t for t in p["topics"] if t not in ("contact",)]
        if back or (not topics and "ask_back" in p["intents"]):
            topics = [s.get("last_topic") or "life"]
        if not topics and s.get("last_shared") and (len(p["norm"]) <= 12 or re.search(r"什么事|为什么|怎么了|然后呢|后来呢|真的吗|什么意思|怎么回事|是什么", p["raw"])):
            topics = [s["last_shared"]]  # 追问对方刚说的那件事
            line = self.pick("follow", FOLLOW_UP)
            if line:
                self.say(line)
        if not topics:
            return self.on_fallback(p)
        topic = topics[0]
        if topic == "us":
            self.add(trust=2, comfort=2)
            self.say(self.pick("us_" + s["rel"], US_ANSWER[s["rel"]]))
            return True
        if not back and not p["about_you"] and p["about_me"] and topic in ("work", "why_here", "hobby", "worry", "family", "love", "regret", "city") \
                and not re.search(r"我(们)?(该|要|能|可以|是不是|会不会)", p["raw"]):
            return self.on_self({**p, "self": p["self"] or [("recent", self.snippet(re.sub(r"^我[？?]", "", p["raw"])))]})
        if topic == "fault" and has_any(p["raw"], ["掉", "安全", "危险", "坠"]):
            topic = "safety"
        s["last_topic"] = topic
        s["topics"][topic] = s["topics"].get(topic, 0) + 1
        if topic == "drift":
            return self.answer_drift(p)
        if topic == "safety":
            self.add(comfort=2)
            self.say(self.pick("safety", SAFETY))
            return True
        if topic == "weather" and "雨" in p["raw"]:
            self.add(comfort=2, tension=-2)
            self.say(self.pick("rain", WEATHER_RAIN))
            return True
        if topic in SMALL_BANKS:
            self.add(comfort=2, tension=-2)
            self.say(self.pick(topic, SMALL_BANKS[topic]))
            if topic == "injury" and self.claustro() and s["anxious"] > 0 and self.rng.chance(0.5):
                self.narrate("{ta}说完，又看了一眼门。")
            return True
        if topic == "name":
            return self.answer_name()
        if topic == "recognize":
            self.add(curiosity=3)
            self.say(self.pick("recog_" + s["rel"], RECOGNIZE[s["rel"]]))
            return True
        if topic == "impression" and re.search(r"做什么|干什么|哪行|职业|干嘛的|工作", p["raw"]):
            self.add(curiosity=3, comfort=2)
            m = self.mem_of("job")
            if m:
                self.say(self.fill("你不是说了吗，「{x}」。……不过说实话，看着不太像。", x=m["x"]))
            else:
                self.say(self.pick("guess", ["我猜……你是做设计的？", "老师吧？你说话有那种语气。", "猜不出来。反正不像坐办公室的。",
                                             "看着像程序员。……别生气，这是夸你。", "你像那种……做什么都很认真的人。职业我就猜不出来了。"]))
            return True
        if topic == "impression":
            self.add(curiosity=3)
            key = "quarrel_ok" if s["rel"] == "quarrel" and s["quarrel"] >= 1 and self.v("tension") < 50 else s["rel"]
            self.say(self.pick("impr_" + key, IMPRESSION[key]))
            return True
        if topic == "shared":
            return self.answer_shared(p)
        if topic == "quarrel":
            return self.answer_quarrel(p)
        if topic == "unsaid":
            self.say(self.pick("uns_none", ["我没想说什么啊。", "嗯？没有啊。"]))
            return True
        if topic in ("work", "why_here", "hobby"):
            return self.answer_tiered(topic)
        if topic in PRIVATE:
            return self.answer_private(topic, p)
        if topic == "animal":
            if self.npc["contrast"] == "soft_on_animals" or self.worry().get("animal"):
                self.add(comfort=5, trust=3)
                if self.worry().get("animal"):
                    return self.answer_private("worry", p)
                self.say(self.pick("animal_soft", ["〔{ta}的表情一下软了〕我以前养过一只猫，胖得跳不上沙发。", "有啊。说起这个我能说一个小时，你确定要听？"]))
                return True
            self.say(self.pick("animal", ["没养过，我连自己都养不太好。", "小时候养过金鱼，养了三天。"]))
            return True
        if topic in ("bored", "life", "city", "food"):
            self.say(self.pick(topic, SMALL_BANKS[topic]))
            return True
        return self.on_fallback(p)

    def answer_drift(self, p):
        s = self.s
        rel = s["rel"]
        if rel == "estranged":
            return self.answer_estranged()
        if rel == "oldfriend":
            self.heavy = True
            if s.get("drift_told"):
                self.say(self.pick("drift_again", ["我刚才说了……就是那样。", "〔{ta}低头笑了一下〕都怪我。"]))
                return True
            if self.v("trust") >= 45 or s["unsaid"] == "started":
                s["drift_told"] = True
                s["deep"] += 1
                self.add(trust=5, tension=-6, contact=5)
                if s["unsaid"] == "started":
                    s["unsaid"] = "resolved"
                self.say(self.pick("drift_old", DRIFT_OLD))
                return True
            self.say(self.pick("drift_shy", ["……这个说来话长。", "〔{ta}愣了一下〕也没什么原因。就是……忙吧。"]))
            return True
        if rel == "quarrel":
            return self.answer_quarrel(p)
        self.say(self.pick("drift_none", ["我们……以前有联系吗？", "嗯？你是说谁？"]))
        return True

    def answer_name(self):
        s = self.s
        if self.told_name and s["rel"] == "stranger":
            self.say(self.pick("name_recv", NAME_RECV))
        if s["name_known"]:
            if s["rel"] == "stranger":
                self.say(self.pick("name_again", ["我刚说了呀，{n}。", "{n}。你忘啦？"]))
            else:
                self.say(self.pick("name_rel", ["……你连我名字都忘了？", "〔{ta}挑了下眉〕你认真的？我是{n}啊。"]))
            return True
        if self.has("cautious", "quiet") and self.v("trust") < 25:
            s["name_known"] = True
            self.add(trust=2)
            self.say(self.fill("……姓{sur}。"))
            self.narrate("过了一会儿，{ta}又补了一句名字。")
            return True
        s["name_known"] = True
        self.add(trust=3, comfort=2)
        self.say(self.pick("name", NAME_GIVE))
        if not s["my_name_known"] and self.rng.chance(0.6):
            self.ask("你呢？", "name")
        return True

    def share(self, topic, tier, lead=""):
        """对方说出自己某个话题的第 tier 层。"""
        s = self.s
        src = {"work": self.job, "why_here": self.reason, "hobby": self.hobby, "worry": self.worry, "fear": self.fear,
               "regret": self.regret}.get(topic)
        if not src:
            return False
        d = src()
        if topic == "why_here" and d.get("private") and tier >= 2 and self.v("trust") < 50:
            return False
        cur = s["depth"].get(topic, 0)
        if tier >= 2 and cur < tier - 1 and f"t{tier - 1}" in d and topic in ("worry", "work"):
            self.say(d[f"t{tier - 1}"])  # 先把前因说了，不然后面那句没头没尾
        key = f"t{tier}"
        if key not in d:
            if tier == 1 and "t2" in d:
                key = "t2"
            else:
                return False
        text = d[key]
        if topic == "work" and s["rel"] == "coworker" and tier == 1:
            text = f"我在{self.pack()['npc_dept']}，你知道的。" + text
        self.say((lead + text) if lead and not text.startswith("〔") else text)
        s["depth"][topic] = max(s["depth"].get(topic, 0), tier)
        s["last_shared"] = topic
        self.shared_now = True
        kws = d.get("kw", [])
        s["npc_facts"].append({"topic": topic, "tier": tier, "kw": kws or [text[:4]]})
        if tier >= 2 and topic in PRIVATE:
            self.add(trust=4, contact=3, comfort=2)
            s["deep"] += 1
            self.heavy = True
        if tier == 3:
            s["deep"] += 1
            self.add(contact=5)
            self.setflag(f"deep_{topic}")
        return True

    def answer_tiered(self, topic):
        s = self.s
        cur = s["depth"].get(topic, 0)
        if topic == "why_here" and self.reason()["private"]:
            if cur == 0:
                return self.share(topic, 1) or True
            if not self.tier_ok("why_here", 2):
                return self.refuse(topic)
        if cur >= 3 or (cur >= 2 and topic in ("why_here", "hobby")):
            self.say(self.pick("done_" + topic, ["就这些了，没什么好说的。", "我说得差不多了。你呢？", "〔{ta}耸耸肩〕"]))
            return True
        tier = cur + 1
        if not self.tier_ok(topic, tier):
            if cur == 0:  # 最浅的总会说一点
                return self.share(topic, 1) or True
            self.say(self.pick("shy_" + topic, ["就……普通。", "也没什么特别的。", "嗯，就那样。"]))
            return True
        self.add(trust=1, comfort=1)
        return self.share(topic, tier) or True

    def refuse(self, topic):
        s = self.s
        n = s["npc_refused"].get(topic, 0) + 1
        s["npc_refused"][topic] = n
        if n == 1:
            self.add(comfort=-3, tension=3)
            self.say(self.pick("defl_" + self.main, DEFLECT[self.main]))
        elif n == 2:
            self.add(comfort=-6, tension=8, trust=-4)
            s["pushy"] += 1
            self.say(self.pick("defl2", DEFLECT_FIRM))
        else:
            self.add(comfort=-10, tension=15, trust=-8, contact=-10)
            s["pushy"] += 1
            s["quiet_until"] = s["turn"] + 2
            self.npc_say(self.pick("defl3", DEFLECT_ANGRY))
        return True

    def answer_private(self, topic, p):
        s = self.s
        self.heavy = True
        cur = s["depth"].get(topic, 0)
        refused = s["npc_refused"].get(topic, 0)
        if topic == "family":
            if self.npc["contrast"] == "family_quiet":
                if refused or not self.tier_ok("family", 3):
                    if refused:
                        return self.refuse(topic)
                    s["npc_refused"][topic] = 1
                    self.add(comfort=-2)
                    self.say(self.pick("fam_quiet", ["〔{ta}的话一下子少了〕……还行吧。", "〔{ta}笑了一下，没接〕家里啊……就那样。"]))
                    return True
            if self.worry().get("family"):
                if not self.tier_ok("worry", s["depth"].get("worry", 0) + 1) and not refused:
                    return self.light("family")
                return self.answer_private("worry", p)
            if self.tier_ok("family", 1):
                self.say(self.pick("fam", ["我爸妈在老家，身体都还行。就是每次打电话都问我什么时候回去。", "家里人挺好的。就是我们家的人不太会说话，有事都憋着。"]))
                self.add(trust=2)
                return True
            return self.refuse(topic) if refused else self.light(topic)
        if topic == "love":
            if s["rel"] == "estranged" and "前任" in self.pack().get("was", ""):
                if self.tier_ok("worry", 2):
                    self.say(self.pick("love_ex", ["……现在没有。你呢？算了，别回答。", "〔{ta}看着你〕你问这个干嘛。……没有。"]))
                    return True
                return self.refuse(topic)
            if self.tier_ok("worry", 2) or refused == 0:
                return self.light(topic) if not self.tier_ok("worry", 2) else (self.say(self.pick("love_open", [
                    "单着。上一段……不提了。", "有过。现在没有。挺好的，至少今天被困在这儿，没人等我报平安。"])) or True)
            return self.refuse(topic)
        if topic == "worry" and self.worry().get("family") and self.npc["contrast"] == "family_quiet" and not self.tier_ok("worry", 3):
            return self.refuse(topic) if refused else self.light("worry")
        tier = cur + 1
        if tier > 3:
            self.say(self.pick("priv_done", ["……我能说的都说了。", "就是这样。", "〔{ta}没再说话，只是点了点头〕"]))
            return True
        if topic == "fear" and self.claustro() and s["anxious"] > 0 and tier <= 2:
            self.setflag("fear_told")
            return self.share("fear", 2) or True
        if topic == "regret" and tier == 1:
            tier = 2
        if self.tier_ok(topic, tier):
            if refused:  # 之前没说，现在愿意说了
                self.say(self.pick("now_ok", ["……你刚才问的那个。我现在可以说一点。", "〔{ta}沉默了一会儿〕好吧。"]))
            lead = self.rng.choice(["还行吧。就是", "就那样。", "还凑合。"]) if topic == "worry" and tier == 1 and re.search(r"过得|还好吗|怎么样", p["raw"]) else ""
            return self.share(topic, tier, lead=lead) or True
        if refused or cur > 0:
            return self.refuse(topic)
        return self.light(topic)

    def light(self, topic):
        """轻描淡写地带过去（不算拒绝，但也没说什么）。"""
        self.s["npc_refused"][topic] = self.s["npc_refused"].get(topic, 0) + 1
        bank = LIGHT.get(topic)
        if bank:
            self.say(self.pick("light_" + topic, bank))
        else:
            self.say(self.pick("defl_" + self.main, DEFLECT[self.main]))
        return True

    def answer_shared(self, p):
        s = self.s
        rel = s["rel"]
        if rel not in ("oldfriend", "estranged"):
            if rel == "coworker":
                self.say(self.pick("sh_cw", ["我们……好像没一起干过什么？除了年会。", "我们以前没怎么说过话吧。"]))
            else:
                self.say(self.pick("sh_st", ["我们以前见过吗？", "我应该……不认识你吧？"]))
            return True
        if rel == "estranged" and self.v("tension") > 60 and self.v("trust") < 40:
            self.add(tension=2)
            self.say(self.pick("sh_hold", ["……以前的事，就别说了吧。", "〔{ta}别开脸〕"]))
            return True
        self.add(comfort=5, trust=3, contact=3, tension=-4)
        s["shared_n"] = s.get("shared_n", 0) + 1
        s["last_npc_topic"] = "shared"
        self.say(self.pick("shared", SHARED_RECALL))
        if rel == "estranged":
            s["estr"] = max(s["estr"], 1)
            self.heavy = True
        if rel == "oldfriend" and s["shared_n"] == 2 and not s.get("mutual_told"):
            s["mutual_told"] = True
            self.say(self.pick("mutual", MUTUAL))
        return True

    def conciliate(self, kind, reply=None):
        """争执局里的一个和解动作：道歉、认对方的理、约以后、给台阶……
        第一次可能被顶回来；对方说出自己的委屈以后，再有一种不同的努力，才算真的和好。"""
        s = self.s
        self.heavy = True
        if s["quarrel"] == 0 and self.v("tension") > 72 and not self.flag("apology_tried"):
            self.setflag("apology_tried")
            self.add(tension=-8, trust=2)
            self.npc_say(self.pick("apq_low", APOLOGY_QUARREL_LOW))
            return True
        self.q_step(kind)
        if s["quarrel"] == 0:
            self.add(tension=-14, trust=8, comfort=6)
            s["quarrel"] = 1
            s["q1_turn"] = s["turn"]
            self.npc_say(self.pick("apq", APOLOGY_QUARREL))
            return True
        if self.quarrel_can_settle():
            self.add(tension=-10, trust=6, comfort=5)
            self.settle_quarrel()
            if reply:
                self.say(reply)
            return True
        self.add(tension=-4, trust=2)
        self.npc_say(self.pick("q_wait", ["〔{ta}点了点头，没说话〕", "……嗯。我知道了。", "〔{ta}看着别处〕你让我缓一缓。"]))
        return True

    def q_step(self, kind):
        """和解的一步。同一种努力只算一次：连说三遍对不起，不如道歉之后再把事情谈开。"""
        steps = self.s.get("q_steps")
        if not isinstance(steps, list):
            steps = []
        if kind not in steps:
            steps.append(kind)
        self.s["q_steps"] = steps

    def quarrel_can_settle(self):
        """和解要不止一步：道歉、把事情谈开、约以后、给对方台阶……凑够两步，对方才真的放下。"""
        s = self.s
        return s["quarrel"] == 1 and len(s.get("q_steps") or []) >= 2 and self.v("tension") < 72

    def settle_quarrel(self):
        s = self.s
        s["quarrel"] = 2
        self.setflag("quarrel_resolved")
        self.add(contact=4)
        self.npc_say(self.pick("qsoft", QUARREL_SOFT))

    def answer_quarrel(self, p):
        s = self.s
        self.heavy = True
        if s["rel"] != "quarrel":
            if s["rel"] == "estranged":
                return self.answer_estranged()
            self.say(self.pick("q_none", ["吵架？谁跟谁？", "我们没吵过架吧？"]))
            return True
        if s["quarrel"] == 0 and (self.v("tension") > 65 or self.v("trust") < 25):
            self.add(tension=2)
            self.say(self.pick("q_hold", QUARREL_HOLD))
            s["q_held"] = s.get("q_held", 0) + 1
            if s["q_held"] >= 3:
                s["pushy"] += 1
            return True
        if s["quarrel"] == 0:
            s["quarrel"] = 1
            s["q1_turn"] = s["turn"]
            self.q_step("talk")
            self.add(tension=-8, trust=4)
            self.say(self.pick("q_raise", QUARREL_RAISE))
            return True
        if s["quarrel"] == 2:
            self.say(self.pick("q_done", ["都过去了，不说了。", "〔{ta}摆摆手〕翻篇了。", "行了，我没生气了。"]))
            return True
        self.q_step("talk")
        if self.quarrel_can_settle():
            self.add(tension=-10, trust=6, comfort=5)
            self.settle_quarrel()
            return True
        self.add(tension=-3, trust=2)
        self.say(self.pick("q_mid", ["……我也不知道怎么说。反正我不是故意的。", "我还在想。你让我想想。", "〔{ta}叹了口气〕吵都吵了。"]))
        return True

    def answer_estranged(self):
        s = self.s
        self.heavy = True
        if self.v("trust") >= 50 and self.v("comfort") >= 35:
            s["estr"] = 2
            self.setflag("estr_talked")
            self.add(trust=6, tension=-10, contact=5)
            s["deep"] += 1
            self.say(self.pick("e_drift", ESTRANGED_DRIFT))
            if s["unsaid"] == "started":
                s["unsaid"] = "resolved"
            return True
        s["estr"] = max(1, s["estr"])
        self.say(self.pick("e_raise", ESTRANGED_RAISE))
        return True

    def on_unsaid_ask(self, p):
        s = self.s
        s["unsaid"] = "resolved"
        self.add(trust=6, comfort=4, tension=-6, contact=6)
        s["deep"] += 1
        what = s.get("unsaid_what")
        self.say(self.pick("uns_open", ["〔{ta}看着门缝，深吸了一口气〕", "〔{ta}犹豫了很久〕……好吧。"]))
        if what == "estr":
            self.setflag("estr_talked")
            s["estr"] = 2
            self.say(self.pick("e_drift", ESTRANGED_DRIFT))
        elif what == "quarrel":
            self.settle_quarrel()
        elif what == "worry":
            self.share("worry", min(3, s["depth"].get("worry", 0) + 1))
        elif what == "drift":
            s["drift_told"] = True
            self.say(self.pick("drift_old", DRIFT_OLD))
        else:
            self.say(self.pick("uns_thanks", ["我想说，今天要是只有我一个人在这里，我可能会疯掉。谢谢你。", "跟你说话，比我想的轻松。就这个。"]))
        return True

    @staticmethod
    def snippet(t, n=14):
        t = re.sub(r"^(我|嗯|啊|哦|就是|其实|那个)[，, ]*", "", str(t).strip())
        t = re.split(r"[，。！？!?；;]", t)[0]
        return t[:n]

    # ── 回合之后：对方主动、时间推进、环境 ──
    def after_turn(self, p, handled):
        s = self.s
        st = s["st"]
        if s["phase"] == "ended":
            return
        # 关系随相处慢慢变化：舒适的时候紧张自然消退，想继续联系的意愿随好感慢慢长
        st["tension"] = clamp(st["tension"] - (2 if st["comfort"] > 40 else 1))
        st["contact"] = clamp(st["contact"] + (st["comfort"] + st["trust"] - 1.5 * st["tension"]) / 60)
        if not p["silence"] and "ack" not in p["intents"]:
            s["short_run"] = 0
        if self.claustro() and s["anxious"] > 0 and s["turn"] >= 3 and not p["silence"] and self.spoke:
            if self.rng.chance(0.25):  # 说说话能分散注意力
                s["anxious"] -= 1
        # 站位：有没有转过身、有没有坐下
        if s["insults"] or "hostile" in p.get("intents", ()) and not p.get("silence"):
            s["pose"] = "sit_away" if s["pose"] in ("sit", "sit_away") else "away"
        elif s["pose"] in ("far", "away") and st["tension"] < 62 and self.spoke and not p.get("silence"):
            s["pose"] = "stand"
        elif s["pose"] == "sit_away" and st["tension"] < 55:
            s["pose"] = "sit"
        elif s["pose"] == "stand" and st["comfort"] >= 40 and self.spoke and not p.get("silence"):
            s["pose"] = "facing"
        self.maybe_initiative(p, handled)
        self.maybe_recall(p)
        self.advance(p)

    def blocked(self, topic):
        """你拒绝过的话题不再问；拒绝过私人问题以后，别的私人问题也不问了；拒绝过两次以上，连工作这些也少问。"""
        b = self.s["my_boundary"]
        if topic in b:
            return True
        if topic in PERSONAL_ASK and (set(b) & PERSONAL_ASK or "personal" in b):
            return True
        if topic in ("work", "why_here", "hobby", "city", "animal") and len(b) >= 2:
            return True
        return False

    def maybe_initiative(self, p, handled):
        s = self.s
        if s["phase"] != "play" or self.npc_asked or s["turn"] <= s["quiet_until"] or self.heavy:
            return
        if s["insults"] >= 1 or self.v("tension") >= 85:
            return
        base = {"talkative": 0.5, "humor": 0.38, "sensitive": 0.28, "rational": 0.22, "cautious": 0.18, "quiet": 0.1}[self.main]
        base += self.v("curiosity") / 250
        if handled == "initiative":
            base += 0.45
        if p.get("silence") and handled != "initiative" and not self.has("talkative"):
            base *= 0.3  # 你不说话，安静的人也就不怎么问
        if s["npc_asked_last"]:
            base -= 0.25
        if len(self.out) > 6:
            base -= 0.2
        if not self.rng.chance(base):
            return
        self.initiative()

    def initiative(self):
        s = self.s
        cands = []
        for it in INITIATIVES:
            if it["id"] in s["inits"] or s["stage"] not in it["stage"]:
                continue
            if "rel" in it and s["rel"] not in it["rel"]:
                continue
            if "tr" in it and not any(t in self.tr for t in it["tr"]):
                continue
            if "contrast" in it and self.npc["contrast"] != it["contrast"]:
                continue
            if it.get("notcontrast") == self.npc["contrast"]:
                continue
            if it.get("claustro") and not (self.claustro() and s["anxious"] > 0):
                continue
            need = it.get("need", {})
            if not self.ok_line({"t": "", **need}):
                continue
            topic = it.get("ask")
            if topic and self.blocked(topic):
                continue
            if it["t"] in ("WORRY", "ESTRANGED") and len(s["my_boundary"]) >= 2:
                continue
            if topic and self.mem_of({"work": "job"}.get(topic, topic)):
                continue
            if it["id"] == "i_late" and s["depth"].get("why_here"):
                continue
            if it["id"] == "i_signal" and s.get("did_phone"):
                continue
            if it["id"] == "i_alarm" and s.get("did_alarm"):
                continue
            if it["id"] == "i_mutual" and s.get("mutual_told"):
                continue
            about = it.get("about") or topic
            if about and (about in s["topics"] or about in s.get("npc_topics", [])):
                continue
            cands.append(it)
        if not cands:
            return
        generic = {"i_alarm", "i_signal", "i_hurt", "i_weather", "i_city", "i_hobby", "i_bored"}
        if s["turn"] >= 5:  # 聊了一阵以后，不再问那些谁都会问的
            cands = [c for c in cands if c["id"] not in generic]
        elif s["silence_run"] >= 2:
            cands = [c for c in cands if c["id"] not in generic] or cands
        if s["rel"] in ("quarrel", "estranged") and self.v("tension") >= 55:  # 关系还僵着，不会没话找话
            cands = [c for c in cands if c.get("rel") and set(c["rel"]) <= {"quarrel", "estranged", "oldfriend"}]
        if not cands:
            return
        it = self.rng.choice(cands)
        s["inits"].append(it["id"])
        t = it["t"]
        if isinstance(t, list):
            t = self.rng.choice(t)
        if it["id"] == "i_mutual":
            s["mutual_told"] = True
        if t == "QUARREL":
            if s["quarrel"] == 0:
                s["quarrel"] = 1
                s["q1_turn"] = s["turn"]
                self.add(tension=-6)
                self.say(self.pick("q_raise", QUARREL_RAISE))
            elif self.quarrel_can_settle() and self.has("honest_late", "sensitive", "talkative", "rational") or self.npc["contrast"] == "honest_late" and self.quarrel_can_settle():
                self.settle_quarrel()
            return
        if t == "NAME":
            if not s["name_known"]:
                s["name_known"] = True
                self.add(trust=2)
                self.say(self.fill(self.rng.choice(["对了，我叫{given}。", "我们困了这么久，还不知道名字。我叫{given}。", "……我叫{given}。"]), given=self.npc["name"]))
                if not s["my_name_known"]:
                    self.ask("你呢？", "name")
            return
        if t == "ESTRANGED":
            self.answer_estranged()
            return
        if t == "WORRY":
            tier = max(2, s["depth"].get("worry", 0) + 1)
            if "worry" not in s["npc_refused"] and tier <= 3 and self.tier_ok("worry", tier):
                self.say(self.pick("w_lead", ["我能说个事吗？你听听就行。", "〔{ta}忽然开口〕说出来你可能觉得没什么……"]))
                self.share("worry", tier)
            return
        if t == "FEAR":
            self.setflag("fear_told")
            self.share("fear", 2)
            return
        if it.get("about"):
            s.setdefault("npc_topics", []).append(it["about"])
        if it.get("share"):
            self.say(self.fill(t))
            s["last_shared"] = it["share"]
            return
        self.ask(self.fill(t), it.get("ask"))

    def maybe_recall(self, p):
        s = self.s
        if s["phase"] != "play" or self.npc_asked or s["turn"] <= s["quiet_until"]:
            return
        cl = self.mem_of("claustro")
        force = bool(cl) and ["claustro", cl["x"]] not in s["recalled"] and s["turn"] - cl["turn"] >= 2
        if force or (s["turn"] - s.get("last_recall", -9) >= 4 and self.rng.chance(0.35)):
            line = self.recall_line(force=force)
            if line:
                s["last_recall"] = s["turn"]
                self.npc_say(line)

    def advance(self, p):
        s = self.s
        step = 9.0 if p["silence"] else 5.0 + self.rng.random() * 2.2
        if s["turn"] >= 20:
            step += 6
        before = s["progress"]
        s["progress"] = min(100.0, s["progress"] + step)
        if s["turn"] >= 24:
            s["progress"] = max(s["progress"], 86.0)
        # 阶段
        if s["stage"] == 1 and (s["turn"] >= 3 or s["progress"] >= 20):
            s["stage"] = 2
        if s["stage"] == 2 and (s["turn"] >= 12 or (s["turn"] >= 6 and self.v("trust") >= 42 and self.v("comfort") >= 38)):
            s["stage"] = 3
        # 维修节拍
        beat = None
        for pct, text in REPAIR_BEATS:
            if before < pct <= s["progress"] and pct not in s["beats"]:
                s["beats"].append(pct)
                beat = text
        if s["phase"] == "closing":
            s["closing_left"] -= 1
            if s["closing_left"] <= 0:
                self.finish()
            return
        if s["progress"] >= 85 and s["phase"] == "play":
            self.blank()
            self.narrate(beat or REPAIR_BEATS[-1][1])
            self.enter_closing()
            return
        if beat:
            self.blank()
            self.narrate(beat)
            s["last_ambient"] = s["turn"]
            return
        prog = [x for t0, x in AMBIENT_PROGRESS if t0 <= s["turn"] and x not in s.setdefault("progress_lines", [])]
        if prog and (s["turn"] - s["last_ambient"] >= 2 or p.get("silence")):
            s["progress_lines"].append(prog[0])
            s["last_ambient"] = s["turn"]
            self.blank()
            self.narrate(prog[0])
            return
        if s["turn"] - s["last_ambient"] >= 3 and self.rng.chance(0.45):
            pool = [x for x in AMBIENT.get(s["stage"], AMBIENT[2]) if x not in s["ambient"]]
            if pool:
                a = self.rng.choice(pool)
                s["ambient"].append(a)
                s["last_ambient"] = s["turn"]
                self.blank()
                self.narrate(a)
                if self.claustro() and s["anxious"] > 0 and self.rng.chance(0.5):
                    line = self.pick("sil_cl", SILENCE_CLAUSTRO, once=True)
                    if line:
                        self.narrate(line)

    def enter_closing(self):
        s = self.s
        s["phase"] = "closing"
        s["stage"] = 4
        s["closing_left"] = 2
        sc = self.contact_score()
        lvl = "high" if self.v("trust") >= 55 and self.v("comfort") >= 45 else "low" if self.v("tension") >= 60 or s["insults"] else "mid"
        self.npc_say(self.pick("close_" + lvl, CLOSING_NPC[lvl]))
        # 有话没说
        what = None
        if s["rel"] == "estranged" and s["estr"] < 2 and self.v("trust") >= 40 and not s["insults"]:
            what = "estr"
        elif s["rel"] == "quarrel" and s["quarrel"] == 1 and self.v("trust") >= 35 and not s["insults"]:
            what = "quarrel"
        elif s["rel"] == "oldfriend" and not s.get("drift_told") and self.v("trust") >= 45 and not s["insults"]:
            what = "drift"
        elif s["depth"].get("worry", 0) == 1 and self.v("trust") >= 50:
            what = "worry"
        elif self.flag("helped_anxious") and self.v("trust") >= 55 and self.rng.chance(0.5):
            what = "thanks"
        if what and s["pushy"] < 2:
            s["unsaid"] = "started"
            s["unsaid_what"] = what
            self.say(self.pick("uns_start", UNSAID_START))
            self.narrate(self.rng.choice(["门外的人喊了一声「好了！站稳！」，{ta}停住了。", "对讲机里突然响起很大的声音，{ta}的话被打断了。"]))
            self.narrate("{ta}张了张嘴，看了你一眼，没再往下说。（{ta}好像还有话没说完。）")
            return
        # 对方主动要联系方式
        c = s["contact"]
        if not c["exchanged"] and not c["npc_declined"] and not c["player_declined"] and sc >= 68 and not self.has("quiet") \
                and (self.has("talkative", "humor") or sc >= 78):
            c["npc_asked"] = True
            self.ask(self.pick("c_ask", CONTACT_ASK_NPC), "contact_q")

    def finish(self):
        s = self.s
        self.blank()
        self.narrate(RESTART)
        if s["unsaid"] == "started":
            self.narrate(UNSAID_CUT)
            s["unsaid"] = "cut"
        self.narrate(DOOR_OPEN)
        typ = self.decide_ending()
        text = compose_ending(self, typ)
        s["ending"] = {"type": typ, "title": ENDING_TITLES[typ], "text": text, "turns": s["turn"]}
        s["phase"] = "ended"
        self.blank()
        for line in text:
            self.out.append(line)
        self.blank()
        self.out.append(f"—— 结局 {typ}：{ENDING_TITLES[typ]} ——（{s['turn']} 轮）")
        self.out.append("（status 回看结局，new force 再来一局：换一个人、一种关系，故事会不一样。）")

    def decide_ending(self):
        s = self.s
        st = s["st"]
        h = s.get("hostile", 0)
        if s["insults"] >= 2 or s["pushy"] >= 3 or h >= 3 or (h >= 2 and st["tension"] >= 55) or (st["tension"] >= 70 and st["trust"] < 30) \
                or (s["insults"] and st["trust"] < 40) or (h and st["tension"] >= 68 and st["trust"] < 35):
            return "D"
        if s["contact"]["exchanged"]:
            return "B"
        if s["unsaid"] == "cut":
            return "C"
        if (self.flag("confessed") and not self.flag("estr_talked")) or self.flag("friend_pending"):
            return "C"
        changed = st["trust"] - s["st0"]["trust"] >= 22 or self.flag("quarrel_resolved") or self.flag("estr_talked") or s["deep"] >= 2
        quiet_bond = s.get("quiet_shared") and self_ok_quiet(self) and st["comfort"] - s["st0"]["comfort"] >= 30 and st["tension"] < 35
        if quiet_bond and st["trust"] >= 30 and not changed:
            self.setflag("quiet_bond")
            s["e_cause"] = "quiet"
            return "E"
        if self.flag("quarrel_resolved") or (self.flag("estr_talked") and st["trust"] >= 35):
            s["e_cause"] = "quarrel" if self.flag("quarrel_resolved") else "estr"
            return "E"
        if changed and st["trust"] >= 45:
            s["e_cause"] = ("quarrel" if self.flag("quarrel_resolved") else "estr" if self.flag("estr_talked") else
                            "deep" if s["deep"] >= 2 else "growth")
            return "E"
        if s["rel"] in ("quarrel", "estranged") and st["tension"] >= 40 and st["trust"] >= 30 and (s["quarrel"] == 1 or s["estr"] == 1):
            return "C"
        if st["tension"] >= 62 and st["comfort"] < 25:
            return "D"
        return "A"


# ════════════════════ 结局文字 ════════════════════
WORRY_END = {
    "quit": "那封辞职信，我回去再看一遍。", "father": "我等会儿给我爸打个电话。就问问他吃了没有，也行。",
    "rent": "我回去想想，到底是走还是留。", "coldwar": "我今天回去，先开口。", "exam": "第三次，应该能过吧。",
    "dog": "我得早点回去了。它还在等我开门。", "kid": "我今天回去，敲敲他的门。", "debt": "钱的事，我明天就跟他说清楚。",
}
QUARREL_END = {
    "水电费": "水电费……这个月我来交吧。", "汇报的方案": "方案我改好了先发你看。", "装修的噪音": "晚上八点以后，我让他们别开电钻了。",
    "活动分工": "周末的事，我们回去重新分一下。",
}


def self_ok_quiet(g):
    return g.has("quiet", "cautious", "sensitive", "rational") and g.v("comfort") >= 40


def compose_ending(g, typ):
    s = g.s
    rel = s["rel"]
    r = g.rng
    ta = g.npc["ta"]
    n = g.label() if s["name_known"] else ta
    lines = []
    mem_why = g.mem_of("why_here")
    cb = []  # 带着这次对话内容的收尾
    if typ != "D":
        if s["depth"].get("worry", 0) >= 2 and r.chance(0.8):
            cb.append(f"{ta}说：「{WORRY_END.get(g.worry()['id'], '我回去想想。')}」")
        if mem_why and r.chance(0.7):
            cb.append(f"{ta}指了指楼上：「你那个「{mem_why['x']}」，" + r.choice(["祝你顺利。」", "一切顺利。」", "去吧，别让人等。」"]))
        if g.flag("helped_anxious") and not (s["unsaid"] == "cut" and s.get("unsaid_what") == "thanks"):
            cb.append(r.choice([f"{ta}小声说：「刚才……谢谢你一直跟我说话。」", f"走出去的时候，{ta}回头说：「谢谢你。真的。」"]))
        if g.flag("player_claustro"):
            cb.append(r.choice([f"{ta}先让你出去：「你不是怕闷吗，快出去透透气。」", f"「你没事吧？」{ta}看着你，「刚才在里面挺能忍的。」"]))
        if s["rel"] == "quarrel" and g.flag("quarrel_resolved"):
            cb.append(f"{ta}停了一下：「{QUARREL_END.get(g.pack().get('about'), '刚才的事，算了。')}」")
        if g.flag("laughed") and r.chance(0.5):
            cb.append(r.choice([f"{ta}忍不住又笑了一下：「那个笑话，我会拿去用的。」", "你们俩在走廊里都没憋住笑，也不知道在笑什么。"]))
        if s.get("quiet_shared") and self_ok_quiet(g) and r.chance(0.6):
            cb.append(f"{ta}说：「刚才安静那一会儿，其实挺好的。」")
    r_cb = cb[:2]

    if typ == "A" and (s["st"]["tension"] >= 50 or s.get("hostile")):  # 冷淡收场：不写笑
        lines.append(r.choice([f"门一开，{n}先走了出去。", "你们一前一后出了电梯，谁都没说话。", f"{n}朝门口的师傅点了下头，没看你。"]))
        if s["rel"] == "quarrel" and not g.flag("quarrel_resolved"):
            lines.append(r.choice(["刚才那件事，谁都没再提。它还在那儿，等你们回去再说。", f"{ta}走得很快。{g.pack().get('about', '那件事')}的事，看来还没完。"]))
        lines.append(r.choice(["走廊很长。你们往两个方向走。", "你在门口站了一会儿，等{ta}走远了才动。".format(ta=ta), "电梯门在身后关上了。"]))
    elif typ == "A":
        lines.append(r.choice([f"{n}朝你点了点头：「那……走了啊。」", f"「终于。」{n}伸了个懒腰，「那我先走了。」", f"{n}拎起包：「今天辛苦了。」"]))
        lines += r_cb
        if s["rel"] == "quarrel" and not g.flag("quarrel_resolved"):
            lines.append("刚才那件事，谁都没再提。它还在那儿，等你们回去再说。")
        lines.append(r.choice(["你也点点头。两个人往不同的方向走，在走廊拐角，谁都没有回头。", f"{ta}往楼梯间走去，说今天再也不坐电梯了。你按了另一部电梯。",
                               "你们在门口互相说了声「慢走」。很普通的一句。"]))
    elif typ == "B":
        lines.append(r.choice(["走廊里信号一下子满了，你的手机震了好几下。好友申请那一栏，跳出来一个新的名字。",
                               f"出了电梯，{n}低头看了一眼手机：「通过了。」"]))
        if s["contact"].get("invite"):
            lines.append(r.choice(["「说好的那顿饭，」{ta}说，「我记着了。」".format(ta=ta), f"「吃饭的事，」{ta}晃了晃手机，「微信上说。」"]))
        lines += r_cb
        if rel in ("oldfriend", "estranged"):
            lines.append(r.choice(["这一次，那个名字大概不会再沉到列表最底下了。", "你们在门口站了一会儿，像是在等对方先走。最后是一起走的。"]))
        else:
            lines.append(r.choice(["你不知道以后会不会真的聊起来。也许只是列表里多了一个人。也许不止。", "你们一前一后走出大楼。外面果然下过雨，地是湿的。"]))
    elif typ == "C" and g.flag("confessed") and s["unsaid"] != "cut":
        lines.append(r.choice([f"{n}在门口停了很久。最后只说：「你刚才说的……我听见了。」", f"走出电梯，{n}回头看了你一眼，像要说什么，又没说。"]))
        lines.append("那句话停在了你们中间。没有被接住，也没有被推开。")
    elif typ == "C" and g.flag("friend_pending") and s["unsaid"] != "cut":
        lines.append(r.choice([f"「你刚才说的那个，」{n}在门口说，「我回去想想。」", f"{n}走了两步，回过头：「……我会想的。」"]))
        lines.append("你不知道这算不算答应。也许下一次，会由{ta}先开口。".format(ta=ta))
    elif typ == "C":
        if s["unsaid"] == "cut":
            lines.append(r.choice([f"{n}走出去两步，又回过头来，像是还想把刚才那句话说完。最后只说了一句：「那就……先这样吧。」",
                                   f"「刚才那个，」{n}张了张嘴，「……算了，没什么。」"]))
        else:
            lines.append(r.choice([f"{n}在门口停了一下，看着你，好像有什么话。走廊里有人在喊{ta}的名字，{ta}应了一声，走了。",
                                   "你们都没提刚才在里面没说完的事。门口的人太多了。"]))
        lines += r_cb[:1]
        lines.append(r.choice(["你站在原地想了一会儿，{ta}到底想说什么。".format(ta=ta), "有些话，可能就留在那两层楼之间了。"]))
    elif typ == "D":
        lines.append(r.choice([f"门一开，{n}第一个走了出去，没有回头。", f"{n}从你身边挤过去，肩膀碰了你一下。没说对不起。",
                               f"{n}出去的时候，对门口的维修师傅说了声谢谢，对你什么都没说。"]))
        lines.append(r.choice(["走廊里的灯很亮。你站了一会儿，才发现自己一直攥着拳头。", "你在原地等了一会儿，等{ta}走远，才往外走。".format(ta=ta),
                               "那半个小时，大概你们俩都不太想再记起来了。"]))
    else:  # E：第一句只写这局里真的发生过的变化
        cause = s.get("e_cause", "growth")
        if cause == "quarrel":
            first = r.choice([f"{n}在门口停下来：「你这个人……其实没我想的那么讨厌。」", f"「刚才那事，」{n}说，「就这么过去了吧。」说完自己先笑了一下。"])
        elif cause == "estr":
            first = r.choice([f"「这些年，」{n}说，「我一直以为你是在怪我。」{ta}摇了摇头，「原来不是。」",
                              f"{n}看着你：「那些话，早几年说出来就好了。」"])
        elif cause == "quiet":
            first = (f"{n}看着你，笑了一下。不是很开心的笑，但是真的。「能见到你，挺好的。」" if rel == "estranged" else
                     r.choice([f"{n}说：「刚才那一阵谁都没说话，挺好的。」", f"{n}朝你点了下头。那半个小时里你们没说几句话，但这一下点头，和刚进电梯时不一样。"]))
        elif cause == "deep":
            first = r.choice([f"「谢谢你听我说那些，」{n}说，「我很少跟人说。」", f"{n}在门口停了一下：「刚才那些话，我连家里人都没说过。」"])
        elif rel == "quarrel":
            first = f"{n}在门口停下来，看了你一眼。没说话，但脸色比进电梯的时候好多了。"
        elif rel == "estranged":
            first = f"{n}看着你，笑了一下。不是很开心的笑，但是真的。「能见到你，挺好的。」"
        elif rel == "oldfriend":
            first = r.choice([f"{n}拍了拍你的肩：「你变了。变得……挺好的。」", f"「你知道吗，」{n}说，「我以前一直觉得你是那种什么都不怕的人。」"])
        elif rel == "coworker":
            first = r.choice([f"{n}说：「以后在食堂碰到，我来找你坐。」说完又补了一句：「……要是你不介意的话。」", f"「我以前，」{n}说，「好像对你有点误会。」"])
        else:
            first = r.choice([f"{n}在门口转过身：「我刚才还在想，要是今天困在里面的是别人就好了。现在不这么想了。」",
                              f"「谢谢你，」{n}说，「不是因为什么。就是……谢谢。」"])
        lines.append(first)
        lines += r_cb
        lines.append(r.choice(["你们没有留联系方式。走出大楼的时候你想，也许不需要。", "你们在门口分开。没有约下次。但那半个小时，你大概会记很久。",
                               f"{ta}走了以后，你才发现自己还记得{ta}说的每一句话。"]))
    return [("  " + g.fill(x)) for x in lines if x]


# ════════════════════ 存档 ════════════════════
def set_save_path(path):
    global _save_path, _cache
    _save_path = path
    _cache = None
    return _save_path


def _consistent(s):
    need = ("v", "rng", "me", "npc", "rel", "st", "turn", "progress", "phase", "history")
    if not isinstance(s, dict) or any(k not in s for k in need):
        return False
    if s["rel"] not in RELATIONS or not all(k in s["st"] for k in STATE_KEYS):
        return False
    return s["phase"] in ("play", "closing", "ended")


def _migrate(s):
    base = fresh_state(s.get("seed", 1))
    for k, val in base.items():
        s.setdefault(k, copy.deepcopy(val))
    if not isinstance(s.get("q_steps"), list):
        s["q_steps"] = []
    s["v"] = VERSION
    return s


def _write(s):
    data = json.dumps(s, ensure_ascii=False)
    tmp = _save_path + ".tmp"
    d = os.path.dirname(_save_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if os.path.exists(_save_path):
        try:
            with open(_save_path, encoding="utf-8") as f:
                old = f.read()
            if old.strip():
                with open(_save_path + ".bak", "w", encoding="utf-8") as f:
                    f.write(old)
        except OSError:
            pass
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, _save_path)


_cache = None
_notes = []


def _load():
    global _cache
    if _cache is not None:
        return _cache
    for path, label in ((_save_path, None), (_save_path + ".bak", "备份")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
            if _consistent(s):
                _cache = _migrate(s)
                if label:
                    _notes.append("⚠️ 存档读不出来，已经从备份恢复（可能少了最后一步）。")
                return _cache
        except (OSError, ValueError):
            pass
        if label is None:
            try:
                os.replace(path, path + f".corrupt-{int(time.time())}")
                _notes.append("⚠️ 存档损坏了，已另存为 .corrupt 文件。")
            except OSError:
                pass
    return None


def new_game(seed=None, force=False, **kw):
    """开新局。已有没结束的进度时需要 force=True（旧档会另存为 .old-时间.json）。"""
    global _cache
    old = _load()
    if old and old["phase"] != "ended" and not force:
        return False
    if old and os.path.exists(_save_path):
        try:
            os.replace(_save_path, _save_path.replace(".json", "") + f".old-{int(time.time())}.json")
        except OSError:
            pass
    if seed is None:
        seed = int(time.time() * 1000) % 1000000007
    s = fresh_state(int(seed), **kw)
    _cache = s
    _write(s)
    return True


def export_code(s=None):
    s = s or _load()
    if not s:
        return ""
    raw = json.dumps(s, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "BF1-" + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")


def import_code(code):
    global _cache
    code = re.sub(r"\s+", "", str(code))
    if code.startswith("BF1-"):
        code = code[4:]
    try:
        s = json.loads(zlib.decompress(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if not _consistent(s):
        return False
    _cache = _migrate(s)
    _write(_cache)
    return True


def set_llm(fn):
    """可选：fn(messages: list[dict]) -> str。接上以后，对方说的话会请大模型按人物设定润色；出错时自动退回离线台词。"""
    global _llm
    _llm = fn


def openai_compatible(url, key, model, timeout=20):
    """生成一个 OpenAI 兼容接口（比如 xAI、DeepSeek）的 set_llm 函数。只用标准库。"""
    import urllib.request

    def call(messages):
        body = json.dumps({"model": model, "messages": messages, "temperature": 0.8, "max_tokens": 200}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    return call


def _llm_polish(g, lines):
    """把这一轮对方说的话交给大模型润色。只改说话的内容，不改发生了什么。"""
    if not _llm or not g.s.get("llm"):
        return lines
    speech = [i for i, x in enumerate(lines) if x.startswith(g.label() + "：「")]
    if not speech:
        return lines
    n = g.npc
    persona = (f"你在扮演一个被困在停电电梯里的人：{n['name']}，{n['gender']}，性格{TRAITS[n['traits'][0]]}、{TRAITS[n['traits'][1]]}，"
               f"{CONTRASTS[n['contrast']]}。和对方的关系：{RELATIONS[g.s['rel']]}。")
    hist = "\n".join(("对方" if w == "me" else "我") + "：" + t for w, t in g.s["history"][-10:])
    orig = "\n".join(lines[i] for i in speech)
    msg = [{"role": "system", "content": persona + "用自然、口语、简短的中文。不要煽情，不要总结人生，不要每句都提问。"
            "只改写措辞，保持原来的意思和态度，不要加新信息。逐行输出，行数和格式保持不变。"},
           {"role": "user", "content": f"最近的对话：\n{hist}\n\n请润色下面这几行：\n{orig}"}]
    try:
        out = [x.strip() for x in str(_llm(msg)).strip().splitlines() if x.strip()]
        if len(out) != len(speech) or any(len(x) > 220 for x in out):
            return lines
        for i, x in zip(speech, out):
            if not x.startswith(g.label() + "：「"):
                x = f"{g.label()}：「{x.strip('「」')}」"
            lines[i] = x
    except Exception:  # noqa: BLE001
        return lines
    return lines


# ════════════════════ 指令 ════════════════════
HELP = """🛗 楼层之间（Between Floors）· 指令

开局
  new                        随机开一局（已有没结束的局会被拒绝，防止误删）
  new force                  放弃当前这局，重新开始（旧档会另存一份）
  new 关系=同事 我=女 对方=男 名字=林夏 性格=沉默,幽默 种子=42 force
  new 名字=林夏 职业=插画师 背景=刚搬来这个城市
                             指定设定（都可以省略，不写就随机；写错了会提示，不会悄悄开成别的）
                             关系：陌生人 / 同事 / 旧友 / 争执（刚吵过架的熟人）/ 疏远（曾经很亲近的人）
                             性格：谨慎 健谈 幽默 沉默 敏感 理性（写一个或两个）
说话
  say <你想说的话>            自由说中文，什么都可以说。也可以不写 say，直接说
                             （指令后面跟了别的字，比如「look 一下外面」「help me」，会当成你说的话）
  ……  /  silence  /  wait    不说话，安静地待一会儿（时间会往前走）
  do <动作>                   做个小动作：看手机、按警铃、坐下、站起来、递水、递饼干、打开手电、关手电、敲门、扇风、听歌
看
  look                       看看电梯里现在的样子
  info                       你知道的设定：你是谁、对方是谁、你们的关系、你已经知道的关于对方的事
  history [条数]              回看最近的对话
  status                     是否已经到了结局；结局之后可以回看结局
存档
  （每一句都会自动存档）
  export                     生成存档码。环境会重置的话，先把它记下来
  import <存档码>             从存档码恢复，接着玩
  code on / code off         每次回复末尾都附上最新的存档码（适合随时可能断线的环境）
其他
  llm on / llm off           如果宿主程序用 set_llm() 接了大模型，可以让它润色对方的台词（默认关闭）
  help                       这份说明

一局大约 12~24 句话。不用赶，没有倒计时。也不必非要聊出什么——等电梯修好，就会结束。"""


def _setup_kw(args):
    """解析 new 后面的设定。返回 (种子, 是否 force, 设定, 错误信息)。"""
    kw = {}
    force = False
    errors = []
    keymap = {"关系": "rel", "rel": "rel", "我": "me", "me": "me", "对方": "npc", "npc": "npc", "名字": "name", "name": "name",
              "性格": "traits", "trait": "traits", "traits": "traits", "种子": "seed", "seed": "seed",
              "职业": "job", "job": "job", "背景": "bg", "bg": "bg"}
    for a in args:
        if a in ("force", "强制", "重来"):
            force = True
            continue
        if "=" in a or "：" in a or ":" in a:
            k, v = re.split(r"[=：:]", a, maxsplit=1)
            key = keymap.get(k.strip().lower(), keymap.get(k.strip()))
            if key:
                kw[key] = v.strip()
            else:
                errors.append(f"不认识「{k}」这个设定（可以用：关系、我、对方、名字、性格、职业、背景、种子）")
        else:
            errors.append(f"「{a}」要写成 设定=值 的样子，比如 关系=旧友")
    seed = kw.pop("seed", None)
    try:
        seed = int(seed) if seed is not None else None
    except ValueError:
        seed = sum(ord(c) for c in str(seed))
    if "rel" in kw:
        r = REL_ALIAS.get(kw["rel"], kw["rel"])
        if r not in RELATIONS:
            errors.append(f"没有「{kw['rel']}」这种关系。可以选：陌生人、同事、旧友、争执、疏远")
        kw["rel"] = r if r in RELATIONS else None
    for k in ("me", "npc"):
        if k in kw and str(kw[k]).lower() not in GENDER_ALIAS:
            errors.append(f"{'我' if k == 'me' else '对方'}=「{kw[k]}」看不懂，写 男 或 女")
    if "traits" in kw:
        bad = [x for x in re.split(r"[,，、+ ]+", kw["traits"]) if x and x not in TRAIT_ALIAS]
        if bad:
            errors.append("性格里有不认识的：" + "、".join(bad) + "。可以选：谨慎 健谈 幽默 沉默 敏感 理性")
    return seed, force, kw, errors

def intro_text(g):
    s = g.s
    n = g.npc
    pk = g.pack()
    out = []
    out.append(g.rng.choice(OPENING_NARRATION))
    rel = s["rel"]
    ta = n["ta"]
    if rel == "stranger":
        out.append(f"轿厢里除了你，只有一个人。一个{'女人' if n['gender'] == '女' else '男人'}，看起来和你差不多大，你不认识{ta}。")
    elif rel == "coworker":
        out.append(f"另一个人是{n['name']}，{pk['npc_dept']}的。你们在同一家公司（{pk['company']}），在{pk['floor']}上班，"
                   f"见过很多次，但没怎么说过话。")
    elif rel == "oldfriend":
        out.append(f"另一个人是{n['name']}。你们是{pk['how']}，算起来已经{pk['years']}年没联系了。")
    elif rel == "quarrel":
        out.append(f"另一个人是{n['name']}，你{pk['who']}。" + g.fill(pk["setup"]))
    else:
        out.append(f"另一个人是{n['name']}。你们曾经是{pk['was']}。{g.fill(pk['drift'])}上一次见面，是{pk['years']}年前。")
    me = s["me"]
    if me.get("job") or me.get("bg"):
        out.append("你是" + "，".join(x for x in (me.get("job", ""), me.get("bg", "")) if x) + "。")
    return ["  " + x for x in out]


def look_text(g):
    s = g.s
    ta = g.npc["ta"]
    lines = []
    p = s["progress"]
    if s["phase"] == "ended":
        return "电梯门开着。走廊里的灯很亮。（这一局已经结束了，status 回看结局。）"
    if p < 25:
        lines.append("应急灯发着黄光。电梯停在两层楼之间，显示屏是黑的。")
    elif p < 50:
        lines.append("还是应急灯的光。空气有点闷了，外面偶尔有人说话，听不清在说什么。")
    elif p < 85:
        lines.append("井道里有维修的声音，叮叮当当的。应急灯比一开始暗了一点，地板有些凉。")
    else:
        lines.append("外面的声音近了，好像随时会好。")
    if s.get("light_on"):
        lines.append("你的手电还开着，白光把轿厢照得很清楚。")
    pose = s.get("pose", "stand")
    anxious = g.claustro() and s["anxious"] >= 2
    desc = {
        "far": f"{ta}站在离你最远的角落，看着门，没往你这边看。",
        "away": f"{ta}站在离你最远的角落，背对着你。",
        "sit_away": f"{ta}坐在地上，侧过身去，不看你。",
        "sit": f"{ta}和你一样坐在地上，背靠着墙。" + ("肩膀放松下来了。" if s["st"]["comfort"] >= 45 else ""),
        "stand": f"{ta}靠在另一边的墙上，转过来对着你这边。",
        "facing": f"{ta}靠着墙，面朝着你，说话时会看你。",
    }.get(pose, f"{ta}站在门边。")
    lines.append(desc)
    if anxious:
        lines.append(f"{ta}的手一直攥着扶手，呼吸有点急。")
    if s.get("unsaid") == "started":
        lines.append(f"{ta}刚才的话被打断了，好像还有话没说完。")
    if s["pending"] and s["pending"].get("topic"):
        lines.append(f"（{ta}刚才问了你一句话，还在等你回答。）")
    return "\n".join("  " + x for x in lines)

def info_text(g):
    s = g.s
    n = g.npc
    me = s["me"]
    pk = g.pack()
    rel = s["rel"]
    out = [f"你：{me['name']}（{me['gender']}）"]
    if s["name_known"]:
        out.append(f"对方：{n['name']}（{n['gender']}）")
    else:
        out.append(f"对方：一个{'女人' if n['gender'] == '女' else '男人'}，你还不知道{n['ta']}叫什么")
    desc = RELATIONS[rel]
    if rel == "coworker":
        desc += f"——同在{pk['company']}，{n['ta']}在{pk['npc_dept']}，你在{pk['my_dept']}"
    elif rel == "oldfriend":
        desc += f"——{pk['how']}，{pk['years']}年没联系了"
    elif rel == "quarrel":
        desc += f"——{n['ta']}是你{pk['who']}，刚才为了{pk['about']}吵了一架"
    elif rel == "estranged":
        desc += f"——曾经是{pk['was']}，{g.fill(pk['drift'])}"
    out.append(f"关系：{desc}")
    known = []
    for f in s["npc_facts"]:
        src = {"work": g.job, "why_here": g.reason, "hobby": g.hobby, "worry": g.worry, "fear": g.fear, "regret": g.regret}[f["topic"]]()
        label = {"work": "工作", "why_here": "今天来这儿", "hobby": "平时", "worry": "心事", "fear": "怕的东西", "regret": "后悔的事"}[f["topic"]]
        t = src.get(f"t{f['tier']}") or src.get("t2", "")
        item = f"{label}：{t}"
        if item not in known:
            known.append(item)
    if known:
        out.append("你听{ta}说过：".format(ta=n["ta"]))
        out += ["  · " + k for k in known[-8:]]
    told = [m for m in s["mem"] if m["kind"] != "claustro" and not m.get("given")]
    kl = {"dislike": "不喜欢", "like": "喜欢", "job": "工作", "why_here": "今天来这儿", "recent": "最近", "family": "家里", "past": "以前",
          "regret": "后悔", "city": "城市", "animal": "小动物", "shared": "过去", "rumor": "传闻", "injury": "身体", "signal": "手机"}
    if told or g.mem_of("claustro"):
        out.append("你跟{ta}说过的事：".format(ta=n["ta"]) + "；".join([f"{kl.get(m['kind'], m['kind'])}「{m['x']}」" for m in told[-6:]] +
                                                                (["你怕密闭的地方"] if g.mem_of("claustro") else [])))
    if s["my_boundary"]:
        tl = {"work": "工作", "why_here": "今天来这儿做什么", "hobby": "平时的爱好", "recent": "最近过得怎么样", "family": "家里的事",
              "regret": "后悔的事", "worry": "心事", "love": "感情", "fear": "怕的东西", "shared": "以前的事", "city": "老家",
              "animal": "小动物", "personal": "私事", "injury": "身体", "signal": "手机", "name": "名字", "rumor": "传闻",
              "quarrel": "刚才的争吵", "drift": "为什么断了联系"}
        out.append("你说过不想聊的：" + "、".join(dict.fromkeys(tl.get(x, "私事") for x in s["my_boundary"])))
    if me.get("job") or me.get("bg"):
        out.insert(1, "　　" + "，".join(x for x in (f"职业：{me['job']}" if me.get("job") else "", f"背景：{me['bg']}" if me.get("bg") else "") if x))
    return "\n".join(out)


def status_text(g):
    s = g.s
    if s["ending"]:
        e = s["ending"]
        return "\n".join([f"🛗 这一局已经结束了（{e['turns']} 轮）。", f"结局 {e['type']}：{e['title']}", ""] + e["text"])
    stage = {1: "刚停下来", 2: "在等", 3: "等了挺久了", 4: "快修好了"}[s["stage"]]
    return f"🛗 还困在电梯里（{stage}，已经说了 {s['turn']} 轮）。还没有结局。"


def run(text):
    """执行一条指令，返回 (输出文字, 是否改变了存档)。"""
    s = _load()
    t = str(text).strip()
    parts = t.split(None, 1)
    head = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    # 只有「指令单独出现」或者「指令 + 它认得的参数」才当指令；其余都当成你说的话（help me、look 一下外面……）
    if head in ("help", "帮助", "?", "？") and not rest:
        return HELP, False
    if head in ("new", "新游戏", "restart", "重来") and (not rest or all(("=" in a or "：" in a or ":" in a or a in ("force", "强制", "重来"))
                                                              for a in rest.split())):
        seed, force, kw, errors = _setup_kw(rest.split())
        if head in ("restart", "重来"):
            force = True
        if errors:
            return "⚠️ 没有开新局：\n" + "\n".join("  · " + e for e in errors), False
        if s and s["phase"] != "ended" and not force:
            return ("⚠️ 现在这一局还没结束（已经说了 %d 轮）。要放弃它重新开始，请用 new force。" % s["turn"]), False
        new_game(seed=seed, force=True, **kw)
        g = Talk(_load())
        g.out += intro_text(g)
        g.out.append("")
        line = g.pick("open_" + g.s["rel"], OPENING[g.s["rel"]])
        if g.s["rel"] == "stranger" and g.rng.chance(0.4):
            line = g.fill("〔{ta}看了你一眼〕{sir}，你没事吧？")
        g.npc_say(line)
        if g.claustro() and g.s["anxious"] > 0:
            g.narrate("{ta}的呼吸有点急。")
        g.out.append("")
        g.out.append("（直接打字说话，或者 …… 保持沉默。help 看全部指令。）")
        g.s["rng"] = g.rng.state
        return "\n".join(g.out), True
    if head in ("import", "导入", "读档") and (not rest or rest.strip().startswith("BF")):
        if not rest:
            return "用法：import <存档码>", False
        if import_code(rest):
            g = Talk(_load())
            return "✅ 已从存档码恢复。\n" + look_text(g) + "\n\n" + "\n".join(_tail_history(g, 6)), False
        return "⚠️ 这个存档码读不出来。请确认是完整复制的（以 BF1- 开头）。", False
    if not s:
        return "还没有开始。用 new 开一局（help 看说明）。", False
    g = Talk(s)
    if head in ("look", "看", "看看") and not rest:
        return look_text(g), False
    if head in ("info", "设定", "信息") and not rest:
        return info_text(g), False
    if head in ("status", "结局", "状态") and not rest:
        return status_text(g), False
    if head in ("history", "记录", "回看") and (not rest or rest.strip().lstrip("-").isdigit()):
        try:
            k = max(1, min(60, int(rest))) if rest else 12
        except ValueError:
            k = 12
        return "\n".join(_tail_history(g, k)) or "还没有说过话。", False
    if head in ("export", "导出", "存档码") and not rest:
        return "📦 存档码（完整复制，import 时贴回来）：\n" + export_code(s), False
    if head == "code" and rest.strip().lower() in ("on", "off", "开", "关", "1", "0"):
        s["autocode"] = rest.strip().lower() in ("on", "开", "1")
        return ("好，之后每次回复末尾都会附上存档码。" if s["autocode"] else "好，不再自动附上存档码。"), True
    if head == "llm" and rest.strip().lower() in ("on", "off", "开", "关", "1", "0"):
        on = rest.strip().lower() in ("on", "开", "1")
        if on and not _llm:
            return "没有接入大模型（需要宿主程序先调用 set_llm()）。现在是离线规则版，可以正常玩。", False
        s["llm"] = on
        return ("已开启大模型润色。" if on else "已关闭大模型润色，使用离线台词。"), True
    if head in ("save", "保存") and not rest:
        return "已保存（其实每一句都会自动保存）。", True
    if s["phase"] == "ended":
        return "这一局已经结束了。status 回看结局，new force 再来一局。", False
    if head in ("silence", "wait", "沉默", "等", "等待", "…", "……", "...", "。。。") and not rest:
        g.turn("")
    elif head in ("do", "动作"):
        if not rest:
            return "do 什么？比如：do 看手机、do 按警铃、do 坐下、do 递水", False
        _do(g, rest)
    elif head in ("say", "说"):
        if not rest.strip():
            g.turn("")
        else:
            g.turn(rest)
    else:
        g.turn(t)
    s["rng"] = g.rng.state
    lines = _llm_polish(g, g.out)
    return "\n".join(x for x in lines if x is not None).strip("\n"), True


def _do(g, what):
    s = g.s
    for kws, key, text in ACTIONS:
        if not has_any(what, kws):
            continue
        g.heavy = False
        g.shared_now = False
        s["history"].append(["me", f"（{what[:20]}）"])
        react_key = key
        if key in ("water", "food"):
            n = s["offered"].get(key, 0)
            s["offered"][key] = n + 1
            if n:  # 已经递过了：时间照样走，关系不再因此变化
                text = "你又把{}递过去一次。".format("水" if key == "water" else "饼干")
                react_key = "again"
            else:
                g.add(comfort=4, trust=3, contact=2)
                if g.claustro() and s["anxious"] > 0:
                    s["anxious"] -= 1
        elif key == "phone":
            s["did_phone"] = True
        elif key == "alarm":
            s["did_alarm"] = True
        elif key == "light":
            if s.get("light_on"):
                text, react_key = "手电已经开着了。", "phone_none"
            else:
                s["light_on"] = True
                if g.fear().get("dark"):
                    g.add(comfort=4, trust=2)
                    g.setflag("helped_anxious")
        elif key == "light_off":
            if not s.get("light_on"):
                text, react_key = "你的手电本来就没开。", "phone_none"
            else:
                s["light_on"] = False
                g.add(comfort=1)
        elif key == "music":
            g.add(comfort=1)
        g.out.append("  " + text)
        if key == "sit":
            if g.has("quiet", "cautious"):
                g.add(comfort=2)
            sits = g.v("comfort") >= 25 and g.v("tension") < 70 and s["pose"] not in ("away", "sit_away")
            if s["pose"] in ("sit", "sit_away"):
                react = ""
            elif sits:
                s["pose"] = "sit"
                react = g.pick("act_sit", ACTION_REACT["sit"])
            else:
                react = g.pick("act_sit_no", ACTION_REACT["sit_no"])
        elif key == "stand":
            if s["pose"] in ("sit", "sit_away"):
                s["pose"] = "stand" if s["pose"] == "sit" else "away"
            react = g.pick("act_stand", ACTION_REACT["stand"])
        else:
            react = g.pick("act_" + react_key, ACTION_REACT[react_key])
        if react:
            g.npc_say(react)
        # 递了水，有时对方会顺口说一点自己的事
        if key in ("water", "food") and react_key != "again" and g.rng.chance(0.5):
            for topic in ("why_here", "hobby", "work"):
                if s["depth"].get(topic, 0) == 0 and not (topic == "why_here" and g.reason().get("private")):
                    g.share(topic, 1, lead=g.rng.choice(["", "我本来……", "说起来，"]) if topic == "why_here" else "")
                    break
        s["turn"] += 1
        g.npc_asked = False
        g.spoke = bool(react)
        g.prev_npc_topic = None
        g.after_turn({"silence": False, "intents": set(), "raw": what}, True)
        return
    g.out.append(f"  你想{what}，但在这么小的电梯里，好像做不了什么。")

def _tail_history(g, k):
    out = []
    for who, t in g.s["history"][-k:]:
        out.append(("你：「" + t + "」") if who == "me" else f"{g.label()}：「{t}」")
    return out


def cmd(text="look"):
    """执行一条指令或一句话，返回文字。每次有效操作后自动存档。"""
    global _cache
    s = _load()
    snap = copy.deepcopy(s) if s else None
    try:
        out, changed = run(text)
        s2 = _load()
        if changed and s2:
            _write(s2)
        if s2 and s2.get("autocode") and changed:
            out += "\n\n📦 存档码：" + export_code(s2)
    except Exception as e:  # noqa: BLE001
        _cache = snap
        out = f"⚠️ 这条指令出错了，已经回滚，游戏没有受影响。（{type(e).__name__}: {e}）"
    if _notes:
        out = "\n".join(_notes) + "\n" + out
        _notes.clear()
    return out
